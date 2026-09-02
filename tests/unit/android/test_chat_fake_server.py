"""chat orchestration through a real in-process gRPC server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import grpc
import pytest

from notebooklm._android.auth import BearerCredential
from notebooklm._android.chat import AndroidChatAPI
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    sources_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import common_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm.exceptions import AuthError

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"


class _Bearer:
    def __init__(self) -> None:
        self.invalidated: list[int] = []

    async def activate(self, epoch: int) -> None:
        self.epoch = epoch

    async def get(self, expected_epoch: int) -> BearerCredential:
        assert expected_epoch == self.epoch
        return BearerCredential("fake-server-token", 1)

    def invalidate(self, generation: int) -> None:
        self.invalidated.append(generation)

    async def prepare_close(self) -> None:
        return None


class _Notebooks:
    async def get_source_ids(self, notebook_id: str) -> list[str]:
        assert notebook_id == "notebook-1"
        return ["source-1"]


class _ChatService:
    def __init__(
        self,
        *,
        fail_auth_after_partial: bool = False,
        final_answer: str = "Cumulative final",
        session_id: str = "conversation-1",
    ) -> None:
        self.asked = False
        self.fail_auth_after_partial = fail_auth_after_partial
        self.final_answer = final_answer
        self.session_id = session_id
        self.list_requests: list[Any] = []
        self.generate_requests: list[Any] = []

    async def list_sessions(self, request: Any, context: Any) -> Any:
        del context
        self.list_requests.append(request)
        if not self.asked:
            return chat_pb2.ListChatSessionsResponse()
        return chat_pb2.ListChatSessionsResponse(
            sessions=[common_pb2.ChatSession(chat_session_id=self.session_id)]
        )

    async def generate(self, request: Any, context: Any):
        self.generate_requests.append(request)
        yield chat_pb2.GenerateFreeFormStreamedResponse(
            answer=chat_pb2.AnswerResponse(response="Cumulative partial"),
            is_final_response=False,
        )
        if self.fail_auth_after_partial:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "expired test credential")
            return
        self.asked = True
        yield chat_pb2.GenerateFreeFormStreamedResponse(
            answer=chat_pb2.AnswerResponse(response=self.final_answer),
            is_final_response=True,
        )


def _handler(service: _ChatService) -> Any:
    return grpc.method_handlers_generic_handler(
        _SERVICE,
        {
            "ListChatSessions": grpc.unary_unary_rpc_method_handler(
                service.list_sessions,
                request_deserializer=chat_pb2.ListChatSessionsRequest.FromString,
                response_serializer=chat_pb2.ListChatSessionsResponse.SerializeToString,
            ),
            "GenerateFreeFormStreamed": grpc.unary_stream_rpc_method_handler(
                service.generate,
                request_deserializer=chat_pb2.GenerateFreeFormStreamedRequest.FromString,
                response_serializer=(chat_pb2.GenerateFreeFormStreamedResponse.SerializeToString),
            ),
        },
    )


@asynccontextmanager
async def _running_api(
    service: _ChatService,
) -> AsyncIterator[tuple[AndroidChatAPI, CallSupervisor, _Bearer]]:
    server = grpc.aio.server()
    server.add_generic_rpc_handlers((_handler(service),))
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    channel: Any | None = None

    def secure_channel(_target: str, _credentials: object, *, options: Any) -> Any:
        nonlocal channel
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}", options=options)
        return channel

    grpc_loader = SimpleNamespace(
        ssl_channel_credentials=lambda: object(),
        aio=SimpleNamespace(secure_channel=secure_channel),
    )
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=2,
    )
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    bearer = _Bearer()
    session = AndroidSession(
        bearer,  # type: ignore[arg-type]
        supervisor,
        timeout=1.0,
        grpc_loader=lambda: grpc_loader,
    )
    session.set_bound_loop(loop)
    session.reset_after_open()
    await session.open(loop, 1)
    api = AndroidChatAPI(
        session=session,
        loop_guard=supervisor,
        notebooks=_Notebooks(),
        chat_timeout=1.0,
        turn_id_factory=lambda: "00000000-0000-4000-8000-000000000123",
    )

    try:
        yield api, supervisor, bearer
    finally:
        await session.prepare_close()
        await session.close_resources()
        await server.stop(0)


@pytest.mark.asyncio
async def test_base_ask_over_real_android_session_and_fake_grpc_server() -> None:
    service = _ChatService()
    async with _running_api(service) as (api, supervisor, _bearer):
        result = await api.ask("notebook-1", "Question?")

    assert result.answer == "Cumulative final"
    assert result.conversation_id == "conversation-1"
    assert result.turn_number == 1
    assert len(service.list_requests) == 2
    assert service.list_requests == [
        chat_pb2.ListChatSessionsRequest(project_id="notebook-1"),
        chat_pb2.ListChatSessionsRequest(project_id="notebook-1"),
    ]
    assert len(service.generate_requests) == 1
    (request,) = service.generate_requests
    assert service.generate_requests == [
        chat_pb2.GenerateFreeFormStreamedRequest(
            sources=[sources_pb2.InputSource(source_id={"id": "source-1"})],
            user_query="Question?",
            request_context=request.request_context,
            user_message_id="00000000-0000-4000-8000-000000000123",
            project_id="notebook-1",
            origin=chat_pb2.QUERY_ORIGIN_CHAT_TEXT_BOX,
        )
    ]
    assert request.request_context.client_type == 2
    assert request.request_context.provenance.client_info.device == 1
    # Both session lookups and the streamed ask emit public RPC telemetry;
    # the stream uses the stable backend-neutral ``chat.ask`` label.
    snapshot = supervisor._metrics.snapshot()
    assert snapshot.rpc_calls_started == 3
    assert snapshot.rpc_calls_succeeded == 3


@pytest.mark.asyncio
async def test_android_session_accepts_stream_and_unary_larger_than_grpcio_default() -> None:
    answer = "x" * (5 * 1024 * 1024)
    session_id = "c" * (5 * 1024 * 1024)
    service = _ChatService(final_answer=answer, session_id=session_id)

    async with _running_api(service) as (api, _supervisor, _bearer):
        result = await api.ask("notebook-1", "Question?")

    assert result.answer == answer
    assert result.conversation_id == session_id


@pytest.mark.asyncio
async def test_android_stream_does_not_retry_after_midstream_auth_failure() -> None:
    service = _ChatService(fail_auth_after_partial=True)
    async with _running_api(service) as (api, _supervisor, bearer):
        with pytest.raises(AuthError):
            await api._stream_answer(
                notebook_id="notebook-1",
                question="Question?",
                source_ids=["source-1"],
                cached_turns=[],
                conversation_id=None,
            )

    assert len(service.generate_requests) == 1
    assert bearer.invalidated == [1]
