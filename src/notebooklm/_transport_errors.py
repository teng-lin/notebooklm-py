"""Transport-level exceptions and error mapping for the POST pipeline."""

from __future__ import annotations

__all__ = [
    "MAX_RETRY_AFTER_SECONDS",
    "TransportAuthExpired",
    "TransportRateLimited",
    "TransportServerError",
    "parse_retry_after",
    "raise_mapped_post_error",
]

import logging
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import NoReturn

import httpx

# Upper bound on Retry-After wait. Caps both integer-seconds and HTTP-date forms
# so a malicious or buggy server can't force a multi-hour pause.
MAX_RETRY_AFTER_SECONDS = 300


def parse_retry_after(value: str | None) -> int | None:
    """Parse a Retry-After header: integer-seconds OR HTTP-date (RFC 7231).

    Fractional-seconds (e.g. ``"1.5"``) are non-conformant but emitted by some
    servers/proxies; they are accepted and rounded UP so we never retry faster
    than the server asked.

    Returns seconds-until-retry as a non-negative int, clamped to
    ``MAX_RETRY_AFTER_SECONDS``. Returns ``None`` for empty or unparseable input.
    """
    if not value:
        return None
    value = value.strip()
    # Integer-seconds form (most common)
    try:
        return min(MAX_RETRY_AFTER_SECONDS, max(0, int(value)))
    except ValueError:
        pass
    # Fractional-seconds form: non-RFC-7231 but emitted by some servers/proxies.
    # Round UP so we never retry faster than the server asked. Reject non-finite
    # (inf/nan) values, which float() accepts but math.ceil() can't handle.
    try:
        seconds = float(value)
        if math.isfinite(seconds):
            return min(MAX_RETRY_AFTER_SECONDS, max(0, math.ceil(seconds)))
    except ValueError:
        pass
    # HTTP-date form (RFC 7231 section 7.1.1.1)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return min(MAX_RETRY_AFTER_SECONDS, max(0, int(delta)))


class TransportAuthExpired(Exception):
    """Raised when auth refresh fails during an auth recovery attempt.

    ``original`` is the transport-layer ``httpx.HTTPStatusError`` that
    triggered the refresh attempt. The refresh callback's error is attached via
    ``__cause__``.
    """

    def __init__(self, message: str, *, original: Exception):
        super().__init__(message)
        self.original = original


class TransportRateLimited(Exception):
    """Raised when the 429 retry budget is exhausted."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None,
        response: httpx.Response,
        original: httpx.HTTPStatusError,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.response = response
        self.original = original


class TransportServerError(Exception):
    """Raised when the server-error retry budget is exhausted."""

    def __init__(
        self,
        message: str,
        *,
        original: Exception,
        response: httpx.Response | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.original = original
        self.response = response
        self.status_code = status_code


def raise_mapped_post_error(
    *,
    log_label: str,
    exc: httpx.HTTPStatusError | httpx.RequestError,
    start: float,
    logger: logging.Logger,
) -> NoReturn:
    """Map retryable ``Kernel.post`` failures to transport exceptions.

    HTTP 429, HTTP 5xx, and network errors become chain-consumed transport
    exceptions. Other HTTP status errors are re-raised unchanged so outer
    middlewares can handle auth refresh and domain-specific failures.
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
        raise TransportRateLimited(
            f"{log_label} rate-limited (HTTP 429)",
            retry_after=retry_after,
            response=exc.response,
            original=exc,
        ) from exc

    if isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600:
        raise TransportServerError(
            f"{log_label} server error (HTTP {exc.response.status_code})",
            original=exc,
            response=exc.response,
            status_code=exc.response.status_code,
        ) from exc

    if isinstance(exc, httpx.RequestError):
        raise TransportServerError(
            f"{log_label} network error: {exc}",
            original=exc,
        ) from exc

    elapsed = time.perf_counter() - start
    logger.debug(
        "%s transport error after %.3fs: %s",
        log_label,
        elapsed,
        exc,
    )
    raise exc
