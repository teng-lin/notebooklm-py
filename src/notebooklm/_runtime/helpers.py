"""Cross-seam helpers: auth-error classification, keepalive validation.

Small, pure helpers extracted from the historical ``notebooklm._core``
preamble (the compatibility shim was removed in v0.5.0). Callers import
directly from this module — e.g.
``from notebooklm._runtime.helpers import is_auth_error``.

These helpers stay separate from :mod:`notebooklm._runtime.config` because
they carry behavior (and therefore tests), while the constants module is
data-only.
"""

from __future__ import annotations

__all__ = [
    "AUTH_ERROR_PATTERNS",
    "MAX_RETRY_AFTER_SECONDS",
    "_resolve_keepalive_interval",
    "is_auth_error",
    "map_google_http_status",
    "parse_retry_after",
    "resolve_sleep",
]

import asyncio
import math
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..exceptions import (
    AuthError,
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
)

# Legacy export kept for callers that import the constant directly. Auth
# classification below no longer uses these as arbitrary RPCError substrings.
AUTH_ERROR_PATTERNS = (
    "authentication",
    "expired",
    "unauthorized",
    "login",
    "re-authenticate",
)

_AUTH_HTTP_STATUS_CODES = frozenset({400, 401, 403})
_AUTH_RPC_NUMERIC_CODES = frozenset({401, 403, 16})
_MAX_RPC_SIGNAL_LENGTH = 256
_AUTH_RPC_LABEL_CODES = frozenset(
    {
        "AUTHENTICATION_REQUIRED",
        "AUTH_EXPIRED",
        "TOKEN_EXPIRED",
        "UNAUTHENTICATED",
        "UNAUTHORIZED",
    }
)
_LEGACY_AUTH_RPC_MESSAGES = frozenset(
    {
        "authentication expired",
        "authentication expired or invalid",
        "authentication required. run 'notebooklm login' to re-authenticate.",
    }
)
MAX_RETRY_AFTER_SECONDS = 300


def _resolve_keepalive_interval(keepalive: float | None, min_interval: float) -> float | None:
    """Validate and clamp the keepalive interval.

    ``None`` disables the background task. Otherwise both values must be
    positive finite numbers; the effective interval is ``max(keepalive,
    min_interval)`` so callers can't accidentally lower the rate-limit floor.
    """
    if not (math.isfinite(min_interval) and min_interval > 0):
        raise ValueError(
            f"keepalive_min_interval must be a positive finite number, got {min_interval!r}"
        )
    if keepalive is None:
        return None
    if not (math.isfinite(keepalive) and keepalive > 0):
        raise ValueError(f"keepalive must be None or a positive finite number, got {keepalive!r}")
    return max(keepalive, min_interval)


def resolve_sleep(
    injected: Callable[[float], Awaitable[object]] | None,
) -> Callable[[float], Awaitable[object]]:
    """Return the call-time sleep function — injected fake-or-real ``asyncio.sleep``.

    Used by ``RetryMiddleware`` and ``AuthRefreshMiddleware`` to honor a
    constructor-time fake while still resolving the real ``asyncio.sleep`` at
    call time. Resolving via the ``asyncio`` module global on each call (rather
    than capturing the callable at construction) is what preserves test
    late-binding: a ``monkeypatch.setattr('asyncio.sleep', ...)`` (which mutates
    the singleton ``asyncio`` module's ``sleep`` attribute) is observed by every
    caller that hits this helper, because ``asyncio.sleep`` is re-read from the
    module's ``__dict__`` on every invocation of ``resolve_sleep``. Capturing
    ``asyncio.sleep`` at module-import or middleware-construction time would
    freeze the binding to whatever was imported then and silently bypass later
    monkeypatches — that's the bug this helper exists to prevent.
    """
    return injected if injected is not None else asyncio.sleep


