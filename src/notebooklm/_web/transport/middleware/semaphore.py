"""Legacy semaphore-middleware characterization helper.

B0 moved the client-wide RPC gate to the protocol-neutral ``CallSupervisor``;
the installed web chain is now ``[Retry, AuthRefresh, ErrorInjection, Tracing]``.
This class remains for direct middleware compatibility tests and is not
installed by the composition root.

When composed directly in the retired seven-middleware ordering, placing the
semaphore here kept three contracts intact:

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

The retained helper still accepts a zero-arg async-context-manager factory so
its direct characterization tests can exercise bounded and no-op gates. Live
production semaphore ownership and loop reset now belong to ``CallSupervisor``.

See ``docs/adr/0009-middleware-chain.md`` §"Chain ordering" for the rationale.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from .context import RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS
from .core import NextCall, RpcRequest, RpcResponse

# Retained compatibility alias for direct tests/imports of the legacy helper.
# Production queue timing is recorded by ``CallSupervisor``.
RPC_QUEUE_WAIT_CONTEXT_KEY = RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS


class SemaphoreMiddleware:
    """Chain middleware that holds an :class:`asyncio.Semaphore` slot.

    Conforms to :class:`notebooklm._web.transport.middleware.core.Middleware` — the ``__call__``
    signature matches the Protocol so instances are assignable into a
    ``Sequence[Middleware]``.

    Constructor input:

    - ``semaphore_factory``: zero-arg callable returning an async context
      manager. Called once per chain invocation; the returned context manager
      is entered around ``next_call``. Production wires
      a test-selected gate. Tests can pass
      ``lambda: contextlib.nullcontext()`` to disable gating.

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
