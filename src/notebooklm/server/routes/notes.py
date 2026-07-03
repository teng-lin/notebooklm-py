"""Note routes — ``/v1/notebooks/{id}/notes`` list / get / create / update / delete.

Thin adapters over the public ``client.notes`` namespace (the facade already
owns the raise-on-miss, idempotent-delete, and update-preflight contracts), in
the same shape as :mod:`.notebooks`. Responses go straight through
:func:`notebooklm._app.serialize.to_jsonable`.

A missing note surfaces as ``NoteNotFoundError`` from the facade, which the
server's exception handler projects onto a 404 ``not_found`` envelope — so these
handlers never hand-raise for the not-found case. ``delete`` is idempotent (never
404s on an absent id). Mind maps are out of scope here; list them via
``/v1/notebooks/{id}/artifacts``.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from ..._app.serialize import to_jsonable
from ...client import NotebookLMClient
from .._context import get_client
from .._pagination import MAX_LIMIT, paginate_envelope

__all__ = ["router"]

router = APIRouter(prefix="/notebooks/{notebook_id}/notes", tags=["notes"])

ClientDep = Annotated[NotebookLMClient, Depends(get_client)]


class NoteCreate(BaseModel):
    """Request body for creating a note (both fields default, matching the facade)."""

    title: str = "New Note"
    content: str = ""


class NoteUpdate(BaseModel):
    """Request body for a full-replacement note update (PUT)."""

    title: str
    content: str


@router.get("")
async def list_notes(
    notebook_id: str,
    client: ClientDep,
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """List a notebook's text notes (excludes mind maps and deleted notes).

    Defaults to the full collection under ``notes`` (unchanged). Supply
    ``?limit=`` to slice and add a ``meta`` block; ``?offset=`` pages forward.
    """
    notes = await client.notes.list(notebook_id)
    return paginate_envelope(
        to_jsonable(notes), key="notes", limit=limit, offset=offset, notebook_id=notebook_id
    )


@router.get("/{note_id}")
async def get_note(notebook_id: str, note_id: str, client: ClientDep) -> dict[str, Any]:
    """Fetch one note (raises ``NoteNotFoundError`` → 404 on a miss)."""
    note = await client.notes.get(notebook_id, note_id)
    return to_jsonable(note)


@router.post("", status_code=201)
async def create_note(notebook_id: str, body: NoteCreate, client: ClientDep) -> dict[str, Any]:
    """Create a note with the given title and content."""
    note = await client.notes.create(notebook_id, body.title, body.content)
    return to_jsonable(note)


@router.put("/{note_id}")
async def update_note(
    notebook_id: str, note_id: str, body: NoteUpdate, client: ClientDep
) -> dict[str, Any]:
    """Replace a note's title and content (raises ``NoteNotFoundError`` → 404 on a miss)."""
    await client.notes.update(notebook_id, note_id, content=body.content, title=body.title)
    # Re-fetch so the response carries the canonical note shape (same as GET);
    # the update preflight already 404s a missing note, so it exists here.
    note = await client.notes.get(notebook_id, note_id)
    return to_jsonable(note)


@router.delete("/{note_id}", status_code=204)
async def delete_note(notebook_id: str, note_id: str, client: ClientDep) -> Response:
    """Delete a note (idempotent-on-missing — never 404 for an absent id)."""
    await client.notes.delete(notebook_id, note_id)
    return Response(status_code=204)
