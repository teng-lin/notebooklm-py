"""RetryBehavior — 429/5xx retry loop for the chain.

Per ADR-0009 §"Chain ordering", ``RetryBehavior`` sits just *inside*
``SemaphoreBehavior`` and just *outside* ``AuthRefreshBehavior``. The
chain is
``[Drain, Metrics, Semaphore, Retry, AuthRefresh, Tracing]``.

This middleware owns the **retry-on-429** and **retry-on-5xx/network** loops.
The chain leaf is a single ``Kernel.post`` attempt that raises
:class:`TransportRateLimited` on HTTP 429 or
:class:`TransportServerError` on HTTP 5xx / network failures —
**immediately**, without internal retry. The middleware catches those
exceptions and decides whether to retry by re-invoking the chain.
Auth-refresh-and-retry lives in :class:`AuthRefreshBehavior`.

Behavior:

- **Same retry counts** — ``rate_limit_max_retries`` /
  ``server_error_max_retries`` are propagated from ``NotebookLMClient`` so the
  budget matches the historical transport loop.
- **Same backoff timing** — :func:`_backoff.compute_backoff_delay` is
  invoked with the same ``base=1.0`` / ``cap=30.0`` / ``jitter_ratio=0.2``
  parameters; ``Retry-After`` is honored before falling back to
  exponential backoff. Sleeps are clamped by the existing client timeout so
  a retry cannot wait past the logical call's aggregate budget.
- **Same base log shape** — "rate-limited (HTTP 429); sleeping (…);
  retrying (n/N)" and "server/network error (…); backing off …; retrying
  (n/N)" are the emitted shapes. Deadline exhaustion emits
  an additional timeout warning when no retry budget remains.
- **Same metrics** — ``rpc_rate_limit_retries`` and
  ``rpc_server_error_retries`` are incremented per retry attempt, same as
  the legacy code.
- **Same disable_internal_retries gate** — read from the immutable
  ``RpcCallState.disable_internal_retries`` field (the post-resolution bool
  produced by ``_idempotency.resolve_effective_disable_internal_retries``
  before chain entry; see ADR-0009 §"Per-request behavior").
- **Optional read-timeout retry gate** —
  ``RpcCallState.disable_read_timeout_retries`` suppresses retries for
  read-side *post-transmission* failures
  (``_NON_REPLAYABLE_POST_SEND_ERRORS``: read timeout, read error, remote
  protocol error) on non-idempotent long-running calls such as streamed chat,
  where a retry would re-run generation from scratch and risk a duplicate
  answer. Connect/Write/Pool failures (request not fully sent) stay retryable,
  and HTTP 401 auth refresh is unaffected.
- **Same exception types on exhaustion** —
  :class:`TransportRateLimited` /
  :class:`TransportServerError` re-raised verbatim so
  ``_web.chat_transport.chat_aware_authed_post`` (which catches both) sees
  the same shape it always did.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract and
``src/notebooklm/_transport_errors.py`` for the terminal error mapper.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx

from .._backoff import compute_backoff_delay
from .._deadline import Monotonic, RuntimeDeadline
from .._transport_errors import TransportRateLimited, TransportServerError, parse_retry_after
from .config import CORE_LOGGER_NAME
from .helpers import resolve_sleep
from .rpc_call import NextCall, RpcRequest, RpcResponse

if TYPE_CHECKING:
    from .._client_metrics import ClientMetrics


# httpx failures that imply the request was already transmitted and the server
# may have started (or finished) work: replaying a non-idempotent streamed call
# like ``chat.ask`` on these risks a duplicate answer. ``ReadTimeout`` is the
# common shared-notebook slow-first-byte case; ``ReadError`` /
# ``RemoteProtocolError`` cover a connection severed after the request was sent
# but before/while the response streamed. Connect/Write/Pool failures are
# deliberately excluded — the request was not fully sent, so a retry is safe —
# and auth-expiry (HTTP 401) is handled separately by ``AuthRefreshBehavior``,
# so this gate does not suppress transparent token refresh.
_NON_REPLAYABLE_POST_SEND_ERRORS = (
    httpx.ReadTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


# Backoff parameters preserve the historical transport retry timing.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 30.0
_BACKOFF_JITTER_RATIO = 0.2
# Floor on the actual sleep so a jitter-pulled-to-zero backoff still yields a
# tiny sleep; mirrors the ``max(0.1, …)`` on both legacy retry paths.
_BACKOFF_MIN_SECONDS = 0.1


class RetryBehavior:
    """Pipeline behavior that retries on HTTP 429 / 5xx / network failures.

    Constructor inputs are wired by :class:`RuntimePipeline`:

    - ``rate_limit_max_retries`` / ``server_error_max_retries``: the same
      budgets exposed by ``NotebookLMClient`` via ``_rate_limit_max_retries`` /
      ``_server_error_max_retries``.
    - ``retry_timeout``: aggregate retry deadline in seconds. Production wires
      this to the existing client HTTP timeout so retry sleeps cannot exceed
      the logical call's timeout budget.
    - ``sleep``: the awaitable sleep function. Defaults to
      :func:`asyncio.sleep`; tests inject a stub to make backoff
      deterministic and to assert on sleep durations.
    - ``logger``: structured logger for the "rate-limited" / "server
      error" retry-info lines. Defaults to the project-canonical
      ``notebooklm._core`` logger so log filters in tests
      (``caplog.at_level(..., logger="notebooklm._core")``) keep
      matching.
    - ``metrics``: a :class:`ClientMetrics` whose ``.increment(...)``
      method we call per retry. ``None`` skips emission (useful for
      tests that don't care about metrics).
    """

    def __init__(
        self,
        *,
        rate_limit_max_retries: int | Callable[[], int],
        server_error_max_retries: int | Callable[[], int],
        retry_timeout: float | Callable[[], float | None] | None = None,
        sleep: Callable[[float], Awaitable[object]] | None = None,
        monotonic: Monotonic | None = None,
        logger: logging.Logger | None = None,
        metrics: ClientMetrics | None = None,
    ) -> None:
        # Budgets accept either a static int or a zero-arg callable so
        # focused owner tests can exercise both resolution forms.
        self._rate_limit_max = rate_limit_max_retries
        self._server_error_max = server_error_max_retries
        # Late-binding rationale lives on ``_runtime.helpers.resolve_sleep``;
        # see that helper for why we resolve at call time instead of capturing
        # the callable at construction.
        self._retry_timeout = retry_timeout
        self._sleep = sleep
        self._monotonic = monotonic
        self._logger = logger or logging.getLogger(CORE_LOGGER_NAME)
        self._metrics = metrics

    def _resolve_rate_limit_max(self) -> int:
        v = self._rate_limit_max
        return v() if callable(v) else v

    def _resolve_server_error_max(self) -> int:
        v = self._server_error_max
        return v() if callable(v) else v

    def _start_retry_deadline(self) -> RuntimeDeadline | None:
        v = self._retry_timeout
        if v is None:
            return None
        timeout = v() if callable(v) else v
        if timeout is None or not math.isfinite(float(timeout)):
            return None
        return RuntimeDeadline.start(float(timeout), monotonic=self._monotonic)

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Retry inner chain calls on 429 / 5xx / network failures.

        Reads ``log_label`` and retry gates from the typed ``request.state``.
        A missing ``log_label`` falls back to a defensive sentinel so a
        ``__new__``-built fixture driving the chain raw still produces a
        useful label (matching DrainBehavior's fallback).
        ``disable_internal_retries`` defaults to ``False`` — the production
        path always populates it from
        :func:`_idempotency.resolve_effective_disable_internal_retries`.
        """
        state = request.state
        log_label = state.log_label or "<unknown-chain-call>"
        disable_internal_retries = state.disable_internal_retries
        disable_read_timeout_retries = state.disable_read_timeout_retries

        rate_limit_retries = 0
        server_error_retries = 0
        # Prefer the aggregate deadline threaded in by WebExecutionRuntime via
        # ``RpcCallState.retry_deadline`` so a decode-time auth-refresh retry
        # re-enters the chain with the SAME T0-anchored budget instead of
        # restarting the retry clock (issue #1873). Only when absent (e.g. the
        # chat path) do we mint a fresh per-chain deadline. ``RuntimeDeadline``
        # carries its own monotonic source, so an inherited deadline needs no
        # clock reconciliation here.
        inherited_deadline = state.retry_deadline
        retry_deadline = (
            inherited_deadline if inherited_deadline is not None else self._start_retry_deadline()
        )

        while True:
            try:
                return await next_call(request)
            except TransportRateLimited as exc:
                rate_limit_max = self._resolve_rate_limit_max()
                if disable_internal_retries or rate_limit_retries >= rate_limit_max:
                    raise
                await self._wait_for_rate_limit(
                    exc=exc,
                    attempt=rate_limit_retries,
                    log_label=log_label,
                    rate_limit_max=rate_limit_max,
                    retry_deadline=retry_deadline,
                )
                rate_limit_retries += 1
                state.advance_retry_attempt()
                if self._metrics is not None:
                    self._metrics.increment(rpc_rate_limit_retries=1)
                continue
            except TransportServerError as exc:
                if disable_read_timeout_retries and isinstance(
                    exc.original, _NON_REPLAYABLE_POST_SEND_ERRORS
                ):
                    raise
                server_error_max = self._resolve_server_error_max()
                if disable_internal_retries or server_error_retries >= server_error_max:
                    raise
                await self._wait_for_server_error(
                    exc=exc,
                    attempt=server_error_retries,
                    log_label=log_label,
                    server_error_max=server_error_max,
                    retry_deadline=retry_deadline,
                )
                server_error_retries += 1
                state.advance_retry_attempt()
                if self._metrics is not None:
                    self._metrics.increment(rpc_server_error_retries=1)
                continue

    async def _wait_for_rate_limit(
        self,
        *,
        exc: TransportRateLimited,
        attempt: int,
        log_label: str,
        rate_limit_max: int,
        retry_deadline: RuntimeDeadline | None,
    ) -> None:
        """Honor ``Retry-After`` if present; otherwise exponential backoff.

        ``Retry-After`` is read from ``exc.retry_after`` — the leaf already
        parsed it via :func:`parse_retry_after` when it raised. We accept
        either the parsed integer (preferred) or fall back to re-parsing
        the header off ``exc.response`` if the parsed value is missing
        (defensive — production always populates ``retry_after``).
        """
        retry_after = exc.retry_after
        if retry_after is None and exc.response is not None:
            retry_after = parse_retry_after(exc.response.headers.get("retry-after"))

        if retry_after is not None:
            sleep_seconds: float = float(retry_after)
            sleep_source = f"Retry-After={retry_after}s"
        else:
            backoff = compute_backoff_delay(
                attempt,
                base=_BACKOFF_BASE_SECONDS,
                cap=_BACKOFF_CAP_SECONDS,
                jitter_ratio=_BACKOFF_JITTER_RATIO,
            )
            sleep_seconds = max(_BACKOFF_MIN_SECONDS, backoff)
            sleep_source = f"exp-backoff={sleep_seconds:.1f}s"

        actual_sleep = self._resolve_retry_sleep(
            retry_deadline=retry_deadline,
            requested_sleep=sleep_seconds,
            log_label=log_label,
            exc=exc,
        )

        self._logger.warning(
            "%s rate-limited (HTTP 429); sleeping (%s) then retrying (%d/%d)",
            log_label,
            sleep_source,
            attempt + 1,
            rate_limit_max,
        )
        await resolve_sleep(self._sleep)(actual_sleep)
        self._raise_if_retry_deadline_expired(
            retry_deadline=retry_deadline,
            log_label=log_label,
            exc=exc,
        )

    async def _wait_for_server_error(
        self,
        *,
        exc: TransportServerError,
        attempt: int,
        log_label: str,
        server_error_max: int,
        retry_deadline: RuntimeDeadline | None,
    ) -> None:
        """Exponential backoff with the same parameters as the legacy loop."""
        backoff = max(
            _BACKOFF_MIN_SECONDS,
            compute_backoff_delay(
                attempt,
                base=_BACKOFF_BASE_SECONDS,
                cap=_BACKOFF_CAP_SECONDS,
                jitter_ratio=_BACKOFF_JITTER_RATIO,
            ),
        )
        actual_backoff = self._resolve_retry_sleep(
            retry_deadline=retry_deadline,
            requested_sleep=backoff,
            log_label=log_label,
            exc=exc,
        )
        # ``status_code`` is set on 5xx; the network-error branch sets
        # ``response`` / ``status_code`` to ``None``, so fall back to the
        # type name of the original exception (RequestError / TimeoutException
        # subclasses, etc.) so the log line stays diagnostic.
        if exc.status_code is not None:
            status_label = f"HTTP {exc.status_code}"
        else:
            status_label = type(exc.original).__name__
        self._logger.warning(
            "%s server/network error (%s); backing off %.1fs then retrying (%d/%d)",
            log_label,
            status_label,
            actual_backoff,
            attempt + 1,
            server_error_max,
        )
        await resolve_sleep(self._sleep)(actual_backoff)
        self._raise_if_retry_deadline_expired(
            retry_deadline=retry_deadline,
            log_label=log_label,
            exc=exc,
        )

    def _resolve_retry_sleep(
        self,
        *,
        retry_deadline: RuntimeDeadline | None,
        requested_sleep: float,
        log_label: str,
        exc: TransportRateLimited | TransportServerError,
    ) -> float:
        if retry_deadline is None:
            return requested_sleep
        remaining = retry_deadline.remaining()
        if remaining <= 0.0 or requested_sleep >= remaining:
            self._logger.warning("%s", retry_deadline.timeout_message(f"{log_label} retry"))
            raise exc
        return retry_deadline.clamp_sleep(requested_sleep)

    def _raise_if_retry_deadline_expired(
        self,
        *,
        retry_deadline: RuntimeDeadline | None,
        log_label: str,
        exc: TransportRateLimited | TransportServerError,
    ) -> None:
        if retry_deadline is not None and retry_deadline.expired():
            self._logger.warning("%s", retry_deadline.timeout_message(f"{log_label} retry"))
            raise exc


__all__ = ["RetryBehavior"]
