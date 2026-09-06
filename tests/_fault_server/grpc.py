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
_METHOD_KINDS = {
    GET_PROJECT: frozenset({"reply", "abort", "wait_reply", "wait_abort"}),
    CREATE_PROJECT: frozenset({"reply", "abort", "wait_reply", "wait_abort", "commit_abort"}),
    LIST_CHAT_SESSIONS: frozenset({"reply", "abort", "wait_reply", "wait_abort"}),
    GENERATE_STREAMED: frozenset({"stream"}),
}
_CLEANUP_TIMEOUT = 1.0


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


def wait_abort(gate: str, status: grpc.StatusCode) -> GrpcAction:
    return GrpcAction("wait_abort", status=status, gate=gate)


def commit_abort(status: grpc.StatusCode) -> GrpcAction:
    return GrpcAction("commit_abort", status=status)


def stream(
    frames: Iterable[Any], *, after: GrpcAction | None = None, gate: str | None = None
) -> GrpcAction:
    return GrpcAction(
        "stream",
        response=tuple(frames),
        gate=gate or (after.gate if after else None),
        status=(after.status if after else None),
    )


@dataclass
class GrpcRequest:
    sequence: int
    method: str
    request: Any
    metadata: tuple[tuple[str, str], ...]
    cancelled: bool = False
    _cancelled_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass
