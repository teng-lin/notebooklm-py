"""shielded shared refresh task survives waiter cancellation.

Regression test for the cancellation-propagation bug at
``src/notebooklm/_core.py:_await_refresh``: prior to the fix, a caller
cancelled via ``asyncio.wait_for(timeout=...)`` while
``await self._refresh_task`` was pending would propagate the
``CancelledError`` into the *shared* refresh task itself, taking down
every other waiter joining the single in-flight refresh.

The fix wraps the await in ``asyncio.shield`` so the
cancelled waiter unwinds locally while the underlying task continues
producing a value for its siblings.

Acceptance criteria:

    Two ``asyncio.gather`` tasks await refresh; cancel one via
    ``wait_for(timeout=0.01)``; assert the other still receives a
    successful refresh result.

The test is the red gate — it must fail on origin/main (unshielded
await cancels the shared task) and pass once the shield is applied.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens

# async-cancellation propagation tests with no HTTP, no cassette.
# Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr

# Event-wait deadlines: tight enough to fail fast on a regression that
# hangs, generous enough not to flake on a slow CI runner. Every
# event-wait below resolves in <100ms locally.
EVENT_TIMEOUT_S = 5.0


@asynccontextmanager
async def _opened_core(refresh_callback):
    """Open a ``ClientCore`` with the given refresh callback; close cleanly.

    Mirrors ``tests/unit/conftest.make_core`` but lives here because the
    ``tests/unit/`` conftest is not importable from
    ``tests/integration/concurrency/`` (pytest only adds the test root
    directory to ``sys.path`` for sibling imports).
    """
    auth = AuthTokens(
        csrf_token="CSRF_OLD",
        session_id="SID_OLD",
        cookies={"SID": "old_sid_cookie"},
    )
    core = ClientCore(
        auth=auth,
        refresh_callback=refresh_callback,
        refresh_retry_delay=0.0,
    )
    await core.open()
    try:
        yield core
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_kill_shared_refresh():
    """Cancelling one waiter must not cancel the shared refresh task.

    Two coroutines call ``_await_refresh()`` concurrently. The first is
    wrapped in ``asyncio.wait_for(timeout=0.01)`` so it gets cancelled
    while the gated refresh callback is still blocked. The second
    coroutine continues awaiting the shared task. After the callback is
    released, the second coroutine must observe a successful refresh
    (no ``CancelledError`` leaking from the shared task).

    Pre-fix (unshielded ``await self._refresh_task``): the cancelled
    waiter's ``CancelledError`` propagates into the shared task,
    cancelling it; the second waiter raises ``CancelledError``.
    Post-fix (``await asyncio.shield(self._refresh_task)``): the
    cancelled waiter unwinds locally; the shared task completes; the
    second waiter resolves to ``None`` (``_await_refresh`` returns
    ``None`` on success).
    """
    callback_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    call_count = 0
    core_ref: list[ClientCore] = []

    async def cb():
        nonlocal call_count
        call_count += 1
        callback_entered.set()
        await release_refresh.wait()
        tokens = AuthTokens(
            csrf_token="CSRF_REFRESHED",
            session_id="SID_REFRESHED",
            cookies={"SID": "post_refresh"},
        )
        # Mirror the production callback contract: update core.auth
        # in place so the freshly-refreshed values are observable on
        # the shared core after the task completes.
        core_ref[0].auth.csrf_token = tokens.csrf_token
        core_ref[0].auth.session_id = tokens.session_id
        return tokens

    async with _opened_core(refresh_callback=cb) as core:
        core_ref.append(core)

        # Caller #1: wrapped in a tight wait_for so it gets cancelled
        # while the callback is gated. The 10ms timeout is deliberately
        # short — the callback is gated on ``release_refresh.wait()`` and
        # we never release until both callers are joined, so #1 is
        # guaranteed to time out.
        async def cancelled_waiter():
            await asyncio.wait_for(core._await_refresh(), timeout=0.01)

        # Caller #2: plain await — represents a parallel 401 retry path
        # that should observe a successful shared refresh regardless of
        # what happens to caller #1.
        async def surviving_waiter():
            return await core._await_refresh()

        task1 = asyncio.create_task(cancelled_waiter())
        task2 = asyncio.create_task(surviving_waiter())

        # Wait for the gated callback to fire — this proves both tasks
        # are now joined on the same shared refresh task (single-flight
        # invariant from the refresh state machine).
        await asyncio.wait_for(callback_entered.wait(), EVENT_TIMEOUT_S)
        assert call_count == 1, (
            f"Single-flight broken: callback fired {call_count} times "
            "before release; both waiters should share one task."
        )

        # Caller #1 should now have timed out and raised TimeoutError.
        # Awaiting it surfaces the timeout cleanly so the failure mode
        # is observable in the test log.
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await task1

        # The shared task must still be alive — caller #1's cancellation
        # should not have cancelled it. This is the load-bearing
        # invariant the shield protects.
        assert core._refresh_task is not None, "shared refresh task vanished"
        assert not core._refresh_task.done(), (
            "Shared refresh task completed/cancelled before release — "
            "Shield regression: waiter cancellation propagated into the "
            "shared task."
        )

        # Release the gate. The shared task should complete successfully
        # and caller #2 should resolve to ``None`` (``_await_refresh``
        # returns None on success).
        release_refresh.set()
        result = await asyncio.wait_for(task2, EVENT_TIMEOUT_S)

        # The successful refresh propagated to the surviving waiter.
        assert result is None
        # The callback fired exactly once — confirms the surviving
        # waiter joined the shared task rather than spawning a new one
        # after caller #1's cancellation cleared the slot.
        assert call_count == 1, (
            f"Refresh re-fired after waiter cancellation: {call_count}. "
            "Dedupe semantics broken — the shield should preserve the "
            "single in-flight task, not trigger a respawn."
        )
        # The in-place core.auth update from the callback is observable,
        # proving the shared task ran to completion despite the
        # cancellation of caller #1.
        assert core.auth.csrf_token == "CSRF_REFRESHED"


@pytest.mark.asyncio
async def test_refresh_task_slot_not_cleared_on_waiter_cancellation():
    """``self._refresh_task`` must persist across a waiter cancellation.

    Acceptance invariant: ``self._refresh_task`` is only cleared after
    the task completes (success OR failure), NOT on caller cancellation.

    In the current implementation the slot is never explicitly cleared —
    it is overwritten on the next refresh wave (see
    ``test_second_wave_creates_distinct_refresh_task``). This test pins
    that contract: a cancelled waiter does NOT cause the slot to be
    cleared mid-flight, which would break the single-flight invariant
    for any sibling waiter that arrives between the cancellation and
    the callback completing.
    """
    callback_entered = asyncio.Event()
    release_refresh = asyncio.Event()

    async def cb():
        callback_entered.set()
        await release_refresh.wait()
        return AuthTokens(
            csrf_token="CSRF_NEW",
            session_id="SID_NEW",
            cookies={"SID": "x"},
        )

    async with _opened_core(refresh_callback=cb) as core:

        async def cancelled_waiter():
            await asyncio.wait_for(core._await_refresh(), timeout=0.01)

        task = asyncio.create_task(cancelled_waiter())
        await asyncio.wait_for(callback_entered.wait(), EVENT_TIMEOUT_S)

        # Snapshot the task identity before the cancellation lands.
        in_flight = core._refresh_task
        assert in_flight is not None

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await task

        # Slot still points at the same task — not cleared, not replaced.
        assert core._refresh_task is in_flight, (
            "Refresh slot mutated by waiter cancellation — invariant "
            "broken: the slot must persist until the task completes."
        )
        assert not in_flight.done(), (
            "Shared task done before release — waiter cancellation "
            "leaked into the shared task (shield regression)."
        )

        # Clean up so the fixture teardown doesn't hang.
        release_refresh.set()
        await asyncio.wait_for(in_flight, EVENT_TIMEOUT_S)
