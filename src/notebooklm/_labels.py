"""Backend-neutral source-label namespace contract."""

from __future__ import annotations

import builtins
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Literal

from ._runtime.call_supervisor import OperationLease
from .exceptions import DecodingError, LabelNotFoundError
from .types import Label, Source

# Narrow capability: just ``sources.list(notebook_id) -> list[Source]``.
ListSources = Callable[[str], Awaitable[builtins.list[Source]]]
logger = logging.getLogger(__name__)


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

    def __init__(self, *, list_sources: ListSources) -> None:
        self._list_sources = list_sources

    @abstractmethod
    async def list(self, notebook_id: str) -> builtins.list[Label]:
        """List all labels in a notebook, including source membership."""

    async def _list_in_scope(self, notebook_id: str) -> builtins.list[Label]:
        """Read labels inside an already-admitted workflow scope."""
        return await self.list(notebook_id)

    async def get_or_none(self, notebook_id: str, label_id: str) -> Label | None:
        """Get a label by id, returning ``None`` when absent."""
        async with self._operation_scope("labels.get_or_none"):
            return next(
                (label for label in await self._list_in_scope(notebook_id) if label.id == label_id),
                None,
            )

    async def get(self, notebook_id: str, label_id: str) -> Label:
        """Get a label by id; raise ``LabelNotFoundError`` on a miss."""
        async with self._operation_scope("labels.get"):
            label = next(
                (label for label in await self._list_in_scope(notebook_id) if label.id == label_id),
                None,
            )
            if label is None:
                raise LabelNotFoundError(label_id, method_id=self._list_method_id)
            return label

    async def sources(self, notebook_id: str, label_id: str) -> builtins.list[Source]:
        """Expand a label to its member ``Source`` objects."""
        async with self._operation_scope("labels.sources"):
            label = next(
                (label for label in await self._list_in_scope(notebook_id) if label.id == label_id),
                None,
            )
            if label is None:
                raise LabelNotFoundError(label_id, method_id=self._list_method_id)
            by_id = {source.id: source for source in await self._list_sources(notebook_id)}
            return [by_id[source_id] for source_id in label.source_ids if source_id in by_id]

    @abstractmethod
    async def generate(
        self, notebook_id: str, *, scope: Literal["all", "unlabeled"] = "unlabeled"
    ) -> builtins.list[Label]:
        """Generate topic labels for all or currently unlabeled sources."""

    @abstractmethod
    async def create(self, notebook_id: str, name: str, emoji: str = "") -> Label:
        """Create an empty, manually named label."""

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
        if name is None and emoji is None:
            raise ValueError("update requires name and/or emoji")
        async with self._operation_scope("labels.update"):
            current = next(
                (label for label in await self._list_in_scope(notebook_id) if label.id == label_id),
                None,
            )
            if current is None:
                raise LabelNotFoundError(label_id, method_id=self._mutation_method_id)
            requested_name = current.name if name is None else name
            requested_emoji = (current.emoji or "") if emoji is None else emoji
            await self._send_update(
                "properties",
                notebook_id,
                [label_id],
                name=name,
                emoji=emoji,
                current=current,
            )
            if not return_object and not self._verify_writes:
                return None
            read_back = next(
                (label for label in await self._list_in_scope(notebook_id) if label.id == label_id),
                None,
            )
            if read_back is None:
                raise LabelNotFoundError(
                    label_id,
                    method_id=self._property_readback_miss_method_id,
                )
            if self._verify_writes and (
                read_back.name != requested_name or (read_back.emoji or "") != requested_emoji
            ):
                raise DecodingError(
                    "Android label mutation did not read back the requested properties",
                    method_id=self._mutation_method_id,
                )
            return read_back if return_object else None

    @abstractmethod
    async def _send_update(
        self,
        operation: Literal["properties", "delete"],
        notebook_id: str,
        label_ids: builtins.list[str],
        *,
        name: str | None = None,
        emoji: str | None = None,
        current: Label | None = None,
    ) -> None:
        """Send one label property update or delete operation."""

    async def rename(
        self, notebook_id: str, label_id: str, name: str, *, return_object: bool = True
    ) -> Label | None:
        """Rename a label while preserving its existing emoji."""
        return await self.update(notebook_id, label_id, name=name, return_object=return_object)

    async def set_emoji(
        self, notebook_id: str, label_id: str, emoji: str, *, return_object: bool = True
    ) -> Label | None:
        """Set a label's emoji."""
        return await self.update(notebook_id, label_id, emoji=emoji, return_object=return_object)

    async def add_sources(
        self,
        notebook_id: str,
        label_id: str,
        source_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Label | None:
        """Add sources to a label."""
        return await self._mutate_members(
            notebook_id,
            label_id,
            source_ids,
            operation="add_sources",
            return_object=return_object,
        )

    async def remove_sources(
        self,
        notebook_id: str,
        label_id: str,
        source_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Label | None:
        """Remove sources from a label without deleting them."""
        return await self._mutate_members(
            notebook_id,
            label_id,
            source_ids,
            operation="remove_sources",
            return_object=return_object,
        )

    async def _mutate_members(
        self,
        notebook_id: str,
        label_id: str,
        source_ids: builtins.list[str],
        *,
        operation: Literal["add_sources", "remove_sources"],
        return_object: bool,
    ) -> Label | None:
        if not source_ids:
            raise ValueError(f"{operation} requires at least one source id")
        unique_ids = list(dict.fromkeys(source_ids))
        logger.debug(
            "%s %d source(s) %s label %s",
            "Adding" if operation == "add_sources" else "Removing",
            len(unique_ids),
            "to" if operation == "add_sources" else "from",
            label_id,
        )
        async with self._operation_scope(f"labels.{operation}"):
            for source_id in unique_ids:
                await self._send_mutate_member(
                    notebook_id,
                    label_id,
                    source_id,
                    operation=operation,
                )
            read_back = next(
                (label for label in await self._list_in_scope(notebook_id) if label.id == label_id),
                None,
            )
            if read_back is None:
                raise LabelNotFoundError(label_id, method_id=self._mutation_method_id)
            if self._verify_writes:
                present = set(read_back.source_ids)
                verified = (
                    set(unique_ids) <= present
                    if operation == "add_sources"
                    else set(unique_ids).isdisjoint(present)
                )
                if not verified:
                    raise DecodingError(
                        "Android label membership mutation did not read back the requested state",
                        method_id=self._mutation_method_id,
                    )
            return read_back if return_object else None

    @abstractmethod
    async def _send_mutate_member(
        self,
        notebook_id: str,
        label_id: str,
        source_id: str,
        *,
        operation: Literal["add_sources", "remove_sources"],
    ) -> None:
        """Send one label membership mutation."""

    async def delete(self, notebook_id: str, label_ids: str | builtins.list[str]) -> None:
        """Delete one or more labels without deleting their sources."""
        requested = [label_ids] if isinstance(label_ids, str) else list(label_ids)
        if self._dedupe_deletes:
            requested = list(dict.fromkeys(requested))
        if not requested:
            return
        async with self._operation_scope("labels.delete"):
            existing = requested
            if self._filter_existing_on_delete:
                current_ids = {label.id for label in await self._list_in_scope(notebook_id)}
                existing = [label_id for label_id in requested if label_id in current_ids]
                if not existing:
                    return
            await self._send_update("delete", notebook_id, existing)
            if self._verify_writes:
                remaining = {label.id for label in await self._list_in_scope(notebook_id)}
                if set(existing) & remaining:
                    raise DecodingError(
                        "Android label delete did not read back absence",
                        method_id=self._delete_method_id,
                    )


__all__ = ["LabelsAPI", "ListSources"]
