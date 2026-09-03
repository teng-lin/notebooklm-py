"""Private Android source-label adapter over the evidenced organization wire."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal, NoReturn

from .._idempotency import mark_unconfirmed
from .._labels import LabelsAPI, ListSources
from .._runtime.call_supervisor import OperationLease
from ..exceptions import DecodingError, LabelError, LabelNotFoundError, NetworkError, RPCError
from ..types import Label
from .codecs.organization import decode_created_labels
from .epoch import bind_workflow_epoch, reset_workflow_epoch, workflow_epoch_for
from .organization import (
    CREATE_LABEL_METHOD,
    DELETE_LABELS_METHOD,
    GET_LABELS_METHOD,
    MUTATE_LABEL_METHOD,
    create_manual,
    delete_resources,
    generate_labels,
    list_labels,
    mutate_member,
    mutate_properties,
)
from .session import AndroidSession


def _label_miss(label_id: str, *, method_id: str) -> LabelNotFoundError:
    return LabelNotFoundError(label_id, method_id=method_id)


def _raise_label_write_miss(label_id: str, error: RPCError) -> None:
    if error.rpc_code == 5:
        raise _label_miss(label_id, method_id=MUTATE_LABEL_METHOD) from error
    raise error


class AndroidLabelsAPI(LabelsAPI):
    """Evidence-qualified manual source-label CRUD and membership adapter."""

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

    def __init__(
        self,
        session: AndroidSession,
        *,
        list_sources: ListSources,
    ) -> None:
        super().__init__(list_sources=list_sources)
        self._transport = session

    async def _list(self, notebook_id: str, *, expected_epoch: int) -> builtins.list[Label]:
        return await list_labels(
            self._transport,
            notebook_id,
            expected_epoch=expected_epoch,
        )

    async def list(self, notebook_id: str) -> builtins.list[Label]:
        async with self._transport.operation_scope("labels.list") as lease:
            return await self._list(notebook_id, expected_epoch=lease.epoch)

    async def _list_in_scope(self, notebook_id: str) -> builtins.list[Label]:
        expected_epoch = workflow_epoch_for(self._transport)
        assert expected_epoch is not None
        return await self._list(notebook_id, expected_epoch=expected_epoch)

    async def generate(
        self,
        notebook_id: str,
        *,
        scope: Literal["all", "unlabeled"] = "unlabeled",
    ) -> builtins.list[Label]:
        if scope not in ("all", "unlabeled"):
            raise ValueError(f"generate scope must be 'all' or 'unlabeled', got {scope!r}")
        async with self._transport.operation_scope("labels.generate") as lease:
            return await generate_labels(
                self._transport,
                notebook_id,
                regenerate_all=scope == "all",
                expected_epoch=lease.epoch,
            )

    async def create(self, notebook_id: str, name: str, emoji: str = "") -> Label:
        async with self._transport.operation_scope("labels.create") as lease:
            response = await create_manual(
                self._transport,
                kind="label",
                name=name,
                emoji=emoji,
                notebook_id=notebook_id,
                expected_epoch=lease.epoch,
            )
            try:
                created = decode_created_labels(
                    response,
                    notebook_id,
                    method_id=CREATE_LABEL_METHOD,
                )
            except DecodingError as error:
                raise mark_unconfirmed(error) from None
            if len(created) != 1:
                raise mark_unconfirmed(
                    LabelError(
                        f"create(name={name!r}) expected exactly 1 created label in the Android "
                        f"response, found {len(created)}"
                    )
                )
            (label,) = created
            if label.name != name or (label.emoji or "") != emoji or bool(label.source_ids):
                raise mark_unconfirmed(
                    DecodingError(
                        "Android label create response did not echo the requested empty label",
                        method_id=CREATE_LABEL_METHOD,
                    )
                )
            return label

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
        expected_epoch = workflow_epoch_for(self._transport)
        assert expected_epoch is not None
        if operation == "delete":
            try:
                await delete_resources(
                    self._transport,
                    kind="label",
                    resource_ids=label_ids,
                    notebook_id=notebook_id,
                    expected_epoch=expected_epoch,
                )
            except RPCError as exc:
                if exc.rpc_code != 5:
                    raise
            return
        (label_id,) = label_ids
        assert current is not None
        requested_name = current.name if name is None else name
        requested_emoji = (current.emoji or "") if emoji is None else emoji
        try:
            await mutate_properties(
                self._transport,
                kind="label",
                resource_id=label_id,
                name=requested_name,
                emoji=requested_emoji,
                notebook_id=notebook_id,
                expected_epoch=expected_epoch,
            )
        except RPCError as exc:
            _raise_label_write_miss(label_id, exc)

    async def _send_mutate_member(
        self,
        notebook_id: str,
        label_id: str,
        source_id: str,
        *,
        operation: Literal["add_sources", "remove_sources"],
    ) -> None:
        expected_epoch = workflow_epoch_for(self._transport)
        assert expected_epoch is not None
        try:
            await mutate_member(
                self._transport,
                kind="label",
                resource_id=label_id,
                member_id=source_id,
                operation=operation,
                notebook_id=notebook_id,
                expected_epoch=expected_epoch,
            )
        except RPCError as exc:
            await self._raise_membership_write_error(notebook_id, label_id, exc)

    async def _raise_membership_write_error(
        self,
        notebook_id: str,
        label_id: str,
        error: RPCError,
    ) -> NoReturn:
        """Map ``NOT_FOUND`` only after proving the label itself is absent."""

        if error.rpc_code != 5:
            raise error
        try:
            label = next(
                (label for label in await self._list_in_scope(notebook_id) if label.id == label_id),
                None,
            )
        except (NetworkError, RPCError):
            raise error from None
        if label is None:
            raise _label_miss(label_id, method_id=MUTATE_LABEL_METHOD) from error
        raise error


__all__ = ["AndroidLabelsAPI"]
