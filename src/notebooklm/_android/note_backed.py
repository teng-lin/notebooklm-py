"""Narrow Web-note projection used by the Android artifact catalog."""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable

from ..types import Artifact, MindMap


class NoteBackedMindMapArtifactAdapter:
    """Project a selected note-backed reader into aggregate artifact rows."""

    def __init__(
        self,
        list_note_backed: Callable[[str], Awaitable[builtins.list[MindMap]]],
    ) -> None:
        self._list_note_backed = list_note_backed

    async def list_note_backed_mind_maps(self, notebook_id: str) -> builtins.list[MindMap]:
        return await self._list_note_backed(notebook_id)

    async def list_mind_map_artifacts(self, notebook_id: str) -> builtins.list[Artifact]:
        return [
            Artifact(
                id=mind_map.id,
                title=mind_map.title,
                _artifact_type=5,
                status=3,
                created_at=mind_map.created_at,
            )
            for mind_map in await self.list_note_backed_mind_maps(notebook_id)
        ]


__all__ = ["NoteBackedMindMapArtifactAdapter"]
