"""Private Android source-label adapter over the evidenced organization wire."""

from __future__ import annotations

import builtins
from typing import Literal, NoReturn

from .._idempotency import mark_unconfirmed
from .._labels import LabelsAPI, ListSources
from ..exceptions import DecodingError, LabelError, LabelNotFoundError, NetworkError, RPCError
from ..types import Label, Source
from .codecs.organization import decode_created_labels
from .organization import (
    CREATE_LABEL_METHOD,
    DELETE_LABELS_METHOD,
    GET_LABELS_METHOD,
    MUTATE_LABEL_METHOD,
    MemberOperation,
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

    def __init__(
        self,
        session: AndroidSession,
        *,
        list_sources: ListSources,
    ) -> None:
        self._transport = session
        self._list_sources = list_sources

    async def _list(self, notebook_id: str, *, expected_epoch: int) -> builtins.list[Label]:
        return await list_labels(
            self._transport,
            notebook_id,
            expected_epoch=expected_epoch,
        )

    async def _get_or_none(
        self,
        notebook_id: str,
        label_id: str,
        *,
        expected_epoch: int,
    ) -> Label | None:
        return next(
            (
                label
                for label in await self._list(notebook_id, expected_epoch=expected_epoch)
                if label.id == label_id
            ),
            None,
        )

    async def list(self, notebook_id: str) -> builtins.list[Label]:
        async with self._transport.operation_scope("labels.list") as lease:
            return await self._list(notebook_id, expected_epoch=lease.epoch)

    async def get_or_none(self, notebook_id: str, label_id: str) -> Label | None:
        async with self._transport.operation_scope("labels.get_or_none") as lease:
            return await self._get_or_none(
                notebook_id,
                label_id,
                expected_epoch=lease.epoch,
            )

    async def get(self, notebook_id: str, label_id: str) -> Label:
        async with self._transport.operation_scope("labels.get") as lease:
            label = await self._get_or_none(
                notebook_id,
                label_id,
                expected_epoch=lease.epoch,
            )
            if label is None:
                raise _label_miss(label_id, method_id=GET_LABELS_METHOD)
            return label

    async def sources(self, notebook_id: str, label_id: str) -> builtins.list[Source]:
        async with self._transport.operation_scope("labels.sources") as lease:
            label = await self._get_or_none(
                notebook_id,
                label_id,
                expected_epoch=lease.epoch,
            )
            if label is None:
                raise _label_miss(label_id, method_id=GET_LABELS_METHOD)
            by_id = {source.id: source for source in await self._list_sources(notebook_id)}
            return [by_id[source_id] for source_id in label.source_ids if source_id in by_id]

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

    async def update(
        self,
        notebook_id: str,
        label_id: str,
        *,
        name: str | None = None,
        emoji: str | None = None,
        return_object: bool = True,
    ) -> Label | None:
        if name is None and emoji is None:
            raise ValueError("update requires name and/or emoji")
        async with self._transport.operation_scope("labels.update") as lease:
            current = await self._get_or_none(
                notebook_id,
                label_id,
                expected_epoch=lease.epoch,
            )
            if current is None:
                raise _label_miss(label_id, method_id=MUTATE_LABEL_METHOD)
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
                    expected_epoch=lease.epoch,
                )
            except RPCError as exc:
                _raise_label_write_miss(label_id, exc)
            read_back = await self._get_or_none(
                notebook_id,
                label_id,
                expected_epoch=lease.epoch,
            )
            if read_back is None:
                raise _label_miss(label_id, method_id=MUTATE_LABEL_METHOD)
            if read_back.name != requested_name or (read_back.emoji or "") != requested_emoji:
                raise DecodingError(
                    "Android label mutation did not read back the requested properties",
                    method_id=MUTATE_LABEL_METHOD,
                )
            return read_back if return_object else None

    async def rename(
        self,
        notebook_id: str,
        label_id: str,
        name: str,
        *,
        return_object: bool = True,
    ) -> Label | None:
        return await self.update(
            notebook_id,
            label_id,
            name=name,
            return_object=return_object,
        )

    async def set_emoji(
        self,
        notebook_id: str,
        label_id: str,
        emoji: str,
        *,
        return_object: bool = True,
    ) -> Label | None:
        return await self.update(
            notebook_id,
            label_id,
            emoji=emoji,
            return_object=return_object,
        )

    async def _mutate_members(
        self,
        notebook_id: str,
        label_id: str,
        source_ids: builtins.list[str],
        *,
        add: bool,
        return_object: bool,
    ) -> Label | None:
        if not source_ids:
            operation_name = "add_sources" if add else "remove_sources"
            raise ValueError(f"{operation_name} requires at least one source id")
        unique_ids = list(dict.fromkeys(source_ids))
        operation: MemberOperation = "add_sources" if add else "remove_sources"
        async with self._transport.operation_scope(f"labels.{operation}") as lease:
            for source_id in unique_ids:
                try:
                    await mutate_member(
                        self._transport,
                        kind="label",
                        resource_id=label_id,
                        member_id=source_id,
                        operation=operation,
                        notebook_id=notebook_id,
                        expected_epoch=lease.epoch,
                    )
                except RPCError as exc:
                    await self._raise_membership_write_error(
                        notebook_id,
                        label_id,
                        exc,
                        expected_epoch=lease.epoch,
                    )
            read_back = await self._get_or_none(
                notebook_id,
                label_id,
                expected_epoch=lease.epoch,
            )
            if read_back is None:
                raise _label_miss(label_id, method_id=MUTATE_LABEL_METHOD)
            present = set(read_back.source_ids)
            verified = set(unique_ids) <= present if add else set(unique_ids).isdisjoint(present)
            if not verified:
                raise DecodingError(
                    "Android label membership mutation did not read back the requested state",
                    method_id=MUTATE_LABEL_METHOD,
                )
            return read_back if return_object else None

    async def _raise_membership_write_error(
        self,
        notebook_id: str,
        label_id: str,
        error: RPCError,
        *,
        expected_epoch: int,
    ) -> NoReturn:
        """Map ``NOT_FOUND`` only after proving the label itself is absent."""

        if error.rpc_code != 5:
            raise error
        try:
            label = await self._get_or_none(
                notebook_id,
                label_id,
                expected_epoch=expected_epoch,
            )
        except (NetworkError, RPCError):
            raise error from None
        if label is None:
            raise _label_miss(label_id, method_id=MUTATE_LABEL_METHOD) from error
        raise error

    async def add_sources(
        self,
        notebook_id: str,
        label_id: str,
        source_ids: builtins.list[str],
        *,
        return_object: bool = True,
    ) -> Label | None:
        return await self._mutate_members(
            notebook_id,
            label_id,
            source_ids,
            add=True,
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
        return await self._mutate_members(
            notebook_id,
            label_id,
            source_ids,
            add=False,
            return_object=return_object,
        )

    async def delete(self, notebook_id: str, label_ids: str | builtins.list[str]) -> None:
        requested = [label_ids] if isinstance(label_ids, str) else list(label_ids)
        requested = list(dict.fromkeys(requested))
        if not requested:
            return
        async with self._transport.operation_scope("labels.delete") as lease:
            current_ids = {
                label.id for label in await self._list(notebook_id, expected_epoch=lease.epoch)
            }
            existing = [label_id for label_id in requested if label_id in current_ids]
            if not existing:
                return
            try:
                await delete_resources(
                    self._transport,
                    kind="label",
                    resource_ids=existing,
                    notebook_id=notebook_id,
                    expected_epoch=lease.epoch,
                )
            except RPCError as exc:
                if exc.rpc_code != 5:
                    raise
            remaining = {
                label.id for label in await self._list(notebook_id, expected_epoch=lease.epoch)
            }
            if set(existing) & remaining:
                raise DecodingError(
                    "Android label delete did not read back absence",
                    method_id=DELETE_LABELS_METHOD,
                )


__all__ = ["AndroidLabelsAPI"]
