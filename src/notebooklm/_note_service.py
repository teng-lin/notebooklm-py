"""Semantic plain-note service.

``NoteService`` owns the migrated NOTE_* workflow used by ``NotesAPI`` and the
note-backed side of ``MindMapsAPI``. The deferred raw note-row implementation
that still serves saved-chat/artifact mind-map compatibility callers lives in
:mod:`notebooklm._mind_map` alongside its only consumer; this module is
backend-neutral and reaches the wire only through ``BackendAdapter``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ._backend import BackendAdapter
from ._projectors import project_mind_map, project_note
from ._records import (
    MIND_MAP_GENERATE_NOTE_DEF,
    MIND_MAP_LIST_DEF,
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    NOTE_UPDATE_DEF,
    MindMapGenerateNoteInput,
    MindMapListInput,
    MindMapRecord,
    NoteCreateInput,
    NoteDeleteInput,
    NoteGetInput,
    NoteListInput,
    NoteUpdateInput,
)
from .exceptions import MindMapNotFoundError
from .types import MindMap, Note

__all__ = ["NoteService"]

logger = logging.getLogger(__name__)


# Module-level strong-ref anchor for fire-and-forget cleanup tasks (RUF006).
# ``asyncio.create_task`` returns a Task that the event loop only holds via a
# weak reference, so an unrooted Task can be garbage-collected mid-execution —
# losing the orphan-row cleanup the cancel-safety shield is supposed to
# guarantee. Each created task adds itself here and removes itself in a
# done-callback so the set stays bounded.
#
# Intentionally module-level (not per-instance): the cleanup tasks are
# detached fire-and-forget work whose only purpose is to keep the loop's
# Task storage from GC-ing them mid-flight. Sharing one set across all
# ``NoteService`` instances is correct and simpler than per-instance
# bookkeeping — there is no per-instance state on the tasks themselves.
# Single-loop-per-client invariant per ADR-0004; not safe for multi-loop fan-out.
_cleanup_tasks: set[asyncio.Task[Any]] = set()


class NoteService:
    """Backend-neutral plain-note and note-backed mind-map workflows."""

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def list_notes(self, notebook_id: str) -> list[Note]:
        """Return active non-mind-map notes in backend order."""

        result = await self._backend.invoke(
            NOTE_LIST_DEF,
            NoteListInput(notebook_id),
            deadline=None,
        )
        return [project_note(record) for record in result.notes]

    async def get_note_or_none(self, notebook_id: str, note_id: str) -> Note | None:
        """Select the first exact note identity, or return a genuine miss."""

        result = await self._backend.invoke(
            NOTE_GET_DEF,
            NoteGetInput(notebook_id, note_id),
            deadline=None,
        )
        return None if result.note is None else project_note(result.note)

    async def create_note(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
        *,
        operation_variant: str = "plain",
    ) -> Note:
        """Create and finalize a note with cancellation-safe orphan cleanup."""

        if operation_variant != "plain":
            raise ValueError("semantic NoteService supports only the plain note variant")
        created = await self._backend.invoke(
            NOTE_CREATE_DEF,
            NoteCreateInput(notebook_id, title, content),
            deadline=None,
        )
        note_id = created.note.id
        update_task = asyncio.create_task(
            self._backend.invoke(
                NOTE_UPDATE_DEF,
                NoteUpdateInput(notebook_id, note_id, content, title),
                deadline=None,
            )
        )
        try:
            await asyncio.shield(update_task)
        except asyncio.CancelledError:

            async def _finalize_then_cleanup() -> None:
                try:
                    try:
                        await update_task
                    except Exception:  # noqa: BLE001 - cleanup must still run
                        logger.debug(
                            "Shielded semantic note update failed before cleanup for %s in %s",
                            note_id,
                            notebook_id,
                            exc_info=True,
                        )
                finally:
                    try:
                        await self._backend.invoke(
                            NOTE_DELETE_DEF,
                            NoteDeleteInput(notebook_id, note_id),
                            deadline=None,
                        )
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        logger.warning(
                            "Best-effort semantic note cleanup failed for %s in %s",
                            note_id,
                            notebook_id,
                            exc_info=True,
                        )

            cleanup_task = asyncio.create_task(_finalize_then_cleanup())
            _cleanup_tasks.add(cleanup_task)
            cleanup_task.add_done_callback(_cleanup_tasks.discard)
            raise
        return project_note(created.note)

    async def update_note(
        self,
        notebook_id: str,
        note_id: str,
        content: str,
        title: str,
    ) -> None:
        """Update an existing note after the facade's existence preflight."""

        await self._backend.invoke(
            NOTE_UPDATE_DEF,
            NoteUpdateInput(notebook_id, note_id, content, title),
            deadline=None,
        )

    async def delete_note(self, notebook_id: str, note_id: str) -> None:
        """Soft-delete one note idempotently."""

        await self._backend.invoke(
            NOTE_DELETE_DEF,
            NoteDeleteInput(notebook_id, note_id),
            deadline=None,
        )

    async def list_mind_maps(self, notebook_id: str) -> list[MindMap]:
        """Return active note-backed mind maps without exposing note rows."""

        return [
            project_mind_map(record) for record in await self._list_mind_map_records(notebook_id)
        ]

    async def _list_mind_map_records(self, notebook_id: str) -> tuple[MindMapRecord, ...]:
        """Keep exact persisted JSON available for title-only updates."""

        result = await self._backend.invoke(
            MIND_MAP_LIST_DEF,
            MindMapListInput(notebook_id),
            deadline=None,
        )
        return result.mind_maps

    async def get_mind_map_or_none(self, notebook_id: str, mind_map_id: str) -> MindMap | None:
        """Select one exact note-backed identity from the semantic listing."""

        return next(
            (item for item in await self.list_mind_maps(notebook_id) if item.id == mind_map_id),
            None,
        )

    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: list[str] | None,
        language: str | None,
        instructions: str | None,
    ) -> MindMap:
        """Generate JSON through MIND_MAP_* and persist it through semantic NOTE_* ops."""

        generated = await self._backend.invoke(
            MIND_MAP_GENERATE_NOTE_DEF,
            MindMapGenerateNoteInput(
                notebook_id,
                None if source_ids is None else tuple(source_ids),
                language,
                instructions,
            ),
            deadline=None,
        )
        tree_json = generated.tree_json
        if tree_json is None:
            return project_mind_map(MindMapRecord("", notebook_id, "Mind Map", "note_backed"))
        title = "Mind Map"
        try:
            tree = json.loads(tree_json)
        except (json.JSONDecodeError, TypeError):
            tree = None
        if isinstance(tree, dict):
            candidate = tree.get("name")
            if isinstance(candidate, str) and candidate:
                title = candidate
        note = await self.create_note(notebook_id, title=title, content=tree_json)
        return project_mind_map(
            MindMapRecord(
                note.id,
                notebook_id,
                title,
                "note_backed",
                note.created_at,
                tree_json,
            )
        )

    async def rename_mind_map(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
    ) -> None:
        """Retitle a note-backed mind map after an exact semantic preflight."""

        existing = next(
            (
                item
                for item in await self._list_mind_map_records(notebook_id)
                if item.id == mind_map_id
            ),
            None,
        )
        if existing is None:
            raise MindMapNotFoundError(mind_map_id)
        await self.update_note(
            notebook_id,
            mind_map_id,
            existing.tree_json or "",
            new_title,
        )

    async def delete_mind_map(self, notebook_id: str, mind_map_id: str) -> None:
        """Delete a note-backed mind map idempotently through NOTE_DELETE."""

        await self.delete_note(notebook_id, mind_map_id)
