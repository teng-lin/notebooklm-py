"""Note MCP tools.

Thin adapters over the transport-neutral ``_app.notes`` core. Notebook refs
resolve via the Phase 1 :func:`resolve_notebook`; note refs resolve via
:func:`resolve_note` (name OR id, notebook-scoped). The ``_app`` executors take
injected ``resolve_notebook_id`` / ``resolve_note_id`` callables shaped for the
CLI; since the MCP adapter resolves refs up front it passes the shared
pass-through resolvers, which return the already-resolved ids unchanged.

This module hosts the single note-authoring verb ``note_save`` (create-or-update
upsert — create when ``note`` is omitted, update when given). Reading and deleting
notes fold into the cross-type Studio surface: ``studio_list`` merges notes with
artifacts (and single-fetches one note by ref via ``item``), and ``studio_delete``
deletes a note or an artifact.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..._app import notes as core
from ...exceptions import ValidationError
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_note, resolve_notebook
from ._passthrough import passthrough_child_id, passthrough_notebook_id


def register(mcp: Any) -> None:
    """Register the note tools on ``mcp``."""

    @mcp.tool
    async def note_save(
        ctx: Context,
        notebook: str,
        note: str | None = None,
        title: str | None = None,
        content: str | None = None,
    ) -> dict[str, Any]:
        """Create a note, or update an existing one (upsert). Accepts a notebook name or ID.

        Mode is chosen SOLELY by ``note``:
        * ``note`` omitted → **create** a new note; ``title`` AND ``content`` are
          both required. Returns ``status="created"``.
        * ``note`` given (a note name or id) → **update** that note; supply ``title``
          and/or ``content`` (at least one — title-only renames, content-only
          replaces the body). A ref that doesn't resolve is a not-found error, NEVER
          a stray create. Returns ``status="updated"``.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            if note is None:
                if not title or not content:
                    raise ValidationError(
                        "title and content are required to create a note "
                        "(omit 'note' to create, or pass 'note' to update)."
                    )
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
                    "note_id": result.note_id,
                    "title": result.title,
                    # ``created`` kept for back-compat alongside the ``status`` envelope.
                    "created": True,
                }
            # update
            if title is None and content is None:
                raise ValidationError("provide 'title' and/or 'content' to update a note.")
            note_id = await resolve_note(client, nb_id, note)
            saved = await core.execute_note_save(
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
                "notebook_id": saved.notebook_id,
                "note_id": saved.note_id,
            }
