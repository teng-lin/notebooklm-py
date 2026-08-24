"""Web workflow bindings for source labels and account-level collections."""

from __future__ import annotations

from types import MappingProxyType

from .._backend import BackendContractError, BackendError, BackendErrorReason
from .._deadline import RuntimeDeadline
from .._operations import Operation
from .._records import (
    LabelCreateInput,
    LabelCreateResult,
    LabelGetInput,
    LabelGetResult,
    LabelKind,
    LabelListInput,
    LabelListResult,
    LabelRecord,
    LabelUpdateInput,
    LabelUpdateResult,
)
from ..rpc import RPCMethod
from .codec.labels import (
    build_create_collection_params,
    build_create_label_params,
    build_rename_collection_params,
    build_update_collection_notebooks_params,
    build_update_label_params,
    decode_label_create_echo,
    decode_label_set_list_result,
    encode_label_set_list,
    require_label_kind,
    require_notebook_scope,
)
from .studio_data import StudioDataWebHandlers

# Collections are account-level: every collection RPC uses the home-page source
# path, not a ``/notebook/<id>`` path (they have no notebook scope).
_ACCOUNT_PATH = "/"


class LabelSetWebHandlers(StudioDataWebHandlers):
    """Composite source-label/collection handlers mixed into the web backend.

    Since P9.3 the leaf reads, the batch deletes and auto-grouping are codec
    rows in ``_web/bindings/labels.py``; only the four create/update composites
    remain here, with the shared set read they preflight and read back through.
    """

    # -- labels and collections ---------------------------------------------
    #
    # One wire surface, two discriminated dialects. The write paths split per
    # dialect only because ``operation_variant`` must be a literal at the
    # dispatch site for the operation catalog to allocate it.

    @staticmethod
    def _require_label_kind(
        actual: LabelKind,
        expected: LabelKind,
        operation: Operation,
    ) -> None:
        """Fail closed when a request addresses the other dialect's operation."""
        require_label_kind(actual, expected, operation)

    @staticmethod
    def _require_notebook_scope(notebook_id: str | None, operation: Operation) -> str:
        """Source labels are notebook-scoped; a null scope is a contract error."""
        return require_notebook_scope(notebook_id, operation)

    @staticmethod
    def _label_not_found(
        *,
        kind: LabelKind,
        label_id: str,
        notebook_id: str | None,
        operation: Operation,
        method_id: str,
    ) -> BackendError:
        """Neutral not-found evidence; the discriminator picks the public class."""
        noun = "Collection" if kind is LabelKind.COLLECTION else "Label"
        return BackendError(
            message=f"{noun} not found: {label_id}",
            operation=operation,
            diagnostics=MappingProxyType(
                {
                    "label_kind": kind.value,
                    "label_id": label_id,
                    "notebook_id": notebook_id,
                    "method_id": method_id,
                }
            ),
            reason=BackendErrorReason.LABEL_NOT_FOUND,
        )

    @staticmethod
    def _reconcile_created_label(
        after: tuple[LabelRecord, ...],
        before_ids: set[str],
        *,
        kind: LabelKind,
        operation: Operation,
        name: str,
        noun: str,
        ambiguity_detail: str,
    ) -> LabelRecord:
        """Attribute the create by exact id-diff, never by name.

        Names may collide, so the group this call created is the one whose id
        was absent from the pre-create snapshot. Zero or several new ids is a
        concurrent write the caller must resolve, so it is intentionally loud.
        """
        new = [label for label in after if label.id not in before_ids]
        if len(new) != 1:
            raise BackendError(
                message=(
                    f"create(name={name!r}) expected exactly 1 new {noun}, "
                    f"found {len(new)} ({ambiguity_detail})"
                ),
                operation=operation,
                diagnostics=MappingProxyType(
                    {"label_kind": kind.value, "candidate_count": len(new), "name": name}
                ),
                reason=BackendErrorReason.LABEL_AMBIGUOUS_CREATE,
            )
        # ``new`` holds typed records, not decoded rows: the positional decode
        # already happened in the codec. Unpacking asserts exactly-one semantics
        # and avoids the single-level ``name[int]`` raw-index guardrail.
        (label,) = new
        return label

    async def _label_set_list(
        self,
        value: LabelListInput,
        *,
        kind: LabelKind,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        outcome_unknown_on_expiry: bool = False,
    ) -> LabelListResult:
        """Read one whole label set for a composite's preflight or readback.

        The leaf ``label.list``/``collection.list`` rows dispatch the same
        payload; this helper exists because composites thread
        ``outcome_unknown_on_expiry`` through the read, which a codec row cannot.
        """
        self._require_label_kind(value.kind, kind, operation)
        notebook_id = None if kind is LabelKind.COLLECTION else value.notebook_id
        payload = encode_label_set_list(kind, notebook_id, operation)
        result = await self._rpc_call(
            RPCMethod.LIST_LABELS,
            payload.params,
            operation=operation,
            deadline=deadline,
            source_path=payload.source_path,
            allow_null=payload.allow_null,
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        return decode_label_set_list_result(result, kind=kind, notebook_id=notebook_id)

    async def _label_set_get(
        self,
        value: LabelGetInput,
        *,
        kind: LabelKind,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        outcome_unknown_on_expiry: bool = False,
    ) -> LabelGetResult:
        """Select one group by exact id from the set read; never by name."""
        self._require_label_kind(value.kind, kind, operation)
        listed = await self._label_set_list(
            LabelListInput(kind, value.notebook_id),
            kind=kind,
            operation=operation,
            deadline=deadline,
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        return LabelGetResult(
            label=next(
                (label for label in listed.labels if label.id == value.label_id),
                None,
            )
        )

    async def _label_create(
        self,
        value: LabelCreateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> LabelCreateResult:
        """Create one source label and attribute it against a fresh baseline.

        ``agX4Bc`` echoes the whole post-operation label set, so the id-diff is
        settled from that echo without a second read.
        """
        self._require_label_kind(value.kind, LabelKind.SOURCE_LABEL, Operation.LABEL_CREATE)
        notebook_id = self._require_notebook_scope(value.notebook_id, Operation.LABEL_CREATE)
        baseline = await self._label_set_list(
            LabelListInput(LabelKind.SOURCE_LABEL, notebook_id),
            kind=LabelKind.SOURCE_LABEL,
            operation=Operation.LABEL_CREATE,
            deadline=deadline,
        )
        result = await self._rpc_call(
            RPCMethod.CREATE_LABEL,
            build_create_label_params(notebook_id, value.name, value.emoji),
            operation=Operation.LABEL_CREATE,
            deadline=deadline,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        after = decode_label_create_echo(
            result,
            notebook_id=notebook_id,
            method_id=RPCMethod.CREATE_LABEL.value,
        )
        return LabelCreateResult(
            label=self._reconcile_created_label(
                after,
                {label.id for label in baseline.labels},
                kind=LabelKind.SOURCE_LABEL,
                operation=Operation.LABEL_CREATE,
                name=value.name,
                noun="label",
                ambiguity_detail=(
                    "concurrent label creation can cause this — retry from a fresh list"
                ),
            )
        )

    async def _collection_create(
        self,
        value: LabelCreateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> LabelCreateResult:
        """Create one collection and attribute it by re-listing the account set.

        Unlike the source-label dialect this re-lists rather than parsing the
        create echo: the collection create-response shape was never captured on
        the wire, so a fresh list is the only trustworthy post-state. The
        collection create wire carries no emoji slot, so ``value.emoji`` is not
        sent; an emoji is set by a later update if at all.
        """
        self._require_label_kind(value.kind, LabelKind.COLLECTION, Operation.COLLECTION_CREATE)
        baseline = await self._label_set_list(
            LabelListInput(LabelKind.COLLECTION),
            kind=LabelKind.COLLECTION,
            operation=Operation.COLLECTION_CREATE,
            deadline=deadline,
        )
        await self._rpc_call(
            RPCMethod.CREATE_LABEL,
            build_create_collection_params(value.name),
            operation=Operation.COLLECTION_CREATE,
            deadline=deadline,
            source_path=_ACCOUNT_PATH,
            allow_null=True,
        )
        after = await self._label_set_list(
            LabelListInput(LabelKind.COLLECTION),
            kind=LabelKind.COLLECTION,
            operation=Operation.COLLECTION_CREATE,
            deadline=deadline,
            outcome_unknown_on_expiry=True,
        )
        return LabelCreateResult(
            label=self._reconcile_created_label(
                after.labels,
                {label.id for label in baseline.labels},
                kind=LabelKind.COLLECTION,
                operation=Operation.COLLECTION_CREATE,
                name=value.name,
                noun="collection",
                ambiguity_detail=(
                    "a concurrent create, or read-after-write lag on the re-list, can cause "
                    "this — retry from a fresh list"
                ),
            )
        )

    async def _label_update(
        self,
        value: LabelUpdateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> LabelUpdateResult:
        """Apply one source-label field or membership mutation.

        A membership write is per id: the server honours only the first id of a
        set-op group per call, so a single multi-id call would silently assign
        one member. The writes are therefore **not atomic** across ids — a
        mid-loop failure leaves the earlier ids written and raises.
        """
        self._require_label_kind(value.kind, LabelKind.SOURCE_LABEL, Operation.LABEL_UPDATE)
        notebook_id = self._require_notebook_scope(value.notebook_id, Operation.LABEL_UPDATE)
        source_path = f"/notebook/{notebook_id}"
        if value.add_member_ids or value.remove_member_ids:
            write_may_have_committed = False
            for source_id in value.add_member_ids:
                await self._rpc_call(
                    RPCMethod.UPDATE_LABEL,
                    build_update_label_params(
                        notebook_id,
                        value.label_id,
                        add_source_id=source_id,
                    ),
                    operation=Operation.LABEL_UPDATE,
                    deadline=deadline,
                    source_path=source_path,
                    allow_null=True,
                    operation_variant="add_sources",  # -> NON_IDEMPOTENT_NO_RETRY
                    outcome_unknown_on_expiry=write_may_have_committed,
                )
                write_may_have_committed = True
            for source_id in value.remove_member_ids:
                await self._rpc_call(
                    RPCMethod.UPDATE_LABEL,
                    build_update_label_params(
                        notebook_id,
                        value.label_id,
                        remove_source_id=source_id,
                    ),
                    operation=Operation.LABEL_UPDATE,
                    deadline=deadline,
                    source_path=source_path,
                    allow_null=True,
                    operation_variant="remove_sources",  # -> IDEMPOTENT_SET_OP
                    outcome_unknown_on_expiry=write_may_have_committed,
                )
                write_may_have_committed = True
            return LabelUpdateResult(
                label=await self._label_membership_readback(
                    value,
                    notebook_id=notebook_id,
                    kind=LabelKind.SOURCE_LABEL,
                    operation=Operation.LABEL_UPDATE,
                    deadline=deadline,
                )
            )

        current = await self._label_update_preflight(
            value,
            notebook_id=notebook_id,
            kind=LabelKind.SOURCE_LABEL,
            operation=Operation.LABEL_UPDATE,
            deadline=deadline,
        )
        await self._rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_update_label_params(
                notebook_id,
                value.label_id,
                name=value.name,
                emoji=self._effective_emoji(value, current),
            ),
            operation=Operation.LABEL_UPDATE,
            deadline=deadline,
            source_path=source_path,
            allow_null=True,
            operation_variant=None,  # default IDEMPOTENT_SET_OP (not "add_sources")
        )
        return LabelUpdateResult(
            label=await self._label_field_readback(
                value,
                notebook_id=notebook_id,
                kind=LabelKind.SOURCE_LABEL,
                operation=Operation.LABEL_UPDATE,
                deadline=deadline,
            )
        )

    async def _collection_update(
        self,
        value: LabelUpdateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> LabelUpdateResult:
        """Apply one collection rename or notebook-membership mutation.

        Same per-id set-op choreography as the source-label dialect, on the
        account path and with the collection field masks.
        """
        self._require_label_kind(value.kind, LabelKind.COLLECTION, Operation.COLLECTION_UPDATE)
        if value.add_member_ids or value.remove_member_ids:
            write_may_have_committed = False
            for notebook_member_id in value.add_member_ids:
                await self._rpc_call(
                    RPCMethod.UPDATE_LABEL,
                    build_update_collection_notebooks_params(
                        value.label_id,
                        add_notebook_id=notebook_member_id,
                    ),
                    operation=Operation.COLLECTION_UPDATE,
                    deadline=deadline,
                    source_path=_ACCOUNT_PATH,
                    allow_null=True,
                    operation_variant="add_notebooks",  # -> NON_IDEMPOTENT_NO_RETRY
                    outcome_unknown_on_expiry=write_may_have_committed,
                )
                write_may_have_committed = True
            for notebook_member_id in value.remove_member_ids:
                await self._rpc_call(
                    RPCMethod.UPDATE_LABEL,
                    build_update_collection_notebooks_params(
                        value.label_id,
                        remove_notebook_id=notebook_member_id,
                    ),
                    operation=Operation.COLLECTION_UPDATE,
                    deadline=deadline,
                    source_path=_ACCOUNT_PATH,
                    allow_null=True,
                    operation_variant="remove_notebooks",  # -> IDEMPOTENT_SET_OP
                    outcome_unknown_on_expiry=write_may_have_committed,
                )
                write_may_have_committed = True
            return LabelUpdateResult(
                label=await self._label_membership_readback(
                    value,
                    notebook_id=None,
                    kind=LabelKind.COLLECTION,
                    operation=Operation.COLLECTION_UPDATE,
                    deadline=deadline,
                )
            )

        if value.name is None:
            raise BackendContractError(
                "collection.update has no emoji-only field mask; a name is required",
                operation=Operation.COLLECTION_UPDATE,
            )
        current = await self._label_update_preflight(
            value,
            notebook_id=None,
            kind=LabelKind.COLLECTION,
            operation=Operation.COLLECTION_UPDATE,
            deadline=deadline,
        )
        await self._rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_rename_collection_params(
                value.label_id,
                value.name,
                self._effective_emoji(value, current),
            ),
            operation=Operation.COLLECTION_UPDATE,
            deadline=deadline,
            source_path=_ACCOUNT_PATH,
            allow_null=True,
            operation_variant=None,  # default IDEMPOTENT_SET_OP (rename/set)
        )
        return LabelUpdateResult(
            label=await self._label_field_readback(
                value,
                notebook_id=None,
                kind=LabelKind.COLLECTION,
                operation=Operation.COLLECTION_UPDATE,
                deadline=deadline,
            )
        )

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

    async def _label_update_preflight(
        self,
        value: LabelUpdateInput,
        *,
        notebook_id: str | None,
        kind: LabelKind,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> LabelRecord:
        """Read the current group before a field mask that must preserve fields."""
        current = await self._label_set_get(
            LabelGetInput(kind, value.label_id, notebook_id),
            kind=kind,
            operation=operation,
            deadline=deadline,
        )
        if current.label is None:
            raise self._label_not_found(
                kind=kind,
                label_id=value.label_id,
                notebook_id=notebook_id,
                operation=operation,
                method_id=RPCMethod.UPDATE_LABEL.value,
            )
        return current.label

    async def _label_field_readback(
        self,
        value: LabelUpdateInput,
        *,
        notebook_id: str | None,
        kind: LabelKind,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> LabelRecord | None:
        """Read a field mutation back only when the caller asked for the object.

        The existence contract is already satisfied by the preflight, so a
        ``return_object=False`` field mutation performs no second read.
        """
        if not value.return_object:
            return None
        updated = await self._label_set_get(
            LabelGetInput(kind, value.label_id, notebook_id),
            kind=kind,
            operation=operation,
            deadline=deadline,
            outcome_unknown_on_expiry=True,
        )
        if updated.label is None:
            raise self._label_not_found(
                kind=kind,
                label_id=value.label_id,
                notebook_id=notebook_id,
                operation=operation,
                method_id=RPCMethod.LIST_LABELS.value,
            )
        return updated.label

    async def _label_membership_readback(
        self,
        value: LabelUpdateInput,
        *,
        notebook_id: str | None,
        kind: LabelKind,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> LabelRecord | None:
        """Re-read after membership writes; the contract needs it either way.

        ``le8sX`` echoes ``[]`` and carries no group, so this single read is the
        only evidence the target exists. It is NOT removable: the not-found
        contract must hold even when ``return_object`` is false.
        """
        current = await self._label_set_get(
            LabelGetInput(kind, value.label_id, notebook_id),
            kind=kind,
            operation=operation,
            deadline=deadline,
            outcome_unknown_on_expiry=True,
        )
        if current.label is None:
            raise self._label_not_found(
                kind=kind,
                label_id=value.label_id,
                notebook_id=notebook_id,
                operation=operation,
                method_id=RPCMethod.UPDATE_LABEL.value,
            )
        return current.label if value.return_object else None


__all__ = ["LabelSetWebHandlers"]
