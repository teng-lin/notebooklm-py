"""Authenticated transport pipeline for NotebookLM core operations."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, cast

import httpx

# Upper bound on Retry-After wait. Caps both integer-seconds and HTTP-date forms
# so a malicious or buggy server can't force a multi-hour pause.
MAX_RETRY_AFTER_SECONDS = 300


def _parse_retry_after(value: str | None) -> int | None:
    """Parse RFC 7231 Retry-After: integer-seconds OR HTTP-date.

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


@dataclass(frozen=True)
class _AuthSnapshot:
    """Point-in-time view of auth headers used to build a single request.

    Captured once per HTTP attempt by ``_perform_authed_post`` and passed
    into the caller-supplied ``build_request`` factory so the URL/body are
    consistent for that attempt. On retry, a *new* snapshot is taken so
    refreshed credentials are picked up before the rebuild.
    """

    csrf_token: str
    session_id: str
    authuser: int
    account_email: str | None


class _TransportAuthExpired(Exception):
    """Raised by ``_perform_authed_post`` when the refresh callback itself
    failed during an auth recovery attempt.

    ``original`` is the transport-layer ``httpx.HTTPStatusError`` that
    triggered the refresh attempt. The refresh callback's error is attached via
    ``__cause__``.
    """

    def __init__(self, message: str, *, original: Exception):
        super().__init__(message)
        self.original = original


class _TransportRateLimited(Exception):
    """Raised by ``_perform_authed_post`` when the 429 retry budget is
    exhausted (or no retries are configured).
    """

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


class _TransportServerError(Exception):
    """Raised by ``_perform_authed_post`` when the server-error retry budget
    is exhausted.
    """

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


# Build-request factory: receives a fresh ``_AuthSnapshot`` and returns the
# triple (url, body, extra_headers) for one HTTP attempt. The transport invokes
# this once per attempt so refreshed snapshots are picked up on retry.
_PostBody = str | bytes
_BuildRequest = Callable[[_AuthSnapshot], tuple[str, _PostBody, dict[str, str] | None]]


class _AuthedTransportHost(Protocol):
    _http_client: httpx.AsyncClient | None
    _bound_loop: asyncio.AbstractEventLoop | None
    _refresh_callback: Callable[[], Awaitable[Any]] | None
    _refresh_retry_delay: float
    _rate_limit_max_retries: int
    _server_error_max_retries: int

    def _get_rpc_semaphore(self) -> AbstractAsyncContextManager[Any]: ...

    async def _snapshot(self) -> _AuthSnapshot: ...

    async def _await_refresh(self) -> None: ...

    def _record_rpc_queue_wait(self, wait_seconds: float) -> None: ...

    def _increment_metrics(self, **increments: int | float) -> None: ...


