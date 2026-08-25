"""Transport-neutral semantic service for the P6.4 label/collection slice.

Source labels and collections are one wire surface twice: a collection is a
label with a distinct type discriminator and no notebook parent, so they share
the RPC ids ``agX4Bc`` / ``I3xc3c`` / ``le8sX`` / ``GyzE7e`` verbatim.  This
module is the single semantic authority over both; :class:`LabelKind` is the
explicit discriminator that selects the operation pair, and no wire vocabulary
crosses this boundary.

The two public facades (``client.labels`` and ``client.collections``) keep their
own argument validation, exception vocabulary, and membership joins; everything
between a validated request and a neutral :class:`LabelRecord` lives here.

Since P9.2 the ``label.update`` workflow is **service-owned**: this service
sequences the ``label.get`` preflight/readback and one ``label.mutate`` leaf
per member above the port, starts one deadline for the whole workflow, and
re-raises every leaf failure as the workflow operation with the leaf retained
in the diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ._backend import (
    BackendAdapter,
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    mark_backend_outcome_unknown,
    rebind_operation,
    require_leaves,
)
from ._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from ._operations import OperationDef
from ._records import (
    COLLECTION_CREATE_DEF,
    COLLECTION_DELETE_DEF,
    COLLECTION_GET_DEF,
    COLLECTION_LIST_DEF,
    COLLECTION_UPDATE_DEF,
    LABEL_CREATE_DEF,
    LABEL_DELETE_DEF,
    LABEL_GENERATE_DEF,
    LABEL_GET_DEF,
    LABEL_LIST_DEF,
    LABEL_MUTATE_DEF,
    LABEL_UPDATE_DEF,
    LabelCreateInput,
    LabelDeleteInput,
    LabelGenerateInput,
    LabelGetInput,
    LabelKind,
    LabelListInput,
    LabelMutateInput,
    LabelRecord,
    LabelUpdateInput,
)


@dataclass(frozen=True, slots=True)
class _KindBinding:
    """The operation pair one :class:`LabelKind` dispatches through."""

    list_def: OperationDef[Any, Any]
    get_def: OperationDef[Any, Any]
    create_def: OperationDef[Any, Any]
    update_def: OperationDef[Any, Any]
    delete_def: OperationDef[Any, Any]
    generate_def: OperationDef[Any, Any] | None
    #: Whether ``update`` is sequenced here from the leaves (P9.2 hoist) or is
    #: still one composite backend invocation.
    update_hoisted: bool


_KIND_BINDINGS: dict[LabelKind, _KindBinding] = {
    LabelKind.SOURCE_LABEL: _KindBinding(
        list_def=LABEL_LIST_DEF,
        get_def=LABEL_GET_DEF,
        create_def=LABEL_CREATE_DEF,
        update_def=LABEL_UPDATE_DEF,
        delete_def=LABEL_DELETE_DEF,
        generate_def=LABEL_GENERATE_DEF,
        update_hoisted=True,
    ),
    # Collections have no auto-grouping mode: ``agX4Bc``'s scope slot is a
    # source-label concept, so the account-level dialect binds no generate.
    LabelKind.COLLECTION: _KindBinding(
        list_def=COLLECTION_LIST_DEF,
        get_def=COLLECTION_GET_DEF,
        create_def=COLLECTION_CREATE_DEF,
        update_def=COLLECTION_UPDATE_DEF,
        delete_def=COLLECTION_DELETE_DEF,
        generate_def=None,
        update_hoisted=False,
    ),
}

#: Diagnostics key naming the workflow phase whose read proved a group absent.
#: The compatibility projector derives the legacy ``method_id`` from it, so no
#: wire vocabulary is needed here.
NOT_FOUND_PHASE_KEY = "phase"
NOT_FOUND_PREFLIGHT = "preflight"
NOT_FOUND_FIELD_READBACK = "field_readback"
NOT_FOUND_MEMBERSHIP_READBACK = "membership_readback"


class LabelSetService:
    """Invoke the discriminated label/collection operations and return records.

    One instance is bound to one :class:`LabelKind` for its whole lifetime, so a
    facade can never accidentally address the other dialect's operations.
    """

    __slots__ = ("_backend", "_binding", "_deadline_factory", "_kind")

    def __init__(
        self,
        backend: BackendAdapter,
        kind: LabelKind,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        self._kind = kind
        self._binding = _KIND_BINDINGS[kind]
        # Contract 3 (P9.2): the service, not the backend, mints the one
        # deadline that covers every leaf of a hoisted workflow.
        self._deadline_factory = deadline_factory

    @property
    def kind(self) -> LabelKind:
        """The discriminator every request from this service carries."""
        return self._kind

    async def list(
        self,
        notebook_id: str | None = None,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[LabelRecord, ...]:
        """List the whole group set in backend order."""
        result = await self._backend.invoke(
            self._binding.list_def,
            LabelListInput(self._kind, notebook_id),
            deadline=deadline,
        )
        return tuple(result.labels)

    async def get(
        self,
        label_id: str,
        notebook_id: str | None = None,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> LabelRecord | None:
        """Select one group by exact id; ``None`` is the not-found state."""
        result = await self._backend.invoke(
            self._binding.get_def,
            LabelGetInput(self._kind, label_id, notebook_id),
            deadline=deadline,
        )
        return result.label

    async def generate(
        self,
        notebook_id: str,
        *,
        replace_existing: bool = False,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[LabelRecord, ...]:
        """Auto-group sources into topic labels and return the full post-op set."""
        definition = self._binding.generate_def
        if definition is None:  # pragma: no cover - collections bind no generate
            raise ValueError(f"{self._kind.value} sets have no generation mode")
        result = await self._backend.invoke(
            definition,
            LabelGenerateInput(notebook_id, replace_existing=replace_existing),
            deadline=deadline,
        )
        return tuple(result.labels)

    async def create(
        self,
        name: str,
        notebook_id: str | None = None,
        *,
        emoji: str = "",
        deadline: RuntimeDeadline | None = None,
    ) -> LabelRecord:
        """Create one empty named group, reconciled by exact id-diff.

        Names may collide, so the backend attributes the new group by the id
        that was absent from its pre-create snapshot and raises rather than
        guessing when zero or several ids are new.
        """
        result = await self._backend.invoke(
            self._binding.create_def,
            LabelCreateInput(self._kind, name, notebook_id, emoji),
            deadline=deadline,
        )
        return result.label

    async def update(
        self,
        label_id: str,
        notebook_id: str | None = None,
        *,
        name: str | None = None,
        emoji: str | None = None,
        add_member_ids: tuple[str, ...] = (),
        remove_member_ids: tuple[str, ...] = (),
        return_object: bool = True,
        deadline: RuntimeDeadline | None = None,
    ) -> LabelRecord | None:
        """Apply one field or membership mutation to an existing group.

        The not-found contract holds in both ``return_object`` modes; the record
        is returned only when the caller asked for it.
        """
        value = LabelUpdateInput(
            self._kind,
            label_id,
            notebook_id,
            name=name,
            emoji=emoji,
            add_member_ids=add_member_ids,
            remove_member_ids=remove_member_ids,
            return_object=return_object,
        )
        if self._binding.update_hoisted:
            return await self._update_workflow(value, deadline=deadline)
        result = await self._backend.invoke(
            self._binding.update_def,
            value,
            deadline=deadline,
        )
        return result.label

    async def delete(
        self,
        label_ids: tuple[str, ...],
        notebook_id: str | None = None,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> None:
        """Delete groups in one batch; an absent id is an idempotent no-op."""
        await self._backend.invoke(
            self._binding.delete_def,
            LabelDeleteInput(self._kind, label_ids, notebook_id),
            deadline=deadline,
        )

    # -- service-owned update workflow (P9.2) --------------------------------------

    def _start_deadline(self, deadline: RuntimeDeadline | None) -> RuntimeDeadline | None:
        """Mint the one workflow deadline unless the caller supplied its own."""
        if deadline is not None or self._deadline_factory is None:
            return deadline
        return self._deadline_factory.start()

    async def _update_workflow(
        self,
        value: LabelUpdateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> LabelRecord | None:
        """Sequence ``label.get`` and ``label.mutate`` leaves as one workflow.

        Membership writes are per id — the server honours only the first id of
        a set-op group per call — followed by one **mandatory** readback (the
        write echoes no group, so the read is the only existence evidence). A
        field mutation preflights the current group so a rename carries the
        emoji through, then reads back only when the caller asked for the
        object.
        """
        workflow = self._binding.update_def.key
        get_def = self._binding.get_def
        if self._kind is LabelKind.COLLECTION and not self._is_membership(value):
            if value.name is None:
                raise BackendContractError(
                    "collection.update has no emoji-only field mask; a name is required",
                    operation=workflow,
                )
        # Principle 5: reject an unsupported leaf before the first side effect.
        require_leaves(self._backend, get_def.key, LABEL_MUTATE_DEF.key)
        deadline = self._start_deadline(deadline)
        write_dispatched = False
        try:
            if self._is_membership(value):
                for member_id in value.add_member_ids:
                    await self._mutate(
                        LabelMutateInput(
                            self._kind, value.label_id, value.notebook_id, add_member_id=member_id
                        ),
                        deadline=deadline,
                    )
                    write_dispatched = True
                for member_id in value.remove_member_ids:
                    await self._mutate(
                        LabelMutateInput(
                            self._kind,
                            value.label_id,
                            value.notebook_id,
                            remove_member_id=member_id,
                        ),
                        deadline=deadline,
                    )
                    write_dispatched = True
                current = await self._read(value, deadline=deadline)
                if current is None:
                    raise self._not_found(value, phase=NOT_FOUND_MEMBERSHIP_READBACK)
                return current if value.return_object else None

            current = await self._read(value, deadline=deadline)
            if current is None:
                raise self._not_found(value, phase=NOT_FOUND_PREFLIGHT)
            await self._mutate(
                LabelMutateInput(
                    self._kind,
                    value.label_id,
                    value.notebook_id,
                    name=value.name,
                    emoji=self._effective_emoji(value, current),
                ),
                deadline=deadline,
            )
            write_dispatched = True
            if not value.return_object:
                # The existence contract is already satisfied by the preflight.
                return None
            updated = await self._read(value, deadline=deadline)
            if updated is None:
                raise self._not_found(value, phase=NOT_FOUND_FIELD_READBACK)
            return updated
        except BackendError as error:
            if error.operation is workflow:
                raise
            if write_dispatched and isinstance(error, BackendDeadlineExceededError):
                # A later phase expired after an earlier write: the requested
                # final outcome is unconfirmed and unsafe to retry blindly. A
                # failing write's own uncertainty (``may_have_committed``) is
                # already carried by the leaf error and needs no re-marking.
                error = mark_backend_outcome_unknown(error)
            raise rebind_operation(error, workflow) from error.__cause__

    async def _mutate(self, value: LabelMutateInput, *, deadline: RuntimeDeadline | None) -> None:
        await self._backend.invoke(LABEL_MUTATE_DEF, value, deadline=deadline)

    async def _read(
        self, value: LabelUpdateInput, *, deadline: RuntimeDeadline | None
    ) -> LabelRecord | None:
        result = await self._backend.invoke(
            self._binding.get_def,
            LabelGetInput(self._kind, value.label_id, value.notebook_id),
            deadline=deadline,
        )
        return result.label

    @staticmethod
    def _is_membership(value: LabelUpdateInput) -> bool:
        return bool(value.add_member_ids or value.remove_member_ids)

    @staticmethod
    def _effective_emoji(value: LabelUpdateInput, current: LabelRecord) -> str | None:
        """Carry the preflight emoji through a name-only field mask.

        Whether a length-1 ``name_emoji`` group preserves or clears an existing
        emoji was long unverified for source labels, so the emoji is always sent
        explicitly and a rename can never clobber it.
        """
        if value.name is not None and value.emoji is None:
            return current.emoji or ""
        return value.emoji

    def _not_found(self, value: LabelUpdateInput, *, phase: str) -> BackendError:
        """Neutral not-found evidence; the discriminator picks the public class."""
        noun = "Collection" if self._kind is LabelKind.COLLECTION else "Label"
        return BackendError(
            message=f"{noun} not found: {value.label_id}",
            operation=self._binding.update_def.key,
            diagnostics=MappingProxyType(
                {
                    "label_kind": self._kind.value,
                    "label_id": value.label_id,
                    "notebook_id": value.notebook_id,
                    NOT_FOUND_PHASE_KEY: phase,
                }
            ),
            reason=BackendErrorReason.LABEL_NOT_FOUND,
        )


def require_member_ids(
    member_ids: list[str],
    method_name: str,
    noun: str,
) -> tuple[str, ...]:
    """Reject an empty membership request and dedupe, order-preserving.

    Both dialects issue one wire call per member id, so duplicates would be
    redundant round-trips (and an append-twice on the wire). Shared by both
    facades because the choreography, not the noun, is the contract.
    """
    if not member_ids:
        raise ValueError(f"{method_name} requires at least one {noun} id")
    return tuple(dict.fromkeys(member_ids))


__all__ = [
    "NOT_FOUND_FIELD_READBACK",
    "NOT_FOUND_MEMBERSHIP_READBACK",
    "NOT_FOUND_PHASE_KEY",
    "NOT_FOUND_PREFLIGHT",
    "LabelSetService",
    "require_member_ids",
]
