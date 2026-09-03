"""Transport-level exceptions and error mapping for the web POST pipeline."""

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
import time
from typing import NoReturn

import httpx

from ..._runtime.helpers import MAX_RETRY_AFTER_SECONDS, parse_retry_after


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
