"""Private client-runtime implementation package.

Cohesive cluster promoted from the former flat ``_runtime_*.py`` modules (issue #1328).
Re-exports the cluster's public names so existing ``from .._runtime import X`` style
references keep resolving; importers may also reach submodules directly
(``from .._runtime.config import DEFAULT_TIMEOUT``).
"""

from . import auth, config, contracts, helpers, init, lifecycle, transport
from .auth import AuthRefreshCoordinator
from .config import (
    CORE_LOGGER_NAME,
    DEFAULT_CHAT_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
    normalize_max_concurrent_uploads,
)
from .contracts import Kernel, LoopGuard, RpcCaller
from .helpers import (
    AUTH_ERROR_PATTERNS,
    _resolve_keepalive_interval,
    is_auth_error,
    resolve_sleep,
)
from .init import (
    ClientInternals,
    RuntimeCollaborators,
    ValidatedSessionConfig,
    WiredMiddleware,
    build_collaborators,
    build_runtime_transport,
    compose_client_internals,
    validate_constructor_args,
    wire_middleware_chain,
)
from .lifecycle import (
    ClientLifecycle,
    CookieRotator,
    CookieSaver,
    _default_cookie_rotator,
    _default_cookie_saver,
)
from .transport import RuntimeTransport

__all__ = [
    "auth",
    "config",
    "contracts",
    "helpers",
    "init",
    "lifecycle",
    "transport",
    "AuthRefreshCoordinator",
    "CORE_LOGGER_NAME",
    "DEFAULT_CHAT_TIMEOUT",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_KEEPALIVE_MIN_INTERVAL",
    "DEFAULT_MAX_CONCURRENT_RPCS",
    "DEFAULT_MAX_CONCURRENT_UPLOADS",
    "DEFAULT_TIMEOUT",
    "normalize_max_concurrent_uploads",
    "Kernel",
    "LoopGuard",
    "RpcCaller",
    "AUTH_ERROR_PATTERNS",
    "_resolve_keepalive_interval",
    "is_auth_error",
    "resolve_sleep",
    "ClientInternals",
    "RuntimeCollaborators",
    "ValidatedSessionConfig",
    "WiredMiddleware",
    "build_collaborators",
    "build_runtime_transport",
    "compose_client_internals",
    "validate_constructor_args",
    "wire_middleware_chain",
    "ClientLifecycle",
    "CookieRotator",
    "CookieSaver",
    "_default_cookie_rotator",
    "_default_cookie_saver",
    "RuntimeTransport",
]
