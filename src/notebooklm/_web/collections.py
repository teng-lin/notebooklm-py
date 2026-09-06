"""Public API for NotebookLM collections (``client.collections``).

Account-level sibling of ``client.labels``: a *collection* groups whole
notebooks (playlist-style) rather than sources within one notebook. On the wire
a collection is a source-``Label`` of type ``3`` with a null notebook parent, so
this API reuses the four label RPCs (``LIST_LABELS`` / ``CREATE_LABEL`` /
``UPDATE_LABEL`` / ``DELETE_LABEL``) via the collection-specific param builders
in :mod:`notebooklm._web.params.collections`, always with ``source_path="/"`` (the
account/home context — collections have no notebook scope).

Like ``WebLabelsAPI`` it takes a narrow ``list_notebooks`` callable
(``client.notebooks.list``) — wired in ``_client_assembly.py`` after
``NotebooksAPI`` is built — for the membership→``Notebook`` join in
``notebooks()``.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Literal

from .._collections import CollectionsAPI, ListNotebooks
from .._idempotency import (
    OperationJournal,
    attach_operation_journal,
    bind_operation_journal_entries,
    reconciliation_report,
)
from ..exceptions import CollectionError, NotebookLMError, UnknownRPCMethodError
from ..outcomes import CommitState, RecoveryAction
from ..rpc import RPCMethod
from ..types import Collection
from .contracts import RpcCaller
from .params.collections import (
    build_create_collection_params,
    build_delete_collections_params,
    build_list_collections_params,
    build_rename_collection_params,
    build_update_collection_notebooks_params,
)
from .rows.collections import decode_collection

if TYPE_CHECKING:
    from .._runtime.call_supervisor import CallSupervisor, OperationLease

# Preserve the historical logger key across the whole-module move.
logger = logging.getLogger("notebooklm._collections")

_SRC = "_collections"

# Collections are account-level: every RPC uses the home-page source path, not a
# ``/notebook/<id>`` path (they have no notebook scope). Compare the home-page
# mutation context in ``_web/notebooks.py::WebNotebooksAPI.update``.
_ACCOUNT_PATH = "/"


class WebCollectionsAPI(CollectionsAPI):
    """Operations on NotebookLM collections (``client.collections``).

    Usage::

        async with NotebookLMClient.from_storage() as client:
            coll = await client.collections.create("Research Q3")
            await client.collections.add_notebooks(coll.id, [nb_id])
            members = await client.collections.notebooks(coll.id)  # -> [Notebook]
            await client.collections.rename(coll.id, "Research Q4")
            await client.collections.delete(coll.id)
    """

    _list_method_id = RPCMethod.LIST_LABELS.value
    _mutation_method_id = RPCMethod.UPDATE_LABEL.value
    _property_readback_miss_method_id = RPCMethod.LIST_LABELS.value
    _delete_method_id = RPCMethod.DELETE_LABEL.value

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease]:
        """Keep neutral collection workflows under the Web supervisor."""
        return self._supervisor.operation_scope(label)

    def __init__(
        self,
        rpc: RpcCaller,
        *,
        list_notebooks: ListNotebooks,
        supervisor: CallSupervisor,
    ) -> None:
        """``list_notebooks`` is ``client.notebooks.list`` (wired in
        ``_client_assembly.py`` after ``NotebooksAPI`` is constructed) — needed
        for the membership→``Notebook`` join in ``notebooks()``. Same client /
        bound loop, so no loop-affinity concern (ADR-0004)."""
        super().__init__(list_notebooks=list_notebooks)
        self._rpc = rpc
        self._supervisor = supervisor

    # -- internal -----------------------------------------------------------

    def _collections_from_envelope(
        self, result: Any, *, method_id: str, index: int
    ) -> builtins.list[Collection]:
        """Map a collection-set envelope to ``Collection`` objects.

        Collections' ``LIST`` echoes ``[None, [collection, ...]]`` (``index=1``)
        — a leading-null wrapper, unlike source labels' ``LIST_LABELS`` which
        echoes ``[[label, ...]]`` (``index=0``). An empty/absent set decodes to
        ``[]``; a present-but-malformed envelope raises.
        """
        if not result:
            return []
        if not isinstance(result, list):
            raise UnknownRPCMethodError(
                message="collection set envelope is not a list",
                method_id=method_id,
                source=_SRC,
            )
        raw = result[index] if len(result) > index else None
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise UnknownRPCMethodError(
                message="collection set envelope malformed",
                method_id=method_id,
                source=_SRC,
            )
        return [decode_collection(Collection, tuple_, method_id=method_id) for tuple_ in raw]

    # -- read ---------------------------------------------------------------

    async def list(self) -> builtins.list[Collection]:
        """List all collections in the account (``LIST_LABELS``, type 3).

        ``allow_null=True`` because an account with zero collections may echo a
        null envelope, which decodes to ``[]`` rather than raising.
        """
        result = await self._rpc.rpc_call(
            RPCMethod.LIST_LABELS,
            build_list_collections_params(),
            source_path=_ACCOUNT_PATH,
            allow_null=True,
        )
        return self._collections_from_envelope(
            result, method_id=RPCMethod.LIST_LABELS.value, index=1
        )

    # -- create -------------------------------------------------------------

    async def create(self, name: str) -> Collection:
        """Create an empty, named collection (``CREATE_LABEL``, type 3).

        The create response and subsequent list are cumulative collection sets,
        not a correlated created-resource response. The method therefore sends
        once and raises ``CollectionError`` with bounded inspection candidates;
        it never attributes another writer's row to this call.

        Collections carry no emoji at creation (the wire has no emoji slot); set
        one later in the UI if desired.
        """
        async with self._operation_scope("collections.create"):
            before_ids = {collection.id for collection in await self.list()}

            journal = OperationJournal("collections.create")
            invocation_id = journal.invocation_id()
            mutation_entry = journal.new_entry(
                method=RPCMethod.CREATE_LABEL.value,
                phase="mutation",
                invocation_id=invocation_id,
            )
            readback_entry = journal.new_entry(
                method=RPCMethod.LIST_LABELS.value,
                phase="readback",
                invocation_id=invocation_id,
            )
            try:
                with bind_operation_journal_entries(mutation_entry):
                    await self._rpc.rpc_call(
                        RPCMethod.CREATE_LABEL,
                        build_create_collection_params(name),
                        source_path=_ACCOUNT_PATH,
                        allow_null=True,
                        # #2290: a status-tagged null is a server rejection, not an empty success.
                        raise_on_null_status=True,
                    )
            except asyncio.CancelledError as exc:
                attach_operation_journal(
                    exc,
                    journal,
                    primary=mutation_entry,
                    recovery_action=(
                        RecoveryAction.RETRY
                        if mutation_entry.commit_state is CommitState.NOT_SENT
                        else RecoveryAction.INSPECT_AND_RECONCILE
                    ),
                )
                raise
            except NotebookLMError as exc:
                attach_operation_journal(exc, journal, primary=mutation_entry)
                raise
            mutation_entry.record(CommitState.CONFIRMED, "decoded collection-set response")
            mutation_entry.recovery_action = RecoveryAction.INSPECT_AND_RECONCILE
            try:
                with bind_operation_journal_entries(readback_entry):
                    result = await self._rpc.rpc_call(
                        RPCMethod.LIST_LABELS,
                        build_list_collections_params(),
                        source_path=_ACCOUNT_PATH,
                        allow_null=True,
                    )
                after = self._collections_from_envelope(
                    result,
                    method_id=RPCMethod.LIST_LABELS.value,
                    index=1,
                )
                readback_entry.record(CommitState.CONFIRMED, "decoded collection readback")
            except asyncio.CancelledError as exc:
                attach_operation_journal(
                    exc,
                    journal,
                    primary=mutation_entry,
                    recovery_action=RecoveryAction.INSPECT_AND_RECONCILE,
                )
                raise
            except NotebookLMError as exc:
                attach_operation_journal(
                    exc,
                    journal,
                    primary=mutation_entry,
                    recovery_action=RecoveryAction.INSPECT_AND_RECONCILE,
                )
                raise
            new = [collection for collection in after if collection.id not in before_ids]
            error = CollectionError(
                f"Collection create for {name!r} returned no caller-correlated id. "
                "Inspect the collection list before creating again."
            )
            mutation_entry.reconciliation = reconciliation_report(
                [collection.id for collection in new],
                [name],
                reason="create response and readback do not correlate a collection id",
            )
            raise attach_operation_journal(
                error,
                journal,
                primary=mutation_entry,
                recovery_action=RecoveryAction.INSPECT_AND_RECONCILE,
            )

    # -- mutate (all UPDATE_LABEL) ------------------------------------------

    async def _send_update(
        self,
        operation: Literal["properties", "delete"],
        collection_ids: builtins.list[str],
        *,
        name: str | None = None,
        current: Collection | None = None,
    ) -> None:
        if operation == "delete":
            await self._rpc.rpc_call(
                RPCMethod.DELETE_LABEL,
                build_delete_collections_params(collection_ids),
                source_path=_ACCOUNT_PATH,
                allow_null=True,
            )
            return
        (collection_id,) = collection_ids
        assert name is not None
        assert current is not None
        await self._rpc.rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_rename_collection_params(collection_id, name, current.emoji or ""),
            source_path=_ACCOUNT_PATH,
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
            operation_variant=None,  # default IDEMPOTENT_SET_OP (rename/set)
        )

    async def _send_mutate_member(
        self,
        collection_id: str,
        notebook_id: str,
        *,
        operation: Literal["add_notebooks", "remove_notebooks"],
    ) -> None:
        await self._rpc.rpc_call(
            RPCMethod.UPDATE_LABEL,
            build_update_collection_notebooks_params(
                collection_id,
                add_notebook_id=notebook_id if operation == "add_notebooks" else None,
                remove_notebook_id=notebook_id if operation == "remove_notebooks" else None,
            ),
            source_path=_ACCOUNT_PATH,
            allow_null=True,
            raise_on_null_status=True,
            operation_variant=operation,
        )


__all__ = ["WebCollectionsAPI"]
