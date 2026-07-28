"""Canonical ``RpcRequest.context`` key vocabulary for the middleware chain."""

from __future__ import annotations

from typing import Final

RPC_CONTEXT_RPC_METHOD: Final = "rpc_method"
RPC_CONTEXT_DISABLE_INTERNAL_RETRIES: Final = "disable_internal_retries"
RPC_CONTEXT_BUILD_REQUEST: Final = "build_request"
RPC_CONTEXT_LOG_LABEL: Final = "log_label"
RPC_CONTEXT_READ_TIMEOUT: Final = "read_timeout"
RPC_CONTEXT_MAX_RESPONSE_BYTES: Final = "max_response_bytes"
RPC_CONTEXT_DISABLE_READ_TIMEOUT_RETRIES: Final = "disable_read_timeout_retries"
RPC_CONTEXT_AUTH_SNAPSHOT: Final = "auth_snapshot"
RPC_CONTEXT_AUTH_REFRESHED: Final = "auth_refreshed"
RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS: Final = "rpc_queue_wait_seconds"
# Optional :class:`notebooklm._auth_refresh_retry.RefreshBudget`. Seeded by
# ``RpcExecutor.rpc_call`` so the HTTP-status refresh layer
# (``AuthRefreshMiddleware``) and the decoded-RPC refresh layer
# (``RpcExecutor``) share ONE once-per-logical-call refresh allowance — a
# wire-401 + decoded-auth-error sequence then drives a single refresh
# (issue #1205). Absent for callers that drive the chain without a budget
# (e.g. the chat path), in which case ``AuthRefreshMiddleware`` falls back to
# the per-chain :data:`RPC_CONTEXT_AUTH_REFRESHED` boolean.
RPC_CONTEXT_REFRESH_BUDGET: Final = "refresh_budget"
# Optional :class:`notebooklm._deadline.RuntimeDeadline`. Seeded by
# ``RpcExecutor.rpc_call`` (via ``perform_authed_post``) so the chain's
# :class:`RetryMiddleware` INHERITS the logical call's aggregate retry
# deadline instead of minting a fresh one anchored at re-entry. On a
# decode-time auth-refresh retry the executor recurses
# ``rpc_call(_is_retry=True, _retry_deadline=...)``; without this key the
# re-entered ``RetryMiddleware`` would start a NEW deadline (T1 > T0) and the
# 429/5xx retry budget would restart, roughly doubling the wall-clock budget
# (issue #1873). Absent for callers that drive the chain without an aggregate
# deadline (e.g. the chat path), in which case ``RetryMiddleware`` falls back
# to ``_start_retry_deadline()``.
RPC_CONTEXT_RETRY_DEADLINE: Final = "retry_deadline"

ALLOWED_RPC_CONTEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        RPC_CONTEXT_RPC_METHOD,
        RPC_CONTEXT_DISABLE_INTERNAL_RETRIES,
        RPC_CONTEXT_BUILD_REQUEST,
        RPC_CONTEXT_LOG_LABEL,
        RPC_CONTEXT_READ_TIMEOUT,
        RPC_CONTEXT_MAX_RESPONSE_BYTES,
        RPC_CONTEXT_DISABLE_READ_TIMEOUT_RETRIES,
        RPC_CONTEXT_AUTH_SNAPSHOT,
        RPC_CONTEXT_AUTH_REFRESHED,
        RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS,
        RPC_CONTEXT_REFRESH_BUDGET,
        RPC_CONTEXT_RETRY_DEADLINE,
    }
)

__all__ = [
    "ALLOWED_RPC_CONTEXT_KEYS",
    "RPC_CONTEXT_AUTH_REFRESHED",
    "RPC_CONTEXT_AUTH_SNAPSHOT",
    "RPC_CONTEXT_BUILD_REQUEST",
    "RPC_CONTEXT_DISABLE_INTERNAL_RETRIES",
    "RPC_CONTEXT_LOG_LABEL",
    "RPC_CONTEXT_MAX_RESPONSE_BYTES",
    "RPC_CONTEXT_READ_TIMEOUT",
    "RPC_CONTEXT_DISABLE_READ_TIMEOUT_RETRIES",
    "RPC_CONTEXT_REFRESH_BUDGET",
    "RPC_CONTEXT_RETRY_DEADLINE",
    "RPC_CONTEXT_RPC_METHOD",
    "RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS",
]