def map_google_http_status(
    response: Any,
    *,
    filename: str,
    chain: bool,
    cause: Exception | None = None,
) -> None:
    """Map Google HTTP auth, throttle, and server statuses consistently.

    Successful responses and endpoint-specific redirects/client errors pass
    through for the caller to classify.  ``chain=False`` is required on
    bearer-authenticated Android paths: their raw HTTP exception may retain an
    ``Authorization`` header, so neither that exception nor its frames may be
    attached to the public error.
    """

    status = int(response if type(response) is int else response.status_code)
    if status != 401 and status != 429 and status < 500:
        return

    if chain and cause is None:
        raise ValueError("chain=True requires the original caught HTTP exception")

    if status == 401:
        public_error: Exception = AuthError(
            f"Google authentication expired while {filename}; reauthenticate the selected "
            "profile with `notebooklm login`, then retry."
        )
    elif status == 429:
        raw_retry_after = getattr(response, "headers", {}).get("retry-after")
        retry_after = parse_retry_after(raw_retry_after)
        message = f"Google throttled the download/request while {filename}; retry after a delay."
        public_error = RateLimitError(message, retry_after=retry_after)
    else:
        public_error = ServerError(
            f"Google returned HTTP {status} while {filename}; retry later.",
            status_code=status,
        )

    if chain and cause is not None:
        raise public_error from cause
    raise public_error from None


def parse_retry_after(value: object) -> int | None:
    """Parse Google's integer, fractional, or HTTP-date Retry-After value."""

    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        return min(MAX_RETRY_AFTER_SECONDS, max(0, int(value)))
    except ValueError:
        pass
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return min(MAX_RETRY_AFTER_SECONDS, max(0, math.ceil(seconds)))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = (parsed - datetime.now(timezone.utc)).total_seconds()
        return min(MAX_RETRY_AFTER_SECONDS, max(0, int(delta)))
    except (OverflowError, TypeError, ValueError):
        return None


def _coerce_int_code(value: object) -> int | None:
    """Return an integer status/code value without accepting bools."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        if len(value) > _MAX_RPC_SIGNAL_LENGTH:
            return None
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _normalize_code_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) > _MAX_RPC_SIGNAL_LENGTH:
        return None
    normalized = value.strip().replace("-", "_").replace(" ", "_").upper()
    return normalized if normalized else None


def _normalized_message(error: Exception) -> str:
    return " ".join(str(error).split()).casefold()


def _rpc_error_has_auth_signal(error: RPCError) -> bool:
    # Some wrappers attach HTTP-like status metadata to a generic RPCError.
    # Treat an explicit status_code as authoritative when present.
    status_code = _coerce_int_code(getattr(error, "status_code", None))
    if status_code is not None:
        return status_code in _AUTH_HTTP_STATUS_CODES

    has_explicit_rpc_signal = False
    for attr_name in ("rpc_code", "code", "status"):
        value = getattr(error, attr_name, None)
        numeric_code = _coerce_int_code(value)
        label_code = _normalize_code_label(value)
        if numeric_code is not None or label_code is not None:
            has_explicit_rpc_signal = True
        if numeric_code in _AUTH_RPC_NUMERIC_CODES:
            return True
        if label_code in _AUTH_RPC_LABEL_CODES:
            return True

    if has_explicit_rpc_signal:
        return False

    # Backward-compatible fallback for historical auth-service RPCError text.
    # This is deliberately exact-match only; auth words in arbitrary RPCError
    # diagnostics are not enough to trigger refresh/re-auth guidance.
    return _normalized_message(error) in _LEGACY_AUTH_RPC_MESSAGES


def is_auth_error(error: Exception) -> bool:
    """Check if an exception indicates an authentication failure.

    Args:
        error: The exception to check.

    Returns:
        True if the error is likely due to authentication issues.
    """
    # AuthError is always an auth error
    if isinstance(error, AuthError):
        return True

    # Don't treat network/rate limit/server errors as auth errors
    # even if they're subclasses of RPCError
    if isinstance(
        error,
        NetworkError | RPCTimeoutError | RateLimitError | ServerError | ClientError,
    ):
        return False

    # HTTP 400/401/403 are auth errors.
    # Google returns 400 for expired CSRF tokens (not 401/403). Layer-1
    # recovery (refresh_auth) re-extracts SNlM0e from the NotebookLM
    # homepage and retries with a fresh token. The retry guard
    # (``_is_retry`` in ``rpc_call``) bounds wasted refreshes on legitimate
    # 400s (bad payload) to one extra GET per call.
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _AUTH_HTTP_STATUS_CODES

    if isinstance(error, RPCError):
        return _rpc_error_has_auth_signal(error)

    return False
