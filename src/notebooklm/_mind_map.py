"""Mind-map facade backed by :mod:`_note_service`.

Mind maps live in the same backend collection as user notes and use the
same ``GET_NOTES_AND_MIND_MAPS`` / ``CREATE_NOTE`` / ``UPDATE_NOTE`` /
``DELETE_NOTE`` RPC family. They are still AI-generated artifacts from
the caller's perspective, so :class:`NoteBackedMindMapService` adapts
the note-row primitives into a mind-map-only surface. Both
``ArtifactsAPI`` (download path) and ``NotesAPI`` (forward-compatible
``list_mind_maps`` / ``delete_mind_map`` surface) consume this adapter.

Phase 6 (refactor-history.md Step 9, ADR-013) retired the legacy
``MindMapService`` class and its module-level compatibility wrappers
(``create_note``, ``list_mind_maps``, ``update_note``, ...) together
with the saved-from-chat encoder, which now lives in
:mod:`_chat_notes`. Only the :class:`NoteBackedMindMapService`
adapter remains.
"""

from __future__ import annotations

from typing import Any

from ._note_service import NoteRowKind, NoteService


class NoteBackedMindMapService:
    """Mind-map-only facade over :class:`NoteService`.

    Adapter that knows mind maps share storage with notes. Consumers
    (``ArtifactsAPI`` download path, ``NotesAPI`` mind-map surface)
    talk to this class instead of reaching into ``NoteService``
    directly, so the "mind maps are notes under the hood" detail
    stays localized.

    The download path doesn't need ``create_mind_map`` — mind-map
    creation goes through :meth:`NoteService.create_note` directly
    from ``_artifact_generation.generate_mind_map`` (a one-shot
    GENERATE_MIND_MAP + persist pipeline). The methods exposed here
    are exactly the ones the artifact download path and ``NotesAPI``
    ``list_mind_maps`` / ``delete_mind_map`` need.
    """

    def __init__(self, notes: NoteService) -> None:
        self._notes = notes

    async def list_mind_maps(self, notebook_id: str) -> list[Any]:
        """Return mind-map rows for a notebook (deleted rows excluded)."""
        rows = await self._notes.fetch_note_rows(notebook_id)
        return [r for r in rows if self._notes.classify_row(r) == NoteRowKind.MIND_MAP]

    def extract_content(self, row: list[Any]) -> str | None:
        """Return the JSON content payload of a mind-map row.

        Delegates to :meth:`NoteService.extract_content` so the download
        path doesn't have to know mind maps share storage with notes.
        """
        return self._notes.extract_content(row)

    async def delete_mind_map(self, notebook_id: str, note_id: str) -> bool:
        """Soft-delete a mind-map row.

        Delegates to :meth:`NoteService.delete_note`. Returns its bool
        result so the v0.4.1 ``NotesAPI.delete_mind_map(...) -> bool``
        public contract is preserved.
        """
        return await self._notes.delete_note(notebook_id, note_id)
