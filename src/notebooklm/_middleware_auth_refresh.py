"""AuthRefreshMiddleware — 401/403/400-CSRF retry-with-refresh for the chain.

Per ADR-009 §"Chain ordering", ``AuthRefreshMiddleware`` sits just *inside*
``RetryMiddleware`` and just *outside* ``ErrorInjectionMiddleware``. The final
Tier-12 chain (post-PR 12.9, after ``SemaphoreMiddleware`` was inserted between
``Metrics`` and ``Retry``) is
``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]`` —
PR 12.8 inserts ``AuthRefresh`` between ``Retry`` and ``ErrorInjection`` so
this ordering is now realized end-to-end.

This middleware owns the **auth-refresh-once retry** loop. The leaf is a
*pure* ``Kernel.post`` terminal that lets ``httpx.HTTPStatusError`` /
``httpx.RequestError`` propagate raw for auth errors (the 429 / 5xx mapping
stays at the terminal since it feeds ``RetryMiddleware``). The middleware
catches the raw auth-error ``httpx.HTTPStatusError``, triggers a coalesced
refresh via :class:`AuthRefreshCoordinator`, rebuilds the request envelope,
then re-invokes ``next_call`` exactly once.

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
that duration AFTER the successful refresh and BEFORE the retry. This
preserves the historical transport behavior so a cassette that recorded the
post-refresh delay replays the same timing.

Request-materialization transition: ``Session`` now enters the chain with
the initial ``RpcRequest.url`` / ``.headers`` / ``.body`` populated and the
terminal consumes that envelope through ``Kernel.post``. After a successful
refresh this middleware re-snapshots auth state and replaces the request
envelope before retrying so the terminal never sends stale URL/body/header
values. See :meth:`AuthRefreshMiddleware._rebuild_request_after_refresh`
for the full in-place context-mutation contract and the paired terminal
rebuild invariant that keeps the post-refresh 429 retry from sending a
stale envelope.

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

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

import httpx

from ._middleware import NextCall, RpcRequest, RpcResponse, materialize_rpc_request
from ._middleware_context import (
    RPC_CONTEXT_AUTH_REFRESHED,
    RPC_CONTEXT_AUTH_SNAPSHOT,
    RPC_CONTEXT_BUILD_REQUEST,
    RPC_CONTEXT_LOG_LABEL,
)
from ._request_types import AuthSnapshot, BuildRequest
from ._session_config import CORE_LOGGER_NAME
from ._session_helpers import resolve_sleep
from ._transport_errors import TransportAuthExpired

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
      ``chain_host.await_refresh``, which dynamically delegates to
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
      ``lambda: chain_host._refresh_retry_delay`` so a test that mutates
      the attr on the live host still takes effect (matches the
      live-binding contract preserved for retry budgets in PR 12.7).
    - ``snapshot_provider``: optional async callable returning a fresh
      :class:`AuthSnapshot` after refresh. Production wires a lambda
      that invokes :meth:`AuthRefreshCoordinator.snapshot` with the
      explicit ``auth=session.auth`` collaborator (the
      Session-shaped ``_AuthRefreshHost`` Protocol was deleted in
      favor of per-method explicit args; the previously-load-bearing
      ``Session._snapshot`` thin wrapper was inlined in PR #4b of the
      session-refactor arc); tests that omit ``snapshot_provider``
      preserve the older "retry the same request" unit shape.
    - ``sleep``: optional sleep injection (defaults to :func:`asyncio.sleep`
      resolved at call time via :func:`_session_helpers.resolve_sleep` —
      the same shared helper :class:`RetryMiddleware` uses).
    - ``logger``: structured logger for the "auth error detected" /
      "refresh successful" / "refresh failed" info / warning lines.
      Defaults to the project-canonical ``notebooklm._core`` logger so
      ``caplog.at_level(..., logger="notebooklm._core")`` keeps matching.
    - ``metrics``: a :class:`ClientMetrics` whose ``increment(...)`` is
      called once per successful refresh (replaces the historical
      ``host._increment_metrics(rpc_auth_retries=1)`` site —
      ``Session._increment_metrics`` was a thin forward to this
      ``ClientMetrics.increment`` call and was deleted in Wave 11b of
      the session-decoupling arc, so middleware now reaches the
      collaborator directly).
    """

    def __init__(
        self,
        *,
        refresh_callable: Callable[[], Awaitable[None]],
        is_auth_error: Callable[[Exception], bool],
        refresh_callback_enabled: Callable[[], bool],
        refresh_retry_delay: Callable[[], float],
        snapshot_provider: Callable[[], Awaitable[AuthSnapshot]] | None = None,
        sleep: Callable[[float], Awaitable[object]] | None = None,
        logger: logging.Logger | None = None,
        metrics: ClientMetrics | None = None,
    ) -> None:
        self._refresh_callable = refresh_callable
        self._is_auth_error = is_auth_error
        self._refresh_callback_enabled = refresh_callback_enabled
        self._refresh_retry_delay = refresh_retry_delay
        self._snapshot_provider = snapshot_provider
        # Late-binding rationale lives on ``_session_helpers.resolve_sleep``.
        self._sleep = sleep
        self._logger = logger or logging.getLogger(CORE_LOGGER_NAME)
        self._metrics = metrics

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Catch auth-error ``HTTPStatusError``, refresh, retry exactly once.

        Reads ``log_label`` from ``request.context`` for log lines (defensive
        sentinel fallback matches DrainMiddleware / RetryMiddleware /
        ErrorInjectionMiddleware).

        Tracks ``RPC_CONTEXT_AUTH_REFRESHED`` to enforce **at most one
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
        3. Mark ``RPC_CONTEXT_AUTH_REFRESHED`` on success.
        4. If the refresh callable itself raises, wrap in
           ``TransportAuthExpired(original=exc)`` and propagate.
        5. Optional post-refresh sleep (``refresh_retry_delay``).
        6. Increment ``rpc_auth_retries`` metric.
        7. Rebuild the request envelope when a ``snapshot_provider`` and
           ``RPC_CONTEXT_BUILD_REQUEST`` are available.
        8. Re-invoke ``next_call(retry_request)`` — exactly once. If the
           retry also raises, propagate unchanged (no second refresh,
           no recursion).
        """
        log_label = request.context.get(RPC_CONTEXT_LOG_LABEL, "<unknown-chain-call>")
        try:
            return await next_call(request)
        except httpx.HTTPStatusError as exc:
            if (
                not self._refresh_callback_enabled()
                or not self._is_auth_error(exc)
                or request.context.get(RPC_CONTEXT_AUTH_REFRESHED)
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
            request.context[RPC_CONTEXT_AUTH_REFRESHED] = True

            delay = self._refresh_retry_delay()
            if delay > 0:
                await resolve_sleep(self._sleep)(delay)
            self._logger.info("Token refresh successful, retrying %s", log_label)
            if self._metrics is not None:
                self._metrics.increment(rpc_auth_retries=1)

            retry_request = await self._rebuild_request_after_refresh(request)

            # Exactly one retry. If this raises (auth or otherwise), the
            # exception propagates — the outer caller decides what to do
            # (chat error mapping, RetryMiddleware does NOT catch auth
            # errors so a persistent 401 won't burn its budget).
            return await next_call(retry_request)

    async def _rebuild_request_after_refresh(self, request: RpcRequest) -> RpcRequest:
        """Return a refreshed request envelope when production collaborators exist.

        After the fresh snapshot await returns, keep the context update and
        envelope materialization synchronous. The terminal still performs a
        final freshness check immediately before ``Kernel.post`` because inner
        middlewares may await between this retry rebuild and the wire.

        **In-place context mutation is the deliberate cross-boundary carrier
        for refreshed auth state and the once-per-call refresh guard.** This
        method (and its caller :meth:`__call__`) intentionally mutates the
        inbound ``request.context`` rather than copying it, because two
        pieces of shared state must survive the ``Retry`` ↔ ``AuthRefresh``
        boundary:

        - ``RPC_CONTEXT_AUTH_REFRESHED`` is written by :meth:`__call__` on
          the **original** ``request.context`` just before this rebuild
          runs. ``RetryMiddleware`` lives one layer *outside* this
          middleware and, on a 429 / 5xx caught after the refresh, re-invokes
          the chain with that same original ``RpcRequest``. The marker on
          the shared context suppresses a second refresh on the
          original-request retry, preserving the "exactly one refresh per
          logical call" contract pinned in ADR-009 §"Retry semantics".

        - ``RPC_CONTEXT_AUTH_SNAPSHOT`` is updated below to the freshly
          captured snapshot. Because :func:`materialize_rpc_request`
          retains the inbound ``context`` dict by reference (see
          :func:`notebooklm._middleware.materialize_rpc_request`), the
          returned ``retry_request`` and the original ``request`` share
          that same context dict and therefore see the same updated
          snapshot. This mutation is what lets the terminal freshness
          guard (:meth:`SessionTransport.refresh_request_for_current_auth`)
          observe the post-refresh snapshot when ``RetryMiddleware`` later
          retries the original request after a 429.

        The companion invariant — and the reason the in-place mutation is
        safe even though the original request's ``url`` / ``headers`` /
        ``body`` are still pre-refresh — is that
        :meth:`SessionTransport.refresh_request_for_current_auth` rebuilds
        URL / body / cookies from the current snapshot on **every** terminal
        attempt, unconditionally. Both halves are load-bearing and must be
        preserved together; deleting the unconditional rebuild reintroduces
        the stale-envelope path on the post-refresh 429 retry.
        """
        if self._snapshot_provider is None:
            return request

        raw_build_request = request.context.get(RPC_CONTEXT_BUILD_REQUEST)
        if raw_build_request is None:
            return request

        build_request = cast(BuildRequest, raw_build_request)
        snapshot = await self._snapshot_provider()
        # Keep ``auth_snapshot`` and the rebuilt envelope paired in one
        # synchronous block; see ``test_concurrency_refresh_race``.
        request.context[RPC_CONTEXT_AUTH_SNAPSHOT] = snapshot
        return materialize_rpc_request(
            build_request=build_request,
            snapshot=snapshot,
            context=request.context,
        )


__all__ = ["AuthRefreshMiddleware"]
