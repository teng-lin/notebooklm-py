"""RetryMiddleware — 429/5xx retry loop for the Tier-12 chain.

Per ADR-009 §"Chain ordering", ``RetryMiddleware`` sits just *inside*
``MetricsMiddleware`` and just *outside* ``AuthRefreshMiddleware`` (which
extracts in PR 12.8). The final Tier-12 chain is
``[Drain, Metrics, Retry, AuthRefresh, ErrorInjection, Tracing]``. PR 12.7
ships the interim 5-middleware chain
``[Drain, Metrics, Retry, ErrorInjection, Tracing]``; PR 12.8 inserts
``AuthRefresh`` BETWEEN ``Retry`` and ``ErrorInjection``.

This PR lifts the **retry-on-429** and **retry-on-5xx/network** loops out
of ``AuthedTransport.perform_authed_post`` (the chain leaf). After PR
12.7 the leaf is a single POST attempt that raises
:class:`TransportRateLimited` on HTTP 429 or
:class:`TransportServerError` on HTTP 5xx / network failures —
**immediately**, without internal retry. The middleware catches those
exceptions and decides whether to retry by re-invoking the chain.
Auth-refresh-and-retry stays in the leaf as a localized loop until PR
12.8 lifts it.

Behavior preservation (vs. pre-PR-12.7):

- **Same retry counts** — ``rate_limit_max_retries`` /
  ``server_error_max_retries`` are propagated from ``Session`` so the
  budget matches the legacy ``AuthedTransport`` loop.
- **Same backoff timing** — :func:`_backoff.compute_backoff_delay` is
  invoked with the same ``base=1.0`` / ``cap=30.0`` / ``jitter_ratio=0.2``
  parameters; ``Retry-After`` is honored before falling back to
  exponential backoff.
- **Same log lines** — "rate-limited (HTTP 429); sleeping (…); retrying
  (n/N)" and "server/network error (…); backing off …; retrying (n/N)"
  match the pre-PR-12.7 message shape so log-grep alerts keep matching.
- **Same metrics** — ``rpc_rate_limit_retries`` and
  ``rpc_server_error_retries`` are incremented per retry attempt, same as
  the legacy code.
- **Same disable_internal_retries gate** — read from
  ``request.context["disable_internal_retries"]`` (post-resolution bool
  produced by ``_idempotency.resolve_effective_disable_internal_retries``
  before chain entry; see ADR-009 §"Per-request behavior").
- **Same exception types on exhaustion** —
  :class:`TransportRateLimited` /
  :class:`TransportServerError` re-raised verbatim so
  ``_chat_transport.chat_aware_authed_post`` (which catches both) sees
  the same shape it always did.

Subtle behavioral note (interim until PR 12.8 lands): pre-PR-12.7 the
auth-refresh-and-retry counter (``refreshed_this_call``) sat inside the
same ``while True`` as the 429/5xx counters, so a single call could
auth-refresh at most once across ALL its retries. Post-PR-12.7, each
``RetryMiddleware`` retry is a fresh chain invocation which means a fresh
leaf invocation with its OWN ``refreshed_this_call``. In theory the same
call could now auth-refresh once per retry. In practice auth refreshes
are idempotent (they get fresh tokens), and ``RetryMiddleware``'s own
budget bounds the total work — no infinite loop is possible. PR 12.8
collapses this back into a single coordinated refresh path by lifting
auth-refresh into a chain middleware outside this one.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract,
``src/notebooklm/_authed_transport.py`` for the (slimmed) leaf and the
exception types this middleware catches, and
``.sisyphus/plans/tier-12-13-greenfield-migration.md`` row 12.7 for the
PR sequence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ._authed_transport import TransportRateLimited, TransportServerError, parse_retry_after
from ._backoff import compute_backoff_delay
from ._middleware import NextCall, RpcRequest, RpcResponse
from ._session_config import CORE_LOGGER_NAME

if TYPE_CHECKING:
    from ._client_metrics import ClientMetrics


# Backoff parameters lifted verbatim from the pre-PR-12.7 retry loop in
# ``AuthedTransport.perform_authed_post`` so end-to-end retry timing is
# preserved bit-for-bit.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 30.0
_BACKOFF_JITTER_RATIO = 0.2
# Floor on the actual sleep so a jitter-pulled-to-zero backoff still yields a
# tiny sleep; mirrors the ``max(0.1, …)`` on both legacy retry paths.
_BACKOFF_MIN_SECONDS = 0.1


class RetryMiddleware:
    """Chain middleware that retries on HTTP 429 / 5xx / network failures.

    Conforms to :class:`notebooklm._middleware.Middleware` —
    ``__call__`` matches the Protocol so instances are assignable into a
    ``Sequence[Middleware]``.

    Constructor inputs (all wired by ``Session.__init__``):

    - ``rate_limit_max_retries`` / ``server_error_max_retries``: the same
      budgets ``Session`` previously passed to ``AuthedTransport`` via
      the host attributes ``_rate_limit_max_retries`` /
      ``_server_error_max_retries``.
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
        sleep: Callable[[float], Awaitable[object]] | None = None,
        logger: logging.Logger | None = None,
        metrics: ClientMetrics | None = None,
    ) -> None:
        # Budgets accept either a static int OR a zero-arg callable. The
        # callable form preserves the pre-PR-12.7 contract where
        # ``AuthedTransport`` read ``host._rate_limit_max_retries`` /
        # ``host._server_error_max_retries`` LIVE inside the retry loop, so
        # tests (and any production tweaks) that mutate those attrs on the
        # core after ``open()`` still take effect. ``Session.__init__``
        # wires the callable form via a ``lambda: self._rate_limit_max_retries``
        # closure; tests that build a middleware in isolation typically pass
        # the int form.
        self._rate_limit_max = rate_limit_max_retries
        self._server_error_max = server_error_max_retries
        # ``None`` defers resolution to call time so a test that
        # ``monkeypatch.setattr("asyncio.sleep", ...)`` (or anywhere
        # asyncio.sleep is exposed) observes the fake. Capturing
        # ``asyncio.sleep`` at construction would freeze the binding to
        # whatever was imported when the middleware was instantiated,
        # silently bypassing later monkeypatches.
        self._sleep = sleep
        self._logger = logger or logging.getLogger(CORE_LOGGER_NAME)
        self._metrics = metrics

    def _resolve_sleep(self) -> Callable[[float], Awaitable[object]]:
        """Return the call-time sleep function — fake-or-real."""
        return self._sleep if self._sleep is not None else asyncio.sleep

    def _resolve_rate_limit_max(self) -> int:
        v = self._rate_limit_max
        return v() if callable(v) else v

    def _resolve_server_error_max(self) -> int:
        v = self._server_error_max
        return v() if callable(v) else v

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Retry inner chain calls on 429 / 5xx / network failures.

        Reads ``log_label`` and ``disable_internal_retries`` from
        ``request.context``. A missing ``log_label`` falls back to a
        defensive sentinel so a ``__new__``-built fixture driving the
        chain raw doesn't trip on a ``KeyError`` (matches DrainMiddleware's
        same fallback). ``disable_internal_retries`` defaults to ``False``
        — the production path always populates it from
        :func:`_idempotency.resolve_effective_disable_internal_retries`.
        """
        log_label = request.context.get("log_label", "<unknown-chain-call>")
        # Post-resolution bool — see ADR-009 §"Per-request behavior".
        disable_internal_retries = bool(request.context.get("disable_internal_retries", False))

        rate_limit_retries = 0
        server_error_retries = 0

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
                )
                rate_limit_retries += 1
                if self._metrics is not None:
                    self._metrics.increment(rpc_rate_limit_retries=1)
                continue
            except TransportServerError as exc:
                server_error_max = self._resolve_server_error_max()
                if disable_internal_retries or server_error_retries >= server_error_max:
                    raise
                await self._wait_for_server_error(
                    exc=exc,
                    attempt=server_error_retries,
                    log_label=log_label,
                    server_error_max=server_error_max,
                )
                server_error_retries += 1
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

        self._logger.warning(
            "%s rate-limited (HTTP 429); sleeping (%s) then retrying (%d/%d)",
            log_label,
            sleep_source,
            attempt + 1,
            rate_limit_max,
        )
        await self._resolve_sleep()(sleep_seconds)

    async def _wait_for_server_error(
        self,
        *,
        exc: TransportServerError,
        attempt: int,
        log_label: str,
        server_error_max: int,
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
            backoff,
            attempt + 1,
            server_error_max,
        )
        await self._resolve_sleep()(backoff)


__all__ = ["RetryMiddleware"]
