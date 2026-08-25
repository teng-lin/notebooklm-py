"""Notebook codec rows (P9.3 notebook reads domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``NOTEBOOK_LIST`` is the non-uniform row: its decoder accepts the empty,
``[None]`` and ``[[rows]]`` payload shapes.  ``NOTEBOOK_GET`` needs the input to
select its source-id-only branch.

``NOTEBOOK_CREATE`` and ``NOTEBOOK_UPDATE`` are :class:`CustomBinding` rows
(P9.4b): the create row declares its ``list``/``create``/``limits`` specs and
sequences snapshot → guarded create → probe/reconcile (plus the quota-limit
diagnosis) through the row-scoped invoker exactly as the P2 handler did; the
update row declares ``mutate``/``readback``.  Both are *deferred-product* rows
— gate table §4 orders their hoists as P9.2-11/12, after the stop/go review.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType

from ..._backend import BackendError, BackendErrorReason, mark_backend_outcome_unknown
from ..._binding import (
    Binding,
    CodecBinding,
    CodecPayload,
    CustomBinding,
    NativeCallSpec,
    RowInvoker,
)
from ..._deadline import RuntimeDeadline
from ..._idempotency import idempotent_create, mark_unconfirmed, transport_may_have_committed
from ..._operations import Operation
from ..._records import (
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_SUMMARIZE_DEF,
    NOTEBOOK_UPDATE_DEF,
    NotebookCreateInput,
    NotebookCreateResult,
    NotebookRecord,
    NotebookUpdateInput,
    NotebookUpdateResult,
)
from ...exceptions import (
    AuthError,
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
)
from ...rpc import GrpcStatusCode, RPCMethod, normalize_grpc_status
from ..codec import notebooks as notebooks_codec
from ..codec import settings as settings_codec
from ..errors import error_diagnostics, translate_web_error

notebook_logger = logging.getLogger("notebooklm._notebooks")

_CREATE_NOTEBOOK_QUOTA_RPC_CODE = 3

NOTEBOOK_LIST = CodecBinding(
    definition=NOTEBOOK_LIST_DEF,
    encode=notebooks_codec.encode_notebook_list,
    decode=notebooks_codec.decode_notebook_list,
    native=NativeCallSpec.constant(RPCMethod.LIST_NOTEBOOKS),
)

NOTEBOOK_GET = CodecBinding(
    definition=NOTEBOOK_GET_DEF,
    encode=notebooks_codec.encode_notebook_get,
    decode=notebooks_codec.decode_notebook_get,
    native=NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK),
)

NOTEBOOK_DELETE = CodecBinding(
    definition=NOTEBOOK_DELETE_DEF,
    encode=notebooks_codec.encode_notebook_delete,
    decode=notebooks_codec.decode_notebook_delete,
    native=NativeCallSpec.constant(RPCMethod.DELETE_NOTEBOOK),
)

NOTEBOOK_REMOVE_RECENT = CodecBinding(
    definition=NOTEBOOK_REMOVE_RECENT_DEF,
    encode=notebooks_codec.encode_notebook_remove_recent,
    decode=notebooks_codec.decode_notebook_remove_recent,
    native=NativeCallSpec.constant(RPCMethod.REMOVE_RECENTLY_VIEWED),
)

NOTEBOOK_SUMMARIZE = CodecBinding(
    definition=NOTEBOOK_SUMMARIZE_DEF,
    encode=notebooks_codec.encode_notebook_guide_request,
    decode=notebooks_codec.decode_notebook_guide,
    native=NativeCallSpec.constant(RPCMethod.SUMMARIZE),
)

NOTEBOOK_DESCRIBE = CodecBinding(
    definition=NOTEBOOK_DESCRIBE_DEF,
    encode=notebooks_codec.encode_notebook_guide_request,
    decode=notebooks_codec.decode_notebook_guide,
    native=NativeCallSpec.constant(RPCMethod.SUMMARIZE),
)


# --- custom rows (P9.4b) ---------------------------------------------------------

_LIST = "list"
_CREATE = "create"
_LIMITS = "limits"
_MUTATE = "mutate"
_READBACK = "readback"


async def _notebook_create(
    value: NotebookCreateInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> NotebookCreateResult:
    """Snapshot → guarded create → probe/reconcile, with the quota-limit diagnosis."""

    async def snapshot() -> tuple[NotebookRecord, ...]:
        raw = await invoke.call(
            _LIST, notebooks_codec.encode_notebook_snapshot(), deadline=deadline
        )
        return notebooks_codec.decode_notebook_list_result(raw).notebooks

    async def limit_error(error: RPCError) -> BackendError | None:
        if (
            error.method_id != RPCMethod.CREATE_NOTEBOOK.value
            or error.rpc_code != _CREATE_NOTEBOOK_QUOTA_RPC_CODE
        ):
            return None
        try:
            settings = await invoke.call(
                _LIMITS,
                CodecPayload(params=settings_codec.encode_get_user_settings(), source_path="/"),
                deadline=deadline,
            )
            limit = settings_codec.decode_account_limits(settings).notebook_limit
        except Exception:
            notebook_logger.debug(
                "Could not fetch account limits after CREATE_NOTEBOOK failure; "
                "leaving original RPC error unchanged",
                exc_info=True,
            )
            return None
        if limit is None:
            return None
        try:
            listed = await snapshot()
        except Exception:
            notebook_logger.debug(
                "Could not list notebooks after CREATE_NOTEBOOK failure; "
                "leaving original RPC error unchanged",
                exc_info=True,
            )
            return None
        owned_count = sum(1 for notebook in listed if notebook.is_owner)
        if owned_count < max(limit - 1, 0):
            return None
        original = translate_web_error(Operation.NOTEBOOK_CREATE, error)
        return BackendError(
            message="notebook limit reached",
            operation=Operation.NOTEBOOK_CREATE,
            diagnostics=MappingProxyType(
                {
                    "current_count": owned_count,
                    "limit": limit,
                    "original_message": original.message,
                    "original_reason": original.reason.value
                    if original.reason is not None
                    else None,
                    "original_diagnostics": dict(original.diagnostics or {}),
                }
            ),
            reason=BackendErrorReason.NOTEBOOK_LIMIT,
        )

    baseline_ids: set[str] | None
    baseline_error: Exception | None = None
    try:
        baseline_ids = {notebook.id for notebook in await snapshot()}
    except Exception as exc:
        baseline_ids = None
        baseline_error = exc
        notebook_logger.warning(
            "create: baseline list() failed (%s); the idempotency probe can no "
            "longer tell a notebook this call created from one that was already "
            "there, so a transport failure will surface as an ambiguity error "
            "instead of recovering",
            type(exc).__name__,
            exc_info=True,
        )

    async def create() -> NotebookRecord:
        try:
            result = await invoke.call(
                _CREATE,
                notebooks_codec.encode_notebook_create(value),
                deadline=deadline,
                disable_internal_retries=True,
            )
        except RPCError as exc:
            limit = await limit_error(exc)
            if limit is not None:
                raise limit from None
            raise
        return notebooks_codec.decode_notebook(result)

    async def probe() -> NotebookRecord | None:
        try:
            current = await snapshot()
        except RPCTimeoutError:
            # The outer semantic dispatch owns timeout translation. Let it
            # retain notebook.create attribution and mark the post-write
            # reconciliation outcome unknown.
            raise
        except (AuthError, RateLimitError, ServerError, NetworkError) as exc:
            notebook_logger.warning(
                "create: probe list() failed with transport/auth error; "
                "propagating so the caller can avoid a duplicate-resource retry"
            )
            mark_unconfirmed(exc)
            raise
        except BackendError as exc:
            raise mark_backend_outcome_unknown(exc) from exc
        except Exception as exc:
            notebook_logger.warning(
                "create: probe list() failed with a non-transport error (%s); the "
                "create cannot be confirmed, so it will not be retried",
                type(exc).__name__,
                exc_info=True,
            )
            raise mark_unconfirmed(
                RPCError(
                    "UNRESOLVED — do not blindly retry; check your notebook list "
                    f"first. Cannot confirm notebook with title {value.title!r}: the "
                    "create failed at the transport level and may or may not have "
                    "committed, and the idempotency probe that would settle it "
                    f"failed too ({type(exc).__name__}). No FURTHER attempt was made.",
                    method_id=RPCMethod.CREATE_NOTEBOOK.value,
                )
            ) from exc
        matches = tuple(notebook for notebook in current if notebook.title == value.title)
        if baseline_ids is not None:
            matches = tuple(notebook for notebook in matches if notebook.id not in baseline_ids)
        elif matches:
            raise mark_unconfirmed(
                RPCError(
                    f"Cannot disambiguate notebook with title {value.title!r} — check your "
                    "notebook list before retrying: the pre-create baseline snapshot failed "
                    f"({type(baseline_error).__name__}), so "
                    f"{', '.join(f'{item.id} ({item.title!r})' for item in matches)} may "
                    "either predate this create or be the notebook it just created.",
                    method_id=RPCMethod.CREATE_NOTEBOOK.value,
                )
            )
        if len(matches) == 1:
            return next(iter(matches))
        if len(matches) > 1:
            raise mark_unconfirmed(
                RPCError(
                    f"Cannot disambiguate notebook with title {value.title!r}: "
                    f"probe found {len(matches)} new notebooks with this title",
                    method_id=RPCMethod.CREATE_NOTEBOOK.value,
                )
            )
        return None

    result = await idempotent_create(
        create,
        probe,
        may_have_committed=transport_may_have_committed,
        label=f"notebook.create[{value.title!r}]",
    )
    return NotebookCreateResult(notebook=result.value)


async def _notebook_update(
    value: NotebookUpdateInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> NotebookUpdateResult:
    """Property mutation, then one unconditional readback (recency contract)."""
    await invoke.call(
        _MUTATE, notebooks_codec.encode_notebook_update_mutation(value), deadline=deadline
    )
    try:
        result = await invoke.call(
            _READBACK,
            notebooks_codec.encode_notebook_update_readback(value),
            deadline=deadline,
            outcome_unknown_on_expiry=True,
        )
    except ClientError as exc:
        if normalize_grpc_status(exc.rpc_code) is not GrpcStatusCode.NOT_FOUND:
            raise
        diagnostics = dict(error_diagnostics(exc, BackendErrorReason.CLIENT))
        diagnostics.update(
            {
                "detail": str(exc),
                "original_message": str(exc.args[0]) if exc.args else str(exc),
            }
        )
        raise notebooks_codec.notebook_update_not_found(value, diagnostics) from exc
    return notebooks_codec.decode_notebook_update_readback(value, result)


NOTEBOOK_CREATE = CustomBinding(
    definition=NOTEBOOK_CREATE_DEF,
    handler=_notebook_create,
    native=(
        NativeCallSpec.constant(RPCMethod.LIST_NOTEBOOKS, key=_LIST),
        NativeCallSpec.constant(RPCMethod.CREATE_NOTEBOOK, key=_CREATE),
        NativeCallSpec.constant(RPCMethod.GET_USER_SETTINGS, key=_LIMITS),
    ),
    justification="Hoist candidate P9.2-12 per gate table §4; awaits the stop/go review.",
    category="deferred-product",
)

NOTEBOOK_UPDATE = CustomBinding(
    definition=NOTEBOOK_UPDATE_DEF,
    handler=_notebook_update,
    native=(
        NativeCallSpec.constant(RPCMethod.RENAME_NOTEBOOK, key=_MUTATE),
        NativeCallSpec.constant(RPCMethod.GET_NOTEBOOK, key=_READBACK),
    ),
    justification="Hoist candidate P9.2-11 per gate table §4; awaits the stop/go review.",
    category="deferred-product",
)

NOTEBOOK_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        NOTEBOOK_LIST.definition.key: NOTEBOOK_LIST,
        NOTEBOOK_GET.definition.key: NOTEBOOK_GET,
        NOTEBOOK_DELETE.definition.key: NOTEBOOK_DELETE,
        NOTEBOOK_REMOVE_RECENT.definition.key: NOTEBOOK_REMOVE_RECENT,
        NOTEBOOK_SUMMARIZE.definition.key: NOTEBOOK_SUMMARIZE,
        NOTEBOOK_DESCRIBE.definition.key: NOTEBOOK_DESCRIBE,
        NOTEBOOK_CREATE.definition.key: NOTEBOOK_CREATE,
        NOTEBOOK_UPDATE.definition.key: NOTEBOOK_UPDATE,
    }
)

__all__ = [
    "NOTEBOOK_CREATE",
    "NOTEBOOK_DELETE",
    "NOTEBOOK_DESCRIBE",
    "NOTEBOOK_GET",
    "NOTEBOOK_LIST",
    "NOTEBOOK_REMOVE_RECENT",
    "NOTEBOOK_ROWS",
    "NOTEBOOK_SUMMARIZE",
    "NOTEBOOK_UPDATE",
]
