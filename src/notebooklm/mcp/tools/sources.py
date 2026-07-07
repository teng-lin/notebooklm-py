"""Source MCP tools.

Thin adapters over the transport-neutral ``_app.source_*`` cores: resolve the
notebook (and, where applicable, the source) reference via the Phase 1
:mod:`._resolve` helpers, drive the ``execute_source_*`` executors, and project
the typed result to the wire with :func:`to_jsonable`.

``source_add`` is a hybrid over two cores: ``url``/``text``/``file``/``youtube``
flow through ``_app.source_add`` (``build_source_add_plan`` + ``execute_source_add``);
``drive`` flows through ``_app.source_mutations.execute_source_add_drive`` (the
neutral ``source_add`` core has no Drive path). It also has a batch mode
(``urls=[...]``) that adds many http(s) URLs sequentially and returns an explicit
per-item result list. ``source_wait`` waits for a subset when ``sources`` is
given, one source when ``source`` is given, else every source in the notebook.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

from fastmcp import Context
from fastmcp.server.dependencies import get_http_request
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from ..._app import labels as labels_core
from ..._app import source_add as add_core
from ..._app import source_content as content_core
from ..._app import source_listing as listing_core
from ..._app import source_mutations as mut_core
from ..._app import source_wait as wait_core
from ..._app.serialize import to_jsonable
from ..._app.views import source_view as _source_view
from ...exceptions import (
    SourceNotFoundError,
    ValidationError,
)
from ...types import source_status_to_str
from ...urls import is_youtube_url
from .._coerce import coerce_list
from .._confirm import DESTRUCTIVE, READ_ONLY, needs_confirmation
from .._context import get_client, get_file_transfer
from .._errors import mcp_errors, tool_error_payload
from .._paginate import DEFAULT_LIMIT, paginate
from .._resolve import resolve_notebook, resolve_source, resolve_sources
from ._content_sanity import _annotate_thin_warnings
from ._fileupload import _add_bytes, _add_one, _broker_upload, _decode_upload_b64
from ._passthrough import passthrough_child_id
from ._preview import title_for_id
from ._waitagg import _aggregate_wait_outcomes, _wait_all_sources

if TYPE_CHECKING:
    from ...client import NotebookLMClient
    from ...types import Source

#: MCP source types. Superset of the neutral ``source_add`` core's types
#: (which lacks ``drive``); ``drive`` is dispatched to the Drive path.
_SOURCE_TYPES = ("url", "text", "file", "drive", "youtube")

#: Drive MIME choices the backend accepts (mirrors the CLI ``--mime-type``).
_DRIVE_MIME_CHOICES = ("google-doc", "google-slides", "google-sheets", "pdf")

#: The default Drive MIME choice when the caller does not specify one.
_DEFAULT_DRIVE_MIME = "google-doc"


# ``_source_view`` (Source → dict with string ``kind`` / ``status_label`` labels)
# now lives in the shared, transport-neutral ``_app.views`` so the REST source
# list/get routes emit the identical enriched shape (Option B). Imported above
# under its historical private name so the tool bodies below are unchanged.


def _json_tool_result(payload: dict[str, Any]) -> ToolResult:
    """Return JSON as the first-class client-visible content block."""
    jsonable_payload = to_jsonable(payload)
    return ToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(jsonable_payload, ensure_ascii=False, sort_keys=True),
            )
        ],
        structured_content=jsonable_payload,
    )


#: Fields kept in a ``source_list(detail="compact")`` roster row — a strict subset of
#: :func:`_source_view`'s output, dropping ``url`` / the raw ``status`` int / ``_type_code``.
_COMPACT_SOURCE_FIELDS = ("id", "title", "kind", "status_label", "created_at")


def _source_compact(source: Source) -> dict[str, Any]:
    """Project a ``Source`` to the compact roster row for ``source_list(detail="compact")``.

    A strict subset of :func:`_source_view` — keeping only
    :data:`_COMPACT_SOURCE_FIELDS` — so a discovery listing stays low-token with no
    extra read while ``kind`` / ``status_label`` / ISO ``created_at`` stay byte-identical
    to the full projection (same single source of truth, no re-derivation).
    """
    view = _source_view(source)
    return {k: view.get(k) for k in _COMPACT_SOURCE_FIELDS}


def _add_result_payload(source: Any, base: dict[str, Any], *, notebook_id: str) -> dict[str, Any]:
    """Project a ``source_add`` result: enrich the added source + flag failure.

    Replaces ``base["source"]`` (the bare ``to_jsonable`` source dict) with the
    label-enriched :func:`_source_view` so ``source_add`` output reaches parity
    with ``source_list`` / ``source_read`` (``kind`` + ``status_label``).

    Echoes the resolved canonical ``notebook_id`` (the added source itself carries
    no ``notebook_id`` field) so a caller that added by notebook *name* gets the id
    back for deterministic chaining, matching the batch-add and ``source_list``
    responses (#1808).

    When the add response ALREADY reflects a failed import (``status`` == ERROR),
    surface it synchronously with a top-level ``warning``. Most imports are
    processed asynchronously, so a freshly-added source is usually still
    PROCESSING/PREPARING and the failure only surfaces later — but when the
    backend echoes ERROR at add-time we say so immediately rather than letting it
    look like a successful add.
    """
    base["notebook_id"] = notebook_id
    base["status"] = "added"
    base["source"] = _source_view(source)
    if source.is_error:
        base["warning"] = (
            "Import failed: the source row was created but processing errored "
            "(status_label='error'). It persists as an incomplete row — delete it "
            "with source_delete, or list failures via source_list(status='error')."
        )
    return base


def register(mcp: Any) -> None:
    """Register the source tools on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def source_list(
        ctx: Context,
        notebook: str,
        status: Literal["ready", "processing", "error", "preparing"] | None = None,
        label: str | None = None,
        detail: Literal["compact", "full"] = "full",
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List a notebook's sources. Accepts a notebook name or ID.

        ``detail`` (default ``full``): ``full`` gives each source's metadata plus string
        ``kind`` / ``status_label`` labels; ``compact`` returns only ``id`` / ``title``
        / ``kind`` / ``status_label`` / ``created_at`` — a low-token roster.

        Pass ``status`` to list only sources whose ``status_label`` matches (``error`` =
        a broken import's ghost row). Pass ``label`` (name or ID) to restrict to that
        label's members; composes with ``status``.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            sources = await listing_core.fetch_sources(
                client, nb_id, label_filter=label, label_resolver=labels_core.resolve_label_id
            )
            # Filter on the raw Source BEFORE serializing, so the projector (which
            # runs to_jsonable) is only paid for the sources that survive the
            # filter. Uses the same source_status_to_str label the rows emit.
            if status is not None:
                sources = [s for s in sources if source_status_to_str(s.status) == status]
            project = _source_compact if detail == "compact" else _source_view
            page, meta = paginate([project(s) for s in sources], limit, offset)
            return {"notebook_id": nb_id, "sources": page, **meta}

    @mcp.tool(annotations=READ_ONLY)
    async def source_read(
        ctx: Context,
        notebook: str,
        source: str,
        detail: Literal["summary", "full"] = "full",
        output_format: Literal["text", "markdown"] = "text",
        max_chars: int | None = None,
        offset: int = 0,
    ) -> ToolResult:
        """Read a source at one of two detail levels. Accepts a notebook/source name or ID.

        ``detail`` selects what you get back (two distinct shapes):
        * ``summary`` — a tiny AI digest for low-token triage:
          ``{notebook_id, source_id, summary, keywords}``. Cheap to fan out across
          many sources before deciding which to pull in full.
        * ``full`` (DEFAULT) — the source metadata (incl. string ``kind``/``status_label``)
          plus the extracted ``content``, the full ``char_count``, and a
          ``truncated`` flag. ``content`` is ALWAYS bounded: omitting ``max_chars``
          caps it at the first 10,000 chars; raise ``max_chars`` and/or page with
          ``offset`` (slice ``[offset : offset+max_chars]``). ``char_count`` stays
          the FULL length. ``content`` is ``null`` (``char_count`` 0) when the
          source isn't ready yet or has no extractable text.

        ``output_format`` (``text`` default / ``markdown``, needs the server's
        ``markdownify`` extra) and ``max_chars`` / ``offset`` apply only to
        ``detail="full"`` (ignored for ``summary``). Prefer ``chat_ask`` for
        querying large sources rather than pulling the whole body.
        """
        client = get_client(ctx)
        with mcp_errors():
            # Validate windowing args unconditionally — a bad value must error even
            # in ``summary`` mode (where they are ignored), never silently pass.
            # (``execute_source_read`` re-validates for the full path; this keeps the
            # error raised BEFORE any notebook I/O and covers the summary path too.)
            if max_chars is not None and max_chars < 0:
                raise ValidationError(f"max_chars must be >= 0; got {max_chars}")
            if offset < 0:
                raise ValidationError(f"offset must be >= 0; got {offset}")
            nb_id = await resolve_notebook(client, notebook)
            src_id = await resolve_source(client, nb_id, source)

            if detail == "summary":
                # Existence guard: a full-UUID ref skips list resolution (the
                # resolver trusts a full id), so a non-existent id reaches
                # ``get_or_none`` and yields a ``None`` source — surface NOT_FOUND
                # rather than a misleading empty success.
                get_result = await content_core.execute_source_get(
                    client, content_core.SourceGetPlan(notebook_id=nb_id, source_id=src_id)
                )
                if get_result.source is None:
                    raise SourceNotFoundError(src_id)
                # Guide RPC → the AI digest (a missing guide returns empty summary/
                # keywords — the existence guard above already ruled out a deleted
                # source, so this is a real "no guide yet", not a false success).
                guide = await content_core.execute_source_guide(
                    client, content_core.SourceGuidePlan(notebook_id=nb_id, source_id=src_id)
                )
                return _json_tool_result(
                    {
                        "notebook_id": nb_id,
                        "source_id": guide.source_id,
                        "summary": guide.summary,
                        "keywords": list(guide.keywords),
                    }
                )

            # detail == "full": the existence/ready gate + ready-only fulltext fetch
            # + max_chars/offset windowing live in the shared ``execute_source_read``
            # core (also driven by the REST content route), so both surfaces stay in
            # lock-step. A resolved-but-missing source raises NOT_FOUND; a not-ready
            # source returns content=None; the markdown ImportError→CONFIG remap and
            # the default cap are handled inside the core.
            read = await content_core.execute_source_read(
                client,
                content_core.SourceReadPlan(
                    notebook_id=nb_id,
                    source_id=src_id,
                    output_format=output_format,
                    max_chars=max_chars,
                    offset=offset,
                ),
            )
            return _json_tool_result(
                {
                    "notebook_id": nb_id,
                    "source_id": src_id,
                    "source": _source_view(read.source),
                    "content": read.content,
                    "char_count": read.char_count,
                    "truncated": read.truncated,
                    "output_format": output_format,
                }
            )

    @mcp.tool
    async def source_rename(
        ctx: Context, notebook: str, source: str, new_title: str
    ) -> dict[str, Any]:
        """Rename a source. Accepts a notebook/source name or ID."""
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            src_id = await resolve_source(client, nb_id, source)
            result = await mut_core.execute_source_rename(
                client,
                mut_core.SourceRenamePlan(
                    notebook_id=nb_id, source_id=src_id, new_title=new_title, json_output=False
                ),
                resolve_source_id=passthrough_child_id,
            )
            return {"status": "renamed", **to_jsonable(result)}

    @mcp.tool(annotations=DESTRUCTIVE)
    async def source_delete(
        ctx: Context, notebook: str, source: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Delete a source (irreversible). Accepts a notebook/source name or ID.

        Two-step confirmation: with ``confirm=False`` (default) it returns a
        ``needs_confirmation`` preview of the resolved source without deleting;
        call again with ``confirm=True`` to perform the delete.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            src_id = await resolve_source(client, nb_id, source)
            if not confirm:
                title = title_for_id(await client.sources.list(nb_id), src_id)
                return needs_confirmation(
                    {
                        "action": "delete_source",
                        "notebook_id": nb_id,
                        "source_id": src_id,
                        "title": title,
                    }
                )
            await client.sources.delete(nb_id, src_id)
            return {"status": "deleted", "notebook_id": nb_id, "source_id": src_id}

    @mcp.tool(annotations=READ_ONLY)
    async def source_wait(
        ctx: Context,
        notebook: str,
        source: str | None = None,
        sources: list[str] | str | None = None,
        timeout: float = 120.0,
        interval: float = 1.0,
    ) -> ToolResult:
        """Wait for sources to finish processing. Accepts a notebook name or ID.

        Waits for a subset when ``sources`` (list or comma/JSON string) is given, a
        single source when ``source`` (name or ID) is given, else every source. All
        three modes return the SAME structured aggregate, so an agent never has to
        branch on the shape:

            {"notebook_id", "ok", "ready", "timed_out", "failed", "not_found"}

        ``ready`` holds sources that reached READY (with ``kind`` / ``status_label``
        labels); ``timed_out`` / ``failed`` / ``not_found`` hold ``{"source_id",
        "error"}`` entries. ``ok`` is ``true`` iff all error buckets are empty. Subset
        and all-sources modes report **partial progress** (a slow or failed source no
        longer discards the ones that did become ready).

        A READY **web-page** entry may carry a non-blocking ``warning`` when its indexed
        text is suspiciously thin (likely dead link / soft-404 / paywall); advisory only
        (still READY, still ``ok`` — verify with ``source_read`` (detail="full")).

        An unresolved ref in ``sources`` / ``source`` raises NOT_FOUND before the wait —
        an input error, distinct from a resolved source the backend reports missing /
        failed / slow (which lands in a bucket).
        """
        client = get_client(ctx)
        with mcp_errors():
            if timeout < 0:
                raise ValidationError(f"timeout must be >= 0; got {timeout}")
            if interval <= 0:
                raise ValidationError(f"interval must be > 0; got {interval}")

            # All input guards fire BEFORE any I/O (fail-fast, like the bounds
            # checks above): the empty-``sources`` and mutual-exclusion errors must
            # not be masked by a notebook NOT_FOUND from ``resolve_notebook``.
            coerced = coerce_list(sources)
            if source is not None and coerced is not None:
                raise ValidationError(
                    "pass either 'source' (one) or 'sources' (a subset), not both"
                )
            if coerced is not None and not coerced:
                raise ValidationError(
                    "'sources' was empty; omit it to wait on all sources, or pass at least one source ref"
                )

            nb_id = await resolve_notebook(client, notebook)

            if coerced is not None:
                # Dedupe: distinct refs can resolve to the same id (title + its id,
                # or a literal repeat), which would spawn redundant pollers and emit
                # duplicate ``ready`` rows. ``dict.fromkeys`` preserves input order.
                src_ids = list(dict.fromkeys(await resolve_sources(client, nb_id, coerced)))
                outcomes = await _wait_all_sources(
                    client, nb_id, src_ids, timeout=timeout, interval=interval
                )
                return _json_tool_result(await _aggregate_wait_outcomes(client, nb_id, outcomes))
            elif source is not None:
                src_id = await resolve_source(client, nb_id, source)
                outcome = await wait_core.execute_source_wait(
                    client,
                    wait_core.SourceWaitPlan(
                        notebook_id=nb_id,
                        source_id=src_id,
                        timeout=timeout,
                        interval=interval,
                    ),
                )
                return _json_tool_result(await _aggregate_wait_outcomes(client, nb_id, [outcome]))
            else:
                sources_list = await client.sources.list(nb_id)
                outcomes = await _wait_all_sources(
                    client,
                    nb_id,
                    [s.id for s in sources_list],
                    timeout=timeout,
                    interval=interval,
                )
                return _json_tool_result(await _aggregate_wait_outcomes(client, nb_id, outcomes))

    @mcp.tool
    async def source_add(
        ctx: Context,
        notebook: str,
        source_type: Literal["url", "text", "file", "drive", "youtube"] | None = None,
        url: str | None = None,
        text: str | None = None,
        title: str | None = None,
        path: str | None = None,
        document_id: str | None = None,
        mime_type: str | None = None,
        allow_internal: bool = False,
        urls: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add a source to a notebook (single or batch). Accepts a notebook name or ID.

        Call in exactly ONE of two modes:

        **Single mode** — pass ``source_type``; it selects the input and which named
        argument is required:

        * ``url``     — requires ``url``.
        * ``youtube`` — requires ``url`` (a YouTube link).
        * ``text``    — requires ``text``; ``title`` optional.
        * ``file``    — over **stdio**, requires ``path`` (a local file path on the
          server host). Over the **remote (http) connector** the server's
          filesystem is unreachable, so instead the tool returns
          ``upload_required`` with two first-class actor paths: ``human_upload``
          (open the signed URL in a browser — works on mobile — and pick the file) and
          ``agent_upload`` (an agent holding the bytes POSTs them as the raw body;
          ``Accept: application/json`` → ``{"status": "added", …}``).
          ``agent_instructions`` is the rule: try ``agent_upload``, fall back to
          ``human_upload.url`` on a network error. ``mime_locked`` is true when
          ``mime_type`` was supplied; ``expires_at_iso`` / ``expires_in_seconds`` give
          the expiry; top-level ``url`` is **deprecated** for ``human_upload.url``.
        * ``drive``   — requires ``document_id`` (Google Drive file id); ``title``
          and ``mime_type`` (one of google-doc|google-slides|google-sheets|pdf,
          default google-doc) optional.

        The single-mode named inputs are mutually exclusive — supply only the one
        your ``source_type`` requires.

        The added source is echoed back under ``source`` with string ``kind`` /
        ``status_label`` labels. Imports are processed ASYNCHRONOUSLY, so the echo
        is usually still ``processing``/``preparing`` — a failure typically surfaces
        only AFTER processing. Confirm the outcome with ``source_wait`` or
        ``source_list(status="error")``. When the add response ALREADY reflects a
        failed import, ``source_add`` flags it inline (``status_label="error"`` plus
        a top-level ``warning``) instead of looking like a clean add. ``source_wait``
        additionally flags a READY web page whose fetched text is suspiciously thin
        (a likely dead link / soft-404 / paywall) with a per-source ``warning``.

        **Batch mode** — pass ``urls`` (a list of **http/https URLs**, YouTube links
        included) to add many in one call instead of one round-trip each. Each entry
        is validated and added independently; the response is an explicit per-item
        list so partial failure is never hidden::

            {"notebook_id": …, "added": <int>, "failed": <int>,
             "results": [{"input": "<url>", "status": "added", "source_id": …,
                          "title": …, "status_label": …, "warning"?: …},
                         {"input": "<url>", "status": "error",
                          "error": {"code": …, "message": …, "retriable": …, "hint"?: …}}]}

        ``results`` is positional (``results[i]`` is for ``urls[i]``); ``status`` is
        ``"added"`` or ``"error"`` (the ADD outcome). An ``"added"`` item also carries
        the source's ``status_label`` (the async-import status) and, when the add
        response already reflects a failed import, an inline ``warning`` — same
        failure-signaling as single mode. A failed item NEVER aborts the rest of the
        batch and an ``error`` item's ``error`` carries the same structured contract a
        single-mode failure raises. Batch is URL-only: a non-URL entry (plain text,
        a local path, ``file://``/``ftp://``) is reported as a per-item ``VALIDATION``
        error — it is never silently added as text or read off the filesystem.
        ``allow_internal`` applies to every entry; the other single-mode named inputs
        (``source_type``/``url``/``text``/``title``/``path``/``document_id``/
        ``mime_type``) are not valid with ``urls``.
        """
        client = get_client(ctx)
        with mcp_errors():
            # Mode selection (fail-closed) BEFORE any notebook I/O, so a malformed
            # call never reaches notebooks.list. Exactly one of source_type / urls.
            if urls is not None and source_type is not None:
                raise ValidationError(
                    "provide either 'source_type' (single add) or 'urls' (batch), not both"
                )
            if urls is None and source_type is None:
                raise ValidationError("provide 'source_type' (single add) or 'urls' (batch)")
            if urls is not None:
                # Batch mode: reject single-mode scalars, then resolve + dispatch.
                _reject_batch_scalars(
                    url=url,
                    text=text,
                    title=title,
                    path=path,
                    document_id=document_id,
                    mime_type=mime_type,
                )
                if not urls:
                    raise ValidationError("urls must contain at least one URL")
                nb_id = await resolve_notebook(client, notebook)
                return await _add_url_batch(client, nb_id, urls, allow_internal=allow_internal)

            # Single-add mode. The mode checks above guarantee source_type is set; a
            # hard raise (not assert — stripped under ``python -O``) both narrows the
            # type for the validators + dispatch below and fails loudly if the
            # invariant is ever broken by a future edit.
            if source_type is None:  # pragma: no cover - unreachable given the mode guards
                raise ValidationError("internal error: source_type unexpectedly None")

            # The drive-mime and content-scalar-exclusivity checks below run BEFORE
            # resolve_notebook, so these malformed calls never pay a notebook
            # round-trip. (Content *presence* + the YouTube-host guard still run
            # later, during dispatch — that ordering is unchanged by #1696.)
            #
            # ``mime_type`` deliberately stays a free-text ``str`` (NOT a ``Literal``):
            # it is DUAL-USE — for ``source_type="file"`` it carries an arbitrary,
            # open-ended MIME type (in the signed upload URL), and only for
            # ``source_type="drive"`` is it restricted to ``_DRIVE_MIME_CHOICES``.
            # A ``Literal`` would wrongly reject valid ``file`` MIME types; splitting a
            # dedicated ``drive_mime_type`` param would grow the ``source_add`` surface
            # for a niche 4-value option. So the drive choice set is enforced here at
            # runtime (and listed in the docstring) instead (issue #1759).
            if (
                mime_type is not None
                and source_type == "drive"
                and mime_type not in _DRIVE_MIME_CHOICES
            ):
                raise ValidationError(
                    f"Invalid mime_type {mime_type!r} for drive; "
                    f"expected one of {list(_DRIVE_MIME_CHOICES)}"
                )
            # Content-scalar exclusivity (fail-closed): reject any content scalar
            # this source_type does not consume. title/mime_type are untouched —
            # they are optional metadata, not content.
            _reject_single_content_scalars(
                source_type,
                url=url,
                text=text,
                path=path,
                document_id=document_id,
            )

            nb_id = await resolve_notebook(client, notebook)

            if source_type == "file":
                cfg = get_file_transfer(ctx)
                if cfg is not None:
                    # Remote connector: broker a signed upload URL (the server path
                    # is unreachable). A supplied `path` is accepted, not opened —
                    # its basename seeds the default title.
                    return _broker_upload(cfg, nb_id, title=title, mime_type=mime_type, path=path)
                if _is_http_transport():
                    raise ValidationError(
                        "remote file transfer is not configured; set "
                        "NOTEBOOKLM_MCP_PUBLIC_URL on the server to enable it"
                    )
                # stdio: fall through to the existing local-path behavior below.

            if source_type == "drive":
                if not document_id:
                    raise ValidationError("source_type 'drive' requires 'document_id'")
                drive_result = await mut_core.execute_source_add_drive(
                    client,
                    mut_core.SourceAddDrivePlan(
                        notebook_id=nb_id,
                        file_id=document_id,
                        title=title or "",
                        mime_type=mime_type or _DEFAULT_DRIVE_MIME,  # type: ignore[arg-type]
                    ),
                )
                return _add_result_payload(
                    drive_result.source, to_jsonable(drive_result), notebook_id=nb_id
                )

            content = _select_content(source_type, url=url, text=text, path=path)
            src = await _add_one(
                client,
                nb_id,
                content,
                source_type=source_type,
                title=title,
                mime_type=mime_type,
                allow_internal=allow_internal,
            )
            return _add_result_payload(
                src, to_jsonable(add_core.SourceAddResult(source=src)), notebook_id=nb_id
            )

    @mcp.tool
    async def source_add_and_wait(
        ctx: Context,
        notebook: str,
        source_type: Literal["url", "text", "file", "drive", "youtube"],
        url: str | None = None,
        text: str | None = None,
        title: str | None = None,
        path: str | None = None,
        document_id: str | None = None,
        mime_type: str | None = None,
        allow_internal: bool = False,
        timeout: float = 120.0,
        interval: float = 1.0,
    ) -> ToolResult:
        """Add ONE source and block until it finishes processing, in a single call.

        Composes single-mode ``source_add`` + ``source_wait`` so an agent skips the
        add→wait round-trip. Takes the single-mode ``source_add`` inputs — ``source_type``
        plus the one it needs (url/youtube→``url``, text→``text``, file→``path`` (stdio
        only), drive→``document_id``; ``title``/``mime_type``/``allow_internal`` optional)
        — and the ``source_wait`` knobs (``timeout``, ``interval``). NOT for batch
        (``source_add(urls=[...])``) or a REMOTE ``file`` upload: that upload is a
        separate step (use ``source_add(source_type="file")`` then ``source_wait``, or
        ``source_upload_bytes`` for a tiny file).

        Returns the ``source_wait`` aggregate (``{notebook_id, ok, ready, timed_out,
        failed, not_found}``) plus a top-level ``source_id`` — always on the returned
        aggregate, since the source persists even when the wait does not reach READY, so
        you can retry/delete it. A READY web page with thin/soft-404 text carries a
        non-blocking ``warning``, as in ``source_wait``.
        """
        client = get_client(ctx)
        with mcp_errors():
            if timeout < 0:
                raise ValidationError(f"timeout must be >= 0; got {timeout}")
            if interval <= 0:
                raise ValidationError(f"interval must be > 0; got {interval}")
            # The same single-add guards source_add applies, all BEFORE any notebook
            # I/O so a malformed call never pays a round-trip. Kept in sync with
            # source_add's copies (the drive-mime check + _reject_single_content_scalars):
            # if _CONTENT_SCALAR_OWNERS / _DRIVE_MIME_CHOICES change, update both sites.
            if (
                mime_type is not None
                and source_type == "drive"
                and mime_type not in _DRIVE_MIME_CHOICES
            ):
                raise ValidationError(
                    f"Invalid mime_type {mime_type!r} for drive; "
                    f"expected one of {list(_DRIVE_MIME_CHOICES)}"
                )
            _reject_single_content_scalars(
                source_type, url=url, text=text, path=path, document_id=document_id
            )

            nb_id = await resolve_notebook(client, notebook)
            src = await _add_source_to_wait_on(
                client,
                ctx,
                nb_id,
                source_type=source_type,
                url=url,
                text=text,
                title=title,
                path=path,
                document_id=document_id,
                mime_type=mime_type,
                allow_internal=allow_internal,
            )
            outcome = await wait_core.execute_source_wait(
                client,
                wait_core.SourceWaitPlan(
                    notebook_id=nb_id, source_id=src.id, timeout=timeout, interval=interval
                ),
            )
            result = await _aggregate_wait_outcomes(client, nb_id, [outcome])
            # The created source persists regardless of the wait outcome — surface its id
            # at the top level so a timed-out / failed caller can retry or delete it.
            result["source_id"] = src.id
            return _json_tool_result(result)

    @mcp.tool
    async def source_upload_bytes(
        ctx: Context,
        notebook: str,
        bytes_base64: str,
        filename: str | None = None,
        mime_type: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Add a SMALL file to a notebook from raw bytes, in-channel. Accepts a notebook name or ID.

        For when an agent HOLDS the file bytes but cannot complete the signed-URL
        upload — e.g. over the remote (http) connector with egress blocked, the
        ``upload_required`` ``agent_upload`` POST fails and no human device has the
        file. Pass the bytes as base64; the connector decodes and adds them
        server-side, returning the created source directly — no signed URL, no
        browser hop. Works on any transport and needs no file-transfer config.

        SMALL FILES ONLY: ``bytes_base64`` must be ≤ 10,000 characters (≈ 7 KB of
        file). A larger payload exceeds the MCP message limit and is rejected — use
        ``source_add(source_type="file")`` instead, whose ``upload_required`` signed
        URL carries large files (≤ 200 MiB) via the browser or a raw-body agent POST.
        Standard base64 only, not URL-safe (``-``/``_``).

        ``filename`` seeds the default title and extension (sanitized to a basename);
        ``mime_type`` / ``title`` are optional. The added source is echoed under
        ``source`` with ``kind`` / ``status_label`` labels, exactly like
        ``source_add`` (imports are async — confirm with ``source_wait`` /
        ``source_list(status="error")``).
        """
        client = get_client(ctx)
        with mcp_errors():
            # Decode + cap + empty guard run BEFORE any notebook I/O, so an over-cap or
            # malformed payload never pays a round-trip (see _decode_upload_b64).
            raw = _decode_upload_b64(bytes_base64)
            nb_id = await resolve_notebook(client, notebook)
            src = await _add_bytes(
                client, nb_id, raw, filename=filename, title=title, mime_type=mime_type
            )
            return _add_result_payload(
                src, to_jsonable(add_core.SourceAddResult(source=src)), notebook_id=nb_id
            )


def _is_http_transport() -> bool:
    """Whether the current tool call arrived over the http transport.

    A remote (http) call has an active Starlette request; stdio does not
    (:func:`get_http_request` raises ``RuntimeError``). Used to tell a remote
    ``file`` add *without* file transfer configured (→ clean "not configured"
    error) apart from a stdio add (→ existing local-path behavior).
    """
    try:
        get_http_request()
    except RuntimeError:
        return False
    return True


def _reject_batch_scalars(
    *,
    url: str | None,
    text: str | None,
    title: str | None,
    path: str | None,
    document_id: str | None,
    mime_type: str | None,
) -> None:
    """Reject single-add scalars supplied alongside the batch ``urls`` param.

    Batch mode derives each title from the server, so the single-add scalars
    belong to single mode only. ``allow_internal`` is intentionally NOT rejected
    — it legitimately applies to every URL in the batch.
    """
    offenders = [
        name
        for name, value in (
            ("url", url),
            ("text", text),
            ("title", title),
            ("path", path),
            ("document_id", document_id),
            ("mime_type", mime_type),
        )
        if value is not None
    ]
    if offenders:
        raise ValidationError(
            "these arguments are not valid with 'urls' (batch mode): " + ", ".join(offenders)
        )


#: Maps each single-add *content* scalar to the ``source_type`` values that
#: legitimately consume it. A content scalar supplied for any other source_type
#: is silently ignored today; :func:`_reject_single_content_scalars` rejects it.
#: ``title`` / ``mime_type`` are intentionally absent — they are optional metadata
#: valid alongside several types, not content.
_CONTENT_SCALAR_OWNERS: dict[str, frozenset[str]] = {
    "url": frozenset({"url", "youtube"}),
    "text": frozenset({"text"}),
    "path": frozenset({"file"}),
    "document_id": frozenset({"drive"}),
}


def _reject_single_content_scalars(
    source_type: str,
    *,
    url: str | None,
    text: str | None,
    path: str | None,
    document_id: str | None,
) -> None:
    """Reject content scalars that don't belong to this single-add ``source_type``.

    Single mode consumes exactly one content scalar (the one its ``source_type``
    needs) and historically ignored the rest, contradicting the docstring's
    mutual-exclusivity claim. Fail closed instead — matching batch mode's posture
    (:func:`_reject_batch_scalars`). Only *content* scalars are checked; ``title`` /
    ``mime_type`` are legitimate optional metadata and are left alone.
    """
    offenders = [
        name
        for name, value in (
            ("url", url),
            ("text", text),
            ("path", path),
            ("document_id", document_id),
        )
        if value is not None and source_type not in _CONTENT_SCALAR_OWNERS[name]
    ]
    if offenders:
        raise ValidationError(
            f"these arguments are not valid with source_type {source_type!r}: "
            + ", ".join(offenders)
        )


async def _add_url_batch(
    client: NotebookLMClient,
    notebook_id: str,
    urls: list[str],
    *,
    allow_internal: bool,
) -> dict[str, Any]:
    """Add many http(s) URLs in one call, returning an explicit per-item result list.

    The saving over N single ``source_add`` calls is the per-call MCP/agent
    round-trip overhead: the URL adds themselves run **sequentially** here, on
    purpose — concurrent bulk writes invite backend rate-limiting (CLAUDE.md
    pitfall #4), and a ``RATE_LIMITED`` failure is then isolated per item and
    surfaced ``retriable=true`` rather than aborting the batch.

    Each entry is added with ``source_type="url"`` so :func:`add_core.validate_url`
    enforces the http/https scheme allowlist + SSRF guard per item; a non-URL entry
    (plain text, a local path, ``file://``/``ftp://``) is reported as a per-item
    ``VALIDATION`` error and is NEVER silently added as text or read off the local
    filesystem. A per-item failure is isolated (recorded + skipped), never raised,
    so partial — or total — failure is always visible per item rather than
    collapsed into one success flag. Results are positional (``results[i]`` ↔
    ``urls[i]``); the per-item ``error`` reuses the same structured contract a
    single-mode failure raises.

    An ``"added"`` item also carries the source's ``status_label`` (the async-import
    status) and, when the add response already reflects a failed import
    (``is_error``), an inline ``warning`` — mirroring single mode's
    :func:`_add_result_payload` failure-signaling (#1679) per entry. A
    synchronously-READY web-page item may additionally carry a content-sanity
    ``warning`` (thin / soft-404 body — see :func:`_thin_content_warning`); most
    adds return still-PROCESSING, so this often does not fire here and such sources
    surface the warning later via ``source_wait``.
    """
    results: list[dict[str, Any]] = []
    # Keep each added item's Source alongside its result dict so a synchronously-ready
    # web-page item can be annotated with the content-sanity warning after the loop,
    # concurrently — never N×fetch in-loop (reuses :func:`_annotate_thin_warnings`).
    ready_pairs: list[tuple[dict[str, Any], Source]] = []
    for entry in urls:
        try:
            src = await _add_one(
                client,
                notebook_id,
                entry,
                source_type="url",
                title=None,
                mime_type=None,
                allow_internal=allow_internal,
            )
        except Exception as exc:  # noqa: BLE001 - per-item isolation; CancelledError (BaseException) still propagates
            results.append({"input": entry, "status": "error", "error": tool_error_payload(exc)})
        else:
            item: dict[str, Any] = {
                "input": entry,
                "status": "added",
                "source_id": src.id,
                "title": src.title,
                "status_label": source_status_to_str(src.status),
            }
            if src.is_error:
                item["warning"] = (
                    "Import failed: the source row was created but processing errored "
                    "(status_label='error'). Delete it with source_delete, or list "
                    "failures via source_list(status='error')."
                )
            elif src.is_ready:
                ready_pairs.append((item, src))
            results.append(item)
    # Annotate any synchronously-ready web-page items with a thin / soft-404 warning
    # (concurrent; web-page-filtered; degrades any fetch failure to no warning).
    await _annotate_thin_warnings(client, notebook_id, ready_pairs)
    # Derive the tallies from `results` (single source of truth) rather than
    # maintaining parallel counters that must be kept in sync with each append.
    added = sum(1 for item in results if item["status"] == "added")
    return {
        # "added" once at least one source was added; "error" when every item
        # failed (so the top-level envelope can't claim success while
        # ``results[].status`` all say error). ``added`` / ``failed`` carry the
        # partial-success detail.
        "status": "added" if added else "error",
        "notebook_id": notebook_id,
        "added": added,
        "failed": len(results) - added,
        "results": results,
    }


async def _add_source_to_wait_on(
    client: NotebookLMClient,
    ctx: Context,
    notebook_id: str,
    *,
    source_type: Literal["url", "text", "file", "drive", "youtube"],
    url: str | None,
    text: str | None,
    title: str | None,
    path: str | None,
    document_id: str | None,
    mime_type: str | None,
    allow_internal: bool,
) -> Source:
    """Add one source and return its ``Source`` for ``source_add_and_wait`` to poll.

    A focused single-add dispatch over the SAME cores ``source_add`` drives
    (:func:`_select_content` + :func:`_add_one` for url/youtube/text/file,
    ``execute_source_add_drive`` for drive). It deliberately does NOT reuse
    ``source_add``'s own dispatch, whose type-specific payloads (the Drive provenance
    fields, the remote-file ``upload_required`` broker dict) are the wrong shape here
    and are pinned by tests — sharing it would force a lossy refactor of that surface.

    A REMOTE ``file`` add is rejected: the signed-URL upload is a separate human/agent
    step, so no source exists yet to wait on (the caller must use ``source_add`` +
    ``source_wait``). Only a stdio ``file`` (a real local path, read in one shot) falls
    through to the add.
    """
    if source_type == "file" and (get_file_transfer(ctx) is not None or _is_http_transport()):
        raise ValidationError(
            "source_add_and_wait cannot one-shot a remote file upload (the upload is a "
            "separate step); use source_add(source_type='file') then source_wait, or "
            "source_upload_bytes for a tiny file"
        )
    if source_type == "drive":
        if not document_id:
            raise ValidationError("source_type 'drive' requires 'document_id'")
        drive_result = await mut_core.execute_source_add_drive(
            client,
            mut_core.SourceAddDrivePlan(
                notebook_id=notebook_id,
                file_id=document_id,
                title=title or "",
                mime_type=mime_type or _DEFAULT_DRIVE_MIME,  # type: ignore[arg-type]
            ),
        )
        return drive_result.source
    content = _select_content(source_type, url=url, text=text, path=path)
    return await _add_one(
        client,
        notebook_id,
        content,
        source_type=source_type,
        title=title,
        mime_type=mime_type,
        allow_internal=allow_internal,
    )


def _select_content(
    source_type: str, *, url: str | None, text: str | None, path: str | None
) -> str:
    """Pick the single content value the ``source_type`` requires, validating presence."""
    if source_type in {"url", "youtube"}:
        if not url:
            raise ValidationError(f"source_type {source_type!r} requires 'url'")
        # ``source_type=youtube`` advertises a YouTube link — reject a non-YouTube
        # host rather than silently adding it as a generic URL (host-parsed, not a
        # substring match: ``evil.com/youtube.com`` does NOT pass).
        if source_type == "youtube" and not is_youtube_url(url):
            raise ValidationError(
                "source_type 'youtube' requires a YouTube URL "
                "(youtube.com / youtu.be / m.youtube.com)"
            )
        return url
    if source_type == "text":
        if not text:
            raise ValidationError("source_type 'text' requires 'text'")
        return text
    if source_type == "file":
        if not path:
            raise ValidationError("source_type 'file' requires 'path'")
        return path
    raise ValidationError(f"Unknown source type {source_type!r}")  # pragma: no cover
