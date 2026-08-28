"""Backend-neutral collections namespace contract."""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from .types import Collection, Notebook

# Narrow capability: just ``notebooks.list() -> list[Notebook]`` (account-level,
# no notebook-id argument — unlike labels' source list).
ListNotebooks = Callable[[], Awaitable[builtins.list[Notebook]]]


class CollectionsAPI(ABC):
    """Operations on NotebookLM collections (``client.collections``).

    Usage::

        async with NotebookLMClient.from_storage() as client:
            coll = await client.collections.create("Research Q3")
            await client.collections.add_notebooks(coll.id, [nb_id])
            members = await client.collections.notebooks(coll.id)
            await client.collections.rename(coll.id, "Research Q4")
            await client.collections.delete(coll.id)
    """

    @abstractmethod
    async def list(self) -> builtins.list[Collection]:
        """List all collections in the account."""

    @abstractmethod
    async def get_or_none(self, collection_id: str) -> Collection | None:
        """Get a collection by id, returning ``None`` when absent."""

    @abstractmethod
    async def get(self, collection_id: str) -> Collection:
        """Get a collection by id; raise ``CollectionNotFoundError`` on a miss."""

    @abstractmethod
    async def notebooks(self, collection_id: str) -> builtins.list[Notebook]:
        """Expand a collection to its member ``Notebook`` objects."""

    @abstractmethod
    async def create(self, name: str) -> Collection:
        """Create an empty, named collection."""

    @abstractmethod
    async def rename(
        self, collection_id: str, name: str, *, return_object: bool = True
    ) -> Collection | None:
        """Rename a collection while preserving its existing emoji."""

    @abstractmethod
    async def add_notebooks(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Collection | None:
        """Add notebooks to a collection."""

    @abstractmethod
    async def remove_notebooks(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Collection | None:
        """Remove notebooks from a collection without deleting them."""

    @abstractmethod
    async def delete(self, collection_ids: str | builtins.list[str]) -> None:
        """Delete one or more collections without deleting member notebooks."""


__all__ = ["CollectionsAPI", "ListNotebooks"]
