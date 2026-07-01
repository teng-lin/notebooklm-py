"""Note MCP tools.

Thin adapters over the transport-neutral ``_app.notes`` core. Notebook refs
resolve via the Phase 1 :func:`resolve_notebook`; note refs resolve via
:func:`resolve_note` (name OR id, notebook-scoped). The ``_app`` executors take
injected ``resolve_notebook_id`` / ``resolve_note_id`` callables shaped for the
CLI; since the MCP adapter resolves refs up front it passes the shared
pass-through resolvers, which return the already-resolved ids unchanged.

Split into verbs (``note_create`` / ``note_list`` / ``note_update`` /
``note_delete``), NOT an ``action`` enum. ``note_delete`` follows the two-step
confirm contract; ``note_list`` is read-only (and single-fetches one note by ref
when ``note`` is given). ``note_update`` updates content and/or title
(title-only = rename).

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..._app import notes as core
from ..._app.serialize import to_jsonable
from ...exceptions import NoteNotFoundError, ValidationError
from .._confirm import DESTRUCTIVE, READ_ONLY, needs_confirmation
from .._context import get_client
from .._errors import mcp_errors
from .._paginate import DEFAULT_LIMIT, paginate
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
                "status": "created",
                "notebook_id": result.notebook_id,
                "title": result.title,
                "note_id": result.note_id,
                # The facade raises on failure (no degenerate result), so
                # reaching here always means the note was really created.
                # ``created`` kept for back-compat alongside the ``status`` envelope.
                "created": True,
            }

    @mcp.tool(annotations=READ_ONLY)
    async def note_list(
        ctx: Context,
        notebook: str,
        note: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List a notebook's notes, or fetch one by ref. Accepts a notebook name or ID.

        Omit ``note`` for a bounded page: ``limit`` (default 50) notes from
        ``offset``, plus ``total`` / ``offset`` / ``has_more`` (page with
        ``offset += limit``). Pass ``note`` (a note name or id) to return just that
        one note — still in the ``notes`` list shape (a 1-element list, ``total`` 1)
        — or a not-found error if the ref doesn't resolve. ``limit`` / ``offset``
        are ignored when ``note`` is given.
        """
        client = get_client(ctx)
        with mcp_errors():
            # Validate pagination args unconditionally, so ``note_list(note=x,
            # limit=0)`` still errors even though they're ignored on the single-fetch
            # path (matches ``paginate``'s bounds on the list path).
            if limit < 1:
                raise ValidationError("limit must be >= 1.")
            if offset < 0:
                raise ValidationError("offset must be >= 0.")
            nb_id = await resolve_notebook(client, notebook)
            if note is not None:
                # Single fetch by ref — the old ``note_get``, wrapped as a 1-element
                # list so the return shape never branches on whether ``note`` is set.
                note_id = await resolve_note(client, nb_id, note)
                result = await core.execute_note_get(
                    client,
                    nb_id,
                    note_id,
                    resolve_notebook_id=passthrough_notebook_id,
                    resolve_note_id=passthrough_child_id,
                )
                # ``resolve_note`` raises for an unknown title/prefix, but its
                # full-UUID fast-path skips the list — so a concrete-but-absent id
                # reaches here as ``found=False``. Surface the same typed not-found.
                if not result.found:
                    raise NoteNotFoundError(note_id)
                return {
                    "notebook_id": nb_id,
                    "notes": [to_jsonable(result.note)],
                    "total": 1,
                    "offset": 0,
                    "has_more": False,
                }
            notes = await client.notes.list(nb_id)
            page, meta = paginate(to_jsonable(notes), limit, offset)
            return {"notebook_id": nb_id, "notes": page, **meta}

    @mcp.tool
    async def note_update(
        ctx: Context,
        notebook: str,
        note: str,
        content: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Update a note's content and/or title. Accepts a notebook/note name or ID.

        Supply ``content``, ``title``, or both. Passing only ``title`` renames the
        note while leaving its body untouched; passing only ``content`` replaces
        the body and keeps the title. At least one of the two is required.
        """
        client = get_client(ctx)
        with mcp_errors():
            if content is None and title is None:
                raise ValidationError("provide 'content' and/or 'title' to update")
            nb_id = await resolve_notebook(client, notebook)
            note_id = await resolve_note(client, nb_id, note)
            result = await core.execute_note_save(
                client,
                nb_id,
                note_id,
                title=title,
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
