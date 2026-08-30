"""Android composition for the unified mind-map namespace.

The mind-map adapter adds no Android wire declarations. Interactive mutations compose the
``ArtifactsAPI`` collaborator supplied by the artifact namespace; note-backed reads and mutations
compose through a typed note-backed projection and note CRUD seams. Interactive
tree reads and generation use the live-proven artifact representation.
"""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, cast

from .._mind_maps_api import MindMapsAPI
from .._runtime.call_supervisor import CallSupervisor
from ..exceptions import ArtifactNotReadyError, MindMapNotFoundError, NoteNotFoundError
from ..types import ArtifactType, MindMap, MindMapKind

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


class AndroidMindMapsAPI(MindMapsAPI):
    """Android mind-map adapter composed from base-typed artifact/note APIs."""

    def __init__(
        self,
        *,
        supervisor: CallSupervisor,
        artifacts: ArtifactsAPI,
        notes: NotesAPI,
        note_backed_reader: _NoteBackedMindMapReader
        | _SelectedNoteBackedMindMapReader
        | None = None,
    ) -> None:
        """Retain the exact artifact/Notes collaborators without selecting a frontend."""
        super().__init__(artifacts=artifacts, notes=notes)
        self._supervisor = supervisor
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

    async def list(self, notebook_id: str) -> builtins.list[MindMap]:
        """List each backing once within one supervisor generation."""
        async with self._supervisor.operation_scope("mind_maps.list"):
            result = list(await self.list_note_backed(notebook_id))
            list_studio = getattr(self._artifacts, "_list_all_studio", None)
            artifacts = (
                await list_studio(notebook_id)
                if list_studio is not None and callable(list_studio)
                else await self._artifacts.list(notebook_id, ArtifactType.MIND_MAP)
            )
            for artifact in artifacts:
                if artifact.is_interactive_mind_map:
                    result.append(
                        MindMap(
                            id=artifact.id,
                            notebook_id=notebook_id,
                            title=artifact.title,
                            kind=MindMapKind.INTERACTIVE,
                            created_at=artifact.created_at,
                        )
                    )
            return result

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
        """Generate either backing through its selected narrow collaborator."""
        async with self._supervisor.operation_scope("mind_maps.generate"):
            if kind is MindMapKind.NOTE_BACKED:
                result = await self._artifacts.generate_mind_map(
                    notebook_id,
                    source_ids,
                    language,
                    instructions,
                )
                tree = result.mind_map if isinstance(result.mind_map, dict) else None
                title = "Mind Map"
                if tree is not None:
                    name = tree.get("name")
                    if isinstance(name, str) and name:
                        title = name
                return MindMap(
                    id=result.note_id or "",
                    notebook_id=notebook_id,
                    title=title,
                    kind=MindMapKind.NOTE_BACKED,
                    created_at=result.created_at,
                    tree=tree,
                )

            generate = getattr(self._artifacts, "_generate_interactive_mind_map", None)
            if generate is None or not callable(generate):
                raise TypeError("artifacts must provide the Android interactive mind-map seam")
            status = await generate(
                notebook_id,
                source_ids,
                language=language,
                instructions=instructions,
            )
            if wait:
                terminal = await self._artifacts.wait_for_completion(notebook_id, status.task_id)
                if terminal.is_failed or terminal.is_removed:
                    raise ArtifactNotReadyError(
                        "mind_map",
                        artifact_id=status.task_id,
                        status=str(terminal.status),
                    )
            artifact = await self._find_interactive(
                notebook_id,
                status.task_id,
                allow_unclassified=True,
            )
            tree = (
                await self.get_tree(
                    notebook_id,
                    status.task_id,
                    kind=MindMapKind.INTERACTIVE,
                )
                if wait
                else None
            )
            return MindMap(
                id=status.task_id,
                notebook_id=notebook_id,
                title=artifact.title if artifact is not None else "Mind Map",
                kind=MindMapKind.INTERACTIVE,
                created_at=artifact.created_at if artifact is not None else None,
                tree=tree,
            )

    async def delete(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        kind: MindMapKind | None = None,
    ) -> None:
        """Compose kind detection and deletion within one resource generation."""
        async with self._supervisor.operation_scope("mind_maps.delete"):
            await super().delete(notebook_id, mind_map_id, kind=kind)

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
        """Return persisted note trees or the live interactive JSON payload."""
        async with self._supervisor.operation_scope("mind_maps.get_tree"):
            read_interactive = getattr(self._artifacts, "_get_interactive_mind_map_tree", None)
            if read_interactive is None or not callable(read_interactive):
                raise TypeError("artifacts must provide the Android interactive tree seam")
            if kind is MindMapKind.INTERACTIVE:
                return await read_interactive(notebook_id, mind_map_id)

            for mind_map in await self.list_note_backed(notebook_id):
                if mind_map.id == mind_map_id:
                    return mind_map.tree
            if kind is MindMapKind.NOTE_BACKED:
                return None

            if await self._find_interactive(notebook_id, mind_map_id) is not None:
                return await read_interactive(notebook_id, mind_map_id)
            return None


__all__ = ["AndroidMindMapsAPI"]
