"""Android composition for the unified mind-map namespace.

B7 adds no Android wire declarations.  The supported workflows compose the
decoded ``ArtifactsAPI`` and ``NotesAPI`` collaborators supplied by B4/B6;
generation and interactive-tree reads remain evidence-gated.
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any, NoReturn

from .._mind_maps_api import MindMapsAPI
from ..exceptions import DecodingError
from ..types import MindMap, MindMapKind
from .errors import unsupported_operation

if TYPE_CHECKING:
    from .._artifacts import ArtifactsAPI
    from .._notes import NotesAPI


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


class AndroidMindMapsAPI(MindMapsAPI):
    """Android mind-map adapter composed from decoded artifact/note APIs."""

    def __init__(self, *, artifacts: ArtifactsAPI, notes: NotesAPI) -> None:
        """Retain the exact B4/B6 collaborators without selecting a frontend."""
        super().__init__(artifacts=artifacts, notes=notes)

    async def list_note_backed(self, notebook_id: str) -> builtins.list[MindMap]:
        """List only note-kind rows that B6 has already decoded as mind maps.

        This boundary deliberately does not inspect raw protobuf messages,
        infer a kind from JSON content, or consult artifacts.  The notes
        implementation must establish note-kind evidence and return public
        ``MindMap`` values; anything else is decode drift.
        """
        items = await self._notes.list_mind_maps(notebook_id)
        if any(
            not isinstance(item, MindMap) or item.kind is not MindMapKind.NOTE_BACKED
            for item in items
        ):
            raise DecodingError(
                "Android notes did not return decoded note-backed mind maps",
            )
        return list(items)

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
