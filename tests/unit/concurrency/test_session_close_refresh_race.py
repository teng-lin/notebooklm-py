"""Regression tests for the close-cancels-refresh-task contract (P0-1).

Before P0-1, ``ClientLifecycle.close`` cancelled the keepalive task and
drained the poll registry but did NOT cancel ``host._auth_coord._refresh_task``.
A slow refresh racing against ``close()`` would survive the shielded
``aclose()`` — the task kept holding the now-closed ``httpx.AsyncClient``
and surfaced as a confusing ``RuntimeError`` from inside httpx, or as a
lingering coroutine that pytest's
"coroutine was never awaited" detector flagged at GC time.

The fix is a small block in :meth:`ClientLifecycle.close` between
keepalive teardown and ``save_cookies``:

    if host._auth_coord._refresh_task and not host._auth_coord._refresh_task.done():
        host._auth_coord._refresh_task.cancel()
        await asyncio.gather(host._auth_coord._refresh_task, return_exceptions=True)

That block:
1. Cancels the in-flight refresh task so the shared single-flight refresh
   wave unwinds cleanly.
2. Awaits the cancellation via ``gather(..., return_exceptions=True)`` so
   ``CancelledError`` does not propagate out of ``close()``.
3. Sits BEFORE the cookie save / shielded aclose so the refresh callback
   never observes a half-closed transport.

These tests exercise both the cancel and the no-cancel-needed paths.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from _fixtures.kernel_test_helpers import install_http_client_for_test
from notebooklm._client_metrics import ClientMetrics
from notebooklm._session_auth import AuthRefreshCoordinator
from notebooklm._session_lifecycle import ClientLifecycle
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm.auth import AuthTokens
from notebooklm.types import ConnectionLimits


def _make_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="csrf",
        session_id="sid",
    )


class _StubHost:
    """Minimal :class:`_LifecycleHost` stand-in for the close path.

    Tests assign ``_auth_coord._refresh_task`` directly when they need to
    exercise the in-flight-refresh branch; the default of ``None`` matches
    the post-``__init__``-real-host shape (no refresh has fired yet).
    """

    def __init__(self) -> None:
        self.auth = _make_auth()
        self._metrics_obj = ClientMetrics(on_rpc_event=None)
        self._drain_tracker = TransportDrainTracker()
        self._auth_coord = AuthRefreshCoordinator(refresh_callback=None)
        self.cookie_persistence = MagicMock()
        self.cookie_persistence.save = AsyncMock()
        self.cookie_persistence.capture_open_snapshot = MagicMock()
        # Wave 2 of session-decoupling: drain hooks live on
        # ``TransportDrainTracker``; the host no longer carries ``_drain_hooks``.
        # The real tracker constructed above already has its own ``_drain_hooks``.
        self._rpc_executor = None


def _make_lifecycle() -> ClientLifecycle:
    return ClientLifecycle(
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        keepalive_interval=None,
        keepalive_storage_path=None,
    )


@pytest.mark.asyncio
async def test_close_cancels_in_flight_refresh_task() -> None:
    """A slow refresh racing against ``close()`` must be cancelled and awaited.

    Setup:
    - Lifecycle is opened so the shielded aclose has a real client to tear
      down.
    - The host's ``_auth_coord._refresh_task`` is a long-sleeping task that
      models a refresh callback parked on Google's identity surface.
    - Close is driven.

    Expected:
    - The task is ``cancelled()`` once close returns.
    - No ``CancelledError`` propagates out of ``close()`` itself
      (``gather(..., return_exceptions=True)`` absorbs it).
    - The lifecycle's ``_http_client`` is ``None`` (the standard close
      contract is preserved).
    """
    lifecycle = _make_lifecycle()

    # Open with a stub transport so the close path has a real client to
    # tear down. We use a no-op MockTransport so no real network is touched.
    host = _StubHost()
    transport = httpx.MockTransport(lambda req: httpx.Response(200))

    # Monkey-patch the construction: we need lifecycle._http_client to be a
    # real AsyncClient so close()'s aclose runs. Easiest: build it inline
    # since ClientLifecycle.open expects to read an httpx client mode from
    # _core which we can't easily stub here.
    install_http_client_for_test(lifecycle._kernel, httpx.AsyncClient(transport=transport))
    lifecycle._bound_loop = asyncio.get_running_loop()

    # Park a long-sleeping task on the auth coordinator — models a refresh
    # callback waiting on Google's identity surface.
    async def _slow_refresh() -> Any:
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            raise

    slow_task = asyncio.create_task(_slow_refresh())
    host._auth_coord._refresh_task = slow_task  # type: ignore[assignment]

    # Yield briefly so the task actually starts and reaches the sleep.
    await asyncio.sleep(0)

    assert not slow_task.done(), "test setup: refresh task should be in-flight"

    # Drive close. Must NOT raise.
    await lifecycle.close(host)

    # Yield to let the cancellation propagate. ``gather`` inside ``close``
    # already awaited, so the task should be done by now.
    assert slow_task.cancelled() or slow_task.done()
    if not slow_task.cancelled():
        # If the task somehow completed by other means, that's still a
        # P0-1 contract violation — refresh during close must be cancelled.
        raise AssertionError("refresh task should have been cancelled by close()")

    assert lifecycle._http_client is None


@pytest.mark.asyncio
async def test_close_with_no_refresh_task_is_a_noop_on_that_path() -> None:
    """``close()`` without an in-flight refresh task must not raise.

    The new guard checks ``host._auth_coord._refresh_task`` for both
    ``None`` and ``done()``. A freshly-opened client never triggered a
    refresh, so ``_refresh_task is None`` — close must take the
    short-circuit branch and not blow up trying to ``.cancel()`` a
    ``None``.
    """
    lifecycle = _make_lifecycle()
    host = _StubHost()
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    install_http_client_for_test(lifecycle._kernel, httpx.AsyncClient(transport=transport))
    lifecycle._bound_loop = asyncio.get_running_loop()

    assert host._auth_coord._refresh_task is None

    # Must not raise.
    await lifecycle.close(host)
    assert lifecycle._http_client is None


@pytest.mark.asyncio
async def test_close_with_completed_refresh_task_does_not_recancel() -> None:
    """A refresh task that already finished must be left untouched.

    ``done()`` short-circuits the cancel+gather so a successfully-completed
    refresh wave doesn't have its task re-cancelled (which would be a
    no-op but would still log noise via ``gather(return_exceptions=True)``).
    """
    lifecycle = _make_lifecycle()
    host = _StubHost()
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    install_http_client_for_test(lifecycle._kernel, httpx.AsyncClient(transport=transport))
    lifecycle._bound_loop = asyncio.get_running_loop()

    async def _quick_refresh() -> str:
        return "done"

    done_task = asyncio.create_task(_quick_refresh())
    # Let it complete.
    result = await done_task
    assert result == "done"
    assert done_task.done()
    assert not done_task.cancelled()

    host._auth_coord._refresh_task = done_task  # type: ignore[assignment]

    # Close must not re-cancel a done task.
    await lifecycle.close(host)

    assert not done_task.cancelled()
    assert done_task.done()
