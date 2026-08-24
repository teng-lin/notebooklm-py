"""MetricsBehavior — per-RPC telemetry emitter for the runtime pipeline.

Per ADR-0009 §"Chain ordering", ``MetricsBehavior`` sits
just inside ``DrainBehavior`` (and just outside ``SemaphoreBehavior``) in
the chain ordering
``[Drain, Metrics, Semaphore, Retry, AuthRefresh, Tracing]``,
which keeps Metrics outside the semaphore.

Pure observer: never mutates ``request`` or transforms ``response``. Around
``next_call`` it captures the wall-clock elapsed time of the chain-inner
operation (which includes whatever HTTP/auth/retry behavior the inner
middlewares + transport leaf perform) and emits one terminal record per
dispatch through the chain:

- Increments ``rpc_calls_succeeded`` / ``rpc_calls_failed`` and
  ``rpc_latency_seconds_total`` on the shared :class:`ClientMetrics` snapshot.
- Awaits ``ClientMetrics.emit_rpc_event`` with a backend-agnostic
  :class:`RpcTelemetryEvent` so application-level ``on_rpc_event``
  callbacks fire (Prometheus exporter, OTEL exporter, custom logger, …).

The emit fires only when ``RpcCallState.rpc_method`` is present on
``request.state``. Other code paths through the chain (e.g. the chat streaming path in
``_web.chat_transport.chat_aware_authed_post``, which calls
``RuntimeTransport.perform_authed_post`` directly without minting an
RPC telemetry frame) leave the field absent and skip emission —
so chat-side requests do not appear in the RPC counters or telemetry
stream. This invariant is pinned
by ``test_skips_emit_when_rpc_method_absent`` in
``tests/unit/test_metrics_middleware.py``.

Failure mode: on any exception from ``next_call``, record the
failed-attempt metrics and re-raise. ``Exception`` (not
``BaseException``) — cooperative-cancellation signals
(``KeyboardInterrupt``, ``SystemExit``, ``asyncio.CancelledError``) are
caller-initiated unwinds, not RPC failures; they propagate without
incrementing counters or emitting events. Same scope as TracingBehavior,
same reason.

The chain owns per-dispatch telemetry emission, and
``WebExecutionRuntime.rpc_call`` keeps only the ``rpc_calls_started`` counter
plus the reqid plumbing — concerns that live OUTSIDE the chain and are not
transport-layer events. A decode-time auth refresh recursively dispatches a
second transport leg with the same logical request id, so it can produce a
second success event while ``rpc_calls_started`` remains one. This preserves
the established transport-observability behavior.

Decode-time errors (e.g. ``NoData`` raised after a 200-OK transport return)
do not increment ``rpc_calls_failed``: the chain wraps only the transport
leg, and :meth:`WebExecutionRuntime.rpc_call` decodes AFTER the chain returns.
This disentangles two failure modes — chain failures = transport failures,
while decode failures use the separate ``rpc_decode_errors`` counter.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .._logging import get_request_id
from .._types.common import RpcTelemetryEvent
from .rpc_call import NextCall, RpcRequest, RpcResponse

if TYPE_CHECKING:
    from .._client_metrics import ClientMetrics


class MetricsBehavior:
    """Runtime behavior that increments counters and emits telemetry events.

    Its ``__call__`` signature matches the fixed behavior-call shape composed
    by :class:`RuntimePipeline`.

    Holds a reference to the shared :class:`ClientMetrics` runtime leaf owned
    by :class:`WebRpcBackend`. The behavior does not own metric state; it is
    purely a write-through into the backend-owned accumulator. This keeps the
    client snapshot view authoritative through backend delegation.
    """

    def __init__(self, metrics: ClientMetrics) -> None:
        self._metrics = metrics

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Time ``next_call``, then increment + emit on its terminal status.

        Reads ``rpc_method`` from ``request.state``: when absent
        (chat-side path; ``__new__``-built fixture) the middleware
        becomes a pure pass-through with no observable effect. When present,
        the value flows into :attr:`RpcTelemetryEvent.method`.
        """
        rpc_method = request.state.rpc_method
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
                        # idiom used by ``TracingBehavior``
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


__all__ = ["MetricsBehavior"]
