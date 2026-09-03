"""``AndroidSession`` contract suite against a real in-process gRPC server.

``test_session.py`` pins the session's control flow with hand-written channel
fakes. This file exercises the *wire* path instead: a real ``grpc.aio.server``
with generic handlers built from the generated protobuf types, reached through
a real insecure ``grpc.aio`` channel injected via the ``grpc_loader`` seam.
Nothing between ``AndroidSession`` and the server is mocked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import grpc
import pytest

from notebooklm._android.auth import BearerCredential
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    read_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm.exceptions import (
    AuthError,
    ClientError,
    RateLimitError,
    RPCTimeoutError,
    ServerError,
)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_PROJECT = f"/{_SERVICE}/GetProject"
CREATE_PROJECT = f"/{_SERVICE}/CreateProject"
GENERATE_STREAMED = f"/{_SERVICE}/GenerateFreeFormStreamed"
# A string that must never leak from ``context.abort`` details into public text.
ABORT_DETAILS = "server-secret-abort-details"

UnaryBehaviour = Callable[[Any, Any], Awaitable[Any]]
StreamBehaviour = Callable[[Any, Any], AsyncIterator[Any]]


class _Bearer:
    """Generation-aware fake: invalidating the live generation mints a new token."""

    def __init__(self) -> None:
        self.generation = 1
        self.invalidated: list[int] = []
        self.activations: list[int] = []

    @property
    def token(self) -> str:
        return f"fake-token-{self.generation}"

    async def activate_for_epoch(self, epoch: int) -> None:
        self.epoch = epoch
        self.activations.append(epoch)

    async def get(self, expected_epoch: int) -> BearerCredential:
        assert expected_epoch == self.epoch
        return BearerCredential(self.token, self.generation)

    def invalidate(self, generation: int) -> None:
        self.invalidated.append(generation)
        if generation == self.generation:
            self.generation += 1

    async def prepare_close(self) -> None:
        return None


def _project_response(request: Any) -> Any:
    return read_pb2.GetProjectResponse(
        project=read_pb2.Project(id=request.project_id, title="Fake project")
    )


async def _default_unary(request: Any, context: Any) -> Any:
    del context
    return _project_response(request)


def _frame(text: str, *, final: bool = False) -> Any:
    return chat_pb2.GenerateFreeFormStreamedResponse(
        answer=chat_pb2.AnswerResponse(response=text),
        is_final_response=final,
    )


async def _default_stream(request: Any, context: Any) -> AsyncIterator[Any]:
    del request, context
    yield _frame("one")
    yield _frame("two")
    yield _frame("three", final=True)


@dataclass
class _Service:
    """Records every invocation and delegates to swappable behaviours."""

    on_unary: UnaryBehaviour = _default_unary
    on_stream: StreamBehaviour = _default_stream
    unary_calls: int = 0
    stream_calls: int = 0
    unary_requests: list[Any] = field(default_factory=list)
    stream_requests: list[Any] = field(default_factory=list)
    unary_metadata: list[list[tuple[str, str]]] = field(default_factory=list)
    stream_metadata: list[list[tuple[str, str]]] = field(default_factory=list)
    # Set by stream behaviours whose pending await is cancelled by the client.
    stream_cancel_seen: asyncio.Event = field(default_factory=asyncio.Event)

    async def get_project(self, request: Any, context: Any) -> Any:
        self.unary_calls += 1
        self.unary_requests.append(request)
        self.unary_metadata.append([(k, v) for k, v in context.invocation_metadata()])
        return await self.on_unary(request, context)

    async def generate(self, request: Any, context: Any) -> AsyncIterator[Any]:
        self.stream_calls += 1
        self.stream_requests.append(request)
        self.stream_metadata.append([(k, v) for k, v in context.invocation_metadata()])
        async for item in self.on_stream(request, context):
            yield item


def _handler(service: _Service) -> Any:
    return grpc.method_handlers_generic_handler(
        _SERVICE,
        {
            "GetProject": grpc.unary_unary_rpc_method_handler(
                service.get_project,
                request_deserializer=read_pb2.GetProjectRequest.FromString,
                response_serializer=read_pb2.GetProjectResponse.SerializeToString,
            ),
            "CreateProject": grpc.unary_unary_rpc_method_handler(
                service.get_project,
                request_deserializer=read_pb2.GetProjectRequest.FromString,
                response_serializer=read_pb2.GetProjectResponse.SerializeToString,
            ),
            "GenerateFreeFormStreamed": grpc.unary_stream_rpc_method_handler(
                service.generate,
                request_deserializer=chat_pb2.GenerateFreeFormStreamedRequest.FromString,
                response_serializer=(chat_pb2.GenerateFreeFormStreamedResponse.SerializeToString),
            ),
        },
    )


@dataclass
class _Harness:
    session: AndroidSession
    supervisor: CallSupervisor
    bearer: _Bearer
    service: _Service
    channels: list[Any]
    loop: asyncio.AbstractEventLoop
    metrics: ClientMetrics

    async def close(self, epoch: int) -> None:
        await self.supervisor.begin_closing(epoch)
        await self.session.prepare_close()
        await self.session.close_resources()
        self.supervisor.mark_closed(epoch)

    async def reopen(self, epoch: int) -> None:
        self.supervisor.set_bound_loop(self.loop)
        self.supervisor.reset_after_open()
        self.supervisor.prepare_generation(epoch)
        self.supervisor.start_accepting(epoch)
        self.session.set_bound_loop(self.loop)
        self.session.reset_after_open()
        await self.session.open(self.loop, epoch)


@asynccontextmanager
async def _running_session(
    service: _Service | None = None,
    *,
    timeout: float | None = 1.0,
    max_concurrent_rpcs: int | None = 2,
    server_error_max_retries: int = 3,
    sleep: Callable[[float], Awaitable[object]] | None = None,
) -> AsyncIterator[_Harness]:
    service = service or _Service()
    server = grpc.aio.server()
    server.add_generic_rpc_handlers((_handler(service),))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    channels: list[Any] = []

    def secure_channel(_target: str, _credentials: object, *, options: Any) -> Any:
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}", options=options)
        channels.append(channel)
        return channel

    grpc_loader = SimpleNamespace(
        ssl_channel_credentials=lambda: object(),
        aio=SimpleNamespace(secure_channel=secure_channel),
    )
    metrics = ClientMetrics()
    supervisor = CallSupervisor(
        metrics=metrics,
        max_concurrent_rpcs=max_concurrent_rpcs,
    )
    bearer = _Bearer()
    session = AndroidSession(
        bearer,  # type: ignore[arg-type]
        supervisor,
        timeout=timeout,
        server_error_max_retries=server_error_max_retries,
        sleep=sleep,
        grpc_loader=lambda: grpc_loader,
    )
    harness = _Harness(
        session=session,
        supervisor=supervisor,
        bearer=bearer,
        service=service,
        channels=channels,
        loop=asyncio.get_running_loop(),
        metrics=metrics,
    )
    try:
        await harness.reopen(1)
        yield harness
    finally:
        try:
            if session.active_epoch is not None:
                await session.prepare_close()
                await session.close_resources()
        finally:
            await server.stop(0)


def _get_project_request(notebook_id: str = "notebook-1") -> Any:
    return read_pb2.GetProjectRequest(project_id=notebook_id, include_audio_overview_ids=True)


def _generate_request() -> Any:
    return chat_pb2.GenerateFreeFormStreamedRequest(project_id="notebook-1", user_query="Q?")


async def _get_project(harness: _Harness, *, replay_safe: bool = True, **kwargs: Any) -> Any:
    return await harness.session.unary(
        GET_PROJECT,
        _get_project_request(),
        replay_safe=replay_safe,
        response_type=read_pb2.GetProjectResponse,
        **kwargs,
    )


async def _mutate_project(harness: _Harness, **kwargs: Any) -> Any:
    return await harness.session.unary(
        CREATE_PROJECT,
        _get_project_request(),
        replay_safe=False,
        response_type=read_pb2.GetProjectResponse,
        **kwargs,
    )


def _generate(harness: _Harness, **kwargs: Any) -> AsyncIterator[Any]:
    return harness.session.stream(
        GENERATE_STREAMED,
        _generate_request(),
        response_type=chat_pb2.GenerateFreeFormStreamedResponse,
        **kwargs,
    )


def _abort_unary(code: grpc.StatusCode) -> UnaryBehaviour:
    async def behaviour(request: Any, context: Any) -> Any:
        del request
        await context.abort(code, ABORT_DETAILS)
        raise AssertionError("abort() must not return")  # pragma: no cover

    return behaviour


# --------------------------------------------------------------------------- #
# 1-3: method path, request bytes, metadata, and typed response
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unary_reaches_exact_method_path_with_deserializable_request() -> None:
    async with _running_session() as harness:
        result = await _get_project(harness)

    assert harness.service.unary_calls == 1
    assert harness.service.stream_calls == 0
    (received,) = harness.service.unary_requests
    assert isinstance(received, read_pb2.GetProjectRequest)
    assert received == _get_project_request()
    assert received.project_id == "notebook-1"
    assert received.include_audio_overview_ids is True
    # 3: the wire bytes deserialize client-side into the pinned response type.
    assert isinstance(result, read_pb2.GetProjectResponse)
    assert result.project.id == "notebook-1"
    assert result.project.title == "Fake project"


@pytest.mark.asyncio
async def test_unary_and_stream_send_exactly_one_bearer_header_and_no_cookie() -> None:
    async with _running_session() as harness:
        await _get_project(harness)
        frames = [frame async for frame in _generate(harness)]

    assert len(frames) == 3
    expected = ("authorization", f"Bearer {harness.bearer.token}")
    for metadata in (*harness.service.unary_metadata, *harness.service.stream_metadata):
        authorization = [entry for entry in metadata if entry[0] == "authorization"]
        assert authorization == [expected]
        assert "cookie" not in {key for key, _ in metadata}
    assert harness.bearer.token == "fake-token-1"


# --------------------------------------------------------------------------- #
# 4-6: stream shapes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_is_lazy_and_yields_frames_in_order() -> None:
    async with _running_session() as harness:
        iterator = _generate(harness)
        await asyncio.sleep(0)
        assert harness.service.stream_calls == 0
        assert harness.channels == []

        first = await iterator.__anext__()
        assert harness.service.stream_calls == 1
        rest = [frame async for frame in iterator]

    frames = [first, *rest]
    assert [frame.answer.response for frame in frames] == ["one", "two", "three"]
    assert [frame.is_final_response for frame in frames] == [False, False, True]
    assert all(isinstance(frame, chat_pb2.GenerateFreeFormStreamedResponse) for frame in frames)
    (received,) = harness.service.stream_requests
    assert received == _generate_request()
    assert harness.service.stream_calls == 1


@pytest.mark.asyncio
async def test_empty_stream_ends_cleanly_with_zero_items() -> None:
    async def empty(request: Any, context: Any) -> AsyncIterator[Any]:
        del request, context
        return
        yield  # pragma: no cover - makes this an async generator

    service = _Service(on_stream=empty)
    async with _running_session(service) as harness:
        frames = [frame async for frame in _generate(harness)]
        # The lease was released: a follow-up unary is admitted immediately.
        await _get_project(harness)

    assert frames == []
    assert service.stream_calls == 1
    assert service.unary_calls == 1


@pytest.mark.asyncio
async def test_partial_stream_then_unavailable_surfaces_first_frame_then_server_error() -> None:
    async def partial_then_abort(request: Any, context: Any) -> AsyncIterator[Any]:
        del request
        yield _frame("one")
        await context.abort(grpc.StatusCode.UNAVAILABLE, ABORT_DETAILS)

    service = _Service(on_stream=partial_then_abort)
    received: list[str] = []
    async with _running_session(service) as harness:
        with pytest.raises(ServerError) as captured:
            async for frame in _generate(harness):
                received.append(frame.answer.response)

    assert received == ["one"]
    assert captured.value.rpc_code == 14
    assert ABORT_DETAILS not in str(captured.value)
    # Streams are never replayed, even for a status the unary path retries.
    assert service.stream_calls == 1


# --------------------------------------------------------------------------- #
# 7: status mapping over the wire, details sanitized
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        (grpc.StatusCode.UNAUTHENTICATED, AuthError),
        (grpc.StatusCode.PERMISSION_DENIED, ClientError),
        (grpc.StatusCode.UNAVAILABLE, ServerError),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, RateLimitError),
        (grpc.StatusCode.INVALID_ARGUMENT, ClientError),
        (grpc.StatusCode.DEADLINE_EXCEEDED, RPCTimeoutError),
    ],
)
@pytest.mark.asyncio
async def test_unary_abort_status_maps_to_public_exception_without_details(
    code: grpc.StatusCode, error_type: type[Exception]
) -> None:
    service = _Service(on_unary=_abort_unary(code))
    async with _running_session(service) as harness:
        with pytest.raises(error_type) as captured:
            await _mutate_project(harness)

    error = captured.value
    assert type(error) is error_type
    assert ABORT_DETAILS not in str(error)
    assert ABORT_DETAILS not in repr(error)
    assert error.__cause__ is None and error.__context__ is None
    assert error.method_id == CREATE_PROJECT
    if isinstance(error, RPCTimeoutError):
        assert error.timeout_seconds == 1.0
    else:
        assert error.rpc_code == code.value[0]
    assert service.unary_calls == 1


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        (grpc.StatusCode.UNAUTHENTICATED, AuthError),
        (grpc.StatusCode.PERMISSION_DENIED, ClientError),
        (grpc.StatusCode.UNAVAILABLE, ServerError),
        (grpc.StatusCode.RESOURCE_EXHAUSTED, RateLimitError),
        (grpc.StatusCode.INVALID_ARGUMENT, ClientError),
        (grpc.StatusCode.DEADLINE_EXCEEDED, RPCTimeoutError),
    ],
)
@pytest.mark.asyncio
async def test_stream_abort_status_maps_to_public_exception_without_details(
    code: grpc.StatusCode, error_type: type[Exception]
) -> None:
    async def abort_immediately(request: Any, context: Any) -> AsyncIterator[Any]:
        del request
        await context.abort(code, ABORT_DETAILS)
        yield  # pragma: no cover - abort() never returns

    service = _Service(on_stream=abort_immediately)
    async with _running_session(service) as harness:
        with pytest.raises(error_type) as captured:
            async for _ in _generate(harness):
                raise AssertionError("no frame expected")

    error = captured.value
    assert type(error) is error_type
    assert ABORT_DETAILS not in str(error)
    assert error.method_id == GENERATE_STREAMED
    assert service.stream_calls == 1


# --------------------------------------------------------------------------- #
# 8: replay policy on the wire
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_replay_safe_read_refreshes_bearer_and_retries_unauthenticated_once() -> None:
    async def first_call_unauthenticated(request: Any, context: Any) -> Any:
        if service.unary_calls == 1:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, ABORT_DETAILS)
        return _project_response(request)

    service = _Service(on_unary=first_call_unauthenticated)
    async with _running_session(service) as harness:
        result = await _get_project(harness, replay_safe=True)

    assert result.project.id == "notebook-1"
    assert service.unary_calls == 2
    assert harness.bearer.invalidated == [1]
    # Each attempt must carry exactly one authorization entry: a retry that
    # appended a second header instead of replacing it would collapse in dict().
    for metadata in service.unary_metadata:
        assert [key for key, _value in metadata].count("authorization") == 1
    tokens = [dict(metadata)["authorization"] for metadata in service.unary_metadata]
    # The retry carried the refreshed credential, not the rejected one.
    assert tokens == ["Bearer fake-token-1", "Bearer fake-token-2"]


@pytest.mark.asyncio
async def test_replay_safe_read_retries_unauthenticated_exactly_once_before_auth_error() -> None:
    service = _Service(on_unary=_abort_unary(grpc.StatusCode.UNAUTHENTICATED))
    async with _running_session(service) as harness:
        with pytest.raises(AuthError):
            await _get_project(harness, replay_safe=True)

    assert service.unary_calls == 2
    # Each rejected generation is invalidated once; no third attempt follows.
    assert harness.bearer.invalidated == [1, 2]


@pytest.mark.asyncio
async def test_mutation_never_retries_unauthenticated() -> None:
    service = _Service(on_unary=_abort_unary(grpc.StatusCode.UNAUTHENTICATED))
    async with _running_session(service) as harness:
        with pytest.raises(AuthError):
            await _mutate_project(harness)

    assert service.unary_calls == 1
    assert harness.bearer.invalidated == [1]


@pytest.mark.asyncio
async def test_replay_safe_read_retries_unavailable_once() -> None:
    service = _Service(on_unary=_abort_unary(grpc.StatusCode.UNAVAILABLE))

    async def sleep(_seconds: float) -> None:
        return None

    async with _running_session(
        service,
        timeout=5.0,
        server_error_max_retries=1,
        sleep=sleep,
    ) as harness:
        with pytest.raises(ServerError):
            await _get_project(harness, replay_safe=True)

    assert service.unary_calls == 2
    assert harness.bearer.invalidated == []


# --------------------------------------------------------------------------- #
# 9: client deadline
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_client_deadline_maps_slow_unary_to_timeout_error() -> None:
    async def slow(request: Any, context: Any) -> Any:
        del context
        await asyncio.sleep(5.0)
        return _project_response(request)  # pragma: no cover - cancelled first

    service = _Service(on_unary=slow)
    async with _running_session(service) as harness:
        with pytest.raises(RPCTimeoutError) as captured:
            await _get_project(harness, timeout=1.0)

    assert captured.value.timeout_seconds == 1.0
    assert captured.value.method_id == GET_PROJECT
    assert service.unary_calls == 1


@pytest.mark.asyncio
async def test_client_deadline_maps_stalled_stream_to_timeout_error() -> None:
    async def stalls_after_one(request: Any, context: Any) -> AsyncIterator[Any]:
        del request, context
        yield _frame("one")
        await asyncio.sleep(5.0)

    service = _Service(on_stream=stalls_after_one)
    received: list[str] = []
    async with _running_session(service) as harness:
        with pytest.raises(RPCTimeoutError) as captured:
            async for frame in _generate(harness, timeout=1.0):
                received.append(frame.answer.response)

    assert received == ["one"]
    assert captured.value.timeout_seconds == 1.0
    assert service.stream_calls == 1


# --------------------------------------------------------------------------- #
# 10: aclose() cancels the wire call and releases the lease
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_aclose_cancels_wire_call_and_releases_call_scope() -> None:
    async def hold_after_one(request: Any, context: Any) -> AsyncIterator[Any]:
        del request
        yield _frame("one")
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            # grpc.aio delivers a client cancel to the handler as task
            # cancellation; ``context.cancelled()`` stays False on the server
            # side even for a raw ``call.cancel()``, so the CancelledError is
            # the observable proof that the wire call was torn down.
            service.stream_cancel_seen.set()
            raise
        raise AssertionError("client cancellation never reached the handler")  # pragma: no cover

    service = _Service(on_stream=hold_after_one)
    # One RPC slot: the follow-up unary is admitted only if the stream's
    # supervisor lease really was released by ``aclose()``.
    async with _running_session(service, max_concurrent_rpcs=1) as harness:
        iterator = _generate(harness)
        first = await iterator.__anext__()
        assert first.answer.response == "one"
        await iterator.aclose()

        await asyncio.wait_for(service.stream_cancel_seen.wait(), timeout=2.0)

        await asyncio.wait_for(_get_project(harness), timeout=2.0)

    assert service.stream_calls == 1
    assert service.unary_calls == 1
    snapshot = harness.metrics.snapshot()
    assert snapshot.rpc_calls_started == 2


# --------------------------------------------------------------------------- #
# 11: session close shuts the channel; reopen with a fresh epoch works
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_close_shuts_channel_and_reopen_with_fresh_epoch_reconnects() -> None:
    async with _running_session() as harness:
        await _get_project(harness)
        (first_channel,) = harness.channels
        assert first_channel.get_state() is not grpc.ChannelConnectivity.SHUTDOWN

        await harness.close(1)

        assert first_channel.get_state() is grpc.ChannelConnectivity.SHUTDOWN
        assert harness.session.active_epoch is None
        with pytest.raises(RuntimeError, match="Client not initialized"):
            await _get_project(harness)
        assert harness.service.unary_calls == 1

        await harness.reopen(2)
        result = await _get_project(harness)

        assert result.project.id == "notebook-1"
        assert harness.session.active_epoch == 2
        assert harness.service.unary_calls == 2
        assert len(harness.channels) == 2
        assert harness.channels[1] is not first_channel
        assert harness.bearer.activations == [1, 2]
