"""Notebook codec rows (P9.3 notebook reads domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``NOTEBOOK_LIST`` is the non-uniform row: its decoder accepts the empty,
``[None]`` and ``[[rows]]`` payload shapes. ``NOTEBOOK_GET`` needs the input to
select its source-id-only branch and exposes a required-readback mode whose
neutral not-found mapping is consumed only by the service-owned update workflow.
``NOTEBOOK_PATCH`` and guarded ``NOTEBOOK_ALLOCATE`` are the one-call mutation
primitives sequenced by the service-owned workflows.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._semantic.backend import BackendError, BackendErrorReason
from ..._semantic.binding import Binding, CodecBinding, NativeCallSpec, NativeChoice
from ..._semantic.operations import Operation
from ..._semantic.records import (
    NOTEBOOK_ALLOCATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_PATCH_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_SUMMARIZE_DEF,
    NotebookAllocateInput,
    NotebookGetInput,
)
from ...exceptions import ClientError, RPCError
from ...rpc import GrpcStatusCode, RPCMethod, normalize_grpc_status
from ..codec import notebooks as notebooks_codec
from ..errors import error_diagnostics, translate_web_error

_CREATE_NOTEBOOK_QUOTA_RPC_CODE = 3


def _map_required_get_not_found(
    value: NotebookGetInput,
    raw: Exception,
    native: NativeChoice[RPCMethod],
) -> BackendError | None:
    """Expose status-5 neutrally only for a mutation readback leaf."""
    del native
    if (
        not value.require_notebook
        or not isinstance(raw, ClientError)
        or normalize_grpc_status(raw.rpc_code) is not GrpcStatusCode.NOT_FOUND
    ):
        return None
    diagnostics = dict(error_diagnostics(raw, BackendErrorReason.CLIENT))
    diagnostics.update(
        {
            "notebook_id": value.notebook_id,
            "detail": str(raw),
            "original_message": str(raw.args[0]) if raw.args else str(raw),
        }
    )
    return BackendError(
        message=str(raw.args[0]) if raw.args else "",
        operation=Operation.NOTEBOOK_GET,
        outcome_unknown=bool(getattr(raw, "unconfirmed", False)),
        diagnostics=MappingProxyType(diagnostics),
        reason=BackendErrorReason.NOT_FOUND,
        dispatched=bool(getattr(raw, "dispatched", False)),
    )


def _map_allocate_quota_rejection(
    value: NotebookAllocateInput,
    raw: Exception,
    native: NativeChoice[RPCMethod],
) -> BackendError | None:
    """Tag only the guarded CREATE_NOTEBOOK invalid-argument quota signal."""
    del value, native
    if (
        not isinstance(raw, RPCError)
        or raw.method_id != RPCMethod.CREATE_NOTEBOOK.value
        or raw.rpc_code != _CREATE_NOTEBOOK_QUOTA_RPC_CODE
    ):
        return None
    original = translate_web_error(Operation.NOTEBOOK_ALLOCATE, raw)
    diagnostics = dict(original.diagnostics or {})
    diagnostics["quota_rejection"] = True
    return BackendError(
        message=original.message,
        operation=Operation.NOTEBOOK_ALLOCATE,
        outcome_unknown=original.outcome_unknown,
        diagnostics=MappingProxyType(diagnostics),
        reason=original.reason,
        dispatched=original.dispatched,
    )


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
    map_error=_map_required_get_not_found,
)

NOTEBOOK_PATCH = CodecBinding(
    definition=NOTEBOOK_PATCH_DEF,
    encode=notebooks_codec.encode_notebook_patch,
    decode=notebooks_codec.decode_notebook_patch,
    native=NativeCallSpec.constant(RPCMethod.RENAME_NOTEBOOK),
)

NOTEBOOK_ALLOCATE = CodecBinding(
    definition=NOTEBOOK_ALLOCATE_DEF,
    encode=notebooks_codec.encode_notebook_allocate,
    decode=notebooks_codec.decode_notebook_allocate,
    native=NativeCallSpec.constant(RPCMethod.CREATE_NOTEBOOK),
    map_error=_map_allocate_quota_rejection,
    forward_disable_internal_retries=True,
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

NOTEBOOK_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        NOTEBOOK_LIST.definition.key: NOTEBOOK_LIST,
        NOTEBOOK_GET.definition.key: NOTEBOOK_GET,
        NOTEBOOK_ALLOCATE.definition.key: NOTEBOOK_ALLOCATE,
        NOTEBOOK_PATCH.definition.key: NOTEBOOK_PATCH,
        NOTEBOOK_DELETE.definition.key: NOTEBOOK_DELETE,
        NOTEBOOK_REMOVE_RECENT.definition.key: NOTEBOOK_REMOVE_RECENT,
        NOTEBOOK_SUMMARIZE.definition.key: NOTEBOOK_SUMMARIZE,
        NOTEBOOK_DESCRIBE.definition.key: NOTEBOOK_DESCRIBE,
    }
)

__all__ = [
    "NOTEBOOK_ALLOCATE",
    "NOTEBOOK_DELETE",
    "NOTEBOOK_DESCRIBE",
    "NOTEBOOK_GET",
    "NOTEBOOK_LIST",
    "NOTEBOOK_PATCH",
    "NOTEBOOK_REMOVE_RECENT",
    "NOTEBOOK_ROWS",
    "NOTEBOOK_SUMMARIZE",
]
