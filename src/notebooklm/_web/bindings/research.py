"""Research codec rows (P9.3 research domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches.  ``RESEARCH_START`` is the
input-keyed row: one call per input, the native chosen from ``value.mode``
(``START_FAST_RESEARCH`` or ``START_DEEP_RESEARCH``), and its ``map_error``
reproduces the deep-start null-result translation the P6.2 handler owned —
a rejected deep start becomes the closed ``RESEARCH_START_UNAVAILABLE`` reason
with the shared translation of the rejecting native kept as evidence.
``RESEARCH_IMPORT`` inherits the caller's deadline and forwards the
service-computed ``attempt_timeout`` as a typed payload option (gate table §6).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._backend import BackendError, BackendErrorReason
from ..._binding import Binding, CodecBinding, NativeCallSpec, NativeChoice, RpcNative
from ..._operations import Operation
from ..._semantic.records import (
    RESEARCH_CANCEL_DEF,
    RESEARCH_IMPORT_DEF,
    RESEARCH_POLL_DEF,
    RESEARCH_START_DEF,
    ResearchMode,
    ResearchStartInput,
    ResearchStartResult,
)
from ...exceptions import AuthError, RateLimitError, RPCError, ServerError
from ...rpc import RPCMethod
from ..codec import research as research_codec
from ..errors import translate_web_error

_FAST_START = RpcNative(RPCMethod.START_FAST_RESEARCH)
_DEEP_START = RpcNative(RPCMethod.START_DEEP_RESEARCH)


def _select_start(value: ResearchStartInput) -> RpcNative[RPCMethod]:
    return _FAST_START if value.mode is ResearchMode.FAST else _DEEP_START


def _decode_start(value: ResearchStartInput, result: object) -> ResearchStartResult:
    return research_codec.decode_research_start(result, method_id=_select_start(value).method.value)


def _is_deep_start_null_result_error(exc: RPCError) -> bool:
    """Whether a deep-start RPCError is the decoder's null-payload frame."""
    method_id = _DEEP_START.method.value
    null_result_markers = ("rejected this request", "returned an empty result")
    return (
        exc.method_id == method_id
        and method_id in exc.found_ids
        and any(marker in str(exc).lower() for marker in null_result_markers)
    )


def _map_start_error(
    value: ResearchStartInput,
    raw: Exception,
    native: NativeChoice[RPCMethod],
) -> BackendError | None:
    """Translate a rejected deep start; every other failure keeps the shared path."""
    del native
    if isinstance(raw, (AuthError, RateLimitError, ServerError)) or not isinstance(raw, RPCError):
        return None
    if value.mode is not ResearchMode.DEEP or not _is_deep_start_null_result_error(raw):
        return None
    original = translate_web_error(Operation.RESEARCH_START, raw)
    return BackendError(
        message="research start returned no run",
        operation=Operation.RESEARCH_START,
        diagnostics=MappingProxyType(
            {
                "notebook_id": value.notebook_id,
                "mode": value.mode.value,
                "original_message": original.message,
                "original_reason": (original.reason.value if original.reason is not None else None),
                "original_diagnostics": dict(original.diagnostics or {}),
            }
        ),
        reason=BackendErrorReason.RESEARCH_START_UNAVAILABLE,
    )


RESEARCH_START = CodecBinding(
    definition=RESEARCH_START_DEF,
    encode=research_codec.encode_research_start,
    decode=_decode_start,
    native=NativeCallSpec.keyed(_select_start, _FAST_START, _DEEP_START),
    map_error=_map_start_error,
)

RESEARCH_POLL = CodecBinding(
    definition=RESEARCH_POLL_DEF,
    encode=research_codec.encode_research_poll,
    decode=research_codec.decode_research_poll,
    native=NativeCallSpec.constant(RPCMethod.POLL_RESEARCH),
)

RESEARCH_CANCEL = CodecBinding(
    definition=RESEARCH_CANCEL_DEF,
    encode=research_codec.encode_research_cancel,
    decode=research_codec.decode_research_cancel,
    native=NativeCallSpec.constant(RPCMethod.CANCEL_RESEARCH),
)

RESEARCH_IMPORT = CodecBinding(
    definition=RESEARCH_IMPORT_DEF,
    encode=research_codec.encode_research_import,
    decode=research_codec.decode_research_import,
    native=NativeCallSpec.constant(RPCMethod.IMPORT_RESEARCH),
)

RESEARCH_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        RESEARCH_START.definition.key: RESEARCH_START,
        RESEARCH_POLL.definition.key: RESEARCH_POLL,
        RESEARCH_CANCEL.definition.key: RESEARCH_CANCEL,
        RESEARCH_IMPORT.definition.key: RESEARCH_IMPORT,
    }
)

__all__ = [
    "RESEARCH_CANCEL",
    "RESEARCH_IMPORT",
    "RESEARCH_POLL",
    "RESEARCH_ROWS",
    "RESEARCH_START",
]
