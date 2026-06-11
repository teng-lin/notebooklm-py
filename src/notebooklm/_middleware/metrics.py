"""MetricsMiddleware — per-RPC telemetry emitter for the middleware chain.

Per ADR-0009 §"Chain ordering", ``MetricsMiddleware`` sits
just inside ``DrainMiddleware`` (and just outside ``SemaphoreMiddleware``) in
the chain ordering
``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]``,
which keeps Metrics outside the semaphore.

Pure observer: never mutates ``request`` or transforms ``response``. Around
``next_call`` it captures the wall-clock elapsed time of the chain-inner
operation (which includes whatever HTTP/auth/retry behavior the inner
middlewares + transport leaf perform) and emits exactly one terminal record
per logical RPC:

- Increments ``rpc_calls_succeeded`` / ``rpc_calls_failed`` and
  ``rpc_latency_seconds_total`` on the shared :class:`ClientMetrics` snapshot.
- Awaits ``ClientMetrics.emit_rpc_event`` with a backend-agnostic
  :class:`RpcTelemetryEvent` so application-level ``on_rpc_event``
  callbacks fire (Prometheus exporter, OTEL bridge, custom logger, …).

The emit fires only when ``RPC_CONTEXT_RPC_METHOD`` is present in
``request.context``.
Other code paths through the chain (e.g. the chat streaming path in
``_chat.transport.chat_aware_authed_post``, which calls
``RuntimeTransport.perform_authed_post`` directly without minting an
``RpcExecutor`` telemetry frame) leave the key absent and skip emission —
so chat-side requests do not appear in the RPC counters or telemetry
stream. This invariant is pinned
by ``test_skips_emit_when_rpc_method_absent`` in
``tests/unit/test_metrics_middleware.py``.

Failure mode: on any exception from ``next_call``, record the
failed-attempt metrics and re-raise. ``Exception`` (not
``BaseException``) — cooperative-cancellation signals
(``KeyboardInterrupt``, ``SystemExit``, ``asyncio.CancelledError``) are
caller-initiated unwinds, not RPC failures; they propagate without
incrementing counters or emitting events. Same scope as TracingMiddleware,
same reason.

The chain owns per-RPC telemetry emission, and ``RpcExecutor.rpc_call``
keeps only the ``rpc_calls_started`` counter plus the reqid plumbing —
concerns that live OUTSIDE the chain and are not transport-layer events.

Decode-time errors (e.g. ``NoData`` raised after a 200-OK transport return)
do not increment ``rpc_calls_failed``: the chain wraps only the transport
leg, and :meth:`RpcExecutor.rpc_call` decodes AFTER the chain returns. This
disentangles two failure modes — chain failures = transport failures, decode
failures track separately if anyone wants to add them.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .._logging import get_request_id
from .._types.common import RpcTelemetryEvent
from .context import RPC_CONTEXT_RPC_METHOD
from .core import NextCall, RpcRequest, RpcResponse

if TYPE_CHECKING:
    from .._client_metrics import ClientMetrics


class MetricsMiddleware:
    """Middleware that increments counters and emits :class:`RpcTelemetryEvent`.

    Conforms to :class:`notebooklm._middleware.core.Middleware` — the
    ``__call__`` signature matches the Protocol so mypy treats instances
    as assignable into a ``Sequence[Middleware]``.

    Holds a reference to the shared :class:`ClientMetrics` instance owned
    by :class:`NotebookLMClient`. The middleware does not own metric state; it
    is purely a write-through into the host's accumulator. This keeps the
    ``client.metrics`` snapshot view authoritative — a test that swaps a
    middleware out can still observe the counters.
    """

    def __init__(self, metrics: ClientMetrics) -> None:
        self._metrics = metrics

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Time ``next_call``, then increment + emit on its terminal status.

        Reads ``rpc_method`` from ``request.context``: when absent
        (chat-side path; ``__new__``-built fixture) the middleware
        becomes a pure pass-through with no observable effect. When present,
        the value flows into :attr:`RpcTelemetryEvent.method`.
        """
        rpc_method = request.context.get(RPC_CONTEXT_RPC_METHOD)
        # ``perf_counter`` is monotonic and clock-jump-safe. The reading
        # happens here (not inside the success/failure branches) so the
        # elapsed accounting is identical across paths and trivially
        # auditable.
        start = time.perf_counter()
        try:
            response = await next_call(request)
        except Exception as exc:
            elapsed = time.perf_counter() - start
            if rpc_method is not None:
                self._metrics.increment(
                    rpc_calls_failed=1,
                    rpc_latency_seconds_total=elapsed,
                )
                await self._metrics.emit_rpc_event(
                    RpcTelemetryEvent(
                        method=rpc_method,
                        status="error",
                        elapsed_seconds=elapsed,
                        request_id=get_request_id(),
                        # ``__qualname__`` matches the
                        # idiom used by ``TracingMiddleware`` (``_middleware/tracing.py``)
                        # so nested exception classes are distinguishable
                        # in metrics + traces alike.
                        error_type=type(exc).__qualname__,
                    )
                )
            raise

        elapsed = time.perf_counter() - start
        if rpc_method is not None:
            self._metrics.increment(
                rpc_calls_succeeded=1,
                rpc_latency_seconds_total=elapsed,
            )
            await self._metrics.emit_rpc_event(
                RpcTelemetryEvent(
                    method=rpc_method,
                    status="success",
                    elapsed_seconds=elapsed,
                    request_id=get_request_id(),
                )
            )
        return response


__all__ = ["MetricsMiddleware"]
