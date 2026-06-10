"""Source MCP tools.

Thin adapters over the transport-neutral ``_app.source_*`` cores: resolve the
notebook (and, where applicable, the source) reference via the Phase 1
:mod:`._resolve` helpers, drive the ``execute_source_*`` executors, and project
the typed result to the wire with :func:`to_jsonable`.

``source_add`` is a hybrid over two cores: ``url``/``text``/``file``/``youtube``
flow through ``_app.source_add`` (``build_source_add_plan`` + ``execute_source_add``);
``drive`` flows through ``_app.source_mutations.execute_source_add_drive`` (the
neutral ``source_add`` core has no Drive path). ``source_wait`` waits for one
source when ``source`` is given, else every source in the notebook.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..._app import source_add as add_core
from ..._app import source_content as content_core
from ..._app import source_mutations as mut_core
from ..._app import source_wait as wait_core
from ..._app.serialize import to_jsonable
from ...exceptions import SourceNotFoundError, ValidationError
from ...urls import is_youtube_url
from .._confirm import DESTRUCTIVE, READ_ONLY, needs_confirmation
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook, resolve_source
from ._passthrough import passthrough_child_id
from ._preview import title_for_id

#: MCP source types. Superset of the neutral ``source_add`` core's types
#: (which lacks ``drive``); ``drive`` is dispatched to the Drive path.
_SOURCE_TYPES = ("url", "text", "file", "drive", "youtube")

#: The default Drive MIME choice when the caller does not specify one.
_DEFAULT_DRIVE_MIME = "google-doc"


def register(mcp: Any) -> None:
    """Register the source tools on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def source_list(ctx: Context, notebook: str) -> dict[str, Any]:
        """List a notebook's sources. Accepts a notebook name or ID."""
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            sources = await client.sources.list(nb_id)
            return {"notebook_id": nb_id, "sources": to_jsonable(sources)}

    @mcp.tool(annotations=READ_ONLY)
    async def source_get_content(ctx: Context, notebook: str, source: str) -> dict[str, Any]:
        """Fetch a source's details. Accepts a notebook/source name or ID."""
        client = get_client(ctx)
        with mcp_errors():
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
            return to_jsonable(result)

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

    @mcp.tool
    async def source_wait(
        ctx: Context,
        notebook: str,
        source: str | None = None,
        timeout: float = 120.0,
        interval: float = 1.0,
    ) -> dict[str, Any]:
        """Wait for sources to finish processing. Accepts a notebook name or ID.

        Waits for a single source when ``source`` (name or ID) is given, otherwise
        for every source in the notebook. Returns the ready sources, or surfaces a
        not-found / processing / timeout error.
        """
        client = get_client(ctx)
        with mcp_errors():
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
                return _wait_outcome_payload(nb_id, outcome)
            sources = await client.sources.list(nb_id)
            source_ids = [s.id for s in sources]
            # ``wait_for_sources`` forwards **kwargs to ``wait_until_ready``,
            # whose poll-interval kwarg is ``initial_interval`` — thread the
            # advertised ``interval`` through so the all-sources branch honors it
            # just like the single-source branch above.
            ready = await client.sources.wait_for_sources(
                nb_id, source_ids, timeout=timeout, initial_interval=interval
            )
            return {"notebook_id": nb_id, "ready": to_jsonable(ready)}

    @mcp.tool
    async def source_add(
        ctx: Context,
        notebook: str,
        source_type: str,
        url: str | None = None,
        text: str | None = None,
        title: str | None = None,
        path: str | None = None,
        document_id: str | None = None,
        mime_type: str | None = None,
        allow_internal: bool = False,
    ) -> dict[str, Any]:
        """Add a source to a notebook. Accepts a notebook name or ID.

        ``source_type`` selects the input and which named argument is required:

        * ``url``     — requires ``url``.
        * ``youtube`` — requires ``url`` (a YouTube link).
        * ``text``    — requires ``text``; ``title`` optional.
        * ``file``    — requires ``path`` (a local file path on the server host).
        * ``drive``   — requires ``document_id`` (Google Drive file id); ``title``
          and ``mime_type`` (one of google-doc|google-slides|google-sheets|pdf,
          default google-doc) optional.

        The other named inputs are mutually exclusive — supply only the one your
        ``source_type`` requires.
        """
        client = get_client(ctx)
        with mcp_errors():
            if source_type not in _SOURCE_TYPES:
                raise ValidationError(
                    f"Unknown source type {source_type!r}; expected one of {list(_SOURCE_TYPES)}"
                )
            nb_id = await resolve_notebook(client, notebook)

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
                return to_jsonable(drive_result)

            content = _select_content(source_type, url=url, text=text, path=path)
            plan = add_core.build_source_add_plan(
                content=content,
                source_type=source_type,  # type: ignore[arg-type]
                title=title,
                mime_type=mime_type,
                follow_symlinks=False,
                validate_path=add_core.validate_upload_path,
                looks_path_shaped=add_core.looks_like_path,
                allow_internal=allow_internal,
            )
            add_result = await add_core.execute_source_add(
                client,
                add_core.SourceAddExecutionPlan(notebook_id=nb_id, plan=plan),
            )
            return to_jsonable(add_result)


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


def _wait_outcome_payload(notebook_id: str, outcome: wait_core.SourceWaitOutcome) -> dict[str, Any]:
    """Project a single-source :class:`SourceWaitOutcome` onto the wire shape."""
    if isinstance(outcome, wait_core.SourceWaitReady):
        return {
            "notebook_id": notebook_id,
            "status": "ready",
            "source": to_jsonable(outcome.source),
        }
    if isinstance(outcome, wait_core.SourceWaitNotFound):
        return {"notebook_id": notebook_id, "status": "not_found", "error": str(outcome.error)}
    if isinstance(outcome, wait_core.SourceWaitProcessingError):
        return {"notebook_id": notebook_id, "status": "failed", "error": str(outcome.error)}
    return {"notebook_id": notebook_id, "status": "timeout", "error": str(outcome.error)}
