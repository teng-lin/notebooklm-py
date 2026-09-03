"""Failure-propagation edges of the web transport lifecycle.

``prepare_close``/``close_resources`` are the only places where a background
keepalive failure, an auth-refresh cancellation failure, and a kernel teardown
failure can meet. Each of these tests pins one rule about which of those wins
and which are surfaced rather than swallowed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from notebooklm._runtime.config import CORE_LOGGER_NAME
from notebooklm._web.transport.kernel import Kernel
from notebooklm._web.transport.lifecycle import (
    WebTransportLifecycle,
    _BackgroundResult,
    _capture_background,
)
from notebooklm.auth import AuthTokens
from notebooklm.types import ConnectionLimits


@dataclass
class _WebFixture:
    lifecycle: WebTransportLifecycle
    auth_coord: MagicMock
    persistence: MagicMock
    kernel: Kernel


def _make_web(
    *,
    keepalive_interval: float | None = None,
    keepalive_storage_path: Path | None = None,
    cookie_rotator: Any = None,
    canonical_save_error: BaseException | None = None,
    async_client_factory: Any = httpx.AsyncClient,
) -> _WebFixture:
    auth = AuthTokens(csrf_token="CSRF", session_id="SID", cookies={"SID": "v1"})
    auth_coord = MagicMock()
    auth_coord.cancel_inflight_refresh = AsyncMock()
    persistence = MagicMock()
    persistence._prepare_open_baseline = AsyncMock()
    persistence.capture_open_snapshot = MagicMock()
    persistence._save_canonical = AsyncMock(side_effect=canonical_save_error)
    persistence._save_v0_callback = AsyncMock()
    persistence.loaded_cookie_snapshot = None
    kernel = Kernel(auth=auth, async_client_factory=async_client_factory)
    lifecycle = WebTransportLifecycle(
        auth=auth,
        auth_coord=auth_coord,
        cookie_persistence=persistence,
        kernel=kernel,
        timeout=30.0,
        connect_timeout=10.0,
        limits=ConnectionLimits(),
        keepalive_interval=keepalive_interval,
        keepalive_storage_path=keepalive_storage_path,
        cookie_persistence_path=None,
        cookie_saver=None,
        cookie_rotator=cookie_rotator if cookie_rotator is not None else AsyncMock(),
    )
    return _WebFixture(lifecycle, auth_coord, persistence, kernel)


async def _await_task_completion(task: asyncio.Task[Any]) -> None:
    for _ in range(500):
        if task.done():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("keepalive task did not settle")


@pytest.mark.asyncio
async def test_capture_background_reports_no_error_when_the_factory_completes() -> None:
    """The wrapper's whole job is to make a background result inspectable.

    A success must be distinguishable from a captured failure, or ``prepare_close``
    cannot tell "the keepalive ended cleanly" from "the keepalive blew up".
    """
    ran = False

    async def _factory() -> None:
        nonlocal ran
        ran = True

    result = await _capture_background(_factory)

    assert ran is True
    assert result == _BackgroundResult()
    assert result.error is None


@pytest.mark.asyncio
async def test_prepare_close_surfaces_a_background_failure_that_escaped_the_wrapper() -> None:
    """A task settling as a bare exception must still be raised, not dropped.

    ``prepare_close`` reads the settled value structurally; if it only handled
    ``_BackgroundResult`` the fallback branch would silently discard a failure.
    """
    fixture = _make_web()
    boom = RuntimeError("raw background failure")

    async def _raise_raw() -> _BackgroundResult:
        raise boom

    task = asyncio.create_task(_raise_raw())
    fixture.lifecycle._keepalive_task = task
    await _await_task_completion(task)

    with pytest.raises(RuntimeError, match="raw background failure") as raised:
        await fixture.lifecycle.prepare_close()

    assert raised.value is boom
    fixture.auth_coord.cancel_inflight_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_prepare_close_raises_an_ordinary_keepalive_failure_after_the_fence() -> None:
    """A keepalive that wakes into a retired generation is a real error.

    The kernel refuses to hand a retired epoch its client, and that refusal
    escapes ``_keepalive_loop`` uncaught — ``prepare_close`` must report it
    rather than treat the settled task as a clean shutdown.
    """
    fixture = _make_web(keepalive_interval=0.001)
    await fixture.lifecycle.open(asyncio.get_running_loop(), 7)
    task = fixture.lifecycle._keepalive_task
    assert task is not None

    # Retire the generation out from under the running keepalive.
    fixture.kernel.fence_epoch(7)
    await _await_task_completion(task)

    with pytest.raises(RuntimeError, match="generation is retired"):
        await fixture.lifecycle.prepare_close()

    assert isinstance(task.result(), _BackgroundResult)
    await fixture.lifecycle.close_resources()


@pytest.mark.asyncio
async def test_prepare_close_raises_an_auth_cancellation_failure() -> None:
    """Fencing must complete even when the auth coordinator fails, then report it."""
    fixture = _make_web()
    failure = RuntimeError("cancel refresh failed")
    fixture.auth_coord.cancel_inflight_refresh = AsyncMock(side_effect=failure)
    await fixture.lifecycle.open(asyncio.get_running_loop(), 2)

    with pytest.raises(RuntimeError, match="cancel refresh failed") as raised:
        await fixture.lifecycle.prepare_close()

    assert raised.value is failure
    fixture.auth_coord.fence_epoch.assert_called_once_with(2)
    assert fixture.lifecycle._active_epoch is None
    await fixture.lifecycle.close_resources()


@pytest.mark.asyncio
async def test_close_resources_skips_cookie_persistence_when_no_client_was_opened() -> None:
    """Without a live client there is no refreshed jar, so nothing may be written.

    Persisting here would push the pre-open bootstrap projection over whatever
    a sibling process last stored.
    """
    fixture = _make_web()

    await fixture.lifecycle.close_resources()

    fixture.persistence._save_canonical.assert_not_awaited()
    fixture.persistence._save_v0_callback.assert_not_awaited()
    assert fixture.lifecycle._active_epoch is None


@pytest.mark.asyncio
async def test_close_resources_raises_a_cookie_save_process_exit_after_a_clean_close() -> None:
    """A process-exit signal from persistence outranks completing the teardown quietly."""
    process_exit = KeyboardInterrupt("cookie save interrupted")
    fixture = _make_web(canonical_save_error=process_exit)
    await fixture.lifecycle.open(asyncio.get_running_loop(), 1)

    with pytest.raises(KeyboardInterrupt, match="cookie save interrupted") as raised:
        await fixture.lifecycle.close_resources()

    assert raised.value is process_exit
    assert raised.value.__cause__ is None
    assert fixture.kernel.http_client is None


@pytest.mark.asyncio
async def test_close_resources_raises_a_kernel_close_failure_when_persistence_succeeded() -> None:
    """A teardown failure is only swallowed by an earlier process-exit signal."""
    close_failure = RuntimeError("kernel close failed")

    class _FailingCloseClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            await super().aclose()
            raise close_failure

    fixture = _make_web(async_client_factory=_FailingCloseClient)
    await fixture.lifecycle.open(asyncio.get_running_loop(), 1)

    with pytest.raises(RuntimeError, match="kernel close failed") as raised:
        await fixture.lifecycle.close_resources()

    assert raised.value is close_failure
    fixture.persistence._save_canonical.assert_awaited_once()
    assert fixture.lifecycle._active_epoch is None


@pytest.mark.asyncio
async def test_keepalive_continues_past_a_failed_poke_without_persisting_cookies(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed poke is opportunistic, so the loop survives it — and saves nothing.

    Saving after a failed rotation would write a jar the backend never blessed,
    so the failure must skip persistence rather than fall through to it.
    """
    pokes = 0
    parked = asyncio.Event()

    async def rotator(client: httpx.AsyncClient, path: Path | None) -> None:
        del client, path
        nonlocal pokes
        pokes += 1
        if pokes == 1:
            raise RuntimeError("poke failed")
        if pokes == 2:
            return
        parked.set()
        await asyncio.sleep(60)

    fixture = _make_web(
        keepalive_interval=0.001,
        keepalive_storage_path=tmp_path / "storage_state.json",
        cookie_rotator=rotator,
    )
    with caplog.at_level(logging.DEBUG, logger=CORE_LOGGER_NAME):
        await fixture.lifecycle.open(asyncio.get_running_loop(), 1)
        await asyncio.wait_for(parked.wait(), timeout=10)

        assert pokes == 3
        # Only the second (successful) poke persisted cookies.
        assert fixture.persistence._save_canonical.await_count == 1

        await fixture.lifecycle.prepare_close()

    assert any("Keepalive poke failed (non-fatal)" in record.message for record in caplog.records)
    await fixture.lifecycle.close_resources()


@pytest.mark.asyncio
async def test_keepalive_stops_when_cookie_persistence_is_cancelled(tmp_path: Path) -> None:
    """Cancellation during a save ends the loop instead of being logged as a warning.

    The generic handler below it would turn a shutdown into an endless retry
    loop, so the cancellation must be re-raised past it.
    """
    fixture = _make_web(
        keepalive_interval=0.001,
        keepalive_storage_path=tmp_path / "storage_state.json",
        canonical_save_error=asyncio.CancelledError(),
    )
    await fixture.lifecycle.open(asyncio.get_running_loop(), 1)
    task = fixture.lifecycle._keepalive_task
    assert task is not None
    await _await_task_completion(task)

    settled = task.result()
    assert isinstance(settled, _BackgroundResult)
    assert isinstance(settled.error, asyncio.CancelledError)
    fixture.persistence._save_canonical.assert_awaited_once()

    # A cancelled keepalive is a clean shutdown, not an error to re-raise.
    await fixture.lifecycle.prepare_close()
    fixture.persistence._save_canonical.side_effect = None
    await fixture.lifecycle.close_resources()
