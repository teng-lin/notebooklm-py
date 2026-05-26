"""Authed POST transport collaborator — the chain leaf for :class:`Session`.

Extracted from ``Session`` as move #4c of the session-refactor arc
(``docs/improvement.md`` §3.1). ``SessionTransport`` owns the three
pieces of the authed POST hot path that used to live on :class:`Session`:

* :meth:`SessionTransport.terminal` — the middleware-chain leaf. Sends
  the populated :class:`RpcRequest` via :meth:`Kernel.post` and maps the
  raw transport errors into the ``Transport*`` exception shapes consumed
  by ``RetryMiddleware`` / ``AuthRefreshMiddleware``.
* :meth:`SessionTransport.refresh_request_for_current_auth` — re-builds
  the envelope from ``context["build_request"]`` if a concurrent refresh
  moved the auth snapshot between materialization and the terminal POST.
* :meth:`SessionTransport.perform_authed_post` — the entry point the
  RPC executor / chat path / ``Session.transport_post`` call. Runs the
  loop-affinity guard, captures the current auth snapshot, materializes
  the request envelope, dispatches it through the wired middleware
  chain, and records the semaphore queue-wait latency.

``Session`` keeps :meth:`Session._authed_post_chain_terminal`,
:meth:`Session._refresh_request_for_current_auth`, and
:meth:`Session._perform_authed_post` as one-line forwards to the
collaborator so the long-standing call surfaces (``RpcOwner`` Protocol,
``core._perform_authed_post(...)`` direct callers, and the test-side
``core._authed_post_chain_terminal = fake`` monkeypatch point) keep
working unchanged. The two AST guards in
``tests/unit/test_concurrency_refresh_race.py`` follow the source by
inspecting the collaborator methods directly — the Session forwards
carry only the dispatch hop, so there is no try/await structure on the
Session side for those guards to inspect after the move.

``_refresh_retry_delay`` deliberately stays on :class:`Session`.
``perform_authed_post`` does not read it — the retry/backoff budget for
the refresh path is owned by ``AuthRefreshMiddleware`` and by
``RpcExecutor.try_refresh_and_retry``, both of which read
``host._refresh_retry_delay`` live through provider lambdas wired in
``_session_init.wire_middleware_chain``. Integration tests that assign
``client._session._refresh_retry_delay = 0`` keep steering the live
delay without migration.

Construction order in :class:`Session.__init__`:
:func:`notebooklm._session_init.build_session_transport` constructs the
transport **before** :func:`wire_middleware_chain`. The wired chain
leaf is :meth:`Session._authed_post_chain_terminal` (a one-line forward
to :meth:`SessionTransport.terminal`) — wiring through the Session
forward preserves the canonical seam for a subclass override or
fixture-time class-level monkeypatch of that method. The chain itself
is reached by the transport through an injected ``chain_provider``
closure that reads ``host._authed_post_chain`` live, late on every
:meth:`perform_authed_post` call; this both breaks the construction
cycle and preserves the long-standing test pattern of reassigning
``core._authed_post_chain`` to install a fake chain. The
:class:`AuthRefreshCoordinator` snapshot is reached via an injected
``snapshot_provider`` callable so :class:`SessionTransport` never has
to hold a direct back-reference to :class:`Session`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx

from ._middleware import (
    NextCall,
    RpcRequest,
    RpcResponse,
    materialize_rpc_request,
)
from ._middleware_semaphore import RPC_QUEUE_WAIT_CONTEXT_KEY
from ._request_types import AuthSnapshot, BuildRequest
from ._transport_errors import raise_mapped_post_error

if TYPE_CHECKING:
    from ._client_metrics import ClientMetrics
    from ._kernel import Kernel


class SessionTransport:
    """Authed POST chain leaf and entry-point collaborator.

    Owns the three methods extracted from :class:`Session` in move #4c.
    Does NOT own lifecycle (that stays on :class:`ClientLifecycle`) nor
    retry/refresh budget state (that stays on :class:`Session` and is
    threaded into middleware via provider lambdas).

    The chain reference is fetched late on every
    :meth:`perform_authed_post` — just before chain dispatch, after
    snapshot + materialization — through the injected ``chain_provider``
    closure (typically ``lambda: host._authed_post_chain``). The lookup
    is intentionally deferred so a chain reassignment that happens
    while the snapshot capture awaits still steers the dispatch,
    matching the pre-extraction behavior where ``self._authed_post_chain``
    was read at the dispatch site.

    The injected ``logger`` is held so error messages mapped through
    :func:`notebooklm._transport_errors.raise_mapped_post_error` keep
    appearing under the original ``notebooklm._session`` namespace
    rather than ``notebooklm._session_transport`` — preserving the
    log-filter / caplog vocabulary callers may already rely on.
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
        # :class:`Session` AFTER :class:`SessionTransport` is constructed
        # (the chain's leaf is :meth:`terminal`, so the transport must
        # exist first). Tests also reassign ``core._authed_post_chain``
        # post-construction to install a fake chain — going through a
        # provider closure (called late in :meth:`perform_authed_post`)
        # ensures those reassignments take effect on the next call
        # without any further mutation here.
        self._chain_provider = chain_provider
        self._metrics = metrics
        self._bound_loop_check = bound_loop_check
        self._logger = logger

    async def refresh_request_for_current_auth(self, request: RpcRequest) -> RpcRequest:
        """Rebuild the envelope if auth changed before the terminal POST.

        :meth:`perform_authed_post` materializes the request before the
        outer chain runs, so the request may wait behind Drain / Semaphore
        before the leaf sends it. Compare the materialization snapshot to
        a fresh snapshot immediately before :meth:`Kernel.post`; if auth
        moved, rebuild the envelope synchronously from
        ``context["build_request"]``.

        AST guarded — see
        :func:`tests.unit.test_concurrency_refresh_race.test_terminal_freshness_check_has_no_await_after_materialization`
        which reads the source of this method to assert no ``await``
        follows :func:`materialize_rpc_request`. Any restructuring here
        must keep that invariant: the snapshot and the rebuilt envelope
        must be produced together with no suspension point between them.
        """
        context = request.context
        request_snapshot = context.get("auth_snapshot")
        build_request = context.get("build_request")
        if not isinstance(request_snapshot, AuthSnapshot) or build_request is None:
            return request

        current_snapshot = await self._snapshot_provider()
        if current_snapshot == request_snapshot:
            return request

        context["auth_snapshot"] = current_snapshot
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
        log_label = context.get("log_label", "<unknown-chain-call>")
        start = time.perf_counter()
        try:
            response = await self._kernel.post(
                request.url,
                headers=request.headers,
                body=request.body,
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
    ) -> httpx.Response:
        """Authed POST entry point — routes through the middleware chain.

        Compatibility surface preserved on :class:`Session` so
        ``RpcExecutor.execute`` (``_rpc_executor.py``),
        ``_chat_transport`` (``_chat_transport.py``), and direct
        callers (``client._session._perform_authed_post(...)``) keep the
        same keyword-only signature.

        ``RpcRequest.url`` / ``headers`` / ``body`` are populated through
        :func:`materialize_rpc_request` before the chain sees the
        request. ``context["build_request"]`` remains as the bounded
        rebuild recipe for auth-refresh and pre-terminal freshness
        checks.

        Raises:
            RuntimeError: if the chain provider returns ``None``. The
                wired chain is installed by :class:`Session.__init__`
                immediately after :class:`SessionTransport` is built; a
                ``None`` value indicates a construction-time wiring bug,
                not a runtime condition.
        """
        # Event-loop affinity guard. The check lives here so it fires once
        # per chain invocation rather than once per leaf attempt.
        # ``assert_bound_loop`` (forwarded through ``bound_loop_check``) is
        # a no-op when ``bound_loop`` is ``None`` (pre-open / fresh
        # fixture); it raises only when the currently-running loop differs
        # from the one captured at ``open()``-time.
        self._bound_loop_check()
        context = {
            "build_request": build_request,
            "log_label": log_label,
            "disable_internal_retries": disable_internal_retries,
            "rpc_method": rpc_method,
        }
        snapshot = await self._snapshot_provider()

        request = materialize_rpc_request(
            build_request=build_request,
            snapshot=snapshot,
            context=context,
        )
        context["auth_snapshot"] = snapshot

        # The ``max_concurrent_rpcs`` slot is acquired by
        # :class:`SemaphoreMiddleware` (chain position 2, between Metrics
        # and Retry) — that placement keeps Drain admitting queued tasks
        # AND keeps Metrics timing the queue wait, while still bounding
        # the retry-and-refresh cohort to one slot per logical RPC.
        # The middleware writes the queue-wait duration to
        # ``request.context[RPC_QUEUE_WAIT_CONTEXT_KEY]`` so the recorder
        # below can forward it to ``ClientMetrics`` without giving the
        # middleware an opinionated ``ClientMetrics`` dependency.
        #
        # Chain resolution is deferred to here — AFTER snapshot capture +
        # materialization, immediately before dispatch — so a reassignment
        # of ``host._authed_post_chain`` that lands while the snapshot
        # call awaits still steers this dispatch. Pre-extraction, the
        # equivalent ``self._authed_post_chain(request)`` read happened
        # at the dispatch site for the same live-binding reason; the
        # provider closure preserves that timing.
        chain = self._chain_provider()
        if chain is None:  # pragma: no cover - wiring bug guard
            raise RuntimeError(
                "SessionTransport.perform_authed_post called before the "
                "wired chain was installed on Session; Session construction "
                "must assign self._authed_post_chain before any authed POST."
            )
        try:
            result = await chain(request)
            return result.response
        finally:
            # Record queue wait even if the chain raised. A failed chain
            # (RetryMiddleware budget exhaustion, AuthRefreshMiddleware
            # refresh failure, etc.) MUST still surface the queue-wait
            # latency. ``SemaphoreMiddleware`` writes the duration to
            # ``request.context[RPC_QUEUE_WAIT_CONTEXT_KEY]`` after the
            # semaphore is acquired; absence of the key means the slot
            # was never acquired and there's nothing to record (gemini
            # PR 12.9 finding).
            queue_wait = request.context.get(RPC_QUEUE_WAIT_CONTEXT_KEY)
            if queue_wait is not None:
                self._metrics.record_rpc_queue_wait(queue_wait)


__all__ = ["SessionTransport"]
