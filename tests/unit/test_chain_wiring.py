"""Behavior tests for the immutable runtime pipeline."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from notebooklm._auth.cookie_types import CookieJar
from notebooklm._client_metrics import ClientMetrics
from notebooklm._request_types import AuthSnapshot
from notebooklm._rpc_semaphore import RpcSemaphore
from notebooklm._runtime.pipeline import RuntimePipeline
from notebooklm._runtime.rpc_call import RpcRequest, RpcResponse
from notebooklm._transport_drain import TransportDrainTracker
from tests._fixtures.chain import make_request


def _snapshot() -> AuthSnapshot:
    return AuthSnapshot(
        cookies=CookieJar(),
        csrf_token="csrf",
        session_id="session",
        authuser=0,
        account_email=None,
        generation=0,
    )


def _pipeline(
    terminal: Any,
    *,
    rate_limit_max_retries: int = 3,
    server_error_max_retries: int = 3,
) -> tuple[RuntimePipeline, ClientMetrics, TransportDrainTracker]:
    metrics = ClientMetrics()
    drain = TransportDrainTracker()

    async def refresh() -> None:
        return None

    async def snapshot() -> AuthSnapshot:
        return _snapshot()

    pipeline = RuntimePipeline(
        terminal=terminal,
        drain_tracker=drain,
        metrics=metrics,
        rpc_semaphore=RpcSemaphore(None),
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        retry_timeout_provider=lambda: 30.0,
        refresh_retry_delay=0.2,
        refresh_callable=refresh,
        auth_snapshot_provider=snapshot,
        is_auth_error=lambda _exc: False,
        refresh_callback_enabled_provider=lambda: False,
    )
    return pipeline, metrics, drain


@pytest.mark.asyncio
async def test_pipeline_is_complete_before_publication_and_dispatches_terminal() -> None:
    calls: list[RpcRequest] = []

    async def terminal(request: RpcRequest) -> RpcResponse:
        calls.append(request)
        response = httpx.Response(200, request=httpx.Request("POST", request.url))
        return RpcResponse(response=response, state=request.state)

    pipeline, metrics, drain = _pipeline(terminal)
    request = make_request(
        context_updates={"rpc_method": "LIST_NOTEBOOKS", "log_label": "RPC LIST_NOTEBOOKS"}
    )
    response = await pipeline.dispatch(request)

    assert response.response.status_code == 200
    assert calls == [request]
    assert metrics.snapshot().rpc_calls_succeeded == 1
    assert drain._in_flight_posts == 0
    assert not hasattr(pipeline, "middlewares")
    assert not hasattr(pipeline, "chain_host")


def test_pipeline_retry_configuration_is_fixed_at_construction() -> None:
    async def terminal(request: RpcRequest) -> RpcResponse:
        response = httpx.Response(200, request=httpx.Request("POST", request.url))
        return RpcResponse(response=response, state=request.state)

    pipeline, _, _ = _pipeline(
        terminal,
        rate_limit_max_retries=5,
        server_error_max_retries=4,
    )

    assert pipeline.rate_limit_max_retries == 5
    assert pipeline.server_error_max_retries == 4
    with pytest.raises(AttributeError):
        pipeline.rate_limit_max_retries = 9  # type: ignore[misc]
    with pytest.raises(AttributeError):
        pipeline.refresh_retry_delay = 9.0  # type: ignore[misc]
