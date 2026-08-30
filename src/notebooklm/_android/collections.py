"""Private Android notebook-collection adapter over the organization wire."""

from __future__ import annotations

import builtins
from typing import NoReturn

from .._collections import CollectionsAPI, ListNotebooks
from .._idempotency import mark_unconfirmed
from ..exceptions import (
    CollectionError,
    CollectionNotFoundError,
    DecodingError,
    NetworkError,
    RPCError,
)
from ..types import Collection, Notebook
from .codecs.organization import decode_created_collections
from .organization import (
    CREATE_LABEL_METHOD,
    DELETE_LABELS_METHOD,
    GET_LABELS_METHOD,
    MUTATE_LABEL_METHOD,
    MemberOperation,
    create_manual,
    delete_resources,
    list_collections,
    mutate_member,
    mutate_properties,
)
from .session import AndroidSession


def _collection_miss(collection_id: str, *, method_id: str) -> CollectionNotFoundError:
    return CollectionNotFoundError(collection_id, method_id=method_id)


def _raise_collection_write_miss(collection_id: str, error: RPCError) -> None:
    if error.rpc_code == 5:
        raise _collection_miss(collection_id, method_id=MUTATE_LABEL_METHOD) from error
    raise error


class AndroidCollectionsAPI(CollectionsAPI):
    """All nine collection operations over live-validated Android RPC shapes."""

    def __init__(self, session: AndroidSession, *, list_notebooks: ListNotebooks) -> None:
        self._transport = session
        self._list_notebooks = list_notebooks

    async def _list(self, *, expected_epoch: int) -> builtins.list[Collection]:
        return await list_collections(self._transport, expected_epoch=expected_epoch)

    async def _get_or_none(
        self,
        collection_id: str,
        *,
        expected_epoch: int,
    ) -> Collection | None:
        return next(
            (
                collection
                for collection in await self._list(expected_epoch=expected_epoch)
                if collection.id == collection_id
            ),
            None,
        )

    async def list(self) -> builtins.list[Collection]:
        async with self._transport.operation_scope("collections.list") as lease:
            return await self._list(expected_epoch=lease.epoch)

    async def get_or_none(self, collection_id: str) -> Collection | None:
        async with self._transport.operation_scope("collections.get_or_none") as lease:
            return await self._get_or_none(collection_id, expected_epoch=lease.epoch)

    async def get(self, collection_id: str) -> Collection:
        async with self._transport.operation_scope("collections.get") as lease:
            collection = await self._get_or_none(collection_id, expected_epoch=lease.epoch)
            if collection is None:
                raise _collection_miss(collection_id, method_id=GET_LABELS_METHOD)
            return collection

    async def notebooks(self, collection_id: str) -> builtins.list[Notebook]:
        async with self._transport.operation_scope("collections.notebooks") as lease:
            collection = await self._get_or_none(collection_id, expected_epoch=lease.epoch)
            if collection is None:
                raise _collection_miss(collection_id, method_id=GET_LABELS_METHOD)
            by_id = {notebook.id: notebook for notebook in await self._list_notebooks()}
            return [
                by_id[notebook_id]
                for notebook_id in collection.notebook_ids
                if notebook_id in by_id
            ]

    async def create(self, name: str) -> Collection:
        async with self._transport.operation_scope("collections.create") as lease:
            existing_ids = {
                collection.id for collection in await self._list(expected_epoch=lease.epoch)
            }
            response = await create_manual(
                self._transport,
                kind="collection",
                name=name,
                emoji="",
                notebook_id=None,
                expected_epoch=lease.epoch,
            )
            try:
                created = decode_created_collections(response, method_id=CREATE_LABEL_METHOD)
            except DecodingError as error:
                raise mark_unconfirmed(error) from None
            candidates = [collection for collection in created if collection.id not in existing_ids]
            matching = [
                collection
                for collection in candidates
                if collection.name == name
                and (collection.emoji or "") == ""
                and not collection.notebook_ids
            ]
            if len(candidates) == 1 and not matching:
                raise mark_unconfirmed(
                    DecodingError(
                        "Android collection create response did not echo the requested empty "
                        "collection",
                        method_id=CREATE_LABEL_METHOD,
                    )
                )
            if len(matching) != 1:
                raise mark_unconfirmed(
                    CollectionError(
                        f"create(name={name!r}) expected exactly 1 new matching collection in "
                        f"the Android response, found {len(matching)}"
                    )
                )
            (collection,) = matching
            return collection

    async def rename(
        self,
        collection_id: str,
        name: str,
        *,
        return_object: bool = True,
    ) -> Collection | None:
        async with self._transport.operation_scope("collections.rename") as lease:
            current = await self._get_or_none(collection_id, expected_epoch=lease.epoch)
            if current is None:
                raise _collection_miss(collection_id, method_id=MUTATE_LABEL_METHOD)
            requested_emoji = current.emoji or ""
            try:
                await mutate_properties(
                    self._transport,
                    kind="collection",
                    resource_id=collection_id,
                    name=name,
                    emoji=requested_emoji,
                    notebook_id=None,
                    expected_epoch=lease.epoch,
                )
            except RPCError as exc:
                _raise_collection_write_miss(collection_id, exc)
            read_back = await self._get_or_none(collection_id, expected_epoch=lease.epoch)
            if read_back is None:
                raise _collection_miss(collection_id, method_id=MUTATE_LABEL_METHOD)
            if read_back.name != name or (read_back.emoji or "") != requested_emoji:
                raise DecodingError(
                    "Android collection rename did not read back the requested properties",
                    method_id=MUTATE_LABEL_METHOD,
                )
            return read_back if return_object else None

    async def _mutate_notebooks(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        add: bool,
        return_object: bool,
    ) -> Collection | None:
        if not notebook_ids:
            operation_name = "add_notebooks" if add else "remove_notebooks"
            raise ValueError(f"{operation_name} requires at least one notebook id")
        unique_ids = list(dict.fromkeys(notebook_ids))
        operation: MemberOperation = "add_notebooks" if add else "remove_notebooks"
        async with self._transport.operation_scope(f"collections.{operation}") as lease:
            for notebook_id in unique_ids:
                try:
                    await mutate_member(
                        self._transport,
                        kind="collection",
                        resource_id=collection_id,
                        member_id=notebook_id,
                        operation=operation,
                        notebook_id=None,
                        expected_epoch=lease.epoch,
                    )
                except RPCError as exc:
                    await self._raise_membership_write_error(
                        collection_id,
                        exc,
                        expected_epoch=lease.epoch,
                    )
            read_back = await self._get_or_none(collection_id, expected_epoch=lease.epoch)
            if read_back is None:
                raise _collection_miss(collection_id, method_id=MUTATE_LABEL_METHOD)
            present = set(read_back.notebook_ids)
            verified = set(unique_ids) <= present if add else set(unique_ids).isdisjoint(present)
            if not verified:
                raise DecodingError(
                    "Android collection membership mutation did not read back the requested state",
                    method_id=MUTATE_LABEL_METHOD,
                )
            return read_back if return_object else None

    async def _raise_membership_write_error(
        self,
        collection_id: str,
        error: RPCError,
        *,
        expected_epoch: int,
    ) -> NoReturn:
        """Map ``NOT_FOUND`` only after proving the collection is absent.

        A membership mutation names both a collection and a notebook, so the
        transport status alone cannot identify which resource was missing. A
        safe same-epoch collection read establishes the public target-miss
        contract without replaying the write. If reconciliation itself fails,
        preserve the original sanitized mutation error rather than replacing it
        with an unrelated read failure or a guessed domain error.
        """

        if error.rpc_code != 5:
            raise error
        try:
            collection = await self._get_or_none(
                collection_id,
                expected_epoch=expected_epoch,
            )
        except (NetworkError, RPCError):
            raise error from None
        if collection is None:
            raise _collection_miss(collection_id, method_id=MUTATE_LABEL_METHOD) from error
        raise error

    async def add_notebooks(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Collection | None:
        return await self._mutate_notebooks(
            collection_id,
            notebook_ids,
            add=True,
            return_object=return_object,
        )

    async def remove_notebooks(
        self,
        collection_id: str,
        notebook_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Collection | None:
        return await self._mutate_notebooks(
            collection_id,
            notebook_ids,
            add=False,
            return_object=return_object,
        )

    async def delete(self, collection_ids: str | builtins.list[str]) -> None:
        requested = [collection_ids] if isinstance(collection_ids, str) else list(collection_ids)
        requested = list(dict.fromkeys(requested))
        if not requested:
            return
        async with self._transport.operation_scope("collections.delete") as lease:
            current_ids = {
                collection.id for collection in await self._list(expected_epoch=lease.epoch)
            }
            existing = [
                collection_id for collection_id in requested if collection_id in current_ids
            ]
            if not existing:
                return
            try:
                await delete_resources(
                    self._transport,
                    kind="collection",
                    resource_ids=existing,
                    notebook_id=None,
                    expected_epoch=lease.epoch,
                )
            except RPCError as exc:
                if exc.rpc_code != 5:
                    raise
            remaining = {
                collection.id for collection in await self._list(expected_epoch=lease.epoch)
            }
            if set(existing) & remaining:
                raise DecodingError(
                    "Android collection delete did not read back absence",
                    method_id=DELETE_LABELS_METHOD,
                )


__all__ = ["AndroidCollectionsAPI"]
