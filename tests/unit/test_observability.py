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
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.mind_maps import NoteBackedMindMapService
from notebooklm._web.notes import NoteService
from notebooklm._web.sources import WebSourcesAPI
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod
from notebooklm.types import GenerationStatus
from tests._fixtures.kernel_test_helpers import install_http_client_for_test
from tests._helpers.client_factory import build_client_shell_for_tests


@pytest.mark.asyncio
async def test_rpc_metrics_event_and_correlation_scope(auth_tokens: AuthTokens) -> None:
    """Public contract: ``rpc_call`` bumps counters + emits ``RpcTelemetryEvent``.

    As of Tier-12 PR 12.4 the per-RPC success/failure counters and the
    ``on_rpc_event`` fire live inside ``MetricsMiddleware`` (which sits
    in the shared authed transport chain), not inside
    ``RpcExecutor.rpc_call``. The seam the test mocks therefore has to
    live below the chain. We mock the chain terminal so the chain runs
    end-to-end, and we return a wire-format payload that the real decoder
    accepts.

    The test still asserts the same five public-contract invariants it
    always has: result value, correlation-id propagation INTO the chain,
    contextvar cleanup AFTER the chain, counter increments, and one
    event with the expected fields.
    """
    events: list[RpcTelemetryEvent] = []

    # Inject the decoder at construction time (NotebookLMClient test seam; see
    # ``docs/architecture.md``'s ClientSeams wiring). The real decoder requires a wire
    # payload that matches the method's RPC ID; constructing one makes
    # the test brittle to RPC-ID changes. Stubbing keeps the test focused
    # on observability semantics (counters + events + correlation) rather
    # than wire-format details.
    def fake_decode(
        raw: str, rpc_id: str, *, allow_null: bool = False, raise_on_null_status: bool = False
    ) -> dict:
        return {"ok": True}

    core = build_client_shell_for_tests(
        auth_tokens, on_rpc_event=events.append, decode_response=fake_decode
    )
    core._collaborators.kernel.activate_epoch(1)
    install_http_client_for_test(core._collaborators.kernel, AsyncMock(spec=httpx.AsyncClient))
    supervisor = core._collaborators.call_supervisor
    supervisor.set_bound_loop(asyncio.get_running_loop())
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    core._collaborators.auth_coord.activate_epoch(1)
    seen_request_ids: list[str | None] = []

    # Mock the chain LEAF (innermost wrapper around
    # ``Kernel.post``) so the real chain runs
    # end-to-end and ``MetricsMiddleware`` sees the call. Mocking
    # the shared authed transport itself would bypass the chain entirely
    # and silence the counters this test exists to assert. Mocking above
    # the chain would do the same.
    from notebooklm._web.transport.middleware.core import RpcResponse

    async def fake_terminal(request: object) -> RpcResponse:
        # Read the correlation id INSIDE the chain so the assertion
        # below verifies the contextvar survived chain entry.
        seen_request_ids.append(get_request_id())
        return RpcResponse(
            response=httpx.Response(200, text=")]}'\n[]"),
            context=request.context,  # type: ignore[attr-defined]
        )

    core._composed.chain_host._authed_post_chain_terminal = fake_terminal  # type: ignore[method-assign]
    # Rebuild the chain so it wraps the new terminal (the original chain
    # was built in the composition root against the original bound method).
    from notebooklm._web.transport.middleware.core import build_chain

    core._composed.chain_host._authed_post_chain = build_chain(
        core._composed.middlewares, fake_terminal
    )

    with correlation_id("batch-42"):
        result = await core._rpc_executor.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb_123"])

    assert result == {"ok": True}
    assert seen_request_ids == ["batch-42"]
    assert get_request_id() is None

    snapshot = core._collaborators.metrics.snapshot()
    assert isinstance(snapshot, ClientMetricsSnapshot)
    assert snapshot.rpc_calls_started == 1
    assert snapshot.rpc_calls_succeeded == 1
    assert snapshot.rpc_calls_failed == 0
    # A clean decode never touches the drift counter (issue #1492).
    assert snapshot.rpc_decode_errors == 0
    assert snapshot.rpc_latency_seconds_total >= 0

    assert len(events) == 1
    assert events[0].method == "GET_NOTEBOOK"
    assert events[0].status == "success"
    assert events[0].request_id == "batch-42"


