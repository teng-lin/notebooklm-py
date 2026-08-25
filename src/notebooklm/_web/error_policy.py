"""Closed native-to-semantic error policy for the web backend."""

from .._backend import BackendErrorReason, BackendStatus
from ..exceptions import (
    AuthError,
    ChatError,
    ChatResponseParseError,
    ClientError,
    DecodingError,
    IdempotencyVariantError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
    UnknownRPCMethodError,
)
from ..rpc import GrpcStatusCode, normalize_grpc_status

WEB_ERROR_REASONS: dict[type[object], BackendErrorReason] = {
    AuthError: BackendErrorReason.AUTH,
    ChatError: BackendErrorReason.CHAT,
    ChatResponseParseError: BackendErrorReason.CHAT_RESPONSE_PARSE,
    ClientError: BackendErrorReason.CLIENT,
    DecodingError: BackendErrorReason.DECODING,
    IdempotencyVariantError: BackendErrorReason.IDEMPOTENCY_VARIANT,
    NetworkError: BackendErrorReason.NETWORK,
    RateLimitError: BackendErrorReason.RATE_LIMIT,
    RPCResponseTooLargeError: BackendErrorReason.RESPONSE_TOO_LARGE,
    RPCError: BackendErrorReason.RPC,
    ServerError: BackendErrorReason.SERVER,
    RPCTimeoutError: BackendErrorReason.TIMEOUT,
    UnknownRPCMethodError: BackendErrorReason.UNKNOWN_RPC_METHOD,
}

#: Closed gRPC-status to neutral-status table. Only statuses a semantic service
#: branches on appear here; every other code normalizes to ``None`` and reaches
#: services as an absent diagnostic rather than as an unnamed member. Keyed on
#: :class:`~notebooklm.rpc.GrpcStatusCode` so the raw ``rpc_code`` (an int, a
#: name, or ``None``) is normalized exactly once, on the adapter's side of the
#: port — the service never sees a wire status code at all.
WEB_BACKEND_STATUSES: dict[GrpcStatusCode, BackendStatus] = {
    GrpcStatusCode.FAILED_PRECONDITION: BackendStatus.FAILED_PRECONDITION,
}


def web_backend_status(rpc_code: str | int | None) -> BackendStatus | None:
    """Normalize one native ``rpc_code`` into the neutral status vocabulary."""
    grpc_status = normalize_grpc_status(rpc_code)
    if grpc_status is None:
        return None
    return WEB_BACKEND_STATUSES.get(grpc_status)


SAFE_REASON_DIAGNOSTICS: dict[BackendErrorReason, tuple[str, ...]] = {
    BackendErrorReason.AUTH: ("recoverable",),
    BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE: (
        "artifact_type",
        "method_id",
        "raw_response",
    ),
    BackendErrorReason.ARTIFACT_NOT_FOUND: (
        "artifact_id",
        "artifact_type",
        "method_id",
        "raw_response",
    ),
    BackendErrorReason.CHAT: (),
    BackendErrorReason.CHAT_RESPONSE_PARSE: (),
    BackendErrorReason.CLIENT: ("status_code",),
    BackendErrorReason.DECODING: (),
    BackendErrorReason.IDEMPOTENCY_VARIANT: (),
    BackendErrorReason.NETWORK: (),
    BackendErrorReason.NOT_FOUND: ("status_code",),
    BackendErrorReason.RATE_LIMIT: ("retry_after",),
    BackendErrorReason.RESPONSE_TOO_LARGE: ("limit_bytes", "bytes_read"),
    BackendErrorReason.RPC: (),
    BackendErrorReason.SERVER: ("status_code",),
    BackendErrorReason.TIMEOUT: ("timeout_seconds",),
    BackendErrorReason.UNKNOWN_RPC_METHOD: ("path", "source", "data_at_failure"),
}

__all__ = [
    "SAFE_REASON_DIAGNOSTICS",
    "WEB_BACKEND_STATUSES",
    "WEB_ERROR_REASONS",
    "web_backend_status",
]
