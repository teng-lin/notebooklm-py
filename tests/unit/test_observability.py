from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from _fixtures.kernel_test_helpers import install_http_client_for_test
from notebooklm import (
    ClientMetricsSnapshot,
    NotebookLMClient,
    RpcTelemetryEvent,
    correlation_id,
    get_request_id,
)
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._note_service import NoteService
from notebooklm._session import Session
from notebooklm._source_upload import SourceUploadPipeline
from notebooklm._sources import SourcesAPI
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod
from notebooklm.types import GenerationStatus


@pytest.mark.asyncio
async def test_rpc_metrics_event_and_correlation_scope(auth_tokens: AuthTokens) -> None:
    """Public contract: ``rpc_call`` bumps counters + emits ``RpcTelemetryEvent``.

    As of Tier-12 PR 12.4 the per-RPC success/failure counters and the
    ``on_rpc_event`` fire live inside ``MetricsMiddleware`` (which sits
    in the chain around ``_perform_authed_post``), not inside
    ``RpcExecutor.execute_with_telemetry``. The seam the test mocks
    therefore has to live below the chain — mocking ``_rpc_call_impl``
    (the historical swap point) now bypasses the chain entirely and
    would silence both the counter and the event. We mock
    ``_perform_authed_post`` instead so the chain runs end-to-end, and
    we return a wire-format payload that the real decoder accepts.

    The test still asserts the same five public-contract invariants it
    always has: result value, correlation-id propagation INTO the chain,
    contextvar cleanup AFTER the chain, counter increments, and one
    event with the expected fields.
    """
    events: list[RpcTelemetryEvent] = []

    # Inject the decoder at construction time (Session DI seam — see
    # ``docs/improvement.md`` §4.1). The real decoder requires a wire
    # payload that matches the method's RPC ID; constructing one makes
    # the test brittle to RPC-ID changes. Stubbing keeps the test focused
    # on observability semantics (counters + events + correlation) rather
    # than wire-format details.
    def fake_decode(raw: str, rpc_id: str, *, allow_null: bool = False) -> dict:
        return {"ok": True}

    core = Session(auth_tokens, on_rpc_event=events.append, decode_response=fake_decode)
    install_http_client_for_test(core._kernel, AsyncMock(spec=httpx.AsyncClient))
    seen_request_ids: list[str | None] = []

    # Mock the chain LEAF (innermost wrapper around
    # ``Kernel.post``) so the real chain runs
    # end-to-end and ``MetricsMiddleware`` sees the call. Mocking
    # ``_perform_authed_post`` itself would bypass the chain entirely
    # and silence the counters this test exists to assert. Mocking
    # ``_rpc_call_impl`` (the historical swap point) would do the same.
    from notebooklm._middleware import RpcResponse

    async def fake_terminal(request: object) -> RpcResponse:
        # Read the correlation id INSIDE the chain so the assertion
        # below verifies the contextvar survived chain entry.
        seen_request_ids.append(get_request_id())
        return RpcResponse(
            response=httpx.Response(200, text=")]}'\n[]"),
            context=request.context,  # type: ignore[attr-defined]
        )

    core._authed_post_chain_terminal = fake_terminal  # type: ignore[method-assign]
    # Rebuild the chain so it wraps the new terminal (the original chain
    # was built in ``__init__`` against the original bound method).
    from notebooklm._middleware import build_chain

    core._authed_post_chain = build_chain(core._middlewares, fake_terminal)

    with correlation_id("batch-42"):
        result = await core.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb_123"])

    assert result == {"ok": True}
    assert seen_request_ids == ["batch-42"]
    assert get_request_id() is None

    snapshot = core.metrics_snapshot()
    assert isinstance(snapshot, ClientMetricsSnapshot)
    assert snapshot.rpc_calls_started == 1
    assert snapshot.rpc_calls_succeeded == 1
    assert snapshot.rpc_calls_failed == 0
    assert snapshot.rpc_latency_seconds_total >= 0

    assert len(events) == 1
    assert events[0].method == "GET_NOTEBOOK"
    assert events[0].status == "success"
    assert events[0].request_id == "batch-42"


@pytest.mark.asyncio
async def test_drain_rejects_new_work_and_waits_for_in_flight(auth_tokens: AuthTokens) -> None:
    core = Session(auth_tokens)
    started = asyncio.Event()
    release = asyncio.Event()

    async def in_flight() -> None:
        operation_token = await core._drain_tracker.begin_transport_post("test")
        started.set()
        try:
            await release.wait()
        finally:
            await core._drain_tracker.finish_transport_post(operation_token)

    task = asyncio.create_task(in_flight())
    await started.wait()

    drain_task = asyncio.create_task(core.drain(timeout=1.0))
    await asyncio.sleep(0)

    assert not drain_task.done()
    with pytest.raises(RuntimeError, match="draining"):
        await core._drain_tracker.begin_transport_post("new")

    release.set()
    await drain_task
    await task


@pytest.mark.asyncio
async def test_drain_allows_nested_work_inside_accepted_operation(
    auth_tokens: AuthTokens,
) -> None:
    core = Session(auth_tokens)
    outer_token = await core._drain_tracker.begin_transport_post("source upload")
    try:
        drain_task = asyncio.create_task(core.drain(timeout=1.0))
        await asyncio.sleep(0)

        nested_token = await core._drain_tracker.begin_transport_post("RPC ADD_SOURCE")
        await core._drain_tracker.finish_transport_post(nested_token)

        assert not drain_task.done()
    finally:
        await core._drain_tracker.finish_transport_post(outer_token)

    await drain_task


