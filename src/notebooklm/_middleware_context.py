"""Canonical ``RpcRequest.context`` key vocabulary for the middleware chain."""

from __future__ import annotations

from typing import Final

RPC_CONTEXT_RPC_METHOD: Final = "rpc_method"
RPC_CONTEXT_DISABLE_INTERNAL_RETRIES: Final = "disable_internal_retries"
RPC_CONTEXT_BUILD_REQUEST: Final = "build_request"
RPC_CONTEXT_LOG_LABEL: Final = "log_label"
RPC_CONTEXT_AUTH_SNAPSHOT: Final = "auth_snapshot"
RPC_CONTEXT_AUTH_REFRESHED: Final = "auth_refreshed"
RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS: Final = "rpc_queue_wait_seconds"

ALLOWED_RPC_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        RPC_CONTEXT_RPC_METHOD,
        RPC_CONTEXT_DISABLE_INTERNAL_RETRIES,
        RPC_CONTEXT_BUILD_REQUEST,
        RPC_CONTEXT_LOG_LABEL,
        RPC_CONTEXT_AUTH_SNAPSHOT,
        RPC_CONTEXT_AUTH_REFRESHED,
        RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS,
    }
)

__all__ = [
    "ALLOWED_RPC_CONTEXT_KEYS",
    "RPC_CONTEXT_AUTH_REFRESHED",
    "RPC_CONTEXT_AUTH_SNAPSHOT",
    "RPC_CONTEXT_BUILD_REQUEST",
    "RPC_CONTEXT_DISABLE_INTERNAL_RETRIES",
    "RPC_CONTEXT_LOG_LABEL",
    "RPC_CONTEXT_RPC_METHOD",
    "RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS",
]
