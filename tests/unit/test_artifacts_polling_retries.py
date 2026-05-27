import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._artifact_polling import ArtifactPollingService
from notebooklm._artifacts import ArtifactsAPI, GenerationStatus
from notebooklm._polling_registry import PollRegistry
from notebooklm.rpc import AuthError, NetworkError, RPCTimeoutError


class _FakeTransportProvider:
    # ``ArtifactPollingService.wait_for_completion`` calls
    # ``self._session.assert_bound_loop()`` (P0-2). ``None`` is the documented
    # silent-no-op value for the affinity helper, so this stub stays correct
    # without binding to a real loop.
    bound_loop = None

    def assert_bound_loop(self) -> None:
        return None

    def __init__(
        self,
        *,
        token: object | None = None,
        begin_error: BaseException | None = None,
        yield_before_begin_error: bool = False,
        begin_release: asyncio.Event | None = None,
        finish_release: asyncio.Event | None = None,
    ) -> None:
        self.poll_registry = PollRegistry()
        self.token = object() if token is None else token
        self.begin_error = begin_error
        self.yield_before_begin_error = yield_before_begin_error
        self.begin_release = begin_release
        self.finish_release = finish_release
        self.begin_tasks: list[asyncio.Task[object]] = []
        self.begin_labels: list[str] = []
        self.begin_task_done_states: list[bool] = []
        self.begin_started = asyncio.Event()
        self.finish_tokens: list[object] = []
        self.finish_started = asyncio.Event()
        self.finish_finished = asyncio.Event()

    async def rpc_call(self, *args, **kwargs):
        raise AssertionError("unexpected rpc_call")

    async def transport_post(self, *args, **kwargs):
        raise AssertionError("unexpected transport_post")

    async def next_reqid(self, step: int = 100000) -> int:
        return step

    def operation_scope(self, log_label: str):
        provider = self

        class _Scope:
            async def __aenter__(self) -> None:
                await provider._enter_scope(log_label)
                return None

            async def __aexit__(self, exc_type, exc, tb) -> None:
                await provider._exit_scope()
                return None

        return _Scope()

    async def _enter_scope(self, log_label: str) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.begin_tasks.append(task)
        self.begin_labels.append(log_label)
        self.begin_task_done_states.append(task.done())
        self.begin_started.set()
        if self.begin_release is not None:
            await self.begin_release.wait()
        if self.yield_before_begin_error:
            await asyncio.sleep(0)
        if self.begin_error is not None:
            raise self.begin_error

    async def _exit_scope(self) -> None:
        self.finish_tokens.append(self.token)
        self.finish_started.set()
        if self.finish_release is not None:
            await self.finish_release.wait()
        self.finish_finished.set()


@pytest.fixture
def api():
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    core = _make_session_core()
    mock_notebooks = MagicMock()
    mock_notebooks.get_source_ids = AsyncMock(return_value=[])
    return ArtifactsAPI(
        core,
        notebooks=mock_notebooks,
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )


def _make_session_core() -> MagicMock:
    core = MagicMock()
    # Real registry backing so wait_for_completion can ``dict.get(key)``.
    core.assert_bound_loop = MagicMock(return_value=None)
    core.operation_scope = MagicMock(side_effect=lambda _label: _noop_operation_scope())
    return core


@asynccontextmanager
async def _noop_operation_scope():
    yield None


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
async def test_polling_service_operation_scope_wraps_spawned_poll_task() -> None:
    token = object()
    provider = _FakeTransportProvider(token=token)
    service = ArtifactPollingService(provider, provider.poll_registry)

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        assert (notebook_id, task_id) == ("nb1", "task1")
        return GenerationStatus(task_id=task_id, status="completed")

    result = await service.wait_for_completion(
        "nb1",
        "task1",
        initial_interval=0.0,
        max_interval=0.0,
        timeout=1.0,
        poll_status=poll_status,
    )

    assert result.status == "completed"
    assert len(provider.begin_tasks) == 1
    poll_task = provider.begin_tasks[0]
    assert isinstance(poll_task, asyncio.Task)
    assert poll_task.done()
    assert poll_task.get_name() == "artifact-poll-nb1-task1"
    assert provider.begin_task_done_states == [False]
    assert provider.begin_labels == ["artifact wait task1"]
    await asyncio.wait_for(provider.finish_finished.wait(), timeout=1.0)
    assert provider.finish_tokens == [token]


