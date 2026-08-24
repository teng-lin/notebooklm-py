"""Focused behavior tests for RuntimeTransport's immutable pipeline leaf."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx
import pytest

from notebooklm._auth.cookie_types import CookieJar
from notebooklm._client_metrics import ClientMetrics
from notebooklm._kernel import Kernel
from notebooklm._request_types import AuthSnapshot
from notebooklm._rpc_semaphore import RpcSemaphore
from notebooklm._runtime.rpc_call import RpcRequest, RpcResponse
from notebooklm._runtime.transport import RuntimeTransport
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm._transport_errors import TransportRateLimited, TransportServerError
from notebooklm._web_cookie_provider import WebCookieGeneration


def _generation(epoch: int = 0) -> WebCookieGeneration:
    return WebCookieGeneration(
        cookies=CookieJar(),
        csrf_token=f"csrf-{epoch}",
        session_id=f"session-{epoch}",
        authuser=epoch,
        account_email=None,
        generation=epoch,
    )


def _status_error(code: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/rpc")
    response = httpx.Response(
        code,
        headers={"retry-after": retry_after} if retry_after else None,
        request=request,
    )
    return httpx.HTTPStatusError(str(code), request=request, response=response)


def _transport(
    terminal: Callable[[RpcRequest], Awaitable[RpcResponse]],
    *,
    generations: list[WebCookieGeneration] | None = None,
    refresh: Callable[[], Awaitable[None]] | None = None,
    refresh_enabled: bool = False,
    rate_limit_max_retries: int = 0,
    server_error_max_retries: int = 0,
    sleep: Callable[[float], Awaitable[object]] | None = None,
) -> tuple[RuntimeTransport, ClientMetrics]:
    values = generations if generations is not None else [_generation()]
    metrics = ClientMetrics()

    async def snapshot() -> AuthSnapshot:
        return values[-1]

    async def no_refresh() -> None:
        return None

    return (
        RuntimeTransport(
            kernel=Kernel(),
            snapshot_provider=snapshot,
            metrics=metrics,
            bound_loop_check=lambda: None,
            logger=logging.getLogger(__name__),
            drain_tracker=TransportDrainTracker(),
            rpc_semaphore=RpcSemaphore(None),
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            retry_timeout_provider=lambda: 30.0,
            refresh_retry_delay=0.0,
            refresh_callable=no_refresh if refresh is None else refresh,
            is_auth_error=lambda exc: (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in {400, 401, 403}
            ),
            refresh_callback_enabled_provider=lambda: refresh_enabled,
            sleep=sleep,
            terminal=terminal,
        ),
        metrics,
    )


def _build(snapshot: AuthSnapshot) -> tuple[str, bytes, dict[str, str]]:
    return (
        f"https://example.test/rpc?authuser={snapshot.authuser}",
        snapshot.csrf_token.encode(),
        {"x-generation": str(snapshot.generation)},
    )


@pytest.mark.asyncio
async def test_entry_materializes_one_generation_and_returns_terminal_response() -> None:
    seen: list[RpcRequest] = []

    async def terminal(request: RpcRequest) -> RpcResponse:
        seen.append(request)
        response = httpx.Response(200, request=httpx.Request("POST", request.url))
        return RpcResponse(response=response, state=request.state)

    transport, _ = _transport(terminal)
    response = await transport.perform_authed_post(build_request=_build, log_label="RPC TEST")

    assert response.status_code == 200
    assert seen[0].body == b"csrf-0"
    assert seen[0].headers["x-generation"] == "0"


@pytest.mark.asyncio
async def test_auth_error_refreshes_once_and_rematerializes_fresh_generation() -> None:
    generations = [_generation(0)]
    attempts: list[RpcRequest] = []

    async def terminal(request: RpcRequest) -> RpcResponse:
        attempts.append(request)
        if len(attempts) == 1:
            raise _status_error(401)
        response = httpx.Response(200, request=httpx.Request("POST", request.url))
        return RpcResponse(response=response, state=request.state)

    async def refresh() -> None:
        generations.append(_generation(1))

    transport, metrics = _transport(
        terminal,
        generations=generations,
        refresh=refresh,
        refresh_enabled=True,
    )
    response = await transport.perform_authed_post(build_request=_build, log_label="RPC TEST")

    assert response.status_code == 200
    assert [request.body for request in attempts] == [b"csrf-0", b"csrf-1"]
    assert metrics.snapshot().rpc_auth_retries == 1


@pytest.mark.asyncio
async def test_auth_error_propagates_without_refresh_capability() -> None:
    calls = 0

    async def terminal(_request: RpcRequest) -> RpcResponse:
        nonlocal calls
        calls += 1
        raise _status_error(401)

    transport, metrics = _transport(terminal, refresh_enabled=False)
    with pytest.raises(httpx.HTTPStatusError):
        await transport.perform_authed_post(build_request=_build, log_label="RPC TEST")

    assert calls == 1
    assert metrics.snapshot().rpc_auth_retries == 0


@pytest.mark.asyncio
async def test_rate_limit_retry_uses_fixed_budget_and_injected_clock() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def terminal(request: RpcRequest) -> RpcResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            response = httpx.Response(429, request=httpx.Request("POST", request.url))
            raise TransportRateLimited(
                "limited",
                retry_after=2,
                response=response,
                original=httpx.HTTPStatusError(
                    "limited", request=response.request, response=response
                ),
            )
        response = httpx.Response(200, request=httpx.Request("POST", request.url))
        return RpcResponse(response=response, state=request.state)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    transport, metrics = _transport(
        terminal,
        rate_limit_max_retries=1,
        sleep=sleep,
    )
    await transport.perform_authed_post(build_request=_build, log_label="RPC TEST")

    assert attempts == 2
    assert sleeps == [2.0]
    assert metrics.snapshot().rpc_rate_limit_retries == 1


@pytest.mark.asyncio
async def test_retry_budget_exhaustion_preserves_transport_error() -> None:
    async def terminal(_request: RpcRequest) -> RpcResponse:
        raise TransportServerError("down", original=httpx.ConnectError("down"))

    transport, _ = _transport(terminal, server_error_max_retries=0)
    with pytest.raises(TransportServerError):
        await transport.perform_authed_post(build_request=_build, log_label="RPC TEST")
