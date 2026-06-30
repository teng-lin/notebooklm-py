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
per-item result list. ``source_wait`` waits for one source when ``source`` is
given, else every source in the notebook.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING, Any, Literal

from fastmcp import Context
from fastmcp.server.dependencies import get_http_request

from ..._app import source_add as add_core
from ..._app import source_content as content_core
from ..._app import source_mutations as mut_core
from ..._app import source_wait as wait_core
from ..._app.serialize import to_jsonable
from ...exceptions import (
    ConfigurationError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
    ValidationError,
)
from ...types import source_status_to_str
from ...urls import is_youtube_url
from .._confirm import DESTRUCTIVE, READ_ONLY, needs_confirmation
from .._context import get_client, get_file_transfer
from .._errors import mcp_errors, tool_error_payload
from .._filelink import UPLOAD_TTL, FileTransferConfig
from .._resolve import resolve_notebook, resolve_source
from ._content_sanity import _annotate_thin_warnings
from ._passthrough import passthrough_child_id
from ._preview import title_for_id

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


def _source_view(source: Any) -> dict[str, Any]:
    """Serialize a Source with agent-readable string labels added.

    ``to_jsonable`` emits only dataclass fields, so the integer ``status`` and
    ``_type_code`` arrive as bare numbers and the ``kind`` *property* is dropped —
    forcing an agent to guess what ``3``/``5``/``2`` mean. Add ``kind`` (e.g.
    ``"pdf"``/``"web_page"``) and ``status_label`` (e.g. ``"ready"``/``"error"``)
    string labels alongside the raw codes.

    ``status_label`` comes from :func:`~notebooklm.rpc.types.source_status_to_str`
    — the repo's single source of truth for status→string — so the MCP label stays
    in lock-step with the CLI surface. It is one of ``ready``/``processing``/
    ``error``/``preparing`` (``unknown`` for an unrecognized code), the same
    vocabulary the ``source_list`` ``status`` filter accepts.
    """
    view = to_jsonable(source)
    view["kind"] = source.kind.value
    view["status_label"] = source_status_to_str(source.status)
    return view