class AuthedTransport:
    """Shared authenticated POST retry/refresh pipeline."""

    def __init__(
        self,
        host: _AuthedTransportHost,
        *,
        is_auth_error: Callable[[Exception], bool],
        sleep: Callable[[float], Awaitable[Any]],
        uniform: Callable[[float, float], float],
        logger: logging.Logger,
    ):
        self._host = host
        self._is_auth_error = is_auth_error
        self._sleep = sleep
        self._uniform = uniform
        self._logger = logger

    async def perform_authed_post(
        self,
        *,
        build_request: _BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        """Run an authed POST through the shared retry/refresh pipeline."""
        host = self._host
        if host._http_client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        client = host._http_client

        # Event-loop affinity guard (audit section 14 / T7.G2). Placed before
        # semaphore acquisition so cross-loop misuse never reserves a slot.
        if host._bound_loop is not None and asyncio.get_running_loop() is not host._bound_loop:
            raise RuntimeError(
                "NotebookLMClient is bound to a different event loop. "
                "Each client is per-loop; create a new client in the target loop."
            )

        refreshed_this_call = False
        rate_limit_retries = 0
        server_error_retries = 0
        start = time.perf_counter()

        # ---------------------------------------------------------------
        # Semaphore placement contract (T7.H1 / audit section 8) — DO NOT MOVE.
        #
        # The semaphore wraps the entire retry loop. Releasing during backoff
        # would let new callers burst in just as the current cohort wakes up,
        # undoing the smoothing the semaphore exists to provide.
        # ---------------------------------------------------------------
        semaphore = host._get_rpc_semaphore()
        queue_wait_start = time.perf_counter()
        async with semaphore:
            host._record_rpc_queue_wait(time.perf_counter() - queue_wait_start)
            while True:
                snapshot = await host._snapshot()
                url, body, headers = build_request(snapshot)

                try:
                    if headers:
                        response = await client.post(url, content=body, headers=headers)
                    else:
                        response = await client.post(url, content=body)
                    response.raise_for_status()
                except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                    # --- Auth refresh path ---------------------------------
                    if (
                        not refreshed_this_call
                        and host._refresh_callback is not None
                        and self._is_auth_error(exc)
                    ):
                        self._logger.info(
                            "%s auth error detected, attempting token refresh",
                            log_label,
                        )
                        try:
                            await host._await_refresh()
                        except Exception as refresh_error:
                            self._logger.warning("Token refresh failed: %s", refresh_error)
                            raise _TransportAuthExpired(
                                f"auth refresh failed for {log_label}",
                                original=exc,
                            ) from refresh_error
                        if host._refresh_retry_delay > 0:
                            await self._sleep(host._refresh_retry_delay)
                        self._logger.info("Token refresh successful, retrying %s", log_label)
                        refreshed_this_call = True
                        host._increment_metrics(rpc_auth_retries=1)
                        continue

                    # --- 429 rate-limit path --------------------------------
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                        retry_after = _parse_retry_after(exc.response.headers.get("retry-after"))
                        if (
                            not disable_internal_retries
                            and rate_limit_retries < host._rate_limit_max_retries
                        ):
                            if retry_after is not None:
                                sleep_seconds: float = retry_after
                                sleep_source = f"Retry-After={retry_after}s"
                            else:
                                backoff = min(2**rate_limit_retries, 30)
                                backoff += self._uniform(-0.2 * backoff, 0.2 * backoff)
                                sleep_seconds = max(0.1, backoff)
                                sleep_source = f"exp-backoff={sleep_seconds:.1f}s"
                            self._logger.warning(
                                "%s rate-limited (HTTP 429); sleeping (%s) then retrying (%d/%d)",
                                log_label,
                                sleep_source,
                                rate_limit_retries + 1,
                                host._rate_limit_max_retries,
                            )
                            await self._sleep(sleep_seconds)
                            rate_limit_retries += 1
                            host._increment_metrics(rpc_rate_limit_retries=1)
                            continue
                        raise _TransportRateLimited(
                            f"{log_label} rate-limited (HTTP 429)",
                            retry_after=retry_after,
                            response=exc.response,
                            original=exc,
                        ) from exc

                    # --- 5xx / network retry path ---------------------------
                    is_server_error = (
                        isinstance(exc, httpx.HTTPStatusError)
                        and 500 <= exc.response.status_code < 600
                    )
                    is_network_error = isinstance(exc, httpx.RequestError)
                    if is_server_error or is_network_error:
                        if (
                            not disable_internal_retries
                            and server_error_retries < host._server_error_max_retries
                        ):
                            backoff = min(2**server_error_retries, 30)
                            backoff += self._uniform(-0.2 * backoff, 0.2 * backoff)
                            backoff = max(0.1, backoff)
                            status_label = (
                                f"HTTP {exc.response.status_code}"  # type: ignore[union-attr]
                                if is_server_error
                                else type(exc).__name__
                            )
                            self._logger.warning(
                                "%s server/network error (%s); backing off %.1fs then retrying (%d/%d)",
                                log_label,
                                status_label,
                                backoff,
                                server_error_retries + 1,
                                host._server_error_max_retries,
                            )
                            await self._sleep(backoff)
                            server_error_retries += 1
                            host._increment_metrics(rpc_server_error_retries=1)
                            continue
                        if is_server_error:
                            status_error = cast(httpx.HTTPStatusError, exc)
                            raise _TransportServerError(
                                f"{log_label} server error "
                                f"(HTTP {status_error.response.status_code}) after "
                                f"{server_error_retries} retries",
                                original=status_error,
                                response=status_error.response,
                                status_code=status_error.response.status_code,
                            ) from exc
                        raise _TransportServerError(
                            f"{log_label} network error after {server_error_retries} retries: {exc}",
                            original=exc,
                        ) from exc

                    # --- Anything else: propagate the raw transport error ----
                    elapsed = time.perf_counter() - start
                    self._logger.debug(
                        "%s transport error after %.3fs: %s",
                        log_label,
                        elapsed,
                        exc,
                    )
                    raise

                # Success
                return response
