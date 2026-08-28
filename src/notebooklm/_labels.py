"""Backend-neutral source-label namespace contract."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Literal

from .types import Label, Source

# Narrow capability: just ``sources.list(notebook_id) -> list[Source]``.
ListSources = Callable[[str], Awaitable[builtins.list[Source]]]


class LabelsAPI(ABC):
    """Operations on NotebookLM source labels (``client.labels``).

    Usage::

        async with NotebookLMClient.from_storage() as client:
            labels = await client.labels.generate(nb)
            mine = await client.labels.create(nb, "Papers", "\U0001f4c4")
            await client.labels.add_sources(nb, mine.id, [src_id])
            members = await client.labels.sources(nb, mine.id)
            await client.labels.delete(nb, [mine.id])
    """

    @abstractmethod
    async def list(self, notebook_id: str) -> builtins.list[Label]:
        """List all labels in a notebook, including source membership."""

    @abstractmethod
    async def get_or_none(self, notebook_id: str, label_id: str) -> Label | None:
        """Get a label by id, returning ``None`` when absent."""

    @abstractmethod
    async def get(self, notebook_id: str, label_id: str) -> Label:
        """Get a label by id; raise ``LabelNotFoundError`` on a miss."""

    @abstractmethod
    async def sources(self, notebook_id: str, label_id: str) -> builtins.list[Source]:
        """Expand a label to its member ``Source`` objects."""

    @abstractmethod
    async def generate(
        self, notebook_id: str, *, scope: Literal["all", "unlabeled"] = "unlabeled"
    ) -> builtins.list[Label]:
        """Generate topic labels for all or currently unlabeled sources."""

    @abstractmethod
    async def create(self, notebook_id: str, name: str, emoji: str = "") -> Label:
        """Create an empty, manually named label."""

    @abstractmethod
    async def update(
        self,
        notebook_id: str,
        label_id: str,
        *,
        name: str | None = None,
        emoji: str | None = None,
        return_object: bool = True,
    ) -> Label | None:
        """Set a label's name and/or emoji."""

    @abstractmethod
    async def rename(
        self, notebook_id: str, label_id: str, name: str, *, return_object: bool = True
    ) -> Label | None:
        """Rename a label while preserving its existing emoji."""

    @abstractmethod
    async def set_emoji(
        self, notebook_id: str, label_id: str, emoji: str, *, return_object: bool = True
    ) -> Label | None:
        """Set a label's emoji."""

    @abstractmethod
    async def add_sources(
        self,
        notebook_id: str,
        label_id: str,
        source_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Label | None:
        """Add sources to a label."""

    @abstractmethod
    async def remove_sources(
        self,
        notebook_id: str,
        label_id: str,
        source_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Label | None:
        """Remove sources from a label without deleting them."""

    @abstractmethod
    async def delete(self, notebook_id: str, label_ids: str | builtins.list[str]) -> None:
        """Delete one or more labels without deleting their sources."""


__all__ = ["LabelsAPI", "ListSources"]
