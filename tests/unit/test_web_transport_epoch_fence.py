"""B0b web Kernel/Auth resource-generation fencing."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from notebooklm._web.transport.auth import AuthRefreshCoordinator
from notebooklm._web.transport.kernel import Kernel
from notebooklm.auth import AuthTokens
from notebooklm.types import ConnectionLimits


def _client_factory(**kwargs: object) -> httpx.AsyncClient:
    kwargs.pop("limits", None)
    return httpx.AsyncClient(
        **kwargs,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    )


@pytest.mark.asyncio
async def test_kernel_rejects_retired_epoch_before_wire_io() -> None:
    auth = AuthTokens(csrf_token="csrf", session_id="sid", cookies={"SID": "cookie"})
    kernel = Kernel(auth=auth, async_client_factory=_client_factory)
    kernel.activate_epoch(1)
    await kernel.open(
        auth=auth,
        timeout=1,
        connect_timeout=1,
        limits=ConnectionLimits(),
        capture_cookie_snapshot=lambda cookies: None,
        expected_epoch=1,
    )
    assert kernel.get_http_client(expected_epoch=1) is kernel.http_client

    kernel.fence_epoch(1)
    with pytest.raises(RuntimeError, match="generation is retired"):
        kernel.get_http_client(expected_epoch=1)

    await kernel.aclose()
    kernel.activate_epoch(2)
    await kernel.open(
        auth=auth,
        timeout=1,
        connect_timeout=1,
        limits=ConnectionLimits(),
        capture_cookie_snapshot=lambda cookies: None,
        expected_epoch=2,
    )
    with pytest.raises(RuntimeError, match="expected=1, active=2"):
        await kernel.post("https://example.test", {}, b"", expected_epoch=1)
    await kernel.aclose()


@pytest.mark.asyncio
async def test_auth_refresh_waiter_cannot_publish_into_reopened_generation() -> None:
    gate = asyncio.Event()
    callback_epochs: list[int] = []

    async def refresh(expected_epoch: int) -> AuthTokens:
        callback_epochs.append(expected_epoch)
        await gate.wait()
        return AuthTokens(csrf_token="new", session_id="new", cookies={})

    coordinator = AuthRefreshCoordinator(refresh_callback=refresh)
    coordinator.set_bound_loop(asyncio.get_running_loop())
    coordinator.activate_epoch(1)
    waiter = asyncio.create_task(coordinator.await_refresh(1))
    await asyncio.sleep(0)

    coordinator.fence_epoch(1)
    coordinator.activate_epoch(2)
    gate.set()

    with pytest.raises(RuntimeError, match="expected=1, active=2"):
        await waiter

    assert callback_epochs == [1]
    assert coordinator._active_epoch == 2
    assert coordinator._refresh_task_epoch == 1


def test_web_transport_lifecycle_has_physical_web_owner() -> None:
    from notebooklm._runtime import lifecycle as root_lifecycle
    from notebooklm._web.transport.lifecycle import WebTransportLifecycle

    assert WebTransportLifecycle.__module__ == "notebooklm._web.transport.lifecycle"
    assert "WebTransportLifecycle" not in root_lifecycle.__all__
