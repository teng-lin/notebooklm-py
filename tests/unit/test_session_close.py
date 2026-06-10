"""Tests for the lifecycle drain on ``Session.close``.

Pins down:

- ``PollRegistry.active_tasks()`` returns the leader poll tasks currently
  parked in the registry, and excludes already-completed tasks.
- ``ArtifactsAPI`` owns its poll registry and registers a close-time drain hook
  so ``Session.close()`` cancels active polls without reaching into feature
  state.
- ``NotebookLMClient.close()`` and ``__aexit__`` default to ``drain=True``
  (BREAKING). Old fire-and-forget callers must pass ``drain=False`` to opt out.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._polling_registry import PollRegistry
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from tests._helpers.client_factory import build_client_shell_for_tests


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
        registry.register(("nb_1", "task_1"), future, task)

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

    registry.register(("nb_1", "task_1"), future, task)

    assert registry.active_tasks() == []


@pytest.mark.asyncio
async def test_active_tasks_returns_empty_for_fresh_registry() -> None:
    assert PollRegistry().active_tasks() == []


# ---------------------------------------------------------------------------
# Session.close runs feature-owned drain hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_close_drains_artifact_poll_hook() -> None:
    """``close()`` cancels in-flight poll tasks within 1s and tears down cleanly."""
    from unittest.mock import MagicMock

    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    core = build_client_shell_for_tests(_auth())
    # ``ArtifactsAPI`` consumes its three runtime collaborators
    # (``rpc`` + ``drain`` + ``lifecycle``) directly — mirrors production
    # wiring in ``NotebookLMClient.__init__``.
    artifacts = ArtifactsAPI(
        rpc=core._rpc_executor,
        drain=core._collaborators.drain_tracker,
        lifecycle=core._collaborators.lifecycle,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )
    assert (
        core._collaborators.drain_tracker._drain_hooks["artifacts.polls"]
        == artifacts._polling.drain
    )
    await core.__aenter__()

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
    artifacts._poll_registry.register(("nb_1", "task_1"), future, task)

    # Real-time deadline so a regression that fails to cancel surfaces as a
    # 1s timeout rather than hanging the suite.
    await asyncio.wait_for(core.close(), timeout=1.0)

    assert task.done()
    assert cancellation_seen.is_set()


@pytest.mark.asyncio
async def test_session_close_absorbs_drain_hook_errors() -> None:
    """A drain hook raising during close does not block transport teardown."""
    core = build_client_shell_for_tests(_auth())
    await core.__aenter__()

    async def angry_hook() -> None:
        raise RuntimeError("poll cleanup failed")

    core._collaborators.drain_tracker.register_drain_hook("angry", angry_hook)

    # return_exceptions=True in close() means this should NOT propagate.
    await asyncio.wait_for(core.close(), timeout=1.0)

    assert core._collaborators.kernel.http_client is None


@pytest.mark.asyncio
async def test_session_close_with_no_polls_is_noop_on_drain_step() -> None:
    """``close()`` works unchanged when no polls are registered."""
    core = build_client_shell_for_tests(_auth())
    await core.__aenter__()
    await core.close()
    assert core._collaborators.kernel.http_client is None


@pytest.mark.asyncio
async def test_close_drain_cancels_inflight_poll_in_operation_scope() -> None:
    """Issue #1161: ``close(drain=True)`` cancels an in-flight poll counted in
    ``operation_scope`` instead of blocking on its in-flight counter.

    Reproduces the production wiring: the artifact poll loop runs inside
    ``TransportDrainTracker.operation_scope`` (incrementing ``_in_flight_posts``)
    and registers a drain hook that cancels the leader task. Before the fix,
    ``close()`` awaited ``drain()`` BEFORE the lifecycle ran the cancel hook,
    so ``drain()`` parked on the in-flight counter until the poll's own timeout
    (the cancel hook ran too late). The fix fires the cancel hooks before the
    drain wait so ``drain()`` observes a cancelled-then-settled count.

    A real-time deadline turns a regression into a fast failure rather than a
    suite hang.
    """
    core = build_client_shell_for_tests(_auth())
    await core.__aenter__()

    tracker = core._collaborators.drain_tracker
    registry = PollRegistry()
    cancellation_seen = asyncio.Event()
    scope_entered = asyncio.Event()

    async def parked_poll() -> None:
        # Mirror the poll loop: hold an ``operation_scope`` open (bumping the
        # in-flight counter ``drain()`` waits on) while parked, and unwind via
        # CancelledError when the drain hook cancels us.
        async with tracker.operation_scope("artifact wait task_1"):
            scope_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                raise

    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(parked_poll())
    # Let the task enter ``operation_scope`` so ``_in_flight_posts`` is bumped
    # before close() drains; otherwise the drain wait would trivially pass.
    await asyncio.wait_for(scope_entered.wait(), timeout=1.0)
    assert tracker._in_flight_posts == 1
    registry.register(("nb_1", "task_1"), future, task)

    async def cancel_polls() -> None:
        # Snapshot once before cancelling, matching the production
        # ``ArtifactPollingService.drain`` pattern (``_artifact/polling.py``).
        poll_tasks = registry.active_tasks()
        for poll_task in poll_tasks:
            poll_task.cancel()
        if poll_tasks:
            await asyncio.gather(*poll_tasks, return_exceptions=True)

    tracker.register_drain_hook("artifacts.polls", cancel_polls)

    # Default drain=True. Real-time deadline so the pre-fix block (which would
    # only end at the poll's own timeout) surfaces as a 1s failure.
    await asyncio.wait_for(core.close(), timeout=1.0)

    assert task.done()
    assert cancellation_seen.is_set()
    assert tracker._in_flight_posts == 0
    assert core._collaborators.kernel.http_client is None
    # Resolve the registered future so it isn't GC'd un-awaited (the poll task
    # was cancelled, so mirror that on the shared future).
    if not future.done():
        future.cancel()


@pytest.mark.asyncio
async def test_close_fires_drain_hooks_before_drain_wait() -> None:
    """Issue #1161: ``close(drain=True)`` fires the registered cancel hooks
    BEFORE awaiting ``drain()`` — the ordering that lets the poll-cancel hook
    short-circuit a poll counted in the in-flight counter.

    This pins the ordering directly (independent of the operation_scope
    integration test) so a future refactor that moves the hook fire back
    after the drain wait fails here.
    """
    client = NotebookLMClient(_auth())
    order: list[str] = []

    async def fake_run_drain_hooks() -> None:
        order.append("hooks")

    async def fake_drain(timeout: float | None = None) -> None:
        order.append("drain")

    async def fake_close(**_kwargs: object) -> None:
        order.append("close")

    client._collaborators.drain_tracker.run_drain_hooks = fake_run_drain_hooks  # type: ignore[method-assign]
    client._collaborators.drain_tracker.drain = fake_drain  # type: ignore[method-assign]
    client._collaborators.lifecycle.close = fake_close  # type: ignore[method-assign]

    await client.close()

    assert order == ["hooks", "drain", "close"], (
        "cancel hooks must fire before the drain wait (issue #1161)"
    )


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

    async def fake_close(**_kwargs: object) -> None:
        pass

    client._collaborators.drain_tracker.drain = fake_drain  # type: ignore[method-assign]
    client._collaborators.lifecycle.close = fake_close  # type: ignore[method-assign]

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

    async def fake_close(**_kwargs: object) -> None:
        pass

    client._collaborators.drain_tracker.drain = fake_drain  # type: ignore[method-assign]
    client._collaborators.lifecycle.close = fake_close  # type: ignore[method-assign]

    await client.close(drain=False)

    assert drain_calls == []


@pytest.mark.asyncio
async def test_client_aexit_uses_drain_true_default() -> None:
    """``async with`` exit now drains (BREAKING)."""
    client = NotebookLMClient(_auth())
    drain_calls: list[float | None] = []

    async def fake_drain(timeout: float | None = None) -> None:
        drain_calls.append(timeout)

    async def fake_close(**_kwargs: object) -> None:
        pass

    client._collaborators.drain_tracker.drain = fake_drain  # type: ignore[method-assign]
    client._collaborators.lifecycle.close = fake_close  # type: ignore[method-assign]

    # Drive __aexit__ directly rather than `async with` so we can use the
    # patched core without going through ``open()``.
    await client.__aexit__(None, None, None)

    assert drain_calls == [None]
