import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._artifacts import ArtifactsAPI, GenerationStatus
from notebooklm._core_polling import PollRegistry
from notebooklm.rpc import AuthError, NetworkError, RPCTimeoutError


@pytest.fixture
def api():
    core = MagicMock()
    # Real registry backing so wait_for_completion can ``dict.get(key)``.
    core.poll_registry = PollRegistry()
    core._pending_polls = core.poll_registry.pending
    core._begin_transport_task = AsyncMock(return_value=object())
    core._finish_transport_post = AsyncMock()
    notes_api = MagicMock()
    return ArtifactsAPI(core, notes_api)


@pytest.mark.asyncio
async def test_wait_for_completion_retry_success(api):
    # Mock poll_status to fail twice then succeed
    status_ready = GenerationStatus(task_id="task1", status="completed")

    api.poll_status = AsyncMock()
    api.poll_status.side_effect = [
        NetworkError("transient net"),
        RPCTimeoutError("transient timeout"),
        status_ready,
    ]

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        # Also need to patch asyncio.get_running_loop().time() to avoid timeout
        # but here we just test the retry logic
        result = await api.wait_for_completion("nb1", "task1", timeout=60.0)

        assert result == status_ready
        assert api.poll_status.call_count == 3
        assert mock_sleep.call_count == 2
        # Backoff: 2^1=2, 2^2=4
        mock_sleep.assert_any_call(2.0)
        mock_sleep.assert_any_call(4.0)


@pytest.mark.asyncio
async def test_wait_for_completion_retry_exhausted(api):
    api.poll_status = AsyncMock()
    api.poll_status.side_effect = NetworkError("persistent fail")

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(NetworkError, match="persistent fail"):
            await api.wait_for_completion("nb1", "task1", timeout=60.0)

        # Initial call + 3 retries = 4 total calls
        assert api.poll_status.call_count == 4


@pytest.mark.asyncio
async def test_wait_for_completion_no_retry_on_auth_error(api):
    api.poll_status = AsyncMock()
    api.poll_status.side_effect = AuthError("auth fail")

    with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
        with pytest.raises(AuthError, match="auth fail"):
            await api.wait_for_completion("nb1", "task1", timeout=60.0)

        assert api.poll_status.call_count == 1
        assert mock_sleep.call_count == 0


@pytest.mark.asyncio
async def test_wait_for_completion_follower_cancellation_does_not_cancel_leader_or_later_waiter():
    core = MagicMock()
    core.poll_registry = PollRegistry()
    core._pending_polls = core.poll_registry.pending
    core._begin_transport_task = AsyncMock(return_value=object())
    core._finish_transport_post = AsyncMock()
    api = ArtifactsAPI(core, MagicMock())

    poll_started = asyncio.Event()
    release_poll = asyncio.Event()
    status_ready = GenerationStatus(task_id="task1", status="completed")
    poll_call_count = 0
    test_timeout = 1.0

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        nonlocal poll_call_count
        assert (notebook_id, task_id) == ("nb1", "task1")
        poll_call_count += 1
        poll_started.set()
        await release_poll.wait()
        return status_ready

    api.poll_status = AsyncMock(side_effect=poll_status)

    leader = asyncio.create_task(api.wait_for_completion("nb1", "task1", timeout=60.0))
    key = ("nb1", "task1")
    later_waiter: asyncio.Task[GenerationStatus] | None = None
    try:
        await asyncio.wait_for(poll_started.wait(), timeout=test_timeout)
        for _ in range(10):
            if key in core.poll_registry.pending:
                break
            await asyncio.sleep(0)

        assert core._pending_polls is core.poll_registry.pending
        assert key in core.poll_registry.pending

        follower = asyncio.create_task(api.wait_for_completion("nb1", "task1", timeout=60.0))
        await asyncio.sleep(0)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(follower, timeout=test_timeout)

        assert not leader.done()
        assert key in core.poll_registry.pending
        assert poll_call_count == 1

        later_waiter = asyncio.create_task(api.wait_for_completion("nb1", "task1", timeout=60.0))
        await asyncio.sleep(0)
        release_poll.set()

        assert await asyncio.wait_for(leader, timeout=test_timeout) == status_ready
        assert await asyncio.wait_for(later_waiter, timeout=test_timeout) == status_ready
        assert poll_call_count == 1
        assert core.poll_registry.pending == {}
        assert core._pending_polls == {}
    finally:
        release_poll.set()
        cleanup_tasks = []
        for task in (leader, later_waiter):
            if task is not None and not task.done():
                task.cancel()
                cleanup_tasks.append(task)
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
