"""Transport-neutral client-runtime helpers.

The web composition and lifecycle modules are intentionally not imported here:
transport leaves depend on :mod:`._runtime.config`, so eagerly importing those
roots would create an import cycle through :mod:`notebooklm._web.transport`.
"""

from . import config, contracts, helpers
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
    resolve_import_research_read_timeout,
    validate_read_timeout_kwarg,
)
from .contracts import LoopGuard
from .helpers import (
    AUTH_ERROR_PATTERNS,
    _resolve_keepalive_interval,
    is_auth_error,
    resolve_sleep,
)

__all__ = [
    "config",
    "contracts",
    "helpers",
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
    "resolve_import_research_read_timeout",
    "validate_read_timeout_kwarg",
    "LoopGuard",
    "AUTH_ERROR_PATTERNS",
    "_resolve_keepalive_interval",
    "is_auth_error",
    "resolve_sleep",
]
