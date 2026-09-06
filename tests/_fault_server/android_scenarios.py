"""Deterministic public Android resilience scenarios over local gRPC sockets."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import grpc

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import chat_pb2
from notebooklm._android.proto.labs.language.tailwind.common.protos import common_pb2
from notebooklm.exceptions import AuthError, RateLimitError, RPCTimeoutError, ServerError
from notebooklm.outcomes import CommitState
from notebooklm.raw import GrpcUnaryStreamMethod

from .android import SyntheticOAuthMinter, build_android_client
from .common import ScenarioResult
from .grpc import (
    CREATE_PROJECT,
    GENERATE_STREAMED,
    GET_PROJECT,
    LIST_CHAT_SESSIONS,
    GrpcFaultServer,
    abort,
    commit_abort,
    reply,
    stream,
    wait_abort,
    wait_reply,
)

SCENARIOS = (
    "auth",
    "auth_concurrent",
    "commit_lost_response",
    "deadline_and_cancellation",
    "minter_failure",
    "public_flows",
    "rate_limit",
    "stream_auth",
    "unavailable",
)

_FAULTS = {
    "auth": ("GetProject:UNAUTHENTICATED->reply", "GetProject:UNAUTHENTICATED->UNAUTHENTICATED"),
    "auth_concurrent": (
        "GetProject x2:gate(both-old)->UNAUTHENTICATED",
        "GetProject x2:fresh reply",
    ),
    "commit_lost_response": ("CreateProject:commit->UNAVAILABLE",),
    "deadline_and_cancellation": (
        "GetProject:gate(deadline)",
        "GenerateFreeFormStreamed:gate(deadline)",
        "active unary/stream cancellation and close",
    ),
    "minter_failure": ("bearer minter failure before dispatch",),
    "public_flows": ("GetProject:reply", "CreateProject:reply", "chat stream:final reply"),
    "rate_limit": (
        "GetProject:RESOURCE_EXHAUSTED->reply",
        "GetProject:RESOURCE_EXHAUSTED exhaustion",
    ),
    "stream_auth": (
        "chat stream:partial->UNAUTHENTICATED",
        "raw stream:observe partial->UNAUTHENTICATED",
        "later GetProject:fresh bearer",
    ),
    "unavailable": ("GetProject:UNAVAILABLE->reply", "GetProject:UNAVAILABLE exhaustion"),
}


def _result(name: str, operation_id: str, supplied: ScenarioResult | None) -> ScenarioResult:
    if name not in SCENARIOS:
        raise ValueError(f"unknown Android fault scenario: {name}")
    result = supplied or ScenarioResult("android", name, operation_id)
    if (result.backend, result.scenario, result.operation_id) != ("android", name, operation_id):
        raise ValueError("supplied scenario result identity does not match Android scenario")
    result.record(
        "plan",
        faults=_FAULTS[name],
        cohort_ids=[operation_id],
        phases=["assemble", "open", "call", "cleanup"],
    )
    return result


def _authorizations(server: GrpcFaultServer, method: str) -> list[str]:
    return [
        dict(item.metadata).get("authorization", "")
        for item in server.requests
        if item.method == method
    ]


async def _run_server(
    result: ScenarioResult,
    setup: Callable[[GrpcFaultServer], Awaitable[None]],
) -> ScenarioResult:
    server = GrpcFaultServer()
    try:
        async with server:
            await setup(server)
            await server.wait_for_idle()
            server.assert_consumed()
    finally:
        result.record(
            "grpc_journal",
            methods=[request.method.rpartition("/")[2] for request in server.requests],
            authorization_generations=[
                dict(request.metadata).get("authorization", "").rsplit("-", 1)[-1]
                for request in server.requests
            ],
        )
        result.record(
            "cleanup",
            requests=len(server.requests),
            cancellations=[request.method.rpartition("/")[2] for request in server.cancellations],
            handler_errors=server.handler_errors,
        )
        result.require(f"server_handlers_released_{len(result.events)}", not server._active)
    return result


async def _public_flows(result: ScenarioResult) -> None:
    async def setup(server: GrpcFaultServer) -> None:
        server.plan(GET_PROJECT, reply(), reply())
        server.plan(CREATE_PROJECT, reply())
        server.plan(LIST_CHAT_SESSIONS, reply(), reply(_chat_sessions("conversation-1")))
        server.plan(GENERATE_STREAMED, stream([_frame("Fault answer", final=True)]))
        harness = build_android_client(server)
        async with harness.client as client:
            notebook = await client.notebooks.get("notebook-1")
            created = await client.notebooks.create("Created by fault test")
            answer = await client.chat.ask("notebook-1", "Question?")
        result.require("get_decodes", notebook.id == "notebook-1")
        result.require("create_decodes", created.id == "created-1")
        result.require("chat_decodes", answer.answer == "Fault answer")
        result.require("production_target", harness.targets == ["notebooklm-pa.googleapis.com:443"])
        result.require(
            "initial_bearer",
            all(v == "Bearer fault-bearer-1" for v in _authorizations(server, GET_PROJECT)),
        )

    await _run_server(result, setup)


async def _unavailable(result: ScenarioResult) -> None:
    async def setup(server: GrpcFaultServer) -> None:
        server.plan(GET_PROJECT, abort(grpc.StatusCode.UNAVAILABLE), reply())
        harness = build_android_client(server, timeout=5.0, server_error_max_retries=1)
        async with harness.client as client:
            assert (await client.notebooks.get("notebook-1")).id == "notebook-1"
        result.require("unavailable_retries_once", len(_authorizations(server, GET_PROJECT)) == 2)

    await _run_server(result, setup)

    async def exhaustion(server: GrpcFaultServer) -> None:
        server.plan(
            GET_PROJECT, abort(grpc.StatusCode.UNAVAILABLE), abort(grpc.StatusCode.UNAVAILABLE)
        )
        harness = build_android_client(server, timeout=5.0, server_error_max_retries=1)
        async with harness.client as client:
            try:
                await client.notebooks.get("notebook-1")
            except ServerError:
                pass
            else:
                result.require("unavailable_exhaustion_raises", False)
        result.require(
            "unavailable_exhausts_budget", len(_authorizations(server, GET_PROJECT)) == 2
        )

    await _run_server(result, exhaustion)


async def _rate_limit(result: ScenarioResult) -> None:
    async def setup(server: GrpcFaultServer) -> None:
        server.plan(GET_PROJECT, abort(grpc.StatusCode.RESOURCE_EXHAUSTED), reply())
        harness = build_android_client(server, timeout=5.0, rate_limit_max_retries=1)
        async with harness.client as client:
            await client.notebooks.get("notebook-1")
        result.require("rate_limit_retries_once", len(_authorizations(server, GET_PROJECT)) == 2)

    await _run_server(result, setup)

    async def exhaustion(server: GrpcFaultServer) -> None:
        server.plan(
            GET_PROJECT,
            abort(grpc.StatusCode.RESOURCE_EXHAUSTED),
            abort(grpc.StatusCode.RESOURCE_EXHAUSTED),
        )
        harness = build_android_client(server, timeout=5.0, rate_limit_max_retries=1)
        async with harness.client as client:
            try:
                await client.notebooks.get("notebook-1")
            except RateLimitError:
                pass
            else:
                result.require("rate_limit_exhaustion_raises", False)
        result.require("rate_limit_exhausts_budget", len(_authorizations(server, GET_PROJECT)) == 2)

    await _run_server(result, exhaustion)


async def _auth(result: ScenarioResult) -> None:
    async def setup(server: GrpcFaultServer) -> None:
        server.plan(
            GET_PROJECT,
            abort(grpc.StatusCode.UNAUTHENTICATED),
            reply(),
            abort(grpc.StatusCode.UNAUTHENTICATED),
            abort(grpc.StatusCode.UNAUTHENTICATED),
        )
        harness = build_android_client(server)
        async with harness.client as client:
            await client.notebooks.get("notebook-1")
            try:
                await client.notebooks.get("notebook-1")
            except AuthError:
                pass
            else:
                result.require("repeated_auth_raises", False)
        auth = _authorizations(server, GET_PROJECT)
        result.require(
            "auth_replays_once", auth[:2] == ["Bearer fault-bearer-1", "Bearer fault-bearer-2"]
        )
        result.require("repeated_auth_bounded", len(auth) == 4)
        result.require("three_mints", harness.minter.calls == 3)

    await _run_server(result, setup)


async def _auth_concurrent(result: ScenarioResult) -> None:
    async def setup(server: GrpcFaultServer) -> None:
        server.plan(
            GET_PROJECT,
            wait_abort("both-old", grpc.StatusCode.UNAUTHENTICATED),
            wait_abort("both-old", grpc.StatusCode.UNAUTHENTICATED),
            reply(),
            reply(),
        )
        release = asyncio.Event()
        minter = SyntheticOAuthMinter(block_after=2, release=release)
        harness = build_android_client(server, minter=minter)
        async with harness.client as client:
            first = asyncio.create_task(client.notebooks.get("notebook-1"))
            second = asyncio.create_task(client.notebooks.get("notebook-1"))
            await _wait_for(lambda: len(_authorizations(server, GET_PROJECT)) == 2)
            server.gate("both-old").set()
            await _wait_for(lambda: harness.minter.calls == 2 and harness.bearer._mint_waiters == 2)
            release.set()
            await asyncio.gather(first, second)
        auth = _authorizations(server, GET_PROJECT)
        result.require("concurrent_initial_token", auth[:2] == ["Bearer fault-bearer-1"] * 2)
        result.require("concurrent_replay_token", auth[2:] == ["Bearer fault-bearer-2"] * 2)
        result.require("refresh_mint_coalesced", harness.minter.calls == 2)

    await _run_server(result, setup)


async def _minter_failure(result: ScenarioResult) -> None:
    async def setup(server: GrpcFaultServer) -> None:
        minter = SyntheticOAuthMinter(error=RuntimeError("synthetic mint failure"))
        harness = build_android_client(server, minter=minter)
        async with harness.client as client:
            try:
                await client.notebooks.get("notebook-1")
            except AuthError:
                pass
            else:
                result.require("mint_failure_raises", False)
        result.require("mint_failure_never_reaches_server", not server.requests)
        result.require("mint_failure_bounded", minter.calls == 1)

    await _run_server(result, setup)


async def _stream_auth(result: ScenarioResult) -> None:
    async def setup(server: GrpcFaultServer) -> None:
        server.plan(GET_PROJECT, reply(), reply())
        server.plan(LIST_CHAT_SESSIONS, reply())
        server.plan(
            GENERATE_STREAMED,
            stream([_frame("partial")], after=abort(grpc.StatusCode.UNAUTHENTICATED)),
            stream([_frame("raw partial")], after=abort(grpc.StatusCode.UNAUTHENTICATED)),
        )
        harness = build_android_client(server)
        raw_method: GrpcUnaryStreamMethod[
            chat_pb2.GenerateFreeFormStreamedRequest,
            chat_pb2.GenerateFreeFormStreamedResponse,
        ] = GrpcUnaryStreamMethod(
            path=GENERATE_STREAMED,
            response_type=chat_pb2.GenerateFreeFormStreamedResponse,
        )
        async with harness.client as client:
            try:
                await client.chat.ask("notebook-1", "Question?")
            except AuthError:
                pass
            else:
                result.require("stream_auth_raises", False)
            raw_stream: AsyncIterator[chat_pb2.GenerateFreeFormStreamedResponse] = (
                client.raw.unary_stream(
                    raw_method,
                    chat_pb2.GenerateFreeFormStreamedRequest(project_id="notebook-1"),
                    metadata=(("x-fault-call", "raw-partial"),),
                )
            )
            partial = await anext(raw_stream)
            result.require("raw_stream_delivers_partial", partial.answer.response == "raw partial")
            try:
                await anext(raw_stream)
            except AuthError:
                pass
            else:
                result.require("raw_stream_auth_raises_after_partial", False)
            await client.notebooks.get("notebook-1")
        raw_requests = [
            request
            for request in server.requests
            if request.method == GENERATE_STREAMED
            and dict(request.metadata).get("x-fault-call") == "raw-partial"
        ]
        result.require("feature_and_raw_stream_never_replay", len(raw_requests) == 1)
        result.require(
            "two_single_attempt_streams", len(_authorizations(server, GENERATE_STREAMED)) == 2
        )
        result.require(
            "next_unary_gets_fresh_bearer",
            _authorizations(server, GET_PROJECT)[-1] == "Bearer fault-bearer-2",
        )

    await _run_server(result, setup)


async def _commit_lost_response(result: ScenarioResult) -> None:
    async def setup(server: GrpcFaultServer) -> None:
        server.plan(CREATE_PROJECT, commit_abort(grpc.StatusCode.UNAVAILABLE))
        harness = build_android_client(server, server_error_max_retries=3)
        error: ServerError | None = None
        async with harness.client as client:
            try:
                await client.notebooks.create("commit once")
            except ServerError as caught:
                error = caught
            else:
                result.require("lost_response_raises", False)
        result.require("one_committed_create", server.state[CREATE_PROJECT] == ["commit once"])
        result.require("mutation_never_replayed", len(_authorizations(server, CREATE_PROJECT)) == 1)
        result.require(
            "mutation_commit_unknown",
            error is not None and error.commit_state is CommitState.UNKNOWN,
        )

    await _run_server(result, setup)


async def _deadline_and_cancellation(result: ScenarioResult) -> None:
    async def setup(server: GrpcFaultServer) -> None:
        server.plan(
            GET_PROJECT,
            wait_reply("deadline"),
            wait_reply("cancel"),
            wait_reply("close"),
            reply(),
        )
        server.plan(
            GENERATE_STREAMED,
            stream([], gate="stream-deadline"),
            stream([_frame("one")], gate="stream-cancel"),
        )
        harness = build_android_client(server, timeout=0.5)
        raw_stream: GrpcUnaryStreamMethod[
            chat_pb2.GenerateFreeFormStreamedRequest,
            chat_pb2.GenerateFreeFormStreamedResponse,
        ] = GrpcUnaryStreamMethod(
            path=GENERATE_STREAMED,
            response_type=chat_pb2.GenerateFreeFormStreamedResponse,
        )
        async with harness.client as client:
            first_supervisor = client._collaborators.call_supervisor
            first_generation = first_supervisor._current
            assert first_generation is not None
            try:
                await client.notebooks.get("notebook-1")
            except RPCTimeoutError:
                pass
            else:
                result.require("deadline_raises", False)
            deadline_request = server.requests[-1]
            await server.wait_for_cancellation(deadline_request)
            result.require("deadline_cancels_exact_unary", deadline_request.cancelled)
            blocked = asyncio.create_task(client.notebooks.get("notebook-1"))
            await _wait_for(lambda: len(_authorizations(server, GET_PROJECT)) == 2)
            cancelled_request = server.requests[-1]
            blocked.cancel()
            try:
                await blocked
            except asyncio.CancelledError:
                pass
            else:
                result.require("cancellation_propagates", False)
            await server.wait_for_cancellation(cancelled_request)
            result.require("caller_cancels_exact_unary", cancelled_request.cancelled)
            timed_stream = client.raw.unary_stream(
                raw_stream,
                chat_pb2.GenerateFreeFormStreamedRequest(project_id="notebook-1"),
                timeout=0.2,
            )
            try:
                await anext(timed_stream)
            except RPCTimeoutError:
                pass
            else:
                result.require("stream_deadline_raises", False)
            stream_deadline_request = server.requests[-1]
            await server.wait_for_cancellation(stream_deadline_request)
            result.require("deadline_cancels_exact_stream", stream_deadline_request.cancelled)
            cancelling_stream = client.raw.unary_stream(
                raw_stream,
                chat_pb2.GenerateFreeFormStreamedRequest(project_id="notebook-1"),
                timeout=0.5,
            )
            assert (await anext(cancelling_stream)).answer.response == "one"
            stream_cancel_request = server.requests[-1]
            await cancelling_stream.aclose()
            await server.wait_for_cancellation(stream_cancel_request)
            result.require("aclose_cancels_exact_stream", stream_cancel_request.cancelled)
            closing = asyncio.create_task(client.notebooks.get("notebook-1"))
            await _wait_for(lambda: len(_authorizations(server, GET_PROJECT)) == 3)
            close_request = server.requests[-1]
            await client.close(drain=False)
            try:
                await closing
            except asyncio.CancelledError:
                pass
            else:
                result.require("close_cancels_active_call", False)
            await server.wait_for_cancellation(close_request)
            result.require("close_cancels_exact_unary", close_request.cancelled)
            result.require(
                "forced_close_shuts_channel",
                harness.channels[0].get_state() is grpc.ChannelConnectivity.SHUTDOWN,
            )
            await _wait_for(lambda: _admission_settled(harness.client, first_generation))
            result.require(
                "forced_close_settles_admission",
                _admission_settled(harness.client, first_generation),
            )
        # A reopen is a public lifecycle operation and must construct a fresh channel.
        async with harness.client as client:
            second_generation = client._collaborators.call_supervisor._current
            assert second_generation is not None
            await client.notebooks.get("notebook-1")
        result.require("fresh_channel_after_reopen", len(harness.channels) == 2)
        result.require(
            "all_channels_closed",
            all(
                channel.get_state() is grpc.ChannelConnectivity.SHUTDOWN
                for channel in harness.channels
            ),
        )
        await _wait_for(lambda: _admission_settled(harness.client, second_generation))
        result.require(
            "final_close_settles_admission",
            _admission_settled(harness.client, second_generation),
        )
        result.require(
            "deadline_and_cancel_are_one_attempt", len(_authorizations(server, GET_PROJECT)) == 4
        )
        result.require(
            "stream_deadline_and_cancel_release",
            sum(request.method == GENERATE_STREAMED for request in server.cancellations) == 2,
        )

    await _run_server(result, setup)


def _frame(answer: str, *, final: bool = False) -> Any:
    return chat_pb2.GenerateFreeFormStreamedResponse(
        answer=chat_pb2.AnswerResponse(response=answer), is_final_response=final
    )


def _chat_sessions(session_id: str) -> Any:
    return chat_pb2.ListChatSessionsResponse(
        sessions=[common_pb2.ChatSession(chat_session_id=session_id)]
    )


async def _wait_for(predicate: Callable[[], bool]) -> None:
    """Yield only until a deterministic server-journal condition becomes true."""

    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=1.0)


def _admission_settled(client: Any, generation: Any) -> bool:
    supervisor = client._collaborators.call_supervisor
    return (
        supervisor.active_epoch() is None
        and generation.in_flight == 0
        and generation.epoch not in supervisor._retired
        and not supervisor._settlement_tasks
    )


async def run_scenario(
    name: str, *, operation_id: str, result: ScenarioResult | None = None
) -> ScenarioResult:
    """Run one fresh Android fault cohort and preserve its evidence trace."""

    evidence = _result(name, operation_id, result)
    handlers: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
        "public_flows": _public_flows,
        "unavailable": _unavailable,
        "rate_limit": _rate_limit,
        "auth": _auth,
        "auth_concurrent": _auth_concurrent,
        "minter_failure": _minter_failure,
        "stream_auth": _stream_auth,
        "commit_lost_response": _commit_lost_response,
        "deadline_and_cancellation": _deadline_and_cancellation,
    }
    try:
        await handlers[name](evidence)
    finally:
        evidence.record("finished", checks=len(evidence.checks))
    return evidence


__all__ = ["SCENARIOS", "run_scenario"]
