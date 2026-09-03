"""Android composition for the unified mind-map namespace.

The mind-map adapter adds no Android wire declarations. Interactive mutations compose the
``ArtifactsAPI`` collaborator supplied by the artifact namespace; note-backed reads and mutations
compose through a typed note-backed projection and note CRUD seams. Interactive
tree reads and generation use the live-proven artifact representation.
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol, cast

from .._mind_maps_api import MindMapsAPI
from .._runtime.call_supervisor import OperationLease
from ..exceptions import MindMapNotFoundError, NoteNotFoundError
from ..types import Artifact, GenerationStatus, MindMap
from .epoch import bind_workflow_epoch, reset_workflow_epoch
from .session import AndroidSession

if TYPE_CHECKING:
    from .._artifacts import ArtifactsAPI
    from .._notes import NotesAPI


class _NoteBackedMindMapReader(Protocol):
    """The private typed Notes projection consumed by Android mind maps."""

    async def _list_note_backed_mind_maps(
        self,
        notebook_id: str,
    ) -> builtins.list[MindMap]: ...


class _SelectedNoteBackedMindMapReader(Protocol):
    async def list_note_backed(self, notebook_id: str) -> builtins.list[MindMap]: ...


class _AndroidMindMapArtifacts(Protocol):
    """Native artifact hooks required by the Android composition root."""

    async def _list_all_studio(self, notebook_id: str) -> builtins.list[Artifact]: ...

    async def _generate_interactive_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None,
        *,
        language: str | None,
        instructions: str | None,
    ) -> GenerationStatus: ...

    async def _get_interactive_mind_map_tree(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> dict[str, Any] | None: ...


class AndroidMindMapsAPI(MindMapsAPI):
    """Android mind-map adapter composed from base-typed artifact/note APIs."""

    _reject_unsuccessful_interactive_wait = True

    @asynccontextmanager
    async def _operation_scope(self, label: str) -> AsyncIterator[OperationLease]:
        async with self._transport.operation_scope(label) as lease:
            token = bind_workflow_epoch(self._transport, lease.epoch)
            try:
                yield lease
            finally:
                reset_workflow_epoch(token)

    def __init__(
        self,
        *,
        session: AndroidSession,
        artifacts: ArtifactsAPI,
        notes: NotesAPI,
        note_backed_reader: _NoteBackedMindMapReader
        | _SelectedNoteBackedMindMapReader
        | None = None,
    ) -> None:
        """Retain the exact artifact/Notes collaborators without selecting a frontend."""
        super().__init__(artifacts=artifacts, notes=notes)
        self._transport = session
        self._android_artifacts = cast(_AndroidMindMapArtifacts, artifacts)
        reader_owner = notes if note_backed_reader is None else note_backed_reader
        reader = getattr(reader_owner, "_list_note_backed_mind_maps", None)
        if reader is None:
            reader = getattr(reader_owner, "list_note_backed", None)
        if reader is None or not callable(reader):
            raise TypeError("notes must provide the private typed note-backed mind-map read seam")
        self._list_note_backed = cast(
            Callable[[str], Awaitable[builtins.list[MindMap]]],
            reader,
        )

    async def list_note_backed(self, notebook_id: str) -> builtins.list[MindMap]:
        """Return Notes' exact-kind typed projection without reading raw Web rows."""
        return await self._list_note_backed(notebook_id)

    async def _list_studio_mind_map_rows(self, notebook_id: str) -> builtins.list[Artifact]:
        return await self._android_artifacts._list_all_studio(notebook_id)

    async def _send_rename_note_backed(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
    ) -> None:
        """Retitle a classified map while preserving its exact persisted content."""
        if not any(
            mind_map.id == mind_map_id for mind_map in await self.list_note_backed(notebook_id)
        ):
            raise MindMapNotFoundError(mind_map_id)
        try:
            note = await self._notes.get(notebook_id, mind_map_id)
            await self._notes.update(notebook_id, mind_map_id, note.content, new_title)
        except NoteNotFoundError as exc:
            raise MindMapNotFoundError(mind_map_id) from exc

    async def _start_interactive_mind_map(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None,
        *,
        language: str | None,
        instructions: str | None,
    ) -> str:
        status = await self._android_artifacts._generate_interactive_mind_map(
            notebook_id,
            source_ids,
            language=language,
            instructions=instructions,
        )
        return status.task_id

    async def _read_interactive_tree(
        self,
        notebook_id: str,
        mind_map_id: str,
    ) -> dict[str, Any] | None:
        return await self._android_artifacts._get_interactive_mind_map_tree(
            notebook_id,
            mind_map_id,
        )


__all__ = ["AndroidMindMapsAPI"]