@pytest.mark.asyncio
async def test_rpc_decode_error_bumps_drift_counter(auth_tokens: AuthTokens) -> None:
    """Public contract: a decode/drift failure increments ``rpc_decode_errors``.

    End-to-end mirror of the success test above (issue #1492). The transport
    leg returns 200 OK; the injected decoder then raises a ``DecodingError``
    (the base of ``UnknownRPCMethodError``), exercising the executor's
    decode-boundary increment. Wire-schema drift is the stated #1 breakage
    class, so the snapshot must expose it as a dedicated counter distinct from
    ``rpc_calls_failed`` (which tracks transport-leg failures).
    """
    from notebooklm.exceptions import DecodingError

    def drifting_decode(
        raw: str, rpc_id: str, *, allow_null: bool = False, raise_on_null_status: bool = False
    ) -> dict:
        raise DecodingError("Google reshaped the response", method_id=rpc_id)

    core = build_client_shell_for_tests(auth_tokens, decode_response=drifting_decode)
    core._collaborators.kernel.activate_epoch(1)
    install_http_client_for_test(core._collaborators.kernel, AsyncMock(spec=httpx.AsyncClient))
    supervisor = core._collaborators.call_supervisor
    supervisor.set_bound_loop(asyncio.get_running_loop())
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    core._collaborators.auth_coord.activate_epoch(1)

    from notebooklm._web.transport.middleware.core import RpcResponse, build_chain

    async def fake_terminal(request: object) -> RpcResponse:
        return RpcResponse(
            response=httpx.Response(200, text=")]}'\n[]"),
            context=request.context,  # type: ignore[attr-defined]
        )

    core._composed.chain_host._authed_post_chain_terminal = fake_terminal  # type: ignore[method-assign]
    core._composed.chain_host._authed_post_chain = build_chain(
        core._composed.middlewares, fake_terminal
    )

    with pytest.raises(DecodingError):
        await core._rpc_executor.rpc_call(RPCMethod.GET_NOTEBOOK, ["nb_123"])

    snapshot = core._collaborators.metrics.snapshot()
    assert snapshot.rpc_decode_errors == 1
    # The transport leg succeeded (200 OK), so the generic transport-failure
    # counter stays 0 — the drift is counted ONLY under the dedicated signal.
    assert snapshot.rpc_calls_failed == 0
    assert snapshot.rpc_calls_started == 1


@pytest.mark.asyncio
async def test_drain_rejects_new_work_and_waits_for_in_flight() -> None:
    tracker = TransportDrainTracker()
    started = asyncio.Event()
    release = asyncio.Event()

    async def in_flight() -> None:
        operation_token = await tracker.begin_transport_post("test")
        started.set()
        try:
            await release.wait()
        finally:
            await tracker.finish_transport_post(operation_token)

    task = asyncio.create_task(in_flight())
    await started.wait()

    drain_task = asyncio.create_task(tracker.drain(timeout=1.0))
    await asyncio.sleep(0)

    assert not drain_task.done()
    with pytest.raises(RuntimeError, match="draining"):
        await tracker.begin_transport_post("new")

    release.set()
    await drain_task
    await task


@pytest.mark.asyncio
async def test_drain_allows_nested_work_inside_accepted_operation() -> None:
    tracker = TransportDrainTracker()
    outer_token = await tracker.begin_transport_post("source upload")
    try:
        drain_task = asyncio.create_task(tracker.drain(timeout=1.0))
        await asyncio.sleep(0)

        nested_token = await tracker.begin_transport_post("RPC ADD_SOURCE")
        await tracker.finish_transport_post(nested_token)

        assert not drain_task.done()
    finally:
        await tracker.finish_transport_post(outer_token)

    await drain_task


