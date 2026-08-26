"""Semantic plain-note service.

``NoteService`` owns the migrated NOTE_* workflow used by ``NotesAPI`` and the
note-backed side of ``MindMapsAPI``. It is backend-neutral: it reaches the wire
only through ``BackendAdapter``, and it returns the neutral ``NoteRecord`` /
``MindMapRecord`` values the backend produced rather than public models —
projection belongs to the two facades above it (P10 invariant I1).

The raw compatibility listings ``NotesAPI`` still publishes come back as
undecoded wire rows through the ``MIND_MAP_LIST`` raw-payload flag, so the
public ``list[Any]`` contract survives without a wire import here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ._backend import BackendAdapter
from ._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from ._env import get_default_language
from ._read_services import NotebookReadService
from ._semantic.records import (
    MIND_MAP_GENERATE_NOTE_DEF,
    MIND_MAP_LIST_DEF,
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    NOTE_UPDATE_DEF,
    RAW_ALL_NOTE_ROWS,
    RAW_MIND_MAP_ROWS,
    MindMapGenerateNoteInput,
    MindMapListInput,
    MindMapRecord,
    NoteCreateInput,
    NoteDeleteInput,
    NoteGetInput,
    NoteListInput,
    NoteRecord,
    NoteUpdateInput,
    SourceIdDiagnostics,
)
from .exceptions import MindMapNotFoundError

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

    __slots__ = ("_backend", "_deadline_factory", "_notebooks")

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        # Note-backed mind-map generation defaults its source scope here, above
        # the port, and shares one captured budget with the generation native
        # the way the row used to (P10 R5.1b).
        self._deadline_factory = deadline_factory
        self._notebooks = NotebookReadService(backend)

    async def list_notes(self, notebook_id: str) -> list[NoteRecord]:
        """Return active non-mind-map notes in backend order."""

        result = await self._backend.invoke(
            NOTE_LIST_DEF,
            NoteListInput(notebook_id),
            deadline=None,
        )
        return list(result.notes)

    async def get_note_or_none(self, notebook_id: str, note_id: str) -> NoteRecord | None:
        """Select the first exact note identity, or return a genuine miss."""

        result = await self._backend.invoke(
            NOTE_GET_DEF,
            NoteGetInput(notebook_id, note_id),
            deadline=None,
        )
        return result.note

    async def create_note_record(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
        *,
        operation_variant: str = "plain",
        deadline: RuntimeDeadline | None = None,
    ) -> NoteRecord:
        """Create and finalize a note, returning the neutral allocation record.

        The one cancellation-safe create in the package: services that persist
        generated content sequence this rather than repeating the shield.
        """

        if operation_variant != "plain":
            raise ValueError("semantic NoteService supports only the plain note variant")
        created = await self._backend.invoke(
            NOTE_CREATE_DEF,
            NoteCreateInput(notebook_id, title, content),
            deadline=deadline,
        )
        note_id = created.note.id
        update_task = asyncio.create_task(
            self._backend.invoke(
                NOTE_UPDATE_DEF,
                NoteUpdateInput(notebook_id, note_id, content, title),
                deadline=deadline,
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
                            deadline=deadline,
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
        return created.note

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

    async def list_mind_maps(self, notebook_id: str) -> list[MindMapRecord]:
        """Return active note-backed mind maps without exposing note rows.

        The records keep the exact persisted JSON, which is what
        :meth:`rename_mind_map` re-sends on a title-only update.
        """

        result = await self._backend.invoke(
            MIND_MAP_LIST_DEF,
            MindMapListInput(notebook_id),
            deadline=None,
        )
        return list(result.mind_maps)

    async def list_mind_map_rows(self, notebook_id: str) -> list[object]:
        """Return the active note-backed mind-map rows exactly as the wire sent them.

        The raw compatibility listing ``NotesAPI.list_mind_maps`` publishes: the
        same rows, in the same order, with the same fields the deferred raw
        note-row service returned.
        """

        return await self._list_raw_rows(notebook_id, RAW_MIND_MAP_ROWS)

    async def list_note_rows(self, notebook_id: str) -> list[object]:
        """Return the whole raw note+mind-map row collection, deletions included."""

        return await self._list_raw_rows(notebook_id, RAW_ALL_NOTE_ROWS)

    async def _list_raw_rows(self, notebook_id: str, scope: str) -> list[object]:
        result = await self._backend.invoke(
            MIND_MAP_LIST_DEF,
            MindMapListInput(notebook_id, raw_rows=scope),
            deadline=None,
        )
        return list(result.rows)

    async def get_mind_map_or_none(
        self, notebook_id: str, mind_map_id: str
    ) -> MindMapRecord | None:
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
    ) -> MindMapRecord:
        """Generate JSON through MIND_MAP_* and persist it through semantic NOTE_* ops.

        ``source_ids=None`` is this service's documented default for "every
        source in the notebook" and ``language=None`` for the environment
        default; both are resolved here because the operation takes a
        pre-resolved input record (P10 R5.1b, ADR-0035 addendum D1(a)).  The
        read decodes with :attr:`SourceIdDiagnostics.SILENT`, which is what this
        family has always reported about a snapshot it cannot read: nothing.
        The budget is captured before the read, so the read and the generation
        native spend one client timeout — the aggregate the row used to get from
        the backend's deadline ledger.  Note persistence stays outside it, as
        before.
        """

        deadline = None if self._deadline_factory is None else self._deadline_factory.start()
        generated = await self._backend.invoke(
            MIND_MAP_GENERATE_NOTE_DEF,
            MindMapGenerateNoteInput(
                notebook_id,
                await self._resolve_scope(notebook_id, source_ids, deadline=deadline),
                get_default_language() if language is None else language,
                instructions,
            ),
            deadline=deadline,
        )
        tree_json = generated.tree_json
        if tree_json is None:
            return MindMapRecord("", notebook_id, "Mind Map", "note_backed")
        title = "Mind Map"
        try:
            tree = json.loads(tree_json)
        except (json.JSONDecodeError, TypeError):
            tree = None
        if isinstance(tree, dict):
            candidate = tree.get("name")
            if isinstance(candidate, str) and candidate:
                title = candidate
        note = await self.create_note_record(notebook_id, title=title, content=tree_json)
        return MindMapRecord(
            note.id,
            notebook_id,
            title,
            "note_backed",
            note.created_at,
            tree_json,
        )

    async def _resolve_scope(
        self,
        notebook_id: str,
        source_ids: list[str] | None,
        *,
        deadline: RuntimeDeadline | None,
    ) -> tuple[str, ...]:
        """Expand an omitted scope into the notebook's full embedded source set."""

        if source_ids is not None:
            return tuple(source_ids)
        return tuple(
            await self._notebooks.get_source_ids(
                notebook_id,
                diagnostics=SourceIdDiagnostics.SILENT,
                deadline=deadline,
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
            (item for item in await self.list_mind_maps(notebook_id) if item.id == mind_map_id),
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
