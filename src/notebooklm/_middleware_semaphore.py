"""SemaphoreMiddleware — RPC concurrency gate for the Tier-12 chain.

Per ADR-009 §"Chain ordering" (revised by PR 12.9), ``SemaphoreMiddleware``
sits between ``MetricsMiddleware`` and ``RetryMiddleware``. The final chain
ordering is ``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection,
Tracing]``.

PR 12.1 originally pinned six middlewares; PR 12.9 added the seventh after
codex caught a load-bearing regression in the first cut of the audit-find
that moved the semaphore around the chain dispatch in
``Session._perform_authed_post``: queued tasks were no longer counted by
``DrainMiddleware`` (Drain sat outside the semaphore wait), and Metrics
latency no longer included RPC queue wait. The middleware insertion restores
both contracts in one place:

- **Drain admits queued tasks** — ``DrainMiddleware`` (outermost) increments
  ``_in_flight_posts`` before this middleware acquires the slot, so a
  ``client.close()`` mid-flight blocks on queued tasks instead of rejecting
  them once they finally pull a slot.
- **Metrics latency includes queue wait** — ``MetricsMiddleware`` starts its
  ``perf_counter`` BEFORE this middleware's ``async with``, matching the
  pre-PR-12.9 (PR 12.8) telemetry shape.
- **Retry stays in one slot** — ``RetryMiddleware`` sits INSIDE this
  middleware, so its retry attempts re-invoke the inner chain (AuthRefresh,
  ErrorInjection, Tracing, terminal) WITHOUT releasing the semaphore. This
  preserves the pre-Tier-12 "one slot per logical RPC" backpressure
  contract.

The semaphore is supplied as a zero-arg async-context-manager factory rather
than the raw ``asyncio.Semaphore`` so the middleware can be live-bound to
``Session._get_rpc_semaphore`` — which lazily constructs the semaphore on
first use (loop affinity) and returns a ``contextlib.nullcontext`` when
``max_concurrent_rpcs is None`` (unbounded opt-out). A direct semaphore
binding would have to be reset on loop reuse and would have a 2-call
recursive-acquire deadlock risk; the factory closure avoids both.

See ``docs/adr/0009-middleware-chain.md`` §"Chain ordering" + "PR 12.9
close-out notes" for the rationale and ``.sisyphus/plans/
tier-12-13-greenfield-migration.md`` row 12.9 for the PR sequence.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from ._middleware import NextCall, RpcRequest, RpcResponse

# ``RpcRequest.context`` key used to communicate the per-call queue-wait
# duration from this middleware up to ``Session._perform_authed_post``
# (which forwards it to ``ClientMetrics.record_rpc_queue_wait``). Kept as a
# named constant so the chain's metadata vocabulary in ADR-009 §"RpcRequest
# .context keys" can reference it precisely.
RPC_QUEUE_WAIT_CONTEXT_KEY = "rpc_queue_wait_seconds"


class SemaphoreMiddleware:
    """Chain middleware that holds an :class:`asyncio.Semaphore` slot.

    Conforms to :class:`notebooklm._middleware.Middleware` — the ``__call__``
    signature matches the Protocol so instances are assignable into a
    ``Sequence[Middleware]``.

    Constructor input:

    - ``semaphore_factory``: zero-arg callable returning an async context
      manager. Called once per chain invocation; the returned context manager
      is entered around ``next_call``. Production wires
      ``lambda: client_core._get_rpc_semaphore()`` so the live (lazily
      constructed, loop-bound) semaphore is observed on each call. Tests can
      pass ``lambda: contextlib.nullcontext()`` to disable gating.

    Side effect: writes the per-call queue-wait duration to
    ``request.context[RPC_QUEUE_WAIT_CONTEXT_KEY]`` so the host can forward
    it to ``ClientMetrics.record_rpc_queue_wait`` without giving the
    middleware a direct ``ClientMetrics`` reference (keeps the middleware
    opinion-free about metric naming).
    """

    def __init__(
        self,
        semaphore_factory: Callable[[], AbstractAsyncContextManager[Any]],
    ) -> None:
        self._semaphore_factory = semaphore_factory

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        queue_wait_start = time.perf_counter()
        async with self._semaphore_factory():
            request.context[RPC_QUEUE_WAIT_CONTEXT_KEY] = time.perf_counter() - queue_wait_start
            return await next_call(request)


__all__ = ["RPC_QUEUE_WAIT_CONTEXT_KEY", "SemaphoreMiddleware"]
