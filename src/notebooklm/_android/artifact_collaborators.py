"""Narrow collaborator protocols used by the Android artifact adapter."""

from __future__ import annotations

import builtins
from typing import Protocol

from .._types.research import MindMapResult
from ..types import Artifact, MindMap


class NoteBackedMindMapLister(Protocol):
    async def list_mind_map_artifacts(self, notebook_id: str) -> builtins.list[Artifact]: ...

    async def list_note_backed_mind_maps(self, notebook_id: str) -> builtins.list[MindMap]: ...


class NoteBackedMindMapGenerator(Protocol):
    """Web-only ActOnSources plus CreateNote compatibility seam."""

    async def __call__(
        self,
        notebook_id: str,
        source_ids: builtins.list[str] | None = None,
        language: str | None = "en",
        instructions: str | None = None,
    ) -> MindMapResult: ...


__all__ = ["NoteBackedMindMapGenerator", "NoteBackedMindMapLister"]
