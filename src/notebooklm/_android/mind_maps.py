"""Android composition for the unified mind-map namespace.

B7 adds no Android wire declarations. Interactive mutations compose the
``ArtifactsAPI`` collaborator supplied by B4; note-backed reads and mutations
compose through B6's typed projection and exact note CRUD seams. Interactive
tree reads and generation remain evidence-gated.
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any, NoReturn, Protocol, cast

from .._mind_maps_api import MindMapsAPI
from .._runtime.call_supervisor import CallSupervisor
from ..exceptions import MindMapNotFoundError, NoteNotFoundError
from ..types import MindMap, MindMapKind
from .errors import unsupported_operation

if TYPE_CHECKING:
    from .._artifacts import ArtifactsAPI
    from .._notes import NotesAPI


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


class _NoteBackedMindMapReader(Protocol):
    """The private typed B6 projection consumed by Android B7."""

    async def _list_note_backed_mind_maps(
        self,
        notebook_id: str,
    ) -> builtins.list[MindMap]: ...


class AndroidMindMapsAPI(MindMapsAPI):
    """Android mind-map adapter composed from base-typed artifact/note APIs."""

    def __init__(
        self,
        *,
        supervisor: CallSupervisor,
        artifacts: ArtifactsAPI,
        notes: NotesAPI,
    ) -> None:
        """Retain the exact B4/B6 collaborators without selecting a frontend."""
        super().__init__(artifacts=artifacts, notes=notes)
        self._supervisor = supervisor
        reader = getattr(notes, "_list_note_backed_mind_maps", None)
        if reader is None or not callable(reader):
            raise TypeError("notes must provide the private typed note-backed mind-map read seam")
        self._note_backed_reader = cast(_NoteBackedMindMapReader, notes)

    async def list_note_backed(self, notebook_id: str) -> builtins.list[MindMap]:
        """Return B6's exact-kind typed projection without reading raw Web rows."""
        return await self._note_backed_reader._list_note_backed_mind_maps(notebook_id)

    async def list(self, notebook_id: str) -> builtins.list[MindMap]:
        """List both backings within one supervisor generation."""
        async with self._supervisor.operation_scope("mind_maps.list"):
            return await super().list(notebook_id)

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

    async def rename(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
        *,
        kind: MindMapKind | None = None,
        return_object: bool = True,
    ) -> MindMap | None:
        """Compose kind detection, exact mutation, and optional hydration."""
        async with self._supervisor.operation_scope("mind_maps.rename"):
            return await super().rename(
                notebook_id,
                mind_map_id,
                new_title,
                kind=kind,
                return_object=return_object,
            )

    async def generate(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        *,
        kind: MindMapKind,
        language: str | None = "en",
        instructions: str | None = None,
        wait: bool = True,
    ) -> MindMap:
        """Reject generation until a valid-resource ActOnSources capture exists."""
        _reject("mind_maps.generate")

    async def _detect_kind(self, notebook_id: str, mind_map_id: str) -> MindMapKind:
        """Resolve note-backed first, then interactive, using existing read seams."""
        return await super()._detect_kind(notebook_id, mind_map_id)

    async def get_tree(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        kind: MindMapKind | None = None,
    ) -> dict[str, Any] | None:
        """Return persisted note trees while keeping interactive payloads gated."""
        if kind is MindMapKind.INTERACTIVE:
            _reject("mind_maps.get_tree")

        for mind_map in await self.list_note_backed(notebook_id):
            if mind_map.id == mind_map_id:
                return mind_map.tree
        if kind is MindMapKind.NOTE_BACKED:
            return None

        if await self._find_interactive(notebook_id, mind_map_id) is not None:
            _reject("mind_maps.get_tree")
        return None


__all__ = ["AndroidMindMapsAPI"]
