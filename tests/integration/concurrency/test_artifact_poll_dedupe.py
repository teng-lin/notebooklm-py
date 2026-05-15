"""T7.E2 — leader/follower polling dedupe for ``wait_for_completion``.

Regression test for the audit §21 finding: when N callers concurrently
invoke ``ArtifactsAPI.wait_for_completion`` for the *same* ``(notebook_id,
task_id)``, each runs its own poll loop and issues N independent
``LIST_ARTIFACTS`` requests per polling tick — wasteful at best and a
rate-limit / quota burner at worst.

The fix wires a per-instance registry ``ClientCore._pending_polls`` keyed
by ``(notebook_id, task_id)``. The *leader* (first caller) spawns a
shielded poll task; *followers* await the shared future via
``asyncio.shield(future)``. Cancellation is per-caller — only the
cancelled caller's await raises; the underlying poll continues; remaining
followers still receive the result.

Four scenarios:

A. Dedupe — two concurrent waiters; only ONE poll loop runs; both receive
   the same ``GenerationStatus``.
B. Leader cancellation — cancel the first waiter mid-poll; the second
   waiter still receives a result (poll continues via shielded task).
C. Cleanup on success — after both waiters return, ``_pending_polls`` is
   empty.
D. Cleanup on exception — poll raises; both waiters see the exception;
   ``_pending_polls`` is empty.

Implementation strategy: monkey-patch ``ArtifactsAPI.poll_status`` to a
controllable async function so the test owns the poll-loop's progression
deterministically — no httpx mocking, no race-prone sleeps. We assert
on the *number of times* ``poll_status`` was called for the deduped
``(nb, task_id)`` pair: one leader = one logical poll loop = one call
sequence, regardless of how many followers piled on.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.types import GenerationStatus

# Bounded test deadline: every scenario completes in <500ms locally; a
# 5-second cap lets CI absorb a slow runner without masking a real
# regression that hangs.
TEST_TIMEOUT_S = 5.0


@pytest.fixture
def _auth_tokens() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "test_sid"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


@asynccontextmanager
async def _opened_client(auth: AuthTokens) -> AsyncIterator[NotebookLMClient]:
    """Open a ``NotebookLMClient`` and close cleanly.

    The test never touches the transport — ``poll_status`` is
    monkey-patched on the ``ArtifactsAPI`` instance directly — so no
    httpx mock is wired in. The client just needs ``_core`` to exist so
    we can inspect ``_core._pending_polls``.
    """
    client = NotebookLMClient(auth)
    await client.__aenter__()
    try:
        yield client
    finally:
        await client.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_scenario_a_two_waiters_share_one_poll_loop(_auth_tokens):
    """A. Dedupe: two concurrent waiters → one ``LIST_ARTIFACTS`` flight.

    Both waiters receive the same ``GenerationStatus``. The polling loop
    runs exactly once on behalf of both.
    """
    async with _opened_client(_auth_tokens) as client:
        poll_call_count = 0
        release_first_poll = asyncio.Event()
        both_waiters_attached = asyncio.Event()
        completed_status = GenerationStatus(
            task_id="task_xyz", status="completed", url="https://example/url"
        )

        async def fake_poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
            nonlocal poll_call_count
            poll_call_count += 1
            # Hold the first poll until BOTH waiters have registered.
            # Subsequent polls (none expected) return immediately.
            if poll_call_count == 1:
                # Yield once so both waiters can race to register.
                await both_waiters_attached.wait()
                await release_first_poll.wait()
            return completed_status

        client.artifacts.poll_status = fake_poll_status  # type: ignore[method-assign]

        async def waiter():
            return await client.artifacts.wait_for_completion(
                "nb_001", "task_xyz", initial_interval=0.01, timeout=TEST_TIMEOUT_S
            )

        # Start both waiters; ensure they're both parked at the await
        # inside ``wait_for_completion`` before releasing the poll.
        w1 = asyncio.create_task(waiter())
        w2 = asyncio.create_task(waiter())

        # Spin until both have registered (one as leader, one as follower).
        # We assert via the pending-polls registry, which is the public
        # invariant the fix introduces. Cap iterations to avoid a hang
        # if the fix isn't applied at all.
        for _ in range(50):
            if len(client._core._pending_polls) >= 1 and (
                w1.done() is False and w2.done() is False
            ):
                # Both have parked; allow the poll to fire.
                break
            await asyncio.sleep(0.01)
        both_waiters_attached.set()
        # Tiny yield so the poll enters its await.
        await asyncio.sleep(0)
        release_first_poll.set()

        r1, r2 = await asyncio.wait_for(asyncio.gather(w1, w2), timeout=TEST_TIMEOUT_S)

        # Exactly one logical poll for the shared (nb, task) pair.
        assert poll_call_count == 1, (
            f"expected 1 deduped poll, got {poll_call_count} "
            "(followers should attach to the leader's future, not run their own loop)"
        )
        assert r1 == completed_status
        assert r2 == completed_status


@pytest.mark.asyncio
async def test_scenario_b_leader_cancel_does_not_kill_follower(_auth_tokens):
    """B. Leader cancellation: cancel waiter 1; waiter 2 still completes.

    The leader's await on ``asyncio.shield(future)`` unwinds on cancel,
    but the underlying shielded poll task keeps running and eventually
    sets the future. The follower receives the result.
    """
    async with _opened_client(_auth_tokens) as client:
        poll_started = asyncio.Event()
        release_poll = asyncio.Event()
        completed_status = GenerationStatus(task_id="task_b", status="completed")
        poll_call_count = 0

        async def fake_poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
            nonlocal poll_call_count
            poll_call_count += 1
            poll_started.set()
            await release_poll.wait()
            return completed_status

        client.artifacts.poll_status = fake_poll_status  # type: ignore[method-assign]

        # Leader (will be cancelled).
        w1 = asyncio.create_task(
            client.artifacts.wait_for_completion(
                "nb_001", "task_b", initial_interval=0.01, timeout=TEST_TIMEOUT_S
            )
        )
        # Wait until the leader has spawned its poll task.
        await asyncio.wait_for(poll_started.wait(), timeout=TEST_TIMEOUT_S)

        # Follower attaches AFTER the leader has registered.
        w2 = asyncio.create_task(
            client.artifacts.wait_for_completion(
                "nb_001", "task_b", initial_interval=0.01, timeout=TEST_TIMEOUT_S
            )
        )
        # Yield so the follower enters its await on the shared future.
        await asyncio.sleep(0)

        # Cancel the leader.
        w1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w1

        # Now release the poll; the follower must still observe a result.
        release_poll.set()
        result = await asyncio.wait_for(w2, timeout=TEST_TIMEOUT_S)

        assert result == completed_status
        # Exactly one poll ran — the follower never spun up its own loop
        # and the leader's cancel did not retrigger the poll.
        assert poll_call_count == 1, (
            f"expected exactly 1 poll across leader+follower, got {poll_call_count}"
        )


@pytest.mark.asyncio
async def test_scenario_c_pending_polls_empty_after_success(_auth_tokens):
    """C. Cleanup on success: registry is empty after both waiters return."""
    async with _opened_client(_auth_tokens) as client:
        completed_status = GenerationStatus(task_id="task_c", status="completed")

        async def fake_poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
            return completed_status

        client.artifacts.poll_status = fake_poll_status  # type: ignore[method-assign]

        async def waiter():
            return await client.artifacts.wait_for_completion(
                "nb_001", "task_c", initial_interval=0.01, timeout=TEST_TIMEOUT_S
            )

        r1, r2 = await asyncio.wait_for(asyncio.gather(waiter(), waiter()), timeout=TEST_TIMEOUT_S)

        assert r1 == completed_status
        assert r2 == completed_status
        # Give the done-callback a tick to run; on CPython 3.10+ it runs
        # synchronously when the task transitions, but yield once to be
        # safe across event loops.
        await asyncio.sleep(0)
        assert client._core._pending_polls == {}, (
            f"registry leaked entries on success path: {client._core._pending_polls!r}"
        )


@pytest.mark.asyncio
async def test_scenario_d_pending_polls_empty_after_exception(_auth_tokens):
    """D. Cleanup on exception: poll raises → both waiters see it; registry clean."""

    class _Boom(RuntimeError):
        pass

    async with _opened_client(_auth_tokens) as client:
        release_poll = asyncio.Event()
        both_attached = asyncio.Event()
        poll_call_count = 0

        async def fake_poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
            nonlocal poll_call_count
            poll_call_count += 1
            if poll_call_count == 1:
                await both_attached.wait()
                await release_poll.wait()
                raise _Boom("simulated poll failure")
            # Defensive: if somehow a second poll fires, fail loudly so
            # the dedupe regression is visible rather than masked.
            raise AssertionError("dedupe broken: poll_status called twice")

        client.artifacts.poll_status = fake_poll_status  # type: ignore[method-assign]

        async def waiter():
            return await client.artifacts.wait_for_completion(
                "nb_001", "task_d", initial_interval=0.01, timeout=TEST_TIMEOUT_S
            )

        w1 = asyncio.create_task(waiter())
        w2 = asyncio.create_task(waiter())

        # Spin briefly for both to register on the shared future.
        for _ in range(50):
            if len(client._core._pending_polls) >= 1 and not (w1.done() or w2.done()):
                break
            await asyncio.sleep(0.01)
        both_attached.set()
        await asyncio.sleep(0)
        release_poll.set()

        with pytest.raises(_Boom):
            await asyncio.wait_for(w1, timeout=TEST_TIMEOUT_S)
        with pytest.raises(_Boom):
            await asyncio.wait_for(w2, timeout=TEST_TIMEOUT_S)

        await asyncio.sleep(0)
        assert client._core._pending_polls == {}, (
            f"registry leaked on exception path: {client._core._pending_polls!r}"
        )
        assert poll_call_count == 1, (
            f"expected 1 poll, got {poll_call_count} (dedupe must hold on exception path)"
        )
