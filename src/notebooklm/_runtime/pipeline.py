"""Immutable authed-POST behavior pipeline.

The fixed behavior order is drain, metrics, semaphore, transient retry,
auth refresh, tracing, then the transport terminal. It has no bind step,
mutable configuration slots, or replaceable chain reference.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .._client_metrics import ClientMetrics
from .._rpc_semaphore import RpcSemaphore
from .._transport_drain import TransportDrainTracker
from .auth_refresh_behavior import AuthRefreshBehavior
from .drain_behavior import DrainBehavior
from .metrics_behavior import MetricsBehavior
from .retry_behavior import RetryBehavior
from .rpc_call import NextCall, RpcRequest, RpcResponse
from .semaphore_behavior import SemaphoreBehavior
from .tracing_behavior import TracingBehavior


class RuntimePipeline:
    """Run the fixed runtime behavior order around one transport leaf."""

    __slots__ = (
        "_dispatch",
        "_rate_limit_max_retries",
        "_refresh_retry_delay",
        "_server_error_max_retries",
    )

    def __init__(
        self,
        *,
        terminal: NextCall,
        drain_tracker: TransportDrainTracker,
        metrics: ClientMetrics,
        rpc_semaphore: RpcSemaphore,
        rate_limit_max_retries: int,
        server_error_max_retries: int,
        retry_timeout_provider: Callable[[], float | None],
        refresh_retry_delay: float,
        refresh_callable: Callable[[], Awaitable[None]],
        auth_snapshot_provider: Callable[[], Awaitable[Any]],
        is_auth_error: Callable[[Exception], bool],
        refresh_callback_enabled_provider: Callable[[], bool],
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self._rate_limit_max_retries = rate_limit_max_retries
        self._server_error_max_retries = server_error_max_retries
        self._refresh_retry_delay = refresh_retry_delay

        tracing = TracingBehavior()
        auth_refresh = AuthRefreshBehavior(
            refresh_callable=refresh_callable,
            is_auth_error=is_auth_error,
            refresh_callback_enabled=refresh_callback_enabled_provider,
            refresh_retry_delay=lambda: refresh_retry_delay,
            snapshot_provider=auth_snapshot_provider,
            sleep=sleep,
            metrics=metrics,
        )
        retry = RetryBehavior(
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            retry_timeout=retry_timeout_provider,
            sleep=sleep,
            metrics=metrics,
        )
        semaphore = SemaphoreBehavior(rpc_semaphore)
        metric_behavior = MetricsBehavior(metrics)
        drain = DrainBehavior(drain_tracker)

        async def traced(request: RpcRequest) -> RpcResponse:
            return await tracing(request, terminal)

        async def refreshed(request: RpcRequest) -> RpcResponse:
            return await auth_refresh(request, traced)

        async def retried(request: RpcRequest) -> RpcResponse:
            return await retry(request, refreshed)

        async def throttled(request: RpcRequest) -> RpcResponse:
            return await semaphore(request, retried)

        async def measured(request: RpcRequest) -> RpcResponse:
            return await metric_behavior(request, throttled)

        async def admitted(request: RpcRequest) -> RpcResponse:
            return await drain(request, measured)

        self._dispatch = admitted

    @property
    def rate_limit_max_retries(self) -> int:
        """Configured 429 retry budget."""
        return self._rate_limit_max_retries

    @property
    def server_error_max_retries(self) -> int:
        """Configured transient-server retry budget."""
        return self._server_error_max_retries

    @property
    def refresh_retry_delay(self) -> float:
        """Configured post-refresh delay."""
        return self._refresh_retry_delay

    async def dispatch(self, request: RpcRequest) -> RpcResponse:
        """Run one request through the fixed behavior order."""
        return await self._dispatch(request)


__all__ = ["RuntimePipeline"]
