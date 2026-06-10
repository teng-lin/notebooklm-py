"""Note MCP tools.

Thin adapters over the transport-neutral ``_app.notes`` core. Notebook refs
resolve via the Phase 1 :func:`resolve_notebook`; note refs resolve via
:func:`resolve_note` (name OR id, notebook-scoped). The ``_app`` executors take
injected ``resolve_notebook_id`` / ``resolve_note_id`` callables shaped for the
CLI; since the MCP adapter resolves refs up front it passes the shared
pass-through resolvers, which return the already-resolved ids unchanged.

Split into verbs (``note_create`` / ``note_list`` / ``note_update`` /
``note_delete``), NOT an ``action`` enum. ``note_delete`` follows the two-step
confirm contract; ``note_list`` is read-only.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..._app import notes as core
from ..._app.serialize import to_jsonable
from .._confirm import DESTRUCTIVE, READ_ONLY, needs_confirmation
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_note, resolve_notebook
from ._passthrough import passthrough_child_id, passthrough_notebook_id
from ._preview import title_for_id


def register(mcp: Any) -> None:
    """Register the note tools on ``mcp``."""

    @mcp.tool
    async def note_create(ctx: Context, notebook: str, title: str, content: str) -> dict[str, Any]:
        """Create a note in a notebook. Accepts a notebook name or ID."""
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            result = await core.execute_note_create(
                client,
                nb_id,
                title,
                content,
                resolve_notebook_id=passthrough_notebook_id,
            )
            return {
                "notebook_id": result.notebook_id,
                "title": result.title,
                "note_id": result.note_id,
                # The facade raises on failure (no degenerate result), so
                # reaching here always means the note was really created.
                "created": True,
            }

    @mcp.tool(annotations=READ_ONLY)
    async def note_list(ctx: Context, notebook: str) -> dict[str, Any]:
        """List a notebook's notes. Accepts a notebook name or ID."""
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            notes = await client.notes.list(nb_id)
            return {"notebook_id": nb_id, "notes": to_jsonable(notes)}

    @mcp.tool
    async def note_update(ctx: Context, notebook: str, note: str, content: str) -> dict[str, Any]:
        """Update a note's content. Accepts a notebook/note name or ID."""
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            note_id = await resolve_note(client, nb_id, note)
            result = await core.execute_note_save(
                client,
                nb_id,
                note_id,
                title=None,
                content=content,
                resolve_notebook_id=passthrough_notebook_id,
                resolve_note_id=passthrough_child_id,
            )
            return {
                "status": "updated",
                "notebook_id": result.notebook_id,
                "note_id": result.note_id,
            }

    @mcp.tool(annotations=DESTRUCTIVE)
    async def note_delete(
        ctx: Context, notebook: str, note: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Delete a note (irreversible). Accepts a notebook/note name or ID.

        Two-step confirmation: with ``confirm=False`` (default) it returns a
        ``needs_confirmation`` preview of the resolved note without deleting; call
        again with ``confirm=True`` to perform the delete.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            note_id = await resolve_note(client, nb_id, note)
            if not confirm:
                title = title_for_id(await client.notes.list(nb_id), note_id)
                return needs_confirmation(
                    {
                        "action": "delete_note",
                        "notebook_id": nb_id,
                        "note_id": note_id,
                        "title": title,
                    }
                )
            await core.execute_note_delete(client, nb_id, note_id)
            return {"status": "deleted", "notebook_id": nb_id, "note_id": note_id}
