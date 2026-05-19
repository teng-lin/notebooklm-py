from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notebooklm import (
    ClientMetricsSnapshot,
    NotebookLMClient,
    RpcTelemetryEvent,
    correlation_id,
    get_request_id,
)
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._core import ClientCore
from notebooklm._sources import SourcesAPI
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod
from notebooklm.types import GenerationStatus


@pytest.mark.asyncio
async def test_rpc_metrics_event_and_correlation_scope(auth_tokens: AuthTokens) -> None:
    events: list[RpcTelemetryEvent] = []
    core = ClientCore(auth_tokens, on_rpc_event=events.append)
    core._http_client = AsyncMock(spec=httpx.AsyncClient)
    seen_request_ids: list[str | None] = []

    async def fake_impl(*args: object, **kwargs: object) -> dict[str, bool]:
        seen_request_ids.append(get_request_id())
        return {"ok": True}

    core._rpc_call_impl = fake_impl  # type: ignore[method-assign]

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
    core = ClientCore(auth_tokens)
    started = asyncio.Event()
    release = asyncio.Event()

    async def in_flight() -> None:
        operation_token = await core._begin_transport_post("test")
        started.set()
        try:
            await release.wait()
        finally:
            await core._finish_transport_post(operation_token)

    task = asyncio.create_task(in_flight())
    await started.wait()

    drain_task = asyncio.create_task(core.drain(timeout=1.0))
    await asyncio.sleep(0)

    assert not drain_task.done()
    with pytest.raises(RuntimeError, match="draining"):
        await core._begin_transport_post("new")

    release.set()
    await drain_task
    await task


@pytest.mark.asyncio
async def test_drain_allows_nested_work_inside_accepted_operation(
    auth_tokens: AuthTokens,
) -> None:
    core = ClientCore(auth_tokens)
    outer_token = await core._begin_transport_post("source upload")
    try:
        drain_task = asyncio.create_task(core.drain(timeout=1.0))
        await asyncio.sleep(0)

        nested_token = await core._begin_transport_post("RPC ADD_SOURCE")
        await core._finish_transport_post(nested_token)

        assert not drain_task.done()
    finally:
        await core._finish_transport_post(outer_token)

    await drain_task


@pytest.mark.asyncio
async def test_drain_rejects_child_task_spawned_from_accepted_operation(
    auth_tokens: AuthTokens,
) -> None:
    core = ClientCore(auth_tokens)
    outer_token = await core._begin_transport_post("source upload")
    try:
        drain_task = asyncio.create_task(core.drain(timeout=1.0))
        await asyncio.sleep(0)

        async def child_work() -> None:
            child_token = await core._begin_transport_post("child task")
            await core._finish_transport_post(child_token)

        with pytest.raises(RuntimeError, match="draining"):
            await asyncio.create_task(child_work())
    finally:
        await core._finish_transport_post(outer_token)

    await drain_task


@pytest.mark.asyncio
async def test_drain_waits_for_artifact_poll_task(auth_tokens: AuthTokens) -> None:
    core = ClientCore(auth_tokens)
    api = ArtifactsAPI(core)
    first_poll_started = asyncio.Event()
    release_first_poll = asyncio.Event()
    poll_count = 0

    async def fake_poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        nonlocal poll_count
        operation_token = await core._begin_transport_post("poll_status")
        try:
            poll_count += 1
            if poll_count == 1:
                first_poll_started.set()
                await release_first_poll.wait()
                return GenerationStatus(task_id=task_id, status="in_progress")
            return GenerationStatus(task_id=task_id, status="completed")
        finally:
            await core._finish_transport_post(operation_token)

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

    client._core.drain = drain_timeout  # type: ignore[method-assign]
    client._core.close = close_transport  # type: ignore[method-assign]

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

    client._core.drain = invalid_drain  # type: ignore[method-assign]
    client._core.close = close_transport  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="bad deadline"):
        await client.close(drain=True, drain_timeout=-1.0)

    assert calls == ["drain:-1.0"]


@pytest.mark.asyncio
async def test_upload_progress_callback_receives_byte_counts(
    auth_tokens: AuthTokens,
    tmp_path,
) -> None:
    core = ClientCore(auth_tokens)
    await core.open()
    try:
        api = SourcesAPI(core)
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
    core = ClientCore(auth_tokens)
    api = ArtifactsAPI(core)
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
