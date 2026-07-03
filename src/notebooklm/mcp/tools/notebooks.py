"""Notebook MCP tools.

Thin adapters over the transport-neutral ``_app.notebooks`` core: resolve the
notebook reference (name OR id) via the Phase 1 :mod:`._resolve` helper, drive the
``execute_notebook_*`` executors, and project the typed result to the wire with
:func:`to_jsonable`. No business logic lives here.

The ``_app`` rename/describe executors take an injected ``resolve_notebook_id``
callable shaped for the CLI (``(client, ref, *, json_output) -> id``). The MCP
adapter has already resolved the id with :func:`resolve_notebook`, so it passes
the shared :func:`passthrough_notebook_id` resolver, which returns the
already-resolved id unchanged.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Context

from ..._app import notebooks as core
from ..._app.serialize import to_jsonable
from .._confirm import DESTRUCTIVE, READ_ONLY, needs_confirmation
from .._context import get_client
from .._errors import mcp_errors
from .._paginate import DEFAULT_LIMIT, paginate
from .._resolve import resolve_notebook
from ._passthrough import passthrough_notebook_id
from ._preview import title_for_id


def register(mcp: Any) -> None:
    """Register the notebook tools on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def notebook_list(
        ctx: Context, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> dict[str, Any]:
        """List all notebooks (id + title + metadata).

        Returns a bounded page: ``limit`` (default 50) items from ``offset`` (default
        0), plus ``total`` / ``offset`` / ``has_more``. Page forward by re-calling
        with ``offset += limit`` while ``has_more`` is true.
        """
        client = get_client(ctx)
        with mcp_errors():
            notebooks = await client.notebooks.list()
            page, meta = paginate(to_jsonable(notebooks), limit, offset)
            return {"notebooks": page, **meta}

    @mcp.tool
    async def notebook_create(ctx: Context, title: str) -> dict[str, Any]:
        """Create a new notebook with the given title."""
        client = get_client(ctx)
        with mcp_errors():
            result = await core.execute_notebook_create(client, title)
            # Flatten the created notebook to a top-level shape consistent with
            # the sibling create tool (``note_create``) and ``notebook_delete``,
            # which key the notebook by ``notebook_id`` rather than nesting the
            # record under a ``notebook`` key (#1540). The remaining Notebook
            # fields (title, created_at, sources_count, is_owner, modified_at)
            # stay at the top level so no metadata is dropped. The
            # created_at/modified_at backfill (#1699) now lives in the
            # transport-neutral core (``execute_notebook_create``), so CLI / REST
            # / MCP all get populated timestamps from one place (#1705) — no
            # adapter-level re-read here.
            record = to_jsonable(result.notebook)
            notebook_id = record.pop("id")
            return {"status": "created", "notebook_id": notebook_id, **record}

    @mcp.tool(annotations=READ_ONLY)
    async def notebook_describe(
        ctx: Context, notebook: str, include_metadata: bool = False
    ) -> dict[str, Any]:
        """Fetch a notebook's AI-generated description. Accepts a notebook name or ID.

        Returns the resolved ``notebook_id`` plus the AI ``description``. Pass
        ``include_metadata=True`` to additionally fetch the notebook's metadata
        (details + source list) and surface it under a ``metadata`` key; the
        default output (``include_metadata`` omitted) is unchanged.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            if include_metadata:
                # Two independent reads (description + metadata) → run concurrently
                # (repo convention for independent RPCs). Drive explicit tasks so that
                # if either raises, the still-running sibling read is cancelled +
                # drained rather than leaked (mirrors ``_sources._wait_all_sources``).
                # A NotebookLMError from either still propagates through ``mcp_errors``.
                describe_task = asyncio.create_task(
                    core.execute_notebook_describe(
                        client, nb_id, resolve_notebook_id=passthrough_notebook_id
                    )
                )
                meta_task = asyncio.create_task(
                    core.execute_notebook_metadata(
                        client, nb_id, resolve_notebook_id=passthrough_notebook_id
                    )
                )
                tasks = (describe_task, meta_task)
                try:
                    result, meta_result = await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                output = to_jsonable(result)
                output["metadata"] = to_jsonable(meta_result.metadata)
                return output
            result = await core.execute_notebook_describe(
                client, nb_id, resolve_notebook_id=passthrough_notebook_id
            )
            return to_jsonable(result)

    @mcp.tool
    async def notebook_rename(ctx: Context, notebook: str, new_title: str) -> dict[str, Any]:
        """Rename a notebook. Accepts a notebook name or ID."""
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            result = await core.execute_notebook_rename(
                client, nb_id, new_title, resolve_notebook_id=passthrough_notebook_id
            )
            return {"status": "renamed", **to_jsonable(result)}

    @mcp.tool(annotations=DESTRUCTIVE)
    async def notebook_delete(ctx: Context, notebook: str, confirm: bool = False) -> dict[str, Any]:
        """Delete a notebook (irreversible). Accepts a notebook name or ID.

        Two-step confirmation: called with ``confirm=False`` (the default) it does
        NOT delete — it returns a ``needs_confirmation`` preview of the resolved
        notebook. Call again with ``confirm=True`` to perform the delete.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            if not confirm:
                title = title_for_id(await client.notebooks.list(), nb_id)
                return needs_confirmation(
                    {"action": "delete_notebook", "notebook_id": nb_id, "title": title}
                )
            await core.execute_notebook_delete(client, nb_id)
            return {"status": "deleted", "notebook_id": nb_id}
