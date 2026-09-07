"""Android cumulative protobuf response cap through public chat.ask."""

from __future__ import annotations

import asyncio

import grpc

from notebooklm import RPCResponseTooLargeError

from .android import build_android_client
from .android_cleanup import finish_cleanup, settle_actions
from .common import ScenarioResult
from .grpc import GENERATE_STREAMED, LIST_CHAT_SESSIONS, GrpcFaultServer, reply, stream

SCENARIOS = tuple(f"protocol_chat_cumulative_{boundary}" for boundary in ("below", "at", "above"))
CHECKS = [
    "public_outcome",
    "no_partial_success",
    "cumulative_byte_evidence",
    "wire_call_cancelled",
    "same_client_recovery",
    "exact_dispatches",
    "no_commits",
    "scripts_consumed",
    "cleanup",
]


async def run_scenario(
    name: str, *, operation_id: str, result: ScenarioResult | None = None
) -> ScenarioResult:
    from .android_scenarios import _chat_sessions, _frame

    if name not in SCENARIOS:
        raise ValueError("unknown Android protocol scenario")
    result = result or ScenarioResult("android", name, operation_id)
    if (result.backend, result.scenario, result.operation_id) != ("android", name, operation_id):
        raise ValueError("protocol scenario identity mismatch")
    boundary = name.rsplit("_", 1)[-1]
    frames = [_frame("Partial " * 20), _frame("Complete " * 20, final=True)]
    sizes = [frame.ByteSize() for frame in frames]
    total = sum(sizes)
    limit = total + {"below": 1, "at": 0, "above": -1}[boundary]
    failure = boundary == "above"
    result.record(
        "plan",
        required_checks=CHECKS,
        transport="grpc",
        faults=[boundary],
        cohort_ids=[f"{operation_id}:0"],
        entry_point="chat.ask",
        budgets={
            "cumulative_response_cap_bytes": limit,
            "watchdog_s": 8,
            "cleanup_timeout_s": 2,
            "request_limit": 6,
            "commit_limit": 0,
        },
    )
    server = GrpcFaultServer()
    sessions = [reply()]
    if not failure:
        sessions.append(reply(_chat_sessions("first-conversation")))
    sessions += [reply(), reply(_chat_sessions("recovery-conversation"))]
    server.plan(LIST_CHAT_SESSIONS, *sessions)
    server.plan(
        GENERATE_STREAMED,
        stream(frames, gate="first-tail"),
        stream([_frame("Recovered", final=True)], gate="recovery-tail"),
    )
    harness = None
    primary = None
    try:
        await server.__aenter__()
        harness = build_android_client(server, timeout=3)
        client = harness.client
        client.chat._chat_response_max_bytes = limit
        await client.__aenter__()
        returned = None
        error = None
        try:
            returned = await asyncio.wait_for(
                client.chat.ask("notebook-1", "Question", source_ids=[]), 8
            )
        except Exception as exc:
            error = exc
        result.record(
            "protocol_outcome",
            frame_sizes=sizes,
            cumulative_bytes=total,
            limit_bytes=limit,
            error=None if error is None else type(error).__name__,
            error_bytes_read=getattr(error, "bytes_read", None),
        )
        result.require(
            "public_outcome",
            isinstance(error, RPCResponseTooLargeError)
            if failure
            else error is None and returned.answer == "Complete " * 20,
        )
        result.require(
            "no_partial_success",
            returned is None if failure else returned.answer == "Complete " * 20,
        )
        result.require(
            "cumulative_byte_evidence",
            all(size < limit for size in sizes)
            and (error.bytes_read == total and error.limit_bytes == limit if failure else True),
        )
        request = next(
            request for request in server.requests if request.method == GENERATE_STREAMED
        )
        await server.wait_for_cancellation(request)
        result.require("wire_call_cancelled", request.cancelled)
        recovered = await asyncio.wait_for(
            client.chat.ask("notebook-1", "Question", source_ids=[]), 8
        )
        result.require("same_client_recovery", recovered.answer == "Recovered")
        await server.wait_for_cancellation(server.requests[-2])
        result.require(
            "exact_dispatches",
            len(server.requests) == (5 if failure else 6)
            and sum(request.method == GENERATE_STREAMED for request in server.requests) == 2,
        )
        result.require("no_commits", not any(server.state.values()))
        await server.wait_for_idle()
        server.assert_consumed()
        result.require("scripts_consumed", True)
        result.record(
            "grpc_journal",
            methods=[request.method.rpartition("/")[2] for request in server.requests],
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        server.gate("first-tail").set()
        server.gate("recovery-tail").set()
        actions = []
        if harness is not None:
            actions.append(lambda: harness.client.close(drain=False))
        actions.append(lambda: server.__aexit__(None, None, None))
        failures = await settle_actions(actions)
        closed = harness is None or not harness.client._lifecycle.is_open()
        channels_closed = harness is None or all(
            channel.get_state() is grpc.ChannelConnectivity.SHUTDOWN for channel in harness.channels
        )
        finish_cleanup(
            result,
            primary,
            failures,
            clean=closed and channels_closed and not server._active and not server.handler_errors,
            client_closed=closed,
            channels_closed=channels_closed,
            active_handlers=len(server._active),
        )
    return result
