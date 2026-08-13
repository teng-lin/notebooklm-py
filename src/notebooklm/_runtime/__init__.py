"""Private client-runtime implementation package.

Cohesive cluster promoted from the former flat ``_runtime_*.py`` modules (issue #1328).
Re-exports the cluster's public names so existing ``from .._runtime import X`` style
references keep resolving; importers may also reach submodules directly
(``from .._runtime.config import DEFAULT_TIMEOUT``).
"""

from . import auth, config, contracts, helpers, init, lifecycle, transport
from .auth import AuthRefreshCoordinator
from .config import (
    AUTO_READ_TIMEOUT,
    CORE_LOGGER_NAME,
    DEFAULT_CHAT_RESPONSE_MAX_BYTES,
    DEFAULT_CHAT_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
    MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT,
    assert_resolved_read_timeout,
    compose_builtin_read_timeout,
    normalize_max_concurrent_uploads,
    resolve_chat_read_timeout,
    validate_read_timeout_kwarg,
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
    "AUTO_READ_TIMEOUT",
    "CORE_LOGGER_NAME",
    "DEFAULT_CHAT_RESPONSE_MAX_BYTES",
    "DEFAULT_CHAT_TIMEOUT",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT",
    "DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT",
    "DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT",
    "DEFAULT_KEEPALIVE_MIN_INTERVAL",
    "DEFAULT_MAX_CONCURRENT_RPCS",
    "DEFAULT_MAX_CONCURRENT_UPLOADS",
    "DEFAULT_TIMEOUT",
    "MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT",
    "assert_resolved_read_timeout",
    "compose_builtin_read_timeout",
    "normalize_max_concurrent_uploads",
    "resolve_chat_read_timeout",
    "validate_read_timeout_kwarg",
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
    "RuntimeTransport",
]