@pytest.mark.asyncio
async def test_operation_scope_tracks_drain_without_upload_semaphore() -> None:
    tracker = TransportDrainTracker()

    async with tracker.operation_scope("plain-operation"):
        assert tracker._in_flight_posts == 1

    assert tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_drain_rejects_child_task_spawned_from_accepted_operation() -> None:
    tracker = TransportDrainTracker()
    outer_token = await tracker.begin_transport_post("source upload")
    try:
        drain_task = asyncio.create_task(tracker.drain(timeout=1.0))
        await asyncio.sleep(0)

        async def child_work() -> None:
            child_token = await tracker.begin_transport_post("child task")
            await tracker.finish_transport_post(child_token)

        with pytest.raises(RuntimeError, match="draining"):
            await asyncio.create_task(child_work())
    finally:
        await tracker.finish_transport_post(outer_token)

    await drain_task


@pytest.mark.asyncio
async def test_drain_waits_for_artifact_poll_task(auth_tokens: AuthTokens) -> None:
    core = build_client_shell_for_tests(auth_tokens)
    await core.__aenter__()
    # Artifact polling receives the same call supervisor as production.
    api = WebArtifactsAPI(
        rpc=core._rpc_executor,
        supervisor=core._collaborators.call_supervisor,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )
    first_poll_started = asyncio.Event()
    release_first_poll = asyncio.Event()
    poll_count = 0

    async def fake_poll_status(notebook_id: str, task_id: str) -> GenerationStatus:
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            first_poll_started.set()
            await release_first_poll.wait()
            return GenerationStatus(task_id=task_id, status="in_progress")
        return GenerationStatus(task_id=task_id, status="completed")

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
    await core.close(drain=False)


@pytest.mark.asyncio
async def test_close_with_drain_closes_transport_after_timeout(auth_tokens: AuthTokens) -> None:
    client = NotebookLMClient(auth_tokens)
    calls: list[str] = []

    async def close_transport(*, drain: bool, drain_timeout: float | None) -> None:
        calls.append(f"close:{drain}:{drain_timeout}")
        raise TimeoutError("deadline")

    client._collaborators.lifecycle.close = close_transport  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="deadline"):
        await client.close(drain=True, drain_timeout=0.1)

    assert calls == ["close:True:0.1"]


@pytest.mark.asyncio
async def test_close_with_invalid_drain_does_not_close_transport(auth_tokens: AuthTokens) -> None:
    client = NotebookLMClient(auth_tokens)
    calls: list[str] = []

    async def close_transport(*, drain: bool, drain_timeout: float | None) -> None:
        calls.append(f"close:{drain}:{drain_timeout}")
        raise ValueError("bad deadline")

    client._collaborators.lifecycle.close = close_transport  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="bad deadline"):
        await client.close(drain=True, drain_timeout=-1.0)

    assert calls == ["close:True:-1.0"]


@pytest.mark.asyncio
async def test_upload_progress_callback_receives_byte_counts(
    auth_tokens: AuthTokens,
    tmp_path,
) -> None:
    core = build_client_shell_for_tests(auth_tokens)
    await core.__aenter__()
    try:
        api = WebSourcesAPI(
            core,
            supervisor=core._collaborators.call_supervisor,
            uploader=core._source_uploader,
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
                "https://notebooklm.google.com/upload/_/?upload_id=session",
                test_file,
                on_progress=on_progress,
            )

        assert events == [(0, len(content)), (len(content), len(content))]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_wait_for_completion_status_change_callback(auth_tokens: AuthTokens) -> None:
    core = build_client_shell_for_tests(auth_tokens)
    await core.__aenter__()
    api = WebArtifactsAPI(
        rpc=core._rpc_executor,
        supervisor=core._collaborators.call_supervisor,
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
    await core.close()
