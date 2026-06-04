"""AuthRefreshMiddleware — 401/403/400-CSRF retry-with-refresh for the chain.

Per ADR-0009 §"Chain ordering", ``AuthRefreshMiddleware`` sits just *inside*
``RetryMiddleware`` and just *outside* ``ErrorInjectionMiddleware``. The chain
is ``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]``.

This middleware owns the **auth-refresh-once retry** loop. The leaf is a
*pure* ``Kernel.post`` terminal that lets ``httpx.HTTPStatusError`` /
``httpx.RequestError`` propagate raw for auth errors (the 429 / 5xx mapping
stays at the terminal since it feeds ``RetryMiddleware``). The middleware
catches the raw auth-error ``httpx.HTTPStatusError``, triggers a coalesced
refresh via :class:`AuthRefreshCoordinator`, rebuilds the request envelope,
then re-invokes ``next_call`` exactly once.

Why "exactly once": ADR-0009 §"Retry semantics" pins
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

Pre-refresh sleep: when ``refresh_retry_delay > 0`` the middleware sleeps
that duration AFTER the successful refresh and BEFORE the retry. This
preserves the historical transport behavior so a cassette that recorded the
post-refresh delay replays the same timing.

Request-materialization transition: ``NotebookLMClient`` now enters the chain with
the initial ``RpcRequest.url`` / ``.headers`` / ``.body`` populated and the
terminal consumes that envelope through ``Kernel.post``. After a successful
refresh this middleware re-snapshots auth state and replaces the request
envelope before retrying so the terminal never sends stale URL/body/header
values. See :meth:`AuthRefreshMiddleware._rebuild_request_after_refresh`
for the full in-place context-mutation contract and the paired terminal
rebuild invariant that keeps the post-refresh 429 retry from sending a
stale envelope.

Refresh is a chain-level concern: ``RetryMiddleware`` is unaware of
refreshes, and the once-per-call contract holds because
``AuthRefreshMiddleware`` only retries ONCE per ``next_call`` invocation.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract and
``src/notebooklm/_runtime/auth.py`` for :class:`AuthRefreshCoordinator`
(coalesced refresh + auth-snapshot lock).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

import httpx

from .._auth_refresh_retry import RefreshBudget, refresh_and_count
from .._request_types import AuthSnapshot, BuildRequest
from .._runtime.config import CORE_LOGGER_NAME
from .._runtime.helpers import resolve_sleep
from .._transport_errors import TransportAuthExpired
from .context import (
    RPC_CONTEXT_AUTH_REFRESHED,
    RPC_CONTEXT_AUTH_SNAPSHOT,
    RPC_CONTEXT_BUILD_REQUEST,
    RPC_CONTEXT_DISABLE_INTERNAL_RETRIES,
    RPC_CONTEXT_LOG_LABEL,
    RPC_CONTEXT_REFRESH_BUDGET,
)
from .core import NextCall, RpcRequest, RpcResponse, materialize_rpc_request

if TYPE_CHECKING:
    from .._client_metrics import ClientMetrics


class AuthRefreshMiddleware:
    """Chain middleware that retries authed POSTs once after refreshing tokens.

    Conforms to :class:`notebooklm._middleware.core.Middleware` — ``__call__``
    matches the Protocol so instances are assignable into a
    ``Sequence[Middleware]``.

    Constructor inputs (all wired by
    :func:`notebooklm._runtime.init.wire_middleware_chain`, driven from
    ``NotebookLMClient.__init__``):

    - ``refresh_callable``: a zero-arg async callable that drives one
      coalesced auth refresh. Production wires
      ``chain_host.await_refresh``, which dynamically delegates to
      :meth:`AuthRefreshCoordinator.await_refresh`. The middleware never
      reaches into the coordinator directly; this keeps the seam thin
      and testable.
    - ``is_auth_error``: predicate that decides whether an exception is
      an auth failure (HTTP 400 / 401 / 403). Production wires
      :func:`notebooklm._runtime.helpers.is_auth_error` through a lambda
      that resolves it via the canonical module's globals at call time,
      so ``monkeypatch.setattr("notebooklm._runtime.helpers.is_auth_error",
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
      live-binding contract used for retry budgets).
    - ``snapshot_provider``: optional async callable returning a fresh
      :class:`AuthSnapshot` after refresh. Production wires a lambda
      that invokes :meth:`AuthRefreshCoordinator.snapshot` with the
      client's current ``auth`` tokens; tests that omit
      ``snapshot_provider``
      preserve the older "retry the same request" unit shape.
    - ``sleep``: optional sleep injection (defaults to :func:`asyncio.sleep`
      resolved at call time via :func:`_runtime.helpers.resolve_sleep` —
      the same shared helper :class:`RetryMiddleware` uses).
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

        Reads ``log_label`` from ``request.context`` for log lines (defensive
        sentinel fallback matches DrainMiddleware / RetryMiddleware /
        ErrorInjectionMiddleware).

        Enforces **at most one refresh per logical call** even when
        ``RetryMiddleware`` (outside this middleware) re-invokes the chain on
        a 429/5xx that fires after a successful refresh. Without this guard
        the sequence ``401 → refresh → 429 → Retry retry → 401`` would refresh
        twice. With it, the second 401
        propagates without a redundant refresh, matching the
        "one refresh max per logical call" contract.

        The guard reads a shared
        :class:`notebooklm._auth_refresh_retry.RefreshBudget` from
        ``request.context[RPC_CONTEXT_REFRESH_BUDGET]`` when present — the
        executor seeds one per logical ``rpc_call`` so this HTTP-status layer
        and the decoded-RPC layer in :class:`RpcExecutor` share ONE refresh
        allowance and a ``wire-401 → refresh → decoded-auth-error`` sequence
        cannot drive two refreshes (issue #1205). The per-chain
        ``RPC_CONTEXT_AUTH_REFRESHED`` boolean is still written (and read as a
        fallback when no budget is threaded, e.g. the chat path) so the
        RetryMiddleware-re-entry suppression and the terminal freshness
        rebuild keep observing the post-refresh marker on the shared context.

        Pass-through paths:
        - No refresh callback configured → propagate any exception unchanged.
        - Exception is not an auth error → propagate.
        - Refresh already done for this logical call → propagate.
        - ``disable_internal_retries`` is set on the context → propagate.
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
            budget = cast(
                "RefreshBudget | None",
                request.context.get(RPC_CONTEXT_REFRESH_BUDGET),
            )
            already_refreshed = (
                not budget.available
                if budget is not None
                else bool(request.context.get(RPC_CONTEXT_AUTH_REFRESHED))
            )
            if (
                not self._refresh_callback_enabled()
                or not self._is_auth_error(exc)
                or already_refreshed
                or bool(request.context.get(RPC_CONTEXT_DISABLE_INTERNAL_RETRIES, False))
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
            )

            # Mark AFTER a successful refresh (a refresh failure raised above
            # and never reaches here). Consuming the shared budget is what
            # blocks the decoded-RPC layer in ``RpcExecutor`` from refreshing
            # a second time on the SAME logical call (issue #1205). The
            # per-chain boolean is also set so a 429 thrown by the retry then
            # caught by ``RetryMiddleware`` (outside us) doesn't trigger a
            # second refresh when it re-enters our chain leg, and so the
            # terminal freshness rebuild observes the post-refresh marker.
            if budget is not None:
                budget.consume()
            request.context[RPC_CONTEXT_AUTH_REFRESHED] = True

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
          logical call" contract pinned in ADR-0009 §"Retry semantics".

        - ``RPC_CONTEXT_AUTH_SNAPSHOT`` is updated below to the freshly
          captured snapshot. Because :func:`materialize_rpc_request`
          retains the inbound ``context`` dict by reference (see
          :func:`notebooklm._middleware.core.materialize_rpc_request`), the
          returned ``retry_request`` and the original ``request`` share
          that same context dict and therefore see the same updated
          snapshot. This mutation is what lets the terminal freshness
          guard (:meth:`RuntimeTransport.refresh_request_for_current_auth`)
          observe the post-refresh snapshot when ``RetryMiddleware`` later
          retries the original request after a 429.

        The companion invariant — and the reason the in-place mutation is
        safe even though the original request's ``url`` / ``headers`` /
        ``body`` are still pre-refresh — is that
        :meth:`RuntimeTransport.refresh_request_for_current_auth` rebuilds
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
