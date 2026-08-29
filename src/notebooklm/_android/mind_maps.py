"""Android composition for the unified mind-map namespace.

B7 adds no Android wire declarations. Interactive mutations compose the
``ArtifactsAPI`` collaborator supplied by B4; note-backed operations remain
evidence-gated pending a decoded note-kind boundary from B6.
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any, NoReturn

from .._mind_maps_api import MindMapsAPI
from ..types import MindMap, MindMapKind
from .errors import unsupported_operation

if TYPE_CHECKING:
    from .._artifacts import ArtifactsAPI
    from .._notes import NotesAPI


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


class AndroidMindMapsAPI(MindMapsAPI):
    """Android mind-map adapter composed from base-typed artifact/note APIs."""

    def __init__(self, *, artifacts: ArtifactsAPI, notes: NotesAPI) -> None:
        """Retain the exact B4/B6 collaborators without selecting a frontend."""
        super().__init__(artifacts=artifacts, notes=notes)

    async def list_note_backed(self, notebook_id: str) -> builtins.list[MindMap]:
        """Reject until B6 exposes decoded, kind-qualified note-backed maps."""
        _reject("mind_maps.list_note_backed")

    async def _send_rename_note_backed(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
    ) -> None:
        """Reject until exact persisted note content can be preserved."""
        _reject("mind_maps.rename_note_backed")

    async def rename(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
        *,
        kind: MindMapKind | None = None,
        return_object: bool = True,
    ) -> MindMap | None:
        """Compose the explicit interactive no-hydration branch only.

        Auto-detection and hydration both require the evidence-gated aggregate
        note-backed read. Reject those branches before an artifact mutation.
        """
        if kind is not MindMapKind.INTERACTIVE or return_object:
            _reject("mind_maps.rename")
        return await super().rename(
            notebook_id,
            mind_map_id,
            new_title,
            kind=kind,
            return_object=False,
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

    async def get_tree(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        kind: MindMapKind | None = None,
    ) -> dict[str, Any] | None:
        """Reject tree reads until an exact interactive-tree fixture exists."""
        _reject("mind_maps.get_tree")


__all__ = ["AndroidMindMapsAPI"]
