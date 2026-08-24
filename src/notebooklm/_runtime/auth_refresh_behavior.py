"""AuthRefreshBehavior — 401/403/400-CSRF retry-with-refresh for the chain.

Per ADR-0009 §"Chain ordering", ``AuthRefreshBehavior`` sits just *inside*
``RetryBehavior`` and just *outside* ``TracingBehavior``. The chain is
``[Drain, Metrics, Semaphore, Retry, AuthRefresh, Tracing]``.

This middleware owns the **auth-refresh-once retry** loop. The leaf is a
*pure* ``Kernel.post`` terminal that lets ``httpx.HTTPStatusError`` /
``httpx.RequestError`` propagate raw for auth errors (the 429 / 5xx mapping
stays at the terminal since it feeds ``RetryBehavior``). The middleware
catches the raw auth-error ``httpx.HTTPStatusError``, triggers a coalesced
refresh via :class:`AuthRefreshCoordinator`, rebuilds the request envelope,
then re-invokes ``next_call`` exactly once.

Why "exactly once": ADR-0009 §"Retry semantics" pins
"**exactly one** retry per ``next_call`` invocation. If the retry also
raises 401, the exception propagates — no second retry, no recursion."
``RetryBehavior`` outside this middleware does NOT retry on auth
errors (it catches only ``TransportRateLimited`` /
``TransportServerError``), so a persistent 401 surfaces cleanly to the
caller without burning the rate-limit / server-error budget on auth
loops.

Refresh-failure path: if the refresh callback itself raises (network
flake, login expired, etc.), the middleware wraps the original
``httpx.HTTPStatusError`` in :class:`TransportAuthExpired` so callers
that key on the transport exception type still see a coherent shape.

Pre-refresh sleep: when ``refresh_retry_delay > 0`` the middleware sleeps
that duration AFTER the successful refresh and BEFORE the retry. This
preserves the historical transport behavior so a cassette that recorded the
post-refresh delay replays the same timing.

Request-materialization transition: ``NotebookLMClient`` now enters the chain with
the initial ``RpcRequest.url`` / ``.headers`` / ``.body`` populated and the
terminal consumes that envelope through ``Kernel.post``. After a successful
refresh this middleware re-snapshots auth state and replaces the request
envelope before retrying so the terminal never sends stale URL/body/header
values. See :meth:`AuthRefreshBehavior._rebuild_request_after_refresh`
for the typed-state publication contract and the paired terminal rebuild
invariant that keeps the post-refresh 429 retry from sending a stale envelope.

Refresh is a chain-level concern: ``RetryBehavior`` is unaware of
refreshes, and the once-per-call contract holds because
``AuthRefreshBehavior`` only retries ONCE per ``next_call`` invocation.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract and
``src/notebooklm/_runtime/auth.py`` for :class:`AuthRefreshCoordinator`
(coalesced refresh + auth-snapshot lock).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx

from .._auth_refresh_retry import refresh_and_count
from .._request_types import AuthSnapshot
from .._transport_errors import TransportAuthExpired
from .config import CORE_LOGGER_NAME
from .helpers import resolve_sleep
from .rpc_call import NextCall, RpcRequest, RpcResponse, materialize_rpc_request

if TYPE_CHECKING:
    from .._client_metrics import ClientMetrics


class AuthRefreshBehavior:
    """Pipeline behavior that retries authed POSTs once after refreshing tokens.

    Constructor inputs are wired by :class:`RuntimePipeline`:

    - ``refresh_callable``: a zero-arg async callable that drives one
      coalesced auth refresh. Production wires
      the provider refresh owner, which delegates to
      :meth:`AuthRefreshCoordinator.await_refresh`. The behavior never
      reaches into the coordinator directly; this keeps the seam thin
      and testable.
    - ``is_auth_error``: predicate that decides whether an exception is
      an auth failure (HTTP 400 / 401 / 403).
    - ``refresh_callback_enabled``: a zero-arg callable returning ``True``
      iff a refresh callback is wired on the coordinator. Production wires
      ``lambda: collaborators.auth_coord.has_refresh_callback`` so a
      client built without ``refresh_callback`` skips the refresh path
      entirely.
    - ``refresh_retry_delay``: zero-arg callable returning the
      post-refresh sleep duration. Production wires the pipeline's fixed
      construction-time value through this callable; focused behavior tests
      may supply a mutable provider when they need to exercise live resolution.
    - ``snapshot_provider``: optional async callable returning a fresh
      :class:`AuthSnapshot` after refresh. Production wires a lambda
      that invokes :meth:`AuthRefreshCoordinator.snapshot` with the
      client's current ``auth`` tokens; tests that omit
      ``snapshot_provider``
      preserve the older "retry the same request" unit shape.
    - ``sleep``: optional sleep injection (defaults to :func:`asyncio.sleep`
      resolved at call time via :func:`_runtime.helpers.resolve_sleep` —
      the same shared helper :class:`RetryBehavior` uses).
    - ``logger``: structured logger for the "auth error detected" /
      "refresh successful" / "refresh failed" info / warning lines.
      Defaults to the project-canonical ``notebooklm._core`` logger so
      ``caplog.at_level(..., logger="notebooklm._core")`` keeps matching.
    - ``metrics``: a :class:`ClientMetrics` whose ``increment(...)`` is
      called once per successful refresh. The middleware reaches this
      collaborator directly.
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
        # Late-binding rationale lives on ``_runtime.helpers.resolve_sleep``.
        self._sleep = sleep
        self._logger = logger or logging.getLogger(CORE_LOGGER_NAME)
        self._metrics = metrics

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Catch auth-error ``HTTPStatusError``, refresh, retry exactly once.

        Reads ``request.state.log_label`` for log lines (the defensive
        sentinel fallback matches DrainBehavior / RetryBehavior /
        the retired test-only error-injection stage).

        Enforces **at most one refresh per logical call** even when
        ``RetryBehavior`` (outside this middleware) re-invokes the chain on
        a 429/5xx that fires after a successful refresh. Without this guard
        the sequence ``401 → refresh → 429 → Retry retry → 401`` would refresh
        twice. With it, the second 401
        propagates without a redundant refresh, matching the
        "one refresh max per logical call" contract.

        The guard reads the shared
        :class:`notebooklm._auth_refresh_retry.RefreshBudget` from
        ``request.state.refresh_budget`` when present. The backend-owned
        :class:`notebooklm._web.runtime.WebExecutionRuntime` seeds one per
        logical ``rpc_call`` so this HTTP-status layer and the decoded-RPC
        layer share ONE refresh allowance and a
        ``wire-401 → refresh → decoded-auth-error`` sequence cannot drive two
        refreshes (issue #1205). ``request.state.auth_refreshed`` is the
        fallback when no budget is threaded (for example, the chat path).
        Because retries retain the exact same :class:`RpcCallState` object,
        RetryBehavior re-entry and the terminal freshness rebuild both
        observe the published post-refresh state.

        Pass-through paths:
        - No refresh callback configured → propagate any exception unchanged.
        - Exception is not an auth error → propagate.
        - Refresh already done for this logical call → propagate.
        - ``request.state.disable_internal_retries`` is set → propagate.
          The flag is the post-resolution effective bool produced by
          :func:`_idempotency.resolve_effective_disable_internal_retries`
          before chain entry, so a non-idempotent / probe-then-create
          method is NOT replayed after an auth error (issue #1157). A
          mid-flight 401/403 can land *after* the server committed the
          write, so re-POSTing would duplicate the resource / invite /
          generation. Surfacing the original auth error lets the caller's
          probe-then-create wrapper disambiguate instead.
        - First ``next_call`` raises something non-``HTTPStatusError`` → propagate.

        Refresh-and-retry path:
        1. ``next_call`` raises ``httpx.HTTPStatusError`` AND
           ``is_auth_error(exc)`` returns True AND no prior refresh AND
           ``disable_internal_retries`` is not set.
        2. Call ``refresh_callable()`` (coalesced single-flight via
           :class:`AuthRefreshCoordinator`).
        3. Publish ``auth_refreshed`` on the shared typed state after success.
        4. If the refresh callable itself raises, wrap in
           ``TransportAuthExpired(original=exc)`` and propagate.
        5. Optional post-refresh sleep (``refresh_retry_delay``).
        6. Increment ``rpc_auth_retries`` metric.
        7. Rebuild the request envelope when a ``snapshot_provider`` and
           ``request.state.build_request`` are available.
        8. Re-invoke ``next_call(retry_request)`` — exactly once. If the
           retry also raises, propagate unchanged (no second refresh,
           no recursion).
        """
        state = request.state
        log_label = state.log_label or "<unknown-chain-call>"
        try:
            return await next_call(request)
        except httpx.HTTPStatusError as exc:
            budget = state.refresh_budget
            already_refreshed = not budget.available if budget is not None else state.auth_refreshed
            if (
                not self._refresh_callback_enabled()
                or not self._is_auth_error(exc)
                or already_refreshed
                or state.disable_internal_retries
            ):
                # ``disable_internal_retries`` is the post-resolution
                # effective bool (see :func:`_idempotency.
                # resolve_effective_disable_internal_retries`). When set, the
                # write is non-idempotent / probe-then-create and may have
                # already committed before the auth error surfaced — replaying
                # it would duplicate the side effect (issue #1157), so we
                # propagate the original auth error untouched.
                raise

            # Bind the original auth error to a stable local: ``except ... as
            # exc`` unbinds ``exc`` at block exit, and the failure wrapper
            # closure must keep it after that point.
            original_auth_error = exc

            # The logical call's aggregate deadline, seeded by WebExecutionRuntime
            # (issue #1873). Passing it clamps the post-refresh sleep to the
            # time remaining since T0 so a ``refresh_retry_delay`` larger than
            # the remaining budget cannot re-POST past the logical call's
            # deadline — mirroring the decode-time refresh path in
            # ``WebExecutionRuntime.try_refresh_and_retry``. Absent (chat path) → the
            # historical unclamped delay is slept.
            retry_deadline = state.retry_deadline

            # Shared refresh body (log → refresh → on-failure raise → sleep →
            # log → metric). Refresh failure wraps the original auth
            # ``HTTPStatusError`` in ``TransportAuthExpired`` — the chain's
            # historical refresh-failure shape that callers / tests pin.
            await refresh_and_count(
                refresh=self._refresh_callable,
                on_refresh_failure=lambda _refresh_error: TransportAuthExpired(
                    f"auth refresh failed for {log_label}",
                    original=original_auth_error,
                ),
                sleep=resolve_sleep(self._sleep),
                refresh_retry_delay=self._refresh_retry_delay(),
                log_label=log_label,
                logger=self._logger,
                metrics=self._metrics,
                retry_deadline=retry_deadline,
            )

            # Mark AFTER a successful refresh (a refresh failure raised above
            # and never reaches here). Consuming the shared budget is what
            # blocks the decoded-RPC layer in ``WebExecutionRuntime`` from refreshing
            # a second time on the SAME logical call (issue #1205). The
            # per-chain boolean is also set so a 429 thrown by the retry then
            # caught by ``RetryBehavior`` (outside us) doesn't trigger a
            # second refresh when it re-enters our chain leg, and so the
            # terminal freshness rebuild observes the post-refresh marker.
            if budget is not None:
                budget.consume()
            state.mark_auth_refreshed()

            retry_request = await self._rebuild_request_after_refresh(request)

            # Exactly one retry. If this raises (auth or otherwise), the
            # exception propagates — the outer caller decides what to do
            # (chat error mapping, RetryBehavior does NOT catch auth
            # errors so a persistent 401 won't burn its budget).
            return await next_call(retry_request)

    async def _rebuild_request_after_refresh(self, request: RpcRequest) -> RpcRequest:
        """Return a refreshed request envelope when production collaborators exist.

        After the fresh snapshot await returns, keep the state publication and
        envelope materialization synchronous. The terminal still performs a
        final freshness check immediately before ``Kernel.post`` because inner
        middlewares may await between this retry rebuild and the wire.

        **The identity-shared :class:`RpcCallState` is the deliberate
        cross-boundary carrier for refreshed auth state and the once-per-call
        refresh guard.** :meth:`__call__` publishes ``auth_refreshed`` on the
        state carried by the original request. ``RetryBehavior`` lives one
        layer *outside* this middleware and, on a 429 / 5xx caught after the
        refresh, re-invokes the chain with that original ``RpcRequest``. The
        published marker suppresses a second refresh and preserves the
        "exactly one refresh per logical call" contract pinned in ADR-0009
        §"Retry semantics".

        This method publishes the fresh ``auth_snapshot`` through
        :meth:`RpcCallState.publish_auth_snapshot`. Because
        :func:`materialize_rpc_request` retains the exact state object, the
        returned ``retry_request`` and the original request observe the same
        snapshot. That bounded publication lets the terminal freshness guard
        (:meth:`RuntimeTransport.refresh_request_for_current_auth`) observe
        the post-refresh snapshot when ``RetryBehavior`` later retries the
        original request after a 429.

        The companion invariant — and the reason this shared state is safe
        even though the original request's ``url`` / ``headers`` / ``body``
        are still pre-refresh — is that
        :meth:`RuntimeTransport.refresh_request_for_current_auth` rebuilds
        URL / body / cookies from the current snapshot on **every** terminal
        attempt, unconditionally. Both halves are load-bearing and must be
        preserved together; deleting the unconditional rebuild reintroduces
        the stale-envelope path on the post-refresh 429 retry.
        """
        if self._snapshot_provider is None:
            return request

        build_request = request.state.build_request
        if build_request is None:
            return request

        snapshot = await self._snapshot_provider()
        # Keep ``auth_snapshot`` and the rebuilt envelope paired in one
        # synchronous block; see ``test_concurrency_refresh_race``.
        request.state.publish_auth_snapshot(snapshot, refreshed=True)
        return materialize_rpc_request(
            build_request=build_request,
            snapshot=snapshot,
            state=request.state,
        )


__all__ = ["AuthRefreshBehavior"]
