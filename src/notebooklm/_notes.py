"""Backend-neutral notes namespace contract."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from typing import Any

from .types import Note


class NotesAPI(ABC):
    """Operations on NotebookLM notes."""

    @abstractmethod
    async def list(self, notebook_id: str) -> list[Note]:
        """List all text notes in a notebook."""

    @abstractmethod
    async def get(self, notebook_id: str, note_id: str) -> Note:
        """Get a note by ID, raising when it does not exist."""

    @abstractmethod
    async def get_or_none(self, notebook_id: str, note_id: str) -> Note | None:
        """Get a note by ID, returning ``None`` when it does not exist."""

    @abstractmethod
    async def create(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
    ) -> Note:
        """Create and return a note."""

    @abstractmethod
    async def update(
        self,
        notebook_id: str,
        note_id: str,
        content: str,
        title: str,
    ) -> None:
        """Update an existing note's content and title."""

    @abstractmethod
    async def delete(self, notebook_id: str, note_id: str) -> None:
        """Idempotently delete a note."""

    @abstractmethod
    async def list_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """Return raw note-backed mind-map rows."""

    @abstractmethod
    async def delete_mind_map(self, notebook_id: str, mind_map_id: str) -> None:
        """Idempotently delete a note-backed mind map."""


__all__ = ["NotesAPI"]
