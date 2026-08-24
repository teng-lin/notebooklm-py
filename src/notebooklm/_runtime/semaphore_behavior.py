"""SemaphoreBehavior — RPC concurrency gate for the chain.

Per ADR-0009 §"Chain ordering", ``SemaphoreBehavior``
sits between ``MetricsBehavior`` and ``RetryBehavior``. The chain
ordering is ``[Drain, Metrics, Semaphore, Retry, AuthRefresh, Tracing]``.

Placing the semaphore here (rather than around the chain dispatch in
``RuntimeTransport.perform_authed_post``) keeps two contracts intact: queued tasks
stay counted by ``DrainBehavior`` (Drain sits outside the semaphore wait),
and Metrics latency includes RPC queue wait:

- **Drain admits queued tasks** — ``DrainBehavior`` (outermost) increments
  ``_in_flight_posts`` before this middleware acquires the slot, so a
  ``client.close()`` mid-flight blocks on queued tasks instead of rejecting
  them once they finally pull a slot.
- **Metrics latency includes queue wait** — ``MetricsBehavior`` starts its
  ``perf_counter`` BEFORE this middleware's ``async with``, so the telemetry
  shape includes queue wait.
- **Retry stays in one slot** — ``RetryBehavior`` sits INSIDE this
  middleware, so its retry attempts re-invoke the inner chain (AuthRefresh,
  Tracing, terminal) WITHOUT releasing the semaphore. This
  preserves the "one slot per logical RPC" backpressure contract.

The middleware receives the focused :class:`~notebooklm._rpc_semaphore.RpcSemaphore`
owner rather than a raw ``asyncio.Semaphore``. The owner lazily constructs the
gate on first use, resets it on loop reuse, and returns a
``contextlib.nullcontext`` when ``max_concurrent_rpcs is None``. One owner
context surrounds the entire logical request, avoiding retry re-acquisition.

See ``docs/adr/0009-middleware-chain.md`` §"Chain ordering" for the rationale.
"""

from __future__ import annotations

import time

from .._rpc_semaphore import RpcSemaphore
from .rpc_call import NextCall, RpcRequest, RpcResponse


class SemaphoreBehavior:
    """Runtime behavior that holds an :class:`asyncio.Semaphore` slot.

    Its ``__call__`` signature matches the fixed behavior-call shape composed
    by :class:`RuntimePipeline`.

    Constructor input:

    - ``rpc_semaphore``: focused owner of the lazy, loop-bound RPC gate. Its
      context is entered once around ``next_call`` for each logical request.

    Side effect: records the per-call queue-wait duration on the request's
    typed state so the host can forward it to ``ClientMetrics.record_rpc_queue_wait``
    without giving the
    behavior a direct ``ClientMetrics`` reference (keeps the behavior
    opinion-free about metric naming).
    """

    def __init__(
        self,
        rpc_semaphore: RpcSemaphore,
    ) -> None:
        self._rpc_semaphore = rpc_semaphore

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        queue_wait_start = time.perf_counter()
        queue_wait_recorded = False
        try:
            async with self._rpc_semaphore.get():
                request.state.record_queue_wait(time.perf_counter() - queue_wait_start)
                queue_wait_recorded = True
                return await next_call(request)
        finally:
            # Cancellation or a loop-affinity failure can abort acquisition.
            # Preserve the queue-wait observation for the outer metrics layer.
            if not queue_wait_recorded:
                request.state.record_queue_wait(time.perf_counter() - queue_wait_start)


__all__ = [
    "SemaphoreBehavior",
]
