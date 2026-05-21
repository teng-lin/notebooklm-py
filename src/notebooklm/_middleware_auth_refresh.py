"""AuthRefreshMiddleware — 401/403/400-CSRF retry-with-refresh for the chain.

Per ADR-009 §"Chain ordering", ``AuthRefreshMiddleware`` sits just *inside*
``RetryMiddleware`` and just *outside* ``ErrorInjectionMiddleware``. The final
Tier-12 chain (post-PR 12.9, after ``SemaphoreMiddleware`` was inserted between
``Metrics`` and ``Retry``) is
``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]`` —
PR 12.8 inserts ``AuthRefresh`` between ``Retry`` and ``ErrorInjection`` so
this ordering is now realized end-to-end.

This PR lifts the **auth-refresh-once retry** loop out of
``AuthedTransport.perform_authed_post`` (the chain leaf). After PR 12.8 the
leaf is a *pure* POST that lets ``httpx.HTTPStatusError`` /
``httpx.RequestError`` propagate raw for auth errors (the 429 / 5xx
exception-raisers stay since they feed ``RetryMiddleware``). The middleware
catches the raw auth-error ``httpx.HTTPStatusError``, triggers a coalesced
refresh via :class:`AuthRefreshCoordinator`, then re-invokes ``next_call``
exactly once.

Why "exactly once": ADR-009 §"Retry semantics" pins
"**exactly one** retry per ``next_call`` invocation. If the retry also
raises 401, the exception propagates — no second retry, no recursion."
``RetryMiddleware`` outside this middleware does NOT retry on auth
errors (it catches only ``TransportRateLimited`` /
``TransportServerError``), so a persistent 401 surfaces cleanly to the
caller without burning the rate-limit / server-error budget on auth
loops.

Refresh-failure path: if the refresh callback itself raises (network
flake, login expired, etc.), the middleware wraps the original
``httpx.HTTPStatusError`` in :class:`TransportAuthExpired` so callers
that key on the transport exception type still see a coherent shape.
Matches the pre-PR-12.8 leaf-side ``TransportAuthExpired`` raise.

Pre-refresh sleep: when ``refresh_retry_delay > 0`` the middleware sleeps
that duration AFTER the successful refresh and BEFORE the retry. Matches
the legacy ``AuthedTransport`` behavior so a cassette that recorded the
post-refresh delay replays the same timing.

Why this PR does NOT use ADR-009's pinned ``rebuild_headers`` /
``build_request_factory`` closure callbacks (yet): the chain envelope
(``RpcRequest.url`` / ``.headers`` / ``.body``) is still empty in
production — the chain leaf builds the HTTP request from
``context["build_request"]`` on each invocation. So a simple
``await next_call(request)`` on the unchanged ``RpcRequest`` envelope is
sufficient: the leaf re-snapshots auth state via
``AuthRefreshCoordinator.snapshot`` (which sees the refreshed tokens)
and re-builds headers + body. The full closure-callback contract from
ADR-009 §"AuthRefreshMiddleware constructor signature" activates in PR
13.x when the chain envelope carries url/headers/body and the leaf
shrinks to a pure ``Kernel.post``-shaped seam.

This regression-fix from PR 12.7 also closes here: pre-PR-12.7 the leaf's
``refreshed_this_call`` lived in the same loop as 429/5xx retries (one
refresh max per logical call). PR 12.7 split the loops, leaving each
``RetryMiddleware`` retry to spawn a fresh leaf invocation with its own
``refreshed_this_call`` — up to N refreshes per call. PR 12.8 collapses
this back: refresh is now a chain-level concern, ``RetryMiddleware`` is
unaware of refreshes, and the once-per-call contract is restored by the
fact that ``AuthRefreshMiddleware`` only retries ONCE per ``next_call``
invocation.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract,
``src/notebooklm/_session_auth.py`` for :class:`AuthRefreshCoordinator`
(coalesced refresh + auth-snapshot lock), and
``.sisyphus/plans/tier-12-13-greenfield-migration.md`` row 12.8 for the
PR sequence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx

from ._authed_transport import TransportAuthExpired
from ._middleware import NextCall, RpcRequest, RpcResponse
from ._session_config import CORE_LOGGER_NAME

if TYPE_CHECKING:
    from ._client_metrics import ClientMetrics


class AuthRefreshMiddleware:
    """Chain middleware that retries authed POSTs once after refreshing tokens.

    Conforms to :class:`notebooklm._middleware.Middleware` — ``__call__``
    matches the Protocol so instances are assignable into a
    ``Sequence[Middleware]``.

    Constructor inputs (all wired by ``Session.__init__``):

    - ``refresh_callable``: a zero-arg async callable that drives one
      coalesced auth refresh. Production wires
      ``lambda: Session._await_refresh(self)`` which delegates to
      :meth:`AuthRefreshCoordinator.await_refresh`. The middleware never
      reaches into the coordinator directly; this keeps the seam thin
      and testable.
    - ``is_auth_error``: predicate that decides whether an exception is
      an auth failure (HTTP 400 / 401 / 403). Production wires
      :func:`notebooklm._session_helpers.is_auth_error` through a lambda
      that resolves it via the canonical module's globals at call time,
      so ``monkeypatch.setattr("notebooklm._session_helpers.is_auth_error",
      ...)`` reaches the chain live; tests that build the middleware
      directly typically pass the function itself.
    - ``refresh_callback_enabled``: a zero-arg callable returning ``True``
      iff a refresh callback is wired on the host. Production wires
      ``lambda: self._auth_coord._refresh_callback is not None`` so a
      client built without ``refresh_callback`` skips the refresh path
      entirely (matches the legacy leaf gate on
      ``host._refresh_callback is not None``).
    - ``refresh_retry_delay``: zero-arg callable returning the
      post-refresh sleep duration. Production wires
      ``lambda: self._refresh_retry_delay`` so a test that mutates the
      attr on the live client still takes effect (matches the live-binding
      contract preserved for retry budgets in PR 12.7).
    - ``sleep``: optional sleep injection (defaults to :func:`asyncio.sleep`
      resolved at call time). Same lazy-resolve pattern as
      :class:`RetryMiddleware`.
    - ``logger``: structured logger for the "auth error detected" /
      "refresh successful" / "refresh failed" info / warning lines.
      Defaults to the project-canonical ``notebooklm._core`` logger so
      ``caplog.at_level(..., logger="notebooklm._core")`` keeps matching.
    - ``metrics``: a :class:`ClientMetrics` whose ``increment(...)`` is
      called once per successful refresh (matches the legacy
      ``host._increment_metrics(rpc_auth_retries=1)`` site).
    """

    def __init__(
        self,
        *,
        refresh_callable: Callable[[], Awaitable[None]],
        is_auth_error: Callable[[Exception], bool],
        refresh_callback_enabled: Callable[[], bool],
        refresh_retry_delay: Callable[[], float],
        sleep: Callable[[float], Awaitable[object]] | None = None,
        logger: logging.Logger | None = None,
        metrics: ClientMetrics | None = None,
    ) -> None:
        self._refresh_callable = refresh_callable
        self._is_auth_error = is_auth_error
        self._refresh_callback_enabled = refresh_callback_enabled
        self._refresh_retry_delay = refresh_retry_delay
        # See ``RetryMiddleware._resolve_sleep`` for the lazy-binding rationale.
        self._sleep = sleep
        self._logger = logger or logging.getLogger(CORE_LOGGER_NAME)
        self._metrics = metrics

    def _resolve_sleep(self) -> Callable[[float], Awaitable[object]]:
        return self._sleep if self._sleep is not None else asyncio.sleep

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Catch auth-error ``HTTPStatusError``, refresh, retry exactly once.

        Reads ``log_label`` from ``request.context`` for log lines (defensive
        sentinel fallback matches DrainMiddleware / RetryMiddleware /
        ErrorInjectionMiddleware).

        Tracks ``context["auth_refreshed"]`` to enforce **at most one
        refresh per logical call** even when ``RetryMiddleware`` (outside
        this middleware) re-invokes the chain on a 429/5xx that fires
        after a successful refresh. Without this flag the sequence
        ``401 → refresh → 429 → Retry retry → 401`` would refresh twice
        (codex iter-1 catch on PR 12.8). With it, the second 401
        propagates without a redundant refresh, matching the pre-PR-12.7
        "one refresh max per logical call" contract.

        Pass-through paths:
        - No refresh callback configured → propagate any exception unchanged.
        - Exception is not an auth error → propagate.
        - Refresh already done for this logical call → propagate.
        - First ``next_call`` raises something non-``HTTPStatusError`` → propagate.

        Refresh-and-retry path:
        1. ``next_call`` raises ``httpx.HTTPStatusError`` AND
           ``is_auth_error(exc)`` returns True AND no prior refresh.
        2. Call ``refresh_callable()`` (coalesced single-flight via
           :class:`AuthRefreshCoordinator`).
        3. Mark ``context["auth_refreshed"] = True`` on success.
        4. If the refresh callable itself raises, wrap in
           ``TransportAuthExpired(original=exc)`` and propagate.
        5. Optional post-refresh sleep (``refresh_retry_delay``).
        6. Increment ``rpc_auth_retries`` metric.
        7. Re-invoke ``next_call(request)`` — exactly once. If the retry
           also raises, propagate unchanged (no second refresh,
           no recursion).
        """
        log_label = request.context.get("log_label", "<unknown-chain-call>")
        try:
            return await next_call(request)
        except httpx.HTTPStatusError as exc:
            if (
                not self._refresh_callback_enabled()
                or not self._is_auth_error(exc)
                or request.context.get("auth_refreshed")
            ):
                raise

            self._logger.info(
                "%s auth error detected, attempting token refresh",
                log_label,
            )
            try:
                await self._refresh_callable()
            except Exception as refresh_error:
                self._logger.warning("Token refresh failed: %s", refresh_error)
                raise TransportAuthExpired(
                    f"auth refresh failed for {log_label}",
                    original=exc,
                ) from refresh_error

            # Mark BEFORE the retry so a 429 thrown by the retry then
            # caught by ``RetryMiddleware`` (outside us) doesn't trigger
            # a second refresh when it re-enters our chain leg.
            request.context["auth_refreshed"] = True

            delay = self._refresh_retry_delay()
            if delay > 0:
                await self._resolve_sleep()(delay)
            self._logger.info("Token refresh successful, retrying %s", log_label)
            if self._metrics is not None:
                self._metrics.increment(rpc_auth_retries=1)

            # Exactly one retry. If this raises (auth or otherwise), the
            # exception propagates — the outer caller decides what to do
            # (chat error mapping, RetryMiddleware does NOT catch auth
            # errors so a persistent 401 won't burn its budget).
            return await next_call(request)


__all__ = ["AuthRefreshMiddleware"]
