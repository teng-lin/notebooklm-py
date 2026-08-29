"""Regression tests for web close racing an authentication refresh."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from notebooklm._web.transport.auth import AuthRefreshCoordinator
from notebooklm._web.transport.kernel import Kernel
from notebooklm._web.transport.lifecycle import WebTransportLifecycle
from notebooklm.auth import AuthTokens
from notebooklm.types import ConnectionLimits


def _make_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="csrf",
        session_id="sid",
    )


@dataclass
class _Fixture:
    lifecycle: WebTransportLifecycle
    auth_coord: AuthRefreshCoordinator
    kernel: Kernel


def _make_fixture() -> _Fixture:
    auth = _make_auth()
    auth_coord = AuthRefreshCoordinator(refresh_callback=None)
    persistence = MagicMock()
    persistence._prepare_open_baseline = AsyncMock()
    persistence.capture_open_snapshot = MagicMock()
    persistence._save_canonical = AsyncMock()
    persistence._save_v0_callback = AsyncMock()
    persistence.loaded_cookie_snapshot = None
    transport = httpx.MockTransport(lambda request: httpx.Response(200, request=request))

    def _client_factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    kernel = Kernel(auth=auth, async_client_factory=_client_factory)
    lifecycle = WebTransportLifecycle(
        auth=auth,
        auth_coord=auth_coord,
        cookie_persistence=persistence,
        kernel=kernel,
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        keepalive_interval=None,
        keepalive_storage_path=None,
        cookie_persistence_path=None,
        cookie_saver=None,
        cookie_rotator=AsyncMock(),
    )
    return _Fixture(lifecycle, auth_coord, kernel)


async def _open(fixture: _Fixture) -> None:
    loop = asyncio.get_running_loop()
    fixture.auth_coord.set_bound_loop(loop)
    fixture.auth_coord.reset_after_open()
    await fixture.lifecycle.open(loop, 1)


async def _close(fixture: _Fixture) -> None:
    await fixture.lifecycle.prepare_close()
    await fixture.lifecycle.close_resources()


@pytest.mark.asyncio
async def test_close_cancels_in_flight_refresh_task() -> None:
    """Web prepare-close fences, cancels, and awaits the shared refresh task."""
    fixture = _make_fixture()
    await _open(fixture)

    async def _slow_refresh() -> AuthTokens:
        await asyncio.sleep(60.0)
        return _make_auth()

    slow_task = asyncio.create_task(_slow_refresh())
    fixture.auth_coord._refresh_task = slow_task
    fixture.auth_coord._refresh_task_epoch = 1
    await asyncio.sleep(0)
    assert not slow_task.done()

    await _close(fixture)

    assert slow_task.cancelled()
    assert fixture.auth_coord._refresh_task is slow_task
    assert fixture.kernel.http_client is None


@pytest.mark.asyncio
async def test_close_with_no_refresh_task_is_a_noop_on_that_path() -> None:
    """A web generation that never refreshed still closes cleanly."""
    fixture = _make_fixture()
    await _open(fixture)
    assert fixture.auth_coord._refresh_task is None

    await _close(fixture)

    assert fixture.auth_coord._refresh_task is None
    assert fixture.kernel.http_client is None


@pytest.mark.asyncio
async def test_close_with_completed_refresh_task_does_not_recancel() -> None:
    """Prepare-close preserves a successfully completed refresh result."""
    fixture = _make_fixture()
    await _open(fixture)

    done_task = asyncio.create_task(asyncio.sleep(0, result=_make_auth()))
    result = await done_task
    fixture.auth_coord._refresh_task = done_task
    fixture.auth_coord._refresh_task_epoch = 1

    await _close(fixture)

    assert not done_task.cancelled()
    assert done_task.done()
    assert done_task.result() is result
    assert fixture.auth_coord._refresh_task is done_task
    assert fixture.kernel.http_client is None
