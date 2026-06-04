"""SemaphoreMiddleware — RPC concurrency gate for the chain.

Per ADR-0009 §"Chain ordering", ``SemaphoreMiddleware``
sits between ``MetricsMiddleware`` and ``RetryMiddleware``. The chain
ordering is ``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection,
Tracing]``.

Placing the semaphore here (rather than around the chain dispatch in
``RuntimeTransport.perform_authed_post``) keeps two contracts intact: queued tasks
stay counted by ``DrainMiddleware`` (Drain sits outside the semaphore wait),
and Metrics latency includes RPC queue wait:

- **Drain admits queued tasks** — ``DrainMiddleware`` (outermost) increments
  ``_in_flight_posts`` before this middleware acquires the slot, so a
  ``client.close()`` mid-flight blocks on queued tasks instead of rejecting
  them once they finally pull a slot.
- **Metrics latency includes queue wait** — ``MetricsMiddleware`` starts its
  ``perf_counter`` BEFORE this middleware's ``async with``, so the telemetry
  shape includes queue wait.
- **Retry stays in one slot** — ``RetryMiddleware`` sits INSIDE this
  middleware, so its retry attempts re-invoke the inner chain (AuthRefresh,
  ErrorInjection, Tracing, terminal) WITHOUT releasing the semaphore. This
  preserves the "one slot per logical RPC" backpressure contract.

The semaphore is supplied as a zero-arg async-context-manager factory rather
than the raw ``asyncio.Semaphore`` so the middleware can be live-bound to
``ClientComposed.get_rpc_semaphore`` — which lazily constructs the semaphore
on first use (loop affinity) and returns a ``contextlib.nullcontext`` when
``max_concurrent_rpcs is None`` (unbounded opt-out). A direct semaphore
binding would have to be reset on loop reuse and would have a 2-call
recursive-acquire deadlock risk; the factory closure avoids both.

See ``docs/adr/0009-middleware-chain.md`` §"Chain ordering" for the rationale.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from .context import RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS
from .core import NextCall, RpcRequest, RpcResponse

# ``RpcRequest.context`` key used to communicate the per-call queue-wait
# duration from this middleware up to ``RuntimeTransport.perform_authed_post``
# (which forwards it to ``ClientMetrics.record_rpc_queue_wait``). Kept as a
# compatibility alias for older internal imports; new code should use the
# centralized ``RPC_CONTEXT_*`` vocabulary from ``_middleware.context``.
RPC_QUEUE_WAIT_CONTEXT_KEY = RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS


class SemaphoreMiddleware:
    """Chain middleware that holds an :class:`asyncio.Semaphore` slot.

    Conforms to :class:`notebooklm._middleware.core.Middleware` — the ``__call__``
    signature matches the Protocol so instances are assignable into a
    ``Sequence[Middleware]``.

    Constructor input:

    - ``semaphore_factory``: zero-arg callable returning an async context
      manager. Called once per chain invocation; the returned context manager
      is entered around ``next_call``. Production wires
      ``ClientComposed.get_rpc_semaphore`` so the live (lazily
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
            request.context[RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS] = (
                time.perf_counter() - queue_wait_start
            )
            return await next_call(request)


__all__ = [
    "RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS",
    "RPC_QUEUE_WAIT_CONTEXT_KEY",
    "SemaphoreMiddleware",
]
