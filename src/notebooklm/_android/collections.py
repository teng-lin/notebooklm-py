"""Private Android notebook-collection adapter over the organization wire."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal, NoReturn

from .._collections import CollectionsAPI, ListNotebooks
from .._idempotency import mark_unconfirmed
from .._runtime.call_supervisor import OperationLease
from ..exceptions import (
    CollectionError,
    CollectionNotFoundError,
    DecodingError,
    NetworkError,
    RPCError,
)
from ..types import Collection
from .codecs.organization import decode_created_collections
from .epoch import bind_workflow_epoch, reset_workflow_epoch, workflow_epoch_for
from .organization import (
    CREATE_LABEL_METHOD,
    DELETE_LABELS_METHOD,
    GET_LABELS_METHOD,
    MUTATE_LABEL_METHOD,
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

    _list_method_id = GET_LABELS_METHOD
    _mutation_method_id = MUTATE_LABEL_METHOD
    _property_readback_miss_method_id = MUTATE_LABEL_METHOD
    _delete_method_id = DELETE_LABELS_METHOD
    _verify_writes = True
    _filter_existing_on_delete = True
    _dedupe_deletes = True

    @asynccontextmanager
    async def _operation_scope(self, label: str) -> AsyncIterator[OperationLease]:
        async with self._transport.operation_scope(label) as lease:
            token = bind_workflow_epoch(self._transport, lease.epoch)
            try:
                yield lease
            finally:
                reset_workflow_epoch(token)

    def __init__(self, session: AndroidSession, *, list_notebooks: ListNotebooks) -> None:
        super().__init__(list_notebooks=list_notebooks)
        self._transport = session

    async def _list(self, *, expected_epoch: int) -> builtins.list[Collection]:
        return await list_collections(self._transport, expected_epoch=expected_epoch)

    async def list(self) -> builtins.list[Collection]:
        async with self._transport.operation_scope("collections.list") as lease:
            return await self._list(expected_epoch=lease.epoch)

    async def _list_in_scope(self) -> builtins.list[Collection]:
        expected_epoch = workflow_epoch_for(self._transport)
        assert expected_epoch is not None
        return await self._list(expected_epoch=expected_epoch)

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

    async def _send_update(
        self,
        operation: Literal["properties", "delete"],
        collection_ids: builtins.list[str],
        *,
        name: str | None = None,
        current: Collection | None = None,
    ) -> None:
        expected_epoch = workflow_epoch_for(self._transport)
        assert expected_epoch is not None
        if operation == "delete":
            try:
                await delete_resources(
                    self._transport,
                    kind="collection",
                    resource_ids=collection_ids,
                    notebook_id=None,
                    expected_epoch=expected_epoch,
                )
            except RPCError as exc:
                if exc.rpc_code != 5:
                    raise
            return
        (collection_id,) = collection_ids
        assert name is not None
        assert current is not None
        try:
            await mutate_properties(
                self._transport,
                kind="collection",
                resource_id=collection_id,
                name=name,
                emoji=current.emoji or "",
                notebook_id=None,
                expected_epoch=expected_epoch,
            )
        except RPCError as exc:
            _raise_collection_write_miss(collection_id, exc)

    async def _send_mutate_member(
        self,
        collection_id: str,
        notebook_id: str,
        *,
        operation: Literal["add_notebooks", "remove_notebooks"],
    ) -> None:
        expected_epoch = workflow_epoch_for(self._transport)
        assert expected_epoch is not None
        try:
            await mutate_member(
                self._transport,
                kind="collection",
                resource_id=collection_id,
                member_id=notebook_id,
                operation=operation,
                notebook_id=None,
                expected_epoch=expected_epoch,
            )
        except RPCError as exc:
            await self._raise_membership_write_error(
                collection_id,
                exc,
            )

    async def _raise_membership_write_error(
        self,
        collection_id: str,
        error: RPCError,
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
            collection = next(
                (
                    collection
                    for collection in await self._list_in_scope()
                    if collection.id == collection_id
                ),
                None,
            )
        except (NetworkError, RPCError):
            raise error from None
        if collection is None:
            raise _collection_miss(collection_id, method_id=MUTATE_LABEL_METHOD) from error
        raise error


__all__ = ["AndroidCollectionsAPI"]
