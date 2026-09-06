"""Small real-socket gRPC fault server for Android transport scenarios.

The server intentionally supports only the handful of public Android flows
used by the fault suite.  A missing scripted action is a test error, not a
fallback to a plausible response.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import grpc

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    notebooks_pb2,
    read_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import (
    notebooks_pb2 as wire_notebooks_pb2,
)

SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_PROJECT = f"/{SERVICE}/GetProject"
CREATE_PROJECT = f"/{SERVICE}/CreateProject"
LIST_CHAT_SESSIONS = f"/{SERVICE}/ListChatSessions"
GENERATE_STREAMED = f"/{SERVICE}/GenerateFreeFormStreamed"


@dataclass(frozen=True)
class GrpcAction:
    """One deterministic handler result."""

    kind: str
    status: grpc.StatusCode | None = None
    response: Any | None = None
    gate: str | None = None


def reply(response: Any | None = None) -> GrpcAction:
    return GrpcAction("reply", response=response)


def abort(status: grpc.StatusCode) -> GrpcAction:
    return GrpcAction("abort", status=status)


def wait_reply(gate: str, response: Any | None = None) -> GrpcAction:
    return GrpcAction("wait_reply", response=response, gate=gate)


def commit_abort(status: grpc.StatusCode) -> GrpcAction:
    return GrpcAction("commit_abort", status=status)


def stream(
    frames: Iterable[Any], *, after: GrpcAction | None = None, gate: str | None = None
) -> GrpcAction:
    return GrpcAction("stream", response=tuple(frames), gate=gate or (after.gate if after else None), status=(after.status if after else None))


@dataclass
class GrpcRequest:
    method: str
    request: Any
    metadata: tuple[tuple[str, str], ...]


@dataclass
class GrpcFaultServer(AbstractAsyncContextManager["GrpcFaultServer"]):
    """An ephemeral loopback service with journals, gates and action queues."""

    actions: dict[str, deque[GrpcAction]] = field(default_factory=lambda: defaultdict(deque))
    requests: list[GrpcRequest] = field(default_factory=list)
    state: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    cancellations: set[str] = field(default_factory=set)
    _gates: dict[str, asyncio.Event] = field(default_factory=dict)
    _server: grpc.aio.Server | None = None
    _port: int | None = None
    _active: set[asyncio.Task[Any]] = field(default_factory=set)

    @property
    def target(self) -> str:
        assert self._port is not None
        return f"127.0.0.1:{self._port}"

    def gate(self, name: str) -> asyncio.Event:
        return self._gates.setdefault(name, asyncio.Event())

    def plan(self, method: str, *actions: GrpcAction) -> None:
        if method not in {GET_PROJECT, CREATE_PROJECT, LIST_CHAT_SESSIONS, GENERATE_STREAMED}:
            raise ValueError(f"unsupported Android gRPC method: {method}")
        if self.actions[method]:
            raise ValueError(f"method already planned: {method}")
        self.actions[method].extend(actions)

    def assert_consumed(self) -> None:
        pending = {method: len(items) for method, items in self.actions.items() if items}
        if pending:
            raise AssertionError(f"unconsumed gRPC actions: {pending}")
        if self._active:
            raise AssertionError(f"active gRPC handlers leaked: {len(self._active)}")

    async def __aenter__(self) -> "GrpcFaultServer":
        self._server = grpc.aio.server()
        self._server.add_generic_rpc_handlers((self._handler(),))
        self._port = self._server.add_insecure_port("127.0.0.1:0")
        await self._server.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        try:
            if self._active:
                await asyncio.gather(*tuple(self._active), return_exceptions=True)
        finally:
            if self._server is not None:
                await self._server.stop(0)
                await self._server.wait_for_termination()

    def _handler(self) -> grpc.GenericRpcHandler:
        return grpc.method_handlers_generic_handler(
            SERVICE,
            {
                "GetProject": grpc.unary_unary_rpc_method_handler(
                    self._get_project,
                    request_deserializer=read_pb2.GetProjectRequest.FromString,
                    response_serializer=wire_notebooks_pb2.WireGetProjectResponse.SerializeToString,
                ),
                "CreateProject": grpc.unary_unary_rpc_method_handler(
                    self._create_project,
                    request_deserializer=notebooks_pb2.CreateProjectRequest.FromString,
                    response_serializer=read_pb2.Project.SerializeToString,
                ),
                "ListChatSessions": grpc.unary_unary_rpc_method_handler(
                    self._list_chat_sessions,
                    request_deserializer=chat_pb2.ListChatSessionsRequest.FromString,
                    response_serializer=chat_pb2.ListChatSessionsResponse.SerializeToString,
                ),
                "GenerateFreeFormStreamed": grpc.unary_stream_rpc_method_handler(
                    self._generate_streamed,
                    request_deserializer=chat_pb2.GenerateFreeFormStreamedRequest.FromString,
                    response_serializer=chat_pb2.GenerateFreeFormStreamedResponse.SerializeToString,
                ),
            },
        )

    def _next(self, method: str) -> GrpcAction:
        try:
            return self.actions[method].popleft()
        except IndexError as error:
            raise AssertionError(f"unexpected gRPC request: {method}") from error

    def _record(self, method: str, request: Any, context: grpc.aio.ServicerContext) -> None:
        self.requests.append(
            GrpcRequest(method, request, tuple((str(k), str(v)) for k, v in context.invocation_metadata()))
        )

    @staticmethod
    def _project(project_id: str = "notebook-1", title: str = "Fault notebook") -> Any:
        return read_pb2.Project(id=project_id, title=title)

    async def _apply_unary(self, method: str, request: Any, context: grpc.aio.ServicerContext) -> Any:
        self._record(method, request, context)
        action = self._next(method)
        try:
            if action.kind == "wait_reply":
                assert action.gate is not None
                await self.gate(action.gate).wait()
            if action.kind == "commit_abort":
                self.state[method].append(str(getattr(request, "name", "committed")))
                assert action.status is not None
                await context.abort(action.status, "synthetic lost response")
            if action.kind == "abort":
                assert action.status is not None
                await context.abort(action.status, "synthetic fault")
            if action.response is not None:
                return action.response
            if method == GET_PROJECT:
                return wire_notebooks_pb2.WireGetProjectResponse(
                    project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(
                        id=request.project_id, title="Fault notebook"
                    )
                )
            if method == CREATE_PROJECT:
                return self._project("created-1", request.name)
            return chat_pb2.ListChatSessionsResponse()
        except asyncio.CancelledError:
            self.cancellations.add(method)
            raise

    async def _get_project(self, request: Any, context: grpc.aio.ServicerContext) -> Any:
        return await self._apply_unary(GET_PROJECT, request, context)

    async def _create_project(self, request: Any, context: grpc.aio.ServicerContext) -> Any:
        return await self._apply_unary(CREATE_PROJECT, request, context)

    async def _list_chat_sessions(self, request: Any, context: grpc.aio.ServicerContext) -> Any:
        return await self._apply_unary(LIST_CHAT_SESSIONS, request, context)

    async def _generate_streamed(
        self, request: Any, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[Any]:
        self._record(GENERATE_STREAMED, request, context)
        action = self._next(GENERATE_STREAMED)
        try:
            for frame in action.response or ():
                yield frame
            if action.gate is not None:
                await self.gate(action.gate).wait()
            if action.status is not None:
                await context.abort(action.status, "synthetic stream fault")
        except asyncio.CancelledError:
            self.cancellations.add(GENERATE_STREAMED)
            raise


__all__ = [
    "CREATE_PROJECT",
    "GENERATE_STREAMED",
    "GET_PROJECT",
    "LIST_CHAT_SESSIONS",
    "GrpcAction",
    "GrpcFaultServer",
    "GrpcRequest",
    "abort",
    "commit_abort",
    "reply",
    "stream",
    "wait_reply",
]
