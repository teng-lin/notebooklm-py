"""Closed native-to-semantic error policy for the web backend."""

from .._backend import BackendErrorReason
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

__all__ = ["SAFE_REASON_DIAGNOSTICS", "WEB_ERROR_REASONS"]