class GrpcFaultServer(AbstractAsyncContextManager["GrpcFaultServer"]):
    """An ephemeral loopback service with journals, gates and action queues."""

    actions: dict[str, deque[GrpcAction]] = field(default_factory=lambda: defaultdict(deque))
    requests: list[GrpcRequest] = field(default_factory=list)
    state: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    cancellations: list[GrpcRequest] = field(default_factory=list)
    handler_errors: list[str] = field(default_factory=list)
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
        if method not in _METHOD_KINDS:
            raise ValueError(f"unsupported Android gRPC method: {method}")
        if not actions:
            raise ValueError(f"at least one action is required for {method}")
        if self.actions[method]:
            raise ValueError(f"method already planned: {method}")
        for action in actions:
            self._validate_action(method, action)
        self.actions[method].extend(actions)

    @staticmethod
    def _validate_action(method: str, action: GrpcAction) -> None:
        if action.kind not in _METHOD_KINDS[method]:
            raise ValueError(f"action {action.kind!r} is invalid for {method}")
        needs_status = action.kind in {"abort", "wait_abort", "commit_abort"}
        if action.kind != "stream" and needs_status != (action.status is not None):
            raise ValueError(f"action {action.kind!r} has invalid status for {method}")
        if action.status is not None and (
            not isinstance(action.status, grpc.StatusCode) or action.status is grpc.StatusCode.OK
        ):
            raise ValueError(f"action {action.kind!r} requires a non-OK gRPC status")
        needs_gate = action.kind in {"wait_reply", "wait_abort"}
        if action.kind != "stream" and needs_gate != (action.gate is not None):
            raise ValueError(f"action {action.kind!r} has invalid gate for {method}")
        if action.gate is not None and (not isinstance(action.gate, str) or not action.gate):
            raise ValueError(f"action {action.kind!r} requires a non-empty gate")
        if action.kind in {"abort", "wait_abort", "commit_abort"} and action.response is not None:
            raise ValueError(f"action {action.kind!r} cannot include a response")
        if action.kind == "stream" and not isinstance(action.response, tuple):
            raise ValueError("stream action frames must be an immutable tuple")

    def assert_consumed(self) -> None:
        if self.handler_errors:
            raise AssertionError(f"gRPC handler errors: {self.handler_errors}")
        pending = {method: len(items) for method, items in self.actions.items() if items}
        if pending:
            raise AssertionError(f"unconsumed gRPC actions: {pending}")
        if self._active:
            raise AssertionError(f"active gRPC handlers leaked: {len(self._active)}")

    async def wait_for_idle(self, *, timeout: float = 1.0) -> None:
        async def wait() -> None:
            while self._active:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait(), timeout=timeout)

    async def __aenter__(self) -> GrpcFaultServer:
        self._server = grpc.aio.server()
        try:
            self._server.add_generic_rpc_handlers((self._handler(), self._unknown_handler()))
            port = self._server.add_insecure_port("127.0.0.1:0")
            if not port:
                raise RuntimeError("gRPC fault server failed to bind a loopback port")
            self._port = port
            await self._server.start()
        except BaseException:
            await self._cleanup_after_failed_enter()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._shutdown()

    async def _cleanup_after_failed_enter(self) -> None:
        cleanup = asyncio.create_task(self._shutdown(), name="grpc-fault-server-enter-cleanup")
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A second cancellation may detach this caller, but the strongly held
            # task still completes the bounded cleanup before its own deadline.
            await asyncio.gather(cleanup, return_exceptions=True)
        except BaseException as error:
            self.handler_errors.append(f"enter cleanup failed: {type(error).__name__}: {error}")

    async def _shutdown(self) -> None:
        server = self._server
        if server is None:
            return
        errors: list[BaseException] = []
        try:
            await asyncio.wait_for(server.stop(0), timeout=_CLEANUP_TIMEOUT)
        except BaseException as error:
            errors.append(error)
        if self._active:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tuple(self._active), return_exceptions=True),
                    timeout=_CLEANUP_TIMEOUT,
                )
            except BaseException as error:
                errors.append(error)
        try:
            await asyncio.wait_for(server.wait_for_termination(), timeout=_CLEANUP_TIMEOUT)
        except BaseException as error:
            errors.append(error)
        if errors:
            raise errors[0]

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

    def _unknown_handler(self) -> grpc.GenericRpcHandler:
        server = self

        class UnknownHandler(grpc.GenericRpcHandler):
            def service(
                self, handler_call_details: grpc.HandlerCallDetails
            ) -> grpc.RpcMethodHandler:
                method = handler_call_details.method
                server.handler_errors.append(f"unexpected request: {method}")

                async def reject(_request: bytes, context: grpc.aio.ServicerContext) -> None:
                    await context.abort(grpc.StatusCode.UNIMPLEMENTED, "unexpected test RPC")

                return grpc.unary_unary_rpc_method_handler(reject)

        return UnknownHandler()

    def _next(self, method: str) -> GrpcAction:
        try:
            return self.actions[method].popleft()
        except IndexError as error:
            self.handler_errors.append(f"unexpected request: {method}")
            raise AssertionError(f"unexpected gRPC request: {method}") from error

    def _record(self, method: str, request: Any, context: grpc.aio.ServicerContext) -> GrpcRequest:
        recorded = GrpcRequest(
            len(self.requests) + 1,
            method,
            request,
            tuple((str(k), str(v)) for k, v in context.invocation_metadata()),
        )
        self.requests.append(recorded)
        return recorded

    async def wait_for_cancellation(self, request: GrpcRequest, *, timeout: float = 1.0) -> None:
        if not any(item is request for item in self.requests):
            raise ValueError("request does not belong to this gRPC fault server")
        await asyncio.wait_for(request._cancelled_event.wait(), timeout=timeout)

    def _mark_cancelled(self, recorded: GrpcRequest) -> None:
        if recorded.cancelled:
            return
        recorded.cancelled = True
        recorded._cancelled_event.set()
        self.cancellations.append(recorded)

    def _record_handler_error(self, method: str, error: BaseException) -> None:
        self.handler_errors.append(f"{method}: {type(error).__name__}: {error}")

    @staticmethod
    def _project(project_id: str = "notebook-1", title: str = "Fault notebook") -> Any:
        return read_pb2.Project(id=project_id, title=title)

    async def _apply_unary(
        self, method: str, request: Any, context: grpc.aio.ServicerContext
    ) -> Any:
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        recorded: GrpcRequest | None = None
        try:
            recorded = self._record(method, request, context)
            action = self._next(method)
            if action.kind == "wait_reply":
                assert action.gate is not None
                await self.gate(action.gate).wait()
            if action.kind == "wait_abort":
                assert action.gate is not None and action.status is not None
                await self.gate(action.gate).wait()
                await context.abort(action.status, "synthetic gated fault")
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
            if recorded is not None:
                self._mark_cancelled(recorded)
            raise
        except grpc.aio.AbortError:
            raise
        except BaseException as error:
            self._record_handler_error(method, error)
            raise
        finally:
            if task is not None:
                self._active.discard(task)

    async def _get_project(self, request: Any, context: grpc.aio.ServicerContext) -> Any:
        return await self._apply_unary(GET_PROJECT, request, context)

    async def _create_project(self, request: Any, context: grpc.aio.ServicerContext) -> Any:
        return await self._apply_unary(CREATE_PROJECT, request, context)

    async def _list_chat_sessions(self, request: Any, context: grpc.aio.ServicerContext) -> Any:
        return await self._apply_unary(LIST_CHAT_SESSIONS, request, context)

    async def _generate_streamed(
        self, request: Any, context: grpc.aio.ServicerContext
    ) -> AsyncIterator[Any]:
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        recorded: GrpcRequest | None = None
        try:
            recorded = self._record(GENERATE_STREAMED, request, context)
            action = self._next(GENERATE_STREAMED)
            for frame in action.response or ():
                yield frame
            if action.gate is not None:
                await self.gate(action.gate).wait()
            if action.status is not None:
                await context.abort(action.status, "synthetic stream fault")
        except asyncio.CancelledError:
            if recorded is not None:
                self._mark_cancelled(recorded)
            raise
        except grpc.aio.AbortError:
            raise
        except BaseException as error:
            self._record_handler_error(GENERATE_STREAMED, error)
            raise
        finally:
            if task is not None:
                self._active.discard(task)


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
    "wait_abort",
    "wait_reply",
]
