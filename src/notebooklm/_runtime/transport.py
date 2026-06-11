"""Authed POST transport collaborator — the middleware-chain leaf.

``RuntimeTransport`` owns the three pieces of the authed POST hot path
(history: docs/refactor-history.md):

* :meth:`RuntimeTransport.terminal` — the middleware-chain leaf. Sends
  the populated :class:`RpcRequest` via :meth:`Kernel.post` and maps the
  raw transport errors into the ``Transport*`` exception shapes consumed
  by ``RetryMiddleware`` / ``AuthRefreshMiddleware``.
* :meth:`RuntimeTransport.refresh_request_for_current_auth` — re-builds
  the envelope from ``RPC_CONTEXT_BUILD_REQUEST`` if a concurrent refresh
  moved the auth snapshot between materialization and the terminal POST.
* :meth:`RuntimeTransport.perform_authed_post` — the entry point the
  RPC executor / chat path call. Runs the
  loop-affinity guard, captures the current auth snapshot, materializes
  the request envelope, dispatches it through the wired middleware
  chain, and records the semaphore queue-wait latency.

:class:`MiddlewareChainHost` owns the chain leaf
(:meth:`MiddlewareChainHost._authed_post_chain_terminal`), the chain
slot (``chain_host._authed_post_chain``), and the three retry-budget
tunables (``_rate_limit_max_retries`` / ``_server_error_max_retries`` /
``_refresh_retry_delay``). ``perform_authed_post`` does not read the
retry-delay directly — the retry/backoff budget for the refresh path
is owned by ``AuthRefreshMiddleware`` and by
``RpcExecutor.try_refresh_and_retry``, both of which read
``chain_host._refresh_retry_delay`` live through provider lambdas wired
in ``_runtime.init.wire_middleware_chain``. Integration tests that
assign ``client._composed.chain_host._refresh_retry_delay = 0`` keep
steering the live delay.

Construction order in :func:`compose_client_internals`:
:func:`notebooklm._runtime.init.build_runtime_transport` constructs the
transport **before** :func:`wire_middleware_chain`. The wired chain
leaf is :meth:`MiddlewareChainHost._authed_post_chain_terminal` (a
one-line forward to :meth:`RuntimeTransport.terminal`) — wiring through
the host preserves the canonical fixture-rebind seam (tests that
swap the chain leaf or the chain itself rebind on the host directly).
The chain itself is reached by the transport through an injected
``chain_provider`` closure that reads
``chain_host._authed_post_chain`` live, late on every
:meth:`perform_authed_post` call; this both breaks the construction
cycle and preserves the long-standing test pattern of reassigning
``core._composed.chain_host._authed_post_chain`` to install a fake chain. The
:class:`AuthRefreshCoordinator` snapshot is reached via an injected
``snapshot_provider`` callable so :class:`RuntimeTransport` never has
to hold a direct back-reference to the composition root.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from .._middleware.context import (
    RPC_CONTEXT_AUTH_SNAPSHOT,
    RPC_CONTEXT_BUILD_REQUEST,
    RPC_CONTEXT_DISABLE_INTERNAL_RETRIES,
    RPC_CONTEXT_DISABLE_READ_TIMEOUT_RETRIES,
    RPC_CONTEXT_LOG_LABEL,
    RPC_CONTEXT_READ_TIMEOUT,
    RPC_CONTEXT_REFRESH_BUDGET,
    RPC_CONTEXT_RPC_METHOD,
    RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS,
)
from .._middleware.core import (
    NextCall,
    RpcRequest,
    RpcResponse,
    materialize_rpc_request,
)
from .._request_types import AuthSnapshot, BuildRequest
from .._transport_errors import raise_mapped_post_error

if TYPE_CHECKING:
    from .._auth_refresh_retry import RefreshBudget
    from .._client_metrics import ClientMetrics
    from .._kernel import Kernel


class RuntimeTransport:
    """Authed POST chain leaf and entry-point collaborator.

    Owns the three authed-POST hot-path methods.
    Does NOT own lifecycle (that stays on :class:`ClientLifecycle`) nor
    retry/refresh budget state (that lives on
    :class:`MiddlewareChainHost` and is threaded into middleware via
    provider lambdas).

    The chain reference is fetched late on every
    :meth:`perform_authed_post` — just before chain dispatch, after
    snapshot + materialization — through the injected ``chain_provider``
    closure (typically ``lambda: chain_host._authed_post_chain``). The
    lookup is intentionally deferred so a chain reassignment that
    happens while the snapshot capture awaits still steers the
    dispatch; the chain is read at the dispatch site.

    The injected ``logger`` is held so error messages mapped through
    :func:`notebooklm._transport_errors.raise_mapped_post_error` keep
    appearing under the historical session logger namespace rather than
    this module's namespace — preserving the log-filter / caplog
    vocabulary callers may already rely on.
    """

    def __init__(
        self,
        *,
        kernel: Kernel,
        snapshot_provider: Callable[[], Awaitable[AuthSnapshot]],
        chain_provider: Callable[[], NextCall | None],
        metrics: ClientMetrics,
        bound_loop_check: Callable[[], None],
        logger: logging.Logger,
    ) -> None:
        self._kernel = kernel
        self._snapshot_provider = snapshot_provider
        # Live-binding chain accessor. The wired chain is installed onto
        # :class:`MiddlewareChainHost` AFTER :class:`RuntimeTransport`
        # is constructed (the chain's leaf is :meth:`terminal`, so the
        # transport must exist first). Tests also reassign
        # ``core._composed.chain_host._authed_post_chain`` post-construction to
        # install a fake chain — going through a provider closure
        # (called late in :meth:`perform_authed_post`) ensures those
        # reassignments take effect on the next call without any
        # further mutation here.
        self._chain_provider = chain_provider
        self._metrics = metrics
        self._bound_loop_check = bound_loop_check
        self._logger = logger

    async def refresh_request_for_current_auth(self, request: RpcRequest) -> RpcRequest:
        """Rebuild the envelope from the current auth snapshot before every POST.

        This guard is **load-bearing**: it runs on *every* terminal attempt
        (including retries driven by ``RetryMiddleware`` for 429 / 5xx) and
        unconditionally rebuilds ``RpcRequest.url`` / ``.headers`` / ``.body``
        from a freshly captured :class:`AuthSnapshot` whenever
        ``RPC_CONTEXT_BUILD_REQUEST`` is present. The unconditional rebuild
        is the runtime correctness fix for the stale-envelope path that
        existed when the freshness check short-circuited on snapshot
        equality:

        1. Initial attempt: snapshot ``S_old`` is captured by
           :meth:`perform_authed_post`, the envelope is materialized, and
           the request enters the chain.
        2. Terminal POSTs and the response is HTTP 401.
        3. :class:`AuthRefreshMiddleware` (just inside ``RetryMiddleware``)
           catches the auth error, refreshes credentials, mutates
           ``request.context[RPC_CONTEXT_AUTH_SNAPSHOT]`` to ``S_new``
           in-place (see
           :meth:`AuthRefreshMiddleware._rebuild_request_after_refresh`
           for the contract — that mutation is the carrier of the new
           snapshot across the ``Retry`` ↔ ``AuthRefresh`` boundary), and
           hands a freshly built ``retry_request`` to the chain leaf.
        4. The retry attempt POSTs with the refreshed envelope and the
           response is HTTP 429.
        5. The 429 propagates back up to ``RetryMiddleware`` (outside
           ``AuthRefreshMiddleware``), which retries by re-invoking the
           chain with the **original** ``RpcRequest`` from step 1. That
           request's ``.url`` / ``.headers`` / ``.body`` were built from
           ``S_old`` even though its shared ``context`` dict now carries
           ``S_new`` (mutated in step 3).
        6. Without an unconditional rebuild here, a snapshot-equality
           short-circuit would compare ``S_new`` (in context) against
           ``S_new`` (freshly captured), declare "no change," and send the
           stale ``S_old`` envelope. The unconditional rebuild keeps
           ``URL`` / ``headers`` / ``body`` aligned with
           :attr:`Kernel._client.cookies` (which carries the refreshed
           cookie jar) for every attempt.

        Idempotence on the happy path: when no refresh ran, the snapshot
        captured here equals the snapshot used by
        :meth:`perform_authed_post`, so the rebuilt envelope is
        byte-identical to the inbound one. The extra ``build_request``
        invocation per attempt is the cost of the freshness invariant.

        AST guarded — see
        :func:`tests.unit.test_concurrency_refresh_race.test_terminal_freshness_check_has_no_await_after_materialization`
        which reads the source of this method to assert no ``await``
        follows :func:`materialize_rpc_request`. Any restructuring here
        must keep that invariant: the snapshot and the rebuilt envelope
        must be produced together with no suspension point between them
        so a concurrent refresh cannot move the cookie jar between the
        rebuild and :meth:`Kernel.post`.
        """
        context = request.context
        build_request = context.get(RPC_CONTEXT_BUILD_REQUEST)
        if build_request is None:
            return request

        current_snapshot = await self._snapshot_provider()
        context[RPC_CONTEXT_AUTH_SNAPSHOT] = current_snapshot
        return materialize_rpc_request(
            build_request=build_request,
            snapshot=current_snapshot,
            context=context,
        )

    async def terminal(self, request: RpcRequest) -> RpcResponse:
        """Chain leaf — sends the populated ``RpcRequest`` via ``Kernel.post``.

        The chain interface carries the actual HTTP request. The terminal
        reads ``RpcRequest.url`` / ``headers`` / ``body`` directly, maps raw
        ``Kernel.post`` errors into the transport exception shapes consumed
        by ``RetryMiddleware`` / ``AuthRefreshMiddleware``, and wraps the
        returned :class:`httpx.Response` in :class:`RpcResponse`.

        AST guarded — see
        :func:`tests.unit.test_concurrency_refresh_race.test_kernel_post_terminal_has_no_await_before_post_per_attempt`
        which reads the source of this method to assert no ``await``
        precedes the ``self._kernel.post(...)`` call inside the protective
        ``try`` block. A concurrent refresh between freshness rebuild and
        the POST would otherwise mismatch the cookie jar against the
        materialized headers.
        """
        request = await self.refresh_request_for_current_auth(request)
        context = request.context
        log_label = context.get(RPC_CONTEXT_LOG_LABEL, "<unknown-chain-call>")
        read_timeout = context.get(RPC_CONTEXT_READ_TIMEOUT)
        start = time.perf_counter()
        try:
            response = await self._kernel.post(
                request.url,
                headers=request.headers,
                body=request.body,
                read_timeout=read_timeout,
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            raise_mapped_post_error(
                log_label=log_label,
                exc=exc,
                start=start,
                logger=self._logger,
            )
        return RpcResponse(response=response, context=context)

    async def perform_authed_post(
        self,
        *,
        build_request: BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
        rpc_method: str | None = None,
        refresh_budget: RefreshBudget | None = None,
        read_timeout: float | None = None,
        disable_read_timeout_retries: bool = False,
    ) -> httpx.Response:
        """Authed POST entry point — routes through the middleware chain.

        Shared transport surface used by ``RpcExecutor._execute_once``
        (``_rpc_executor.py``) and ``_chat.transport``
        (``_chat/transport.py``); keep the same keyword-only signature.

        ``RpcRequest.url`` / ``headers`` / ``body`` are populated through
        :func:`materialize_rpc_request` before the chain sees the
        request. ``RPC_CONTEXT_BUILD_REQUEST`` remains as the bounded
        rebuild recipe for auth-refresh and pre-terminal freshness
        checks.

        ``refresh_budget`` is an optional
        :class:`notebooklm._auth_refresh_retry.RefreshBudget` seeded by the
        RPC executor so the HTTP-status refresh layer
        (:class:`AuthRefreshMiddleware`) shares its once-per-logical-call
        refresh allowance with the executor's decoded-RPC refresh layer
        (issue #1205). Callers that drive the chain without a budget (the
        chat path) pass ``None``; the middleware then falls back to its
        per-chain ``RPC_CONTEXT_AUTH_REFRESHED`` boolean.

        Raises:
            RuntimeError: if the chain provider returns ``None``. The
                wired chain is installed by the composition root in
                :func:`notebooklm._runtime.init.wire_middleware_chain`
                (driven from ``NotebookLMClient.__init__``) immediately
                after :class:`RuntimeTransport` is built; a ``None`` value
                indicates a construction-time wiring bug, not a runtime
                condition.
        """
        # Event-loop affinity guard. The check lives here so it fires once
        # per chain invocation rather than once per leaf attempt.
        # ``assert_bound_loop`` (forwarded through ``bound_loop_check``) is
        # a no-op when ``bound_loop`` is ``None`` (pre-open / fresh
        # fixture); it raises only when the currently-running loop differs
        # from the one captured at ``open()``-time.
        self._bound_loop_check()
        context: dict[str, Any] = {
            RPC_CONTEXT_BUILD_REQUEST: build_request,
            RPC_CONTEXT_LOG_LABEL: log_label,
            RPC_CONTEXT_DISABLE_INTERNAL_RETRIES: disable_internal_retries,
            RPC_CONTEXT_RPC_METHOD: rpc_method,
        }
        if read_timeout is not None:
            context[RPC_CONTEXT_READ_TIMEOUT] = read_timeout
        if disable_read_timeout_retries:
            context[RPC_CONTEXT_DISABLE_READ_TIMEOUT_RETRIES] = True
        # Only seed the shared refresh budget when one is supplied. Callers
        # that drive the chain without a budget (the chat path) leave the key
        # ABSENT, matching the ``RPC_CONTEXT_REFRESH_BUDGET`` docstring; the
        # auth-refresh middleware then falls back to its per-chain
        # ``RPC_CONTEXT_AUTH_REFRESHED`` boolean.
        if refresh_budget is not None:
            context[RPC_CONTEXT_REFRESH_BUDGET] = refresh_budget
        snapshot = await self._snapshot_provider()

        request = materialize_rpc_request(
            build_request=build_request,
            snapshot=snapshot,
            context=context,
        )
        context[RPC_CONTEXT_AUTH_SNAPSHOT] = snapshot

        # The ``max_concurrent_rpcs`` slot is acquired by
        # :class:`SemaphoreMiddleware` (chain position 2, between Metrics
        # and Retry) — that placement keeps Drain admitting queued tasks
        # AND keeps Metrics timing the queue wait, while still bounding
        # the retry-and-refresh cohort to one slot per logical RPC.
        # The middleware writes the queue-wait duration to
        # ``request.context[RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS]`` so the recorder
        # below can forward it to ``ClientMetrics`` without giving the
        # middleware an opinionated ``ClientMetrics`` dependency.
        #
        # Chain resolution is deferred to here — AFTER snapshot capture +
        # materialization, immediately before dispatch — so a reassignment
        # of ``chain_host._authed_post_chain`` that lands while the
        # snapshot call awaits still steers this dispatch. Pre-extraction,
        # the equivalent read happened at the dispatch site for the same
        # live-binding reason; the provider closure preserves that timing.
        chain = self._chain_provider()
        if chain is None:  # pragma: no cover - wiring bug guard
            raise RuntimeError(
                "RuntimeTransport.perform_authed_post called before the "
                "wired chain was installed on MiddlewareChainHost; the "
                "composition root must assign chain_host._authed_post_chain "
                "before any authed POST."
            )
        try:
            result = await chain(request)
            return result.response
        finally:
            # Record queue wait even if the chain raised. A failed chain
            # (RetryMiddleware budget exhaustion, AuthRefreshMiddleware
            # refresh failure, etc.) MUST still surface the queue-wait
            # latency. ``SemaphoreMiddleware`` writes the duration to
            # ``request.context[RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS]`` after the
            # semaphore is acquired; absence of the key means the slot
            # was never acquired and there's nothing to record.
            queue_wait = request.context.get(RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS)
            if queue_wait is not None:
                self._metrics.record_rpc_queue_wait(queue_wait)


__all__ = ["RuntimeTransport"]