@pytest.mark.asyncio
async def test_polling_service_registers_pending_before_transport_begin_completes() -> None:
    begin_release = asyncio.Event()
    provider = _FakeTransportProvider(begin_release=begin_release)
    service = ArtifactPollingService(provider, provider.poll_registry)
    poll_call_count = 0

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        nonlocal poll_call_count
        poll_call_count += 1
        return GenerationStatus(task_id=task_id, status="completed")

    leader = asyncio.create_task(
        service.wait_for_completion(
            "nb1",
            "task1",
            initial_interval=0.0,
            max_interval=0.0,
            timeout=1.0,
            poll_status=poll_status,
        )
    )
    follower: asyncio.Task[GenerationStatus] | None = None
    key = ("nb1", "task1")
    try:
        await asyncio.wait_for(provider.begin_started.wait(), timeout=1.0)
        assert provider.poll_registry.get(key) is not None

        follower = asyncio.create_task(
            service.wait_for_completion(
                "nb1",
                "task1",
                initial_interval=0.0,
                max_interval=0.0,
                timeout=1.0,
                poll_status=poll_status,
            )
        )
        await asyncio.sleep(0)
        begin_release.set()

        leader_result = await asyncio.wait_for(leader, timeout=1.0)
        follower_result = await asyncio.wait_for(follower, timeout=1.0)

        assert leader_result.status == "completed"
        assert follower_result.status == "completed"
        assert poll_call_count == 1
        await asyncio.wait_for(provider.finish_finished.wait(), timeout=1.0)
        assert provider.poll_registry.get(key) is None
    finally:
        begin_release.set()
        cleanup_tasks = [
            task for task in (leader, follower) if task is not None and not task.done()
        ]
        for task in cleanup_tasks:
            task.cancel()
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_polling_service_resolves_wait_before_slow_transport_finish() -> None:
    token = object()
    finish_release = asyncio.Event()
    provider = _FakeTransportProvider(token=token, finish_release=finish_release)
    service = ArtifactPollingService(provider, provider.poll_registry)

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        return GenerationStatus(task_id=task_id, status="completed")

    waiter = asyncio.create_task(
        service.wait_for_completion(
            "nb1",
            "task1",
            initial_interval=0.0,
            max_interval=0.0,
            timeout=1.0,
            poll_status=poll_status,
        )
    )
    try:
        await asyncio.wait_for(provider.finish_started.wait(), timeout=1.0)
        assert not waiter.done()
        assert provider.finish_tokens == [token]

        finish_release.set()
        result = await asyncio.wait_for(waiter, timeout=1.0)
    finally:
        finish_release.set()
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    assert result.status == "completed"
    assert provider.finish_finished.is_set()


@pytest.mark.asyncio
async def test_polling_service_drain_waits_for_bookkeeping_without_active_polls() -> None:
    token = object()
    finish_release = asyncio.Event()
    provider = _FakeTransportProvider(token=token, finish_release=finish_release)
    service = ArtifactPollingService(provider, provider.poll_registry)

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        return GenerationStatus(task_id=task_id, status="completed")

    waiter = asyncio.create_task(
        service.wait_for_completion(
            "nb1",
            "task1",
            initial_interval=0.0,
            max_interval=0.0,
            timeout=1.0,
            poll_status=poll_status,
        )
    )

    await asyncio.wait_for(provider.finish_started.wait(), timeout=1.0)
    assert not waiter.done()

    drain_task = asyncio.create_task(service.drain())
    try:
        await asyncio.sleep(0)
        assert not drain_task.done()

        finish_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=1.0)
        await asyncio.wait_for(drain_task, timeout=1.0)
    finally:
        finish_release.set()
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if not drain_task.done():
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)

    assert provider.finish_tokens == [token]


@pytest.mark.asyncio
async def test_polling_service_finishes_transport_token_once_after_poll_failure() -> None:
    token = object()
    provider = _FakeTransportProvider(token=token)
    service = ArtifactPollingService(provider, provider.poll_registry)

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        raise ValueError(f"poll failed: {notebook_id}/{task_id}")

    with pytest.raises(ValueError, match="poll failed: nb1/task1"):
        await service.wait_for_completion(
            "nb1",
            "task1",
            initial_interval=0.0,
            max_interval=0.0,
            timeout=1.0,
            poll_status=poll_status,
        )

    assert len(provider.begin_tasks) == 1
    assert provider.begin_tasks[0].done()
    await asyncio.wait_for(provider.finish_finished.wait(), timeout=1.0)
    assert provider.finish_tokens == [token]


@pytest.mark.asyncio
async def test_polling_service_cancels_and_drains_spawned_poll_task_if_begin_fails() -> None:
    begin_error = RuntimeError("draining")
    provider = _FakeTransportProvider(
        begin_error=begin_error,
        yield_before_begin_error=True,
    )
    service = ArtifactPollingService(provider, provider.poll_registry)

    async def poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        raise AssertionError("poll should not start when operation admission fails")

    with pytest.raises(RuntimeError, match="draining"):
        await service.wait_for_completion(
            "nb1",
            "task1",
            initial_interval=0.0,
            max_interval=0.0,
            timeout=1.0,
            poll_status=poll_status,
        )

    assert len(provider.begin_tasks) == 1
    assert provider.begin_tasks[0].done()
    assert provider.poll_registry.get(("nb1", "task1")) is None
    assert not provider.finish_started.is_set()
    assert provider.finish_tokens == []


@pytest.mark.asyncio
async def test_wait_for_completion_follower_cancellation_does_not_cancel_leader_or_later_waiter():
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    core = _make_session_core()
    api = ArtifactsAPI(
        core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )

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
            if api._poll_registry.get(key) is not None:
                break
            await asyncio.sleep(0)

        assert api._poll_registry.get(key) is not None

        follower = asyncio.create_task(api.wait_for_completion("nb1", "task1", timeout=60.0))
        await asyncio.sleep(0)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(follower, timeout=test_timeout)

        assert not leader.done()
        assert api._poll_registry.get(key) is not None
        assert poll_call_count == 1

        later_waiter = asyncio.create_task(api.wait_for_completion("nb1", "task1", timeout=60.0))
        await asyncio.sleep(0)
        release_poll.set()

        assert await asyncio.wait_for(leader, timeout=test_timeout) == status_ready
        assert await asyncio.wait_for(later_waiter, timeout=test_timeout) == status_ready
        assert poll_call_count == 1
        assert api._poll_registry.get(key) is None
    finally:
        release_poll.set()
        cleanup_tasks = []
        for task in (leader, later_waiter):
            if task is not None and not task.done():
                task.cancel()
                cleanup_tasks.append(task)
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
