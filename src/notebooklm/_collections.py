"""Backend-neutral collections namespace contract."""

from __future__ import annotations

import builtins
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Literal

from ._runtime.call_supervisor import OperationLease
from .exceptions import CollectionNotFoundError, DecodingError
from .types import Collection, Notebook

# Narrow capability: just ``notebooks.list() -> list[Notebook]`` (account-level,
# no notebook-id argument — unlike labels' source list).
ListNotebooks = Callable[[], Awaitable[builtins.list[Notebook]]]
logger = logging.getLogger(__name__)


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

    _list_method_id = ""
    _mutation_method_id = ""
    _property_readback_miss_method_id: str
    _delete_method_id = ""
    _verify_writes = False
    _filter_existing_on_delete = False
    _dedupe_deletes = False

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""

        return contextlib.nullcontext(None)

    def __init__(self, *, list_notebooks: ListNotebooks) -> None:
        self._list_notebooks = list_notebooks

    @abstractmethod
    async def list(self) -> builtins.list[Collection]:
        """List all collections in the account."""

    async def _list_in_scope(self) -> builtins.list[Collection]:
        """Read collections inside an already-admitted workflow scope."""
        return await self.list()

    async def get_or_none(self, collection_id: str) -> Collection | None:
        """Get a collection by id, returning ``None`` when absent."""
        async with self._operation_scope("collections.get_or_none"):
            return next(
                (
                    collection
                    for collection in await self._list_in_scope()
                    if collection.id == collection_id
                ),
                None,
            )

    async def get(self, collection_id: str) -> Collection:
        """Get a collection by id; raise ``CollectionNotFoundError`` on a miss."""
        async with self._operation_scope("collections.get"):
            collection = next(
                (
                    collection
                    for collection in await self._list_in_scope()
                    if collection.id == collection_id
                ),
                None,
            )
            if collection is None:
                raise CollectionNotFoundError(collection_id, method_id=self._list_method_id)
            return collection

    async def notebooks(self, collection_id: str) -> builtins.list[Notebook]:
        """Expand a collection to its member ``Notebook`` objects."""
        async with self._operation_scope("collections.notebooks"):
            collection = next(
                (
                    collection
                    for collection in await self._list_in_scope()
                    if collection.id == collection_id
                ),
                None,
            )
            if collection is None:
                raise CollectionNotFoundError(collection_id, method_id=self._list_method_id)
            by_id = {notebook.id: notebook for notebook in await self._list_notebooks()}
            return [
                by_id[notebook_id]
                for notebook_id in collection.notebook_ids
                if notebook_id in by_id
            ]

    @abstractmethod
    async def create(self, name: str) -> Collection:
        """Create an empty, named collection."""

    async def rename(
        self, collection_id: str, name: str, *, return_object: bool = True
    ) -> Collection | None:
        """Rename a collection while preserving its existing emoji."""
        async with self._operation_scope("collections.rename"):
            current = next(
                (
                    collection
                    for collection in await self._list_in_scope()
                    if collection.id == collection_id
                ),
                None,
            )
            if current is None:
                raise CollectionNotFoundError(collection_id, method_id=self._mutation_method_id)
            requested_emoji = current.emoji or ""
            await self._send_update(
                "properties",
                [collection_id],
                name=name,
                current=current,
            )
            if not return_object and not self._verify_writes:
                return None
            read_back = next(
                (
                    collection
                    for collection in await self._list_in_scope()
                    if collection.id == collection_id
                ),
                None,
            )
            if read_back is None:
                raise CollectionNotFoundError(
                    collection_id,
                    method_id=self._property_readback_miss_method_id,
                )
            if self._verify_writes and (
                read_back.name != name or (read_back.emoji or "") != requested_emoji
            ):
                raise DecodingError(
                    "Android collection rename did not read back the requested properties",
                    method_id=self._mutation_method_id,
                )
            return read_back if return_object else None

    @abstractmethod
    async def _send_update(
        self,
        operation: Literal["properties", "delete"],
        collection_ids: builtins.list[str],
        *,
        name: str | None = None,
        current: Collection | None = None,
    ) -> None:
        """Send one collection property update or delete operation."""

    async def add_notebooks(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Collection | None:
        """Add notebooks to a collection."""
        return await self._mutate_members(
            collection_id,
            notebook_ids,
            operation="add_notebooks",
            return_object=return_object,
        )

    async def remove_notebooks(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Collection | None:
        """Remove notebooks from a collection without deleting them."""
        return await self._mutate_members(
            collection_id,
            notebook_ids,
            operation="remove_notebooks",
            return_object=return_object,
        )

    async def _mutate_members(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        operation: Literal["add_notebooks", "remove_notebooks"],
        return_object: bool,
    ) -> Collection | None:
        if not notebook_ids:
            raise ValueError(f"{operation} requires at least one notebook id")
        unique_ids = list(dict.fromkeys(notebook_ids))
        logger.debug(
            "%s %d notebook(s) %s collection %s",
            "Adding" if operation == "add_notebooks" else "Removing",
            len(unique_ids),
            "to" if operation == "add_notebooks" else "from",
            collection_id,
        )
        async with self._operation_scope(f"collections.{operation}"):
            for notebook_id in unique_ids:
                await self._send_mutate_member(
                    collection_id,
                    notebook_id,
                    operation=operation,
                )
            read_back = next(
                (
                    collection
                    for collection in await self._list_in_scope()
                    if collection.id == collection_id
                ),
                None,
            )
            if read_back is None:
                raise CollectionNotFoundError(collection_id, method_id=self._mutation_method_id)
            if self._verify_writes:
                present = set(read_back.notebook_ids)
                verified = (
                    set(unique_ids) <= present
                    if operation == "add_notebooks"
                    else set(unique_ids).isdisjoint(present)
                )
                if not verified:
                    raise DecodingError(
                        "Android collection membership mutation did not read back the requested "
                        "state",
                        method_id=self._mutation_method_id,
                    )
            return read_back if return_object else None

    @abstractmethod
    async def _send_mutate_member(
        self,
        collection_id: str,
        notebook_id: str,
        *,
        operation: Literal["add_notebooks", "remove_notebooks"],
    ) -> None:
        """Send one collection membership mutation."""

    async def delete(self, collection_ids: str | builtins.list[str]) -> None:
        """Delete one or more collections without deleting member notebooks."""
        requested = [collection_ids] if isinstance(collection_ids, str) else list(collection_ids)
        if self._dedupe_deletes:
            requested = list(dict.fromkeys(requested))
        if not requested:
            return
        async with self._operation_scope("collections.delete"):
            existing = requested
            if self._filter_existing_on_delete:
                current_ids = {collection.id for collection in await self._list_in_scope()}
                existing = [
                    collection_id for collection_id in requested if collection_id in current_ids
                ]
                if not existing:
                    return
            await self._send_update("delete", existing)
            if self._verify_writes:
                remaining = {collection.id for collection in await self._list_in_scope()}
                if set(existing) & remaining:
                    raise DecodingError(
                        "Android collection delete did not read back absence",
                        method_id=self._delete_method_id,
                    )


__all__ = ["CollectionsAPI", "ListNotebooks"]
