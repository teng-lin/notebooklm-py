"""Tests for the lifecycle drain on ``ClientCore.close`` (PR-E, audit I1).

Pins down:

- ``PollRegistry.active_tasks()`` returns the leader poll tasks currently
  parked in the registry, and excludes already-completed tasks.
- ``ClientCore.close()`` cancels every active poll task and awaits each with
  ``return_exceptions=True`` so a single misbehaving leader can't block
  teardown.
- ``NotebookLMClient.close()`` and ``__aexit__`` default to ``drain=True``
  (BREAKING). Old fire-and-forget callers must pass ``drain=False`` to opt out.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from notebooklm._core import ClientCore
from notebooklm._core_polling import PollRegistry
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient


def _auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "test_sid"},
        csrf_token="csrf",
        session_id="sid",
    )


# ---------------------------------------------------------------------------
# PollRegistry.active_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_tasks_returns_pending_leader_tasks() -> None:
    registry = PollRegistry()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    async def _never() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_never())
    try:
        registry.pending[("nb_1", "task_1")] = (future, task)

        assert registry.active_tasks() == [task]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_active_tasks_excludes_already_done_tasks() -> None:
    registry = PollRegistry()
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    async def _quick() -> None:
        return None

    task = asyncio.create_task(_quick())
    await task  # task is now done

    registry.pending[("nb_1", "task_1")] = (future, task)

    assert registry.active_tasks() == []


@pytest.mark.asyncio
async def test_active_tasks_returns_empty_for_fresh_registry() -> None:
    assert PollRegistry().active_tasks() == []


# ---------------------------------------------------------------------------
# ClientCore.close drains active poll tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_close_drains_polls() -> None:
    """``close()`` cancels in-flight poll tasks within 1s and tears down cleanly."""
    core = ClientCore(_auth())
    await core.open()

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    cancellation_seen = asyncio.Event()

    async def parked_poll() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            raise

    task = asyncio.create_task(parked_poll())
    # Yield once so the task enters its ``Event().wait()`` — otherwise the
    # cancel arrives before the task body has run and our
    # ``except CancelledError`` handler never executes.
    await asyncio.sleep(0)
    core.poll_registry.pending[("nb_1", "task_1")] = (future, task)

    # Real-time deadline so a regression that fails to cancel surfaces as a
    # 1s timeout rather than hanging the suite.
    await asyncio.wait_for(core.close(), timeout=1.0)

    assert task.done()
    assert cancellation_seen.is_set()


@pytest.mark.asyncio
async def test_core_close_handles_poll_task_raising_during_drain() -> None:
    """A poll task raising during cancel-drain doesn't block close()."""
    core = ClientCore(_auth())
    await core.open()

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    async def angry_poll() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("poll cleanup failed") from None

    task = asyncio.create_task(angry_poll())
    core.poll_registry.pending[("nb_x", "task_x")] = (future, task)

    # return_exceptions=True in close() means this should NOT propagate.
    await asyncio.wait_for(core.close(), timeout=1.0)

    assert task.done()


@pytest.mark.asyncio
async def test_core_close_with_no_polls_is_noop_on_drain_step() -> None:
    """``close()`` works unchanged when no polls are registered."""
    core = ClientCore(_auth())
    await core.open()
    await core.close()
    assert core._http_client is None


# ---------------------------------------------------------------------------
# NotebookLMClient default drain=True (BREAKING)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_close_default_drain_is_true() -> None:
    """``client.close()`` (no args) now drains by default (BREAKING)."""
    client = NotebookLMClient(_auth())
    drain_calls: list[float | None] = []

    async def fake_drain(timeout: float | None = None) -> None:
        drain_calls.append(timeout)

    async def fake_close() -> None:
        pass

    client._core.drain = fake_drain  # type: ignore[method-assign]
    client._core.close = fake_close  # type: ignore[method-assign]

    await client.close()

    assert drain_calls == [None], (
        "default close() must drain; pass drain=False to opt out (BREAKING)"
    )


@pytest.mark.asyncio
async def test_client_close_drain_false_skips_drain() -> None:
    """``client.close(drain=False)`` preserves the old fire-and-forget path."""
    client = NotebookLMClient(_auth())
    drain_calls: list[float | None] = []

    async def fake_drain(timeout: float | None = None) -> None:
        drain_calls.append(timeout)

    async def fake_close() -> None:
        pass

    client._core.drain = fake_drain  # type: ignore[method-assign]
    client._core.close = fake_close  # type: ignore[method-assign]

    await client.close(drain=False)

    assert drain_calls == []


@pytest.mark.asyncio
async def test_client_aexit_uses_drain_true_default() -> None:
    """``async with`` exit now drains (BREAKING)."""
    client = NotebookLMClient(_auth())
    drain_calls: list[float | None] = []

    async def fake_drain(timeout: float | None = None) -> None:
        drain_calls.append(timeout)

    async def fake_close() -> None:
        pass

    client._core.drain = fake_drain  # type: ignore[method-assign]
    client._core.close = fake_close  # type: ignore[method-assign]

    # Drive __aexit__ directly rather than `async with` so we can use the
    # patched core without going through ``open()``.
    await client.__aexit__(None, None, None)

    assert drain_calls == [None]
