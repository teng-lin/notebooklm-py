"""Authed POST transport collaborator and fixed-pipeline terminal.

``RuntimeTransport`` owns the three pieces of the authed POST hot path
(history: docs/refactor-history.md):

* :meth:`RuntimeTransport.terminal` — the pipeline terminal. Sends
  the populated :class:`RpcRequest` via :meth:`Kernel.post` and maps the
  raw transport errors into the ``Transport*`` exception shapes consumed
  by ``RetryBehavior`` / ``AuthRefreshBehavior``.
* :meth:`RuntimeTransport.refresh_request_for_current_auth` — re-builds
  the envelope from ``RpcCallState.build_request`` if a concurrent refresh
  moved the auth snapshot between materialization and the terminal POST.
* :meth:`RuntimeTransport.perform_authed_post` — the entry point the
  :class:`WebExecutionRuntime` / chat path call. Runs the
  loop-affinity guard, captures the current auth snapshot, materializes
  the request envelope, dispatches it through the immutable runtime
  pipeline, and records the semaphore queue-wait latency.

Construction is atomic: :class:`RuntimeTransport` builds its complete
:class:`RuntimePipeline` from explicit leaves and retains no bindable chain
slot. The immutable provider generation is reached through an injected
``snapshot_provider`` callable, so the transport never retains a mutable
credential owner.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from .._request_types import AuthSnapshot, BuildRequest
from .._rpc_semaphore import RpcSemaphore
from .._transport_drain import TransportDrainTracker
from .._transport_errors import raise_mapped_post_error
from .pipeline import RuntimePipeline
from .rpc_call import (
    NextCall,
    RpcRequest,
    RpcResponse,
    materialize_rpc_request,
)
from .rpc_call_state import RpcCallState

if TYPE_CHECKING:
    from .._auth_refresh_retry import RefreshBudget
    from .._client_metrics import ClientMetrics
    from .._deadline import RuntimeDeadline
    from .._kernel import Kernel


class RuntimeTransport:
    """Authed POST chain leaf and entry-point collaborator.

    Owns the three authed-POST hot-path methods.
    Does NOT own lifecycle (that stays on :class:`ClientLifecycle`) nor
    retry/refresh budget state (that is carried by :class:`RpcCallState`).

    The fixed pipeline is constructed with the transport and cannot be
    rebound after client construction.

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
        metrics: ClientMetrics,
        bound_loop_check: Callable[[], None],
        logger: logging.Logger,
        drain_tracker: TransportDrainTracker,
        rpc_semaphore: RpcSemaphore,
        rate_limit_max_retries: int,
        server_error_max_retries: int,
        retry_timeout_provider: Callable[[], float | None],
        refresh_retry_delay: float,
        refresh_callable: Callable[[], Awaitable[None]],
        is_auth_error: Callable[[Exception], bool],
        refresh_callback_enabled_provider: Callable[[], bool],
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        terminal: NextCall | None = None,
    ) -> None:
        self._kernel = kernel
        self._snapshot_provider = snapshot_provider
        self._metrics = metrics
        self._bound_loop_check = bound_loop_check
        self._logger = logger
        self._pipeline = RuntimePipeline(
            terminal=self.terminal if terminal is None else terminal,
            drain_tracker=drain_tracker,
            metrics=metrics,
            rpc_semaphore=rpc_semaphore,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            retry_timeout_provider=retry_timeout_provider,
            refresh_retry_delay=refresh_retry_delay,
            refresh_callable=refresh_callable,
            auth_snapshot_provider=snapshot_provider,
            is_auth_error=is_auth_error,
            refresh_callback_enabled_provider=refresh_callback_enabled_provider,
            sleep=sleep,
        )

    @property
    def pipeline(self) -> RuntimePipeline:
        """Return the complete immutable behavior pipeline."""
        return self._pipeline

    async def refresh_request_for_current_auth(self, request: RpcRequest) -> RpcRequest:
        """Rebuild the envelope from the current auth snapshot before every POST.

        This guard is **load-bearing**: it runs on *every* terminal attempt
        (including retries driven by ``RetryBehavior`` for 429 / 5xx) and
        unconditionally rebuilds ``RpcRequest.url`` / ``.headers`` / ``.body``
        from a freshly captured :class:`AuthSnapshot` whenever
        ``RpcCallState.build_request`` is present. The unconditional rebuild
        is the runtime correctness fix for the stale-envelope path that
        existed when the freshness check short-circuited on snapshot
        equality:

        1. Initial attempt: snapshot ``S_old`` is captured by
           :meth:`perform_authed_post`, the envelope is materialized, and
           the request enters the chain.
        2. Terminal POSTs and the response is HTTP 401.
        3. :class:`AuthRefreshBehavior` (just inside ``RetryBehavior``)
           catches the auth error, refreshes credentials, mutates
           the shared state's auth snapshot to ``S_new`` in-place (see
           :meth:`AuthRefreshBehavior._rebuild_request_after_refresh`
           for the contract — that mutation is the carrier of the new
           snapshot across the ``Retry`` ↔ ``AuthRefresh`` boundary), and
           hands a freshly built ``retry_request`` to the chain leaf.
        4. The retry attempt POSTs with the refreshed envelope and the
           response is HTTP 429.
        5. The 429 propagates back up to ``RetryBehavior`` (outside
           ``AuthRefreshBehavior``), which retries by re-invoking the
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
        follows :func:`materialize_rpc_request` *inside this helper*. The
        terminal may then await the generation-attempt barrier; that lease
        installs exactly this snapshot or rejects it as stale and rebuilds.
        Once admitted, the matching cookie jar cannot move until Kernel.post
        has completed response-cookie extraction.
        """
        state = request.state
        build_request = state.build_request
        if build_request is None:
            return request

        current_snapshot = await self._snapshot_provider()
        state.publish_auth_snapshot(current_snapshot)
        request = materialize_rpc_request(
            build_request=build_request,
            snapshot=current_snapshot,
            state=state,
        )
        return request

    async def terminal(self, request: RpcRequest) -> RpcResponse:
        """Chain leaf — sends the populated ``RpcRequest`` via ``Kernel.post``.

        The chain interface carries the actual HTTP request. The terminal
        reads ``RpcRequest.url`` / ``headers`` / ``body`` directly, maps raw
        ``Kernel.post`` errors into the transport exception shapes consumed
        by ``RetryBehavior`` / ``AuthRefreshBehavior``, and wraps the
        returned :class:`httpx.Response` in :class:`RpcResponse`.

        AST guarded — see
        :func:`tests.unit.test_concurrency_refresh_race.test_kernel_post_terminal_has_no_await_before_post_per_attempt`
        which reads the source of this method to assert no unrelated ``await``
        precedes ``self._kernel.post(...)`` after the generation attempt is
        admitted. Barrier acquisition may wait before that ``try``; a stale
        snapshot is then rebuilt rather than sent with newer cookies.
        """
        rejected_generation: int | None = None
        while True:
            request = await self.refresh_request_for_current_auth(request)
            state = request.state
            snapshot = state.auth_snapshot
            attempt = await self._kernel.begin_generation_attempt(snapshot)
            if attempt is not None:
                break
            if snapshot is not None and rejected_generation == snapshot.generation:
                raise RuntimeError("cookie provider generation fell behind the backend session")
            rejected_generation = snapshot.generation if snapshot is not None else None

        post_kwargs: dict[str, Any] = {}
        if state.max_response_bytes is not None:
            post_kwargs["max_response_bytes"] = state.max_response_bytes
        start = time.perf_counter()
        try:
            try:
                response = await self._kernel.post(
                    request.url,
                    headers=request.headers,
                    body=request.body,
                    read_timeout=state.read_timeout,
                    **post_kwargs,
                )
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                state.record_failure(exc)
                raise_mapped_post_error(
                    log_label=state.log_label or "<unknown-chain-call>",
                    exc=exc,
                    start=start,
                    logger=self._logger,
                )
        finally:
            await attempt.release()
        state.record_response(response.status_code)
        return RpcResponse(response=response, state=state)

    async def perform_authed_post(
        self,
        *,
        build_request: BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
        rpc_method: str | None = None,
        refresh_budget: RefreshBudget | None = None,
        retry_deadline: RuntimeDeadline | None = None,
        read_timeout: float | None = None,
        max_response_bytes: int | None = None,
        disable_read_timeout_retries: bool = False,
    ) -> httpx.Response:
        """Authed POST entry point — routes through the runtime pipeline.

        Shared transport surface used by ``WebExecutionRuntime._execute_once``
        (``_rpc_executor.py``) and the semantic chat binding through
        ``_web/chat_transport.py``; keep the same keyword-only signature.

        ``RpcRequest.url`` / ``headers`` / ``body`` are populated through
        :func:`materialize_rpc_request` before the chain sees the
        request. ``RpcCallState.build_request`` remains as the bounded
        rebuild recipe for auth-refresh and pre-terminal freshness
        checks.

        ``refresh_budget`` is an optional
        :class:`notebooklm._auth_refresh_retry.RefreshBudget` seeded by the
        RPC executor so the HTTP-status refresh layer
        (:class:`AuthRefreshBehavior`) shares its once-per-logical-call
        refresh allowance with the executor's decoded-RPC refresh layer
        (issue #1205). Callers that drive the chain without a budget (the
        chat path) pass ``None``; the middleware then falls back to its
        per-chain ``RpcCallState.auth_refreshed`` boolean.

        ``retry_deadline`` is an optional
        :class:`notebooklm._deadline.RuntimeDeadline` seeded by the RPC
        executor so the chain's :class:`RetryBehavior` INHERITS the logical
        call's aggregate retry deadline (anchored at T0) instead of minting a
        fresh one at chain re-entry. This keeps the 429/5xx retry budget from
        restarting across a decode-time auth-refresh retry (issue #1873).
        Callers that drive the chain without an aggregate deadline (the chat
        path) pass ``None``; ``RetryBehavior`` then falls back to
        ``_start_retry_deadline()``.

        """
        # Event-loop affinity guard. The check lives here so it fires once
        # per chain invocation rather than once per leaf attempt.
        # ``assert_bound_loop`` (forwarded through ``bound_loop_check``) is
        # a no-op when ``bound_loop`` is ``None`` (pre-open / fresh
        # fixture); it raises only when the currently-running loop differs
        # from the one captured at ``open()``-time.
        self._bound_loop_check()
        snapshot = await self._snapshot_provider()
        state = RpcCallState.create(
            build_request=build_request,
            log_label=log_label,
            rpc_method=rpc_method,
            disable_internal_retries=disable_internal_retries,
            disable_read_timeout_retries=disable_read_timeout_retries,
            read_timeout=read_timeout,
            max_response_bytes=max_response_bytes,
            refresh_budget=refresh_budget,
            retry_deadline=retry_deadline,
            auth_snapshot=snapshot,
        )

        request = materialize_rpc_request(
            build_request=build_request,
            snapshot=snapshot,
            state=state,
        )

        # The ``max_concurrent_rpcs`` slot is acquired by
        # :class:`SemaphoreBehavior` (chain position 2, between Metrics
        # and Retry) — that placement keeps Drain admitting queued tasks
        # AND keeps Metrics timing the queue wait, while still bounding
        # the retry-and-refresh cohort to one slot per logical RPC.
        # The middleware writes the queue-wait duration to
        # ``request.state.queue_wait_seconds`` so the recorder
        # below can forward it to ``ClientMetrics`` without giving the
        # middleware an opinionated ``ClientMetrics`` dependency.
        #
        # The complete pipeline was fixed during construction. Dispatch begins
        # here only after snapshot capture and request materialization, with no
        # mutable chain lookup or post-construction bind step.
        try:
            result = await self._pipeline.dispatch(request)
            return result.response
        finally:
            # Record queue wait even if the chain raised. A failed chain
            # (RetryBehavior budget exhaustion, AuthRefreshBehavior
            # refresh failure, etc.) MUST still surface the queue-wait
            # latency. ``SemaphoreBehavior`` writes the duration to
            # ``request.state.queue_wait_seconds`` after the
            # semaphore is acquired; absence of the key means the slot
            # was never acquired and there's nothing to record.
            queue_wait = request.state.queue_wait_seconds
            if queue_wait is not None:
                self._metrics.record_rpc_queue_wait(queue_wait)


__all__ = ["RuntimeTransport"]