@pytest.mark.asyncio
async def test_operation_scope_tracks_drain_without_upload_semaphore(
    auth_tokens: AuthTokens,
) -> None:
    core = Session(auth_tokens)

    async with core.operation_scope("plain-operation"):
        assert core._drain_tracker._in_flight_posts == 1
        assert not hasattr(core, "get_upload_semaphore")

    assert core._drain_tracker._in_flight_posts == 0
    assert "_upload_semaphore" not in core.__dict__


@pytest.mark.asyncio
async def test_drain_rejects_child_task_spawned_from_accepted_operation(
    auth_tokens: AuthTokens,
) -> None:
    core = Session(auth_tokens)
    outer_token = await core._drain_tracker.begin_transport_post("source upload")
    try:
        drain_task = asyncio.create_task(core.drain(timeout=1.0))
        await asyncio.sleep(0)

        async def child_work() -> None:
            child_token = await core._drain_tracker.begin_transport_post("child task")
            await core._drain_tracker.finish_transport_post(child_token)

        with pytest.raises(RuntimeError, match="draining"):
            await asyncio.create_task(child_work())
    finally:
        await core._drain_tracker.finish_transport_post(outer_token)

    await drain_task


@pytest.mark.asyncio
async def test_drain_waits_for_artifact_poll_task(auth_tokens: AuthTokens) -> None:
    core = Session(auth_tokens)
    api = ArtifactsAPI(
        core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )
    first_poll_started = asyncio.Event()
    release_first_poll = asyncio.Event()
    poll_count = 0

    async def fake_poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        nonlocal poll_count
        operation_token = await core._drain_tracker.begin_transport_post("poll_status")
        try:
            poll_count += 1
            if poll_count == 1:
                first_poll_started.set()
                await release_first_poll.wait()
                return GenerationStatus(task_id=task_id, status="in_progress")
            return GenerationStatus(task_id=task_id, status="completed")
        finally:
            await core._drain_tracker.finish_transport_post(operation_token)

    api.poll_status = fake_poll_status  # type: ignore[method-assign]

    wait_task = asyncio.create_task(
        api.wait_for_completion(
            "nb_123",
            "task_1",
            initial_interval=0.0,
            max_interval=0.0,
            timeout=1.0,
        )
    )
    await first_poll_started.wait()

    drain_task = asyncio.create_task(core.drain(timeout=1.0))
    await asyncio.sleep(0)
    assert not drain_task.done()

    release_first_poll.set()
    result = await wait_task
    await drain_task

    assert result.status == "completed"
    assert poll_count == 2


@pytest.mark.asyncio
async def test_close_with_drain_closes_transport_after_timeout(auth_tokens: AuthTokens) -> None:
    client = NotebookLMClient(auth_tokens)
    calls: list[str] = []

    async def drain_timeout(timeout: float | None = None) -> None:
        calls.append(f"drain:{timeout}")
        raise TimeoutError("deadline")

    async def close_transport() -> None:
        calls.append("close")

    client._session.drain = drain_timeout  # type: ignore[method-assign]
    client._session.close = close_transport  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="deadline"):
        await client.close(drain=True, drain_timeout=0.1)

    assert calls == ["drain:0.1", "close"]


@pytest.mark.asyncio
async def test_close_with_invalid_drain_does_not_close_transport(auth_tokens: AuthTokens) -> None:
    client = NotebookLMClient(auth_tokens)
    calls: list[str] = []

    async def invalid_drain(timeout: float | None = None) -> None:
        calls.append(f"drain:{timeout}")
        raise ValueError("bad deadline")

    async def close_transport() -> None:
        calls.append("close")

    client._session.drain = invalid_drain  # type: ignore[method-assign]
    client._session.close = close_transport  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="bad deadline"):
        await client.close(drain=True, drain_timeout=-1.0)

    assert calls == ["drain:-1.0"]


@pytest.mark.asyncio
async def test_upload_progress_callback_receives_byte_counts(
    auth_tokens: AuthTokens,
    tmp_path,
) -> None:
    core = Session(auth_tokens)
    await core.open()
    try:
        api = SourcesAPI(
            core,
            uploader=SourceUploadPipeline(
                core,
                core.kernel,
                core.auth,
                record_upload_queue_wait=core.record_upload_queue_wait,
            ),
        )
        test_file = tmp_path / "upload.txt"
        content = b"hello progress"
        test_file.write_bytes(content)
        events: list[tuple[int, int]] = []

        async def on_progress(sent: int, total: int) -> None:
            events.append((sent, total))

        async def consume_post(*args: object, **kwargs: object) -> MagicMock:
            async for _chunk in kwargs["content"]:
                pass
            response = MagicMock()
            response.raise_for_status.return_value = None
            return response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = consume_post
            mock_client_cls.return_value = mock_client

            await api._upload_file_streaming(
                "https://upload.example.com/session",
                test_file,
                on_progress=on_progress,
            )

        assert events == [(0, len(content)), (len(content), len(content))]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_wait_for_completion_status_change_callback(auth_tokens: AuthTokens) -> None:
    core = Session(auth_tokens)
    api = ArtifactsAPI(
        core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )
    statuses = [
        GenerationStatus(task_id="task_1", status="in_progress"),
        GenerationStatus(task_id="task_1", status="completed", url="https://example.test/out"),
    ]
    seen: list[str] = []

    async def fake_poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        return statuses.pop(0)

    api.poll_status = fake_poll_status  # type: ignore[method-assign]

    result = await api.wait_for_completion(
        "nb_123",
        "task_1",
        initial_interval=0.0,
        timeout=1.0,
        on_status_change=lambda status: seen.append(status.status),
    )

    assert result.status == "completed"
    assert seen == ["in_progress", "completed"]