def _add_result_payload(source: Any, base: dict[str, Any]) -> dict[str, Any]:
    """Project a ``source_add`` result: enrich the added source + flag failure.

    Replaces ``base["source"]`` (the bare ``to_jsonable`` source dict) with the
    label-enriched :func:`_source_view` so ``source_add`` output reaches parity
    with ``source_list`` / ``source_get_content`` (``kind`` + ``status_label``).

    When the add response ALREADY reflects a failed import (``status`` == ERROR),
    surface it synchronously with a top-level ``warning``. Most imports are
    processed asynchronously, so a freshly-added source is usually still
    PROCESSING/PREPARING and the failure only surfaces later — but when the
    backend echoes ERROR at add-time we say so immediately rather than letting it
    look like a successful add.
    """
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
    ) -> dict[str, Any]:
        """List a notebook's sources. Accepts a notebook name or ID.

        Each source carries string ``kind`` / ``status_label`` labels (not just the
        raw type/status codes) so an agent never has to guess the enums.

        Pass ``status`` to return only sources whose ``status_label`` matches —
        filter by the SAME label the listing emits. ``error`` is a failed import
        (the "ghost row" left by a broken ``source_add``); use
        ``source_list(status="error")`` to find them without paging the whole list,
        then clean up with ``source_delete``. Omitting ``status`` returns every
        source (unchanged behavior).

        A ``label`` filter is not offered yet: labels are a notebook-level concept,
        not a per-source attribute, so there is nothing to filter on here today.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            sources = await client.sources.list(nb_id)
            # Filter on the raw Source BEFORE serializing, so _source_view (which
            # runs to_jsonable) is only paid for the sources that survive the
            # filter. Uses the same source_status_to_str label _source_view emits.
            if status is not None:
                sources = [s for s in sources if source_status_to_str(s.status) == status]
            return {"notebook_id": nb_id, "sources": [_source_view(s) for s in sources]}

    @mcp.tool(annotations=READ_ONLY)
    async def source_get_content(
        ctx: Context,
        notebook: str,
        source: str,
        output_format: Literal["text", "markdown"] = "text",
        max_chars: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch a source's metadata AND its full indexed text content.

        Accepts a notebook/source name or ID. Returns the source metadata (incl.
        string ``kind``/``status_label``) plus the extracted ``content``, the full
        ``char_count``, and a ``truncated`` flag.

        ``output_format`` selects how the text is rendered: ``text`` (default) is
        flattened plaintext; ``markdown`` preserves headings/tables/links/emphasis
        (requires the server's ``markdownify`` extra — otherwise a clean error).

        For large sources, window the body with ``offset`` and ``max_chars`` (the
        returned ``content`` is the slice ``[offset : offset+max_chars]``);
        ``char_count`` is always the FULL length and ``truncated`` says whether the
        returned slice is shorter than the remainder. Prefer ``chat_ask`` for
        querying large sources rather than pulling the whole body.

        ``content`` is ``null`` (and ``char_count`` 0) when the source is not yet
        ready (still processing / errored) or has no extractable text, so this tool
        returns the metadata even before the body is available.
        """
        client = get_client(ctx)
        with mcp_errors():
            if max_chars is not None and max_chars < 0:
                raise ValidationError(f"max_chars must be >= 0; got {max_chars}")
            if offset < 0:
                raise ValidationError(f"offset must be >= 0; got {offset}")
            nb_id = await resolve_notebook(client, notebook)
            src_id = await resolve_source(client, nb_id, source)
            result = await content_core.execute_source_get(
                client, content_core.SourceGetPlan(notebook_id=nb_id, source_id=src_id)
            )
            # A full-UUID ref skips list resolution (the resolver trusts a full
            # id), so a non-existent id reaches ``get_or_none`` and yields a
            # ``None`` source. Surface that as NOT_FOUND rather than returning
            # ``{"source": null}`` as a success.
            if result.source is None:
                raise SourceNotFoundError(src_id)

            # Only fetch the body once the source is READY. A not-ready source
            # (still processing / errored) has no retrievable text yet, so return
            # its metadata with content=None instead of fetching. Gating on status
            # (rather than catching the fulltext fetch's SourceNotFoundError) keeps
            # a genuine "source is gone" — e.g. deleted between these two calls —
            # propagating as NOT_FOUND instead of masquerading as "no content".
            content: str | None = None
            char_count = 0
            if result.source.is_ready:
                try:
                    fulltext = await content_core.execute_source_fulltext(
                        client,
                        content_core.SourceFulltextPlan(
                            notebook_id=nb_id, source_id=src_id, output_format=output_format
                        ),
                    )
                except ImportError as exc:
                    # ``output_format='markdown'`` needs the optional ``markdownify``
                    # extra, which the server may not have installed. Surface a
                    # deterministic CONFIG error (with the install hint) rather than
                    # the bug-class UNEXPECTED a bare ImportError would project as.
                    # Restrict the remap to the markdown path: an ImportError on the
                    # text path (or a future regression) is genuinely unexpected and
                    # must keep propagating as such, not be mislabeled CONFIG.
                    if output_format != "markdown":
                        raise
                    raise ConfigurationError(str(exc)) from exc
                content = fulltext.fulltext.content or None
                char_count = fulltext.fulltext.char_count

            # Window the body if requested. ``char_count`` stays the FULL length;
            # ``truncated`` reports whether the returned slice omits any remainder.
            truncated = False
            if content is not None and (offset > 0 or max_chars is not None):
                end = len(content) if max_chars is None else offset + max_chars
                windowed = content[offset:end]
                truncated = len(windowed) < (len(content) - offset)
                # Normalize an empty slice (e.g. offset past the end) to None, matching
                # the fetch-path contract (content is null when there's nothing to show).
                content = windowed or None

            payload = to_jsonable(result)
            payload["source"] = _source_view(result.source)
            payload["content"] = content
            payload["char_count"] = char_count
            payload["truncated"] = truncated
            payload["output_format"] = output_format
            return payload

    @mcp.tool(annotations=READ_ONLY)
    async def source_describe(
        ctx: Context,
        notebook: str,
        source: str,
    ) -> dict[str, Any]:
        """Return a source's AI summary + keywords for low-token triage.

        Accepts a notebook/source name or ID. Returns the backend's AI-generated
        ``summary`` (a short markdown overview) and ``keywords`` (the topic
        keyword list) — a compact "what is this source about?" view.

        Prefer this over ``source_get_content`` when triaging: it returns a tiny
        AI digest instead of the full indexed text, so it is cheap to fan out
        across many sources before deciding which to pull in full. Use
        ``source_get_content`` when you need the actual body, and ``chat_ask`` to
        query across sources.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            src_id = await resolve_source(client, nb_id, source)
            # A full-UUID ref skips list resolution (the resolver trusts a full
            # id), so a non-existent id reaches the guide RPC, which returns an
            # EMPTY guide for a missing source — indistinguishable from a real
            # source that simply has no guide yet. Guard existence first (same as
            # ``source_get_content``) so a deleted source surfaces as NOT_FOUND
            # rather than a misleading ``{summary: "", keywords: []}`` success.
            existing = await content_core.execute_source_get(
                client, content_core.SourceGetPlan(notebook_id=nb_id, source_id=src_id)
            )
            if existing.source is None:
                raise SourceNotFoundError(src_id)
            result = await content_core.execute_source_guide(
                client, content_core.SourceGuidePlan(notebook_id=nb_id, source_id=src_id)
            )
            return {
                "notebook_id": nb_id,
                "source_id": result.source_id,
                "summary": result.summary,
                "keywords": list(result.keywords),
            }

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
            return to_jsonable(result)

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
        timeout: float = 120.0,
        interval: float = 1.0,
    ) -> dict[str, Any]:
        """Wait for sources to finish processing. Accepts a notebook name or ID.

        Waits for a single source when ``source`` (name or ID) is given, otherwise
        for every source in the notebook. BOTH modes return the SAME structured
        aggregate, so an agent never has to branch on the shape:

            {"notebook_id", "ok", "ready", "timed_out", "failed", "not_found"}

        ``ready`` holds the sources that reached READY (each with ``kind`` /
        ``status_label`` labels); ``timed_out`` / ``failed`` / ``not_found`` hold
        ``{"source_id", "error"}`` entries for the sources that did not. ``ok`` is
        ``true`` iff all three error buckets are empty. The all-sources mode reports
        **partial progress** — a slow or failed source no longer discards the ones
        that did become ready.

        A READY **web-page** entry may carry a non-blocking ``warning`` when its
        indexed text is suspiciously thin — a likely dead link / soft-404 / paywalled
        "ghost source" add-time status can't catch. Advisory only (still READY, still
        ``ok``; verify with ``source_get_content``); short pasted text / transcripts
        are never flagged.

        A single-source ``source`` ref that does not resolve (e.g. an unknown title)
        still raises NOT_FOUND before the wait — that is an input error, distinct
        from a resolved source the backend reports missing/failed/slow (which lands
        in a bucket).
        """
        client = get_client(ctx)
        with mcp_errors():
            if timeout < 0:
                raise ValidationError(f"timeout must be >= 0; got {timeout}")
            if interval <= 0:
                raise ValidationError(f"interval must be > 0; got {interval}")
            nb_id = await resolve_notebook(client, notebook)
            if source is not None:
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
                return await _aggregate_wait_outcomes(client, nb_id, [outcome])
            sources = await client.sources.list(nb_id)
            outcomes = await _wait_all_sources(
                client, nb_id, [s.id for s in sources], timeout=timeout, interval=interval
            )
            return await _aggregate_wait_outcomes(client, nb_id, outcomes)

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
          ``{"status": "upload_required", "url": …, "agent_upload": {…}}``. A human
          opens the short-lived signed URL in a browser and uploads the file; an
          **agent that already holds the bytes** skips the browser and POSTs them as
          the raw request body to the same URL (see the ``agent_upload`` recipe in
          the response — with ``Accept: application/json`` it returns
          ``{"status": "added", "source_id": …}``). Then confirm with ``source_wait``
          / ``source_list``. ``title`` / ``mime_type`` (carried in the signed URL)
          and the supplied ``path`` (its basename seeds the default title) are honored.
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
                return _add_result_payload(drive_result.source, to_jsonable(drive_result))

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
            return _add_result_payload(src, to_jsonable(add_core.SourceAddResult(source=src)))


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


def _broker_upload(
    cfg: FileTransferConfig,
    notebook_id: str,
    *,
    title: str | None,
    mime_type: str | None,
    path: str | None,
) -> dict[str, Any]:
    """Mint a signed upload URL for a remote ``source_add type=file``.

    The agent-supplied ``title`` / ``mime_type`` ride in the signed token (so they
    survive the browser round-trip and cannot be tampered with). When ``title`` is
    unset, the supplied ``path``'s basename seeds the default. The signer injects
    expiry; ``expires_at`` mirrors the upload TTL for the caller.
    """
    default_title = title
    if not default_title and path:
        # The agent's path may be Windows-style (``C:\\Users\\me\\report.pdf``) even
        # though this server runs on Linux, where ``os.path.basename`` won't split on
        # ``\\`` — normalize first so the default title is the real leaf.
        default_title = os.path.basename(path.replace("\\", "/")) or None
    payload: dict[str, Any] = {"nb": notebook_id}  # op stamped by upload_url
    if default_title:
        payload["title"] = default_title
    if mime_type:
        payload["mime"] = mime_type
    url = cfg.upload_url(payload)
    return {
        "status": "upload_required",
        "notebook_id": notebook_id,
        "url": url,
        "expires_at": int(time.time()) + UPLOAD_TTL,
        # An agent holding the bytes skips the browser: POST them as the raw body here.
        "agent_upload": {
            "method": "POST",
            "url": f"{url}?filename=<basename>",
            "headers": {
                "Accept": "application/json",
                "Content-Type": "<mime-type> (fallback only; ignored when mime_type was passed)",
            },
            "body": "the raw file bytes (not multipart/form-data)",
            "returns": '{"status": "added", "source_id": ...}',
            "example": (
                'curl -X POST -H "Accept: application/json" --data-binary @report.pdf '
                f'"{url}?filename=report.pdf"'
            ),
        },
    }


async def _add_one(
    client: NotebookLMClient,
    notebook_id: str,
    content: str,
    *,
    source_type: add_core.SourceAddType,
    title: str | None,
    mime_type: str | None,
    allow_internal: bool,
) -> Source:
    """Build the source-add plan + execute it, returning the created ``Source``.

    The single seam shared by single-mode and batch-mode ``source_add`` (and the
    point #1679 layers add-time failure-signaling onto). Callers do their own
    presence / host validation BEFORE reaching here — single mode via
    :func:`_select_content` (which keeps the YouTube-host guard), batch mode via
    the explicit ``source_type="url"`` that forces :func:`add_core.validate_url`.
    """
    plan = add_core.build_source_add_plan(
        content=content,
        source_type=source_type,
        title=title,
        mime_type=mime_type,
        follow_symlinks=False,
        validate_path=add_core.validate_upload_path,
        looks_path_shaped=add_core.looks_like_path,
        allow_internal=allow_internal,
    )
    result = await add_core.execute_source_add(
        client,
        add_core.SourceAddExecutionPlan(notebook_id=notebook_id, plan=plan),
    )
    return result.source


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
        "notebook_id": notebook_id,
        "added": added,
        "failed": len(results) - added,
        "results": results,
    }


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


async def _wait_all_sources(
    client: NotebookLMClient,
    notebook_id: str,
    source_ids: list[str],
    *,
    timeout: float,
    interval: float,
) -> list[wait_core.SourceWaitOutcome]:
    """Wait for every source concurrently, returning one outcome per source.

    Unlike ``client.sources.wait_for_sources`` (which re-raises the first failure
    and discards the sources that already became ready), each per-source wait runs
    through :func:`execute_source_wait`, which maps the three handled
    ``SourceWait*`` failures to a typed outcome instead of raising — so a slow or
    failed source never throws away its siblings' progress.

    An UNEXPECTED exception (e.g. an auth/transport ``RPCError``, a bug) is NOT a
    handled outcome: a bare ``asyncio.gather`` would re-raise it without cancelling
    the still-running sibling pollers, leaking coroutines. Mirror the library's
    ``wait_for_sources`` discipline (``_source/polling.py``): drive explicit tasks
    and, on any such escape, cancel + drain the pending siblings before re-raising
    (it then flows through ``mcp_errors()``).
    """
    tasks = [
        asyncio.create_task(
            wait_core.execute_source_wait(
                client,
                wait_core.SourceWaitPlan(
                    notebook_id=notebook_id,
                    source_id=sid,
                    timeout=timeout,
                    interval=interval,
                ),
            )
        )
        for sid in source_ids
    ]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _wait_bucket_entry(
    error: SourceNotFoundError | SourceProcessingError | SourceTimeoutError,
) -> dict[str, str]:
    """Project a handled wait failure onto its ``{source_id, error}`` bucket entry."""
    return {"source_id": error.source_id, "error": str(error)}


async def _aggregate_wait_outcomes(
    client: NotebookLMClient,
    notebook_id: str,
    outcomes: list[wait_core.SourceWaitOutcome],
) -> dict[str, Any]:
    """Project per-source wait outcomes onto the unified aggregate wire shape.

    Both ``source_wait`` modes (single source, all sources) share this contract:
    ready sources are returned alongside the ones that timed out / failed / went
    missing, so the all-sources mode reports partial progress instead of discarding
    the sources that did become ready. ``ok`` is ``True`` iff nothing landed in an
    error bucket.

    READY web-page entries are additionally annotated with a non-blocking
    content-sanity ``warning`` when their indexed text is suspiciously thin (a
    likely dead link / soft-404 / paywall ghost source) — see
    :func:`_annotate_thin_warnings`. The warning is purely advisory: a thin source
    is still READY and the wait is still ``ok``.
    """
    ready: list[dict[str, Any]] = []
    # Pair each ready view with its Source so the thin-content sanity check can
    # read the kind + fetch the body without re-resolving.
    ready_pairs: list[tuple[dict[str, Any], Source]] = []
    timed_out: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    not_found: list[dict[str, str]] = []
    for outcome in outcomes:
        if isinstance(outcome, wait_core.SourceWaitReady):
            view = _source_view(outcome.source)
            ready.append(view)
            ready_pairs.append((view, outcome.source))
        elif isinstance(outcome, wait_core.SourceWaitTimeout):
            timed_out.append(_wait_bucket_entry(outcome.error))
        elif isinstance(outcome, wait_core.SourceWaitProcessingError):
            failed.append(_wait_bucket_entry(outcome.error))
        elif isinstance(outcome, wait_core.SourceWaitNotFound):
            not_found.append(_wait_bucket_entry(outcome.error))
        else:  # exhaustive over the closed SourceWaitOutcome union
            # mypy narrows ``outcome`` to ``Never`` here; a future outcome variant
            # would surface as a type error AND fail loudly at runtime rather than
            # being silently dropped from every bucket.
            raise AssertionError(f"unhandled SourceWaitOutcome: {outcome!r}")
    await _annotate_thin_warnings(client, notebook_id, ready_pairs)
    return {
        "notebook_id": notebook_id,
        "ok": not (timed_out or failed or not_found),
        "ready": ready,
        "timed_out": timed_out,
        "failed": failed,
        "not_found": not_found,
    }
