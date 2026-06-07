"""Private HTTP-shaped middleware implementation package.

Cohesive cluster promoted from the former flat ``_middleware*.py`` modules (issue #1328).
The former ``_middleware.py`` envelope/chain primitive now lives in :mod:`._middleware.core`.
Re-exports the cluster's public names; importers may also reach submodules directly.
"""

from . import (
    auth_refresh,
    chain,
    chain_host,
    context,
    core,
    drain,
    error_injection,
    metrics,
    retry,
    semaphore,
    tracing,
)
from .auth_refresh import AuthRefreshMiddleware
from .chain import MiddlewareChainBuilder
from .chain_host import MiddlewareChainHost
from .context import (
    ALLOWED_RPC_CONTEXT_KEYS,
    RPC_CONTEXT_AUTH_REFRESHED,
    RPC_CONTEXT_AUTH_SNAPSHOT,
    RPC_CONTEXT_BUILD_REQUEST,
    RPC_CONTEXT_DISABLE_INTERNAL_RETRIES,
    RPC_CONTEXT_DISABLE_READ_TIMEOUT_RETRIES,
    RPC_CONTEXT_LOG_LABEL,
    RPC_CONTEXT_READ_TIMEOUT,
    RPC_CONTEXT_REFRESH_BUDGET,
    RPC_CONTEXT_RPC_METHOD,
    RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS,
)
from .core import (
    Middleware,
    NextCall,
    RpcRequest,
    RpcResponse,
    build_chain,
    materialize_rpc_request,
)
from .drain import DrainMiddleware
from .error_injection import ErrorInjectionMiddleware
from .metrics import MetricsMiddleware
from .retry import RetryMiddleware
from .semaphore import RPC_QUEUE_WAIT_CONTEXT_KEY, SemaphoreMiddleware
from .tracing import TracingMiddleware

__all__ = [
    "auth_refresh",
    "chain",
    "chain_host",
    "context",
    "core",
    "drain",
    "error_injection",
    "metrics",
    "retry",
    "semaphore",
    "tracing",
    "AuthRefreshMiddleware",
    "MiddlewareChainBuilder",
    "MiddlewareChainHost",
    "ALLOWED_RPC_CONTEXT_KEYS",
    "RPC_CONTEXT_AUTH_REFRESHED",
    "RPC_CONTEXT_AUTH_SNAPSHOT",
    "RPC_CONTEXT_BUILD_REQUEST",
    "RPC_CONTEXT_DISABLE_INTERNAL_RETRIES",
    "RPC_CONTEXT_DISABLE_READ_TIMEOUT_RETRIES",
    "RPC_CONTEXT_LOG_LABEL",
    "RPC_CONTEXT_READ_TIMEOUT",
    "RPC_CONTEXT_REFRESH_BUDGET",
    "RPC_CONTEXT_RPC_METHOD",
    "RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS",
    "Middleware",
    "NextCall",
    "RpcRequest",
    "RpcResponse",
    "build_chain",
    "materialize_rpc_request",
    "DrainMiddleware",
    "ErrorInjectionMiddleware",
    "MetricsMiddleware",
    "RetryMiddleware",
    "RPC_QUEUE_WAIT_CONTEXT_KEY",
    "SemaphoreMiddleware",
    "TracingMiddleware",
]
