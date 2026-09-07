"""Generated Android schema probes for stale mutations and cyclic pagination."""

from __future__ import annotations

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    organization_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import common_pb2
from notebooklm._android.proto.notebooklm.android.wire.v1 import organization_mutations_pb2 as wire
from notebooklm.exceptions import DecodingError, UnknownRPCMethodError
from notebooklm.outcomes import CommitState

from .android import build_android_client
from .android_cleanup import android_cohort
from .common import ScenarioResult
from .grpc import (
    GET_LABELS,
    LIST_CHAT_SESSIONS,
    LIST_CHAT_TURNS,
    MUTATE_LABEL,
    GrpcFaultServer,
    reply,
)
from .http import HttpFaultServer

_COLLECTION = "00000000-0000-4000-8000-000000000401"


def _collection(name: str):
    return wire.GetLabelsWireResponse(
        collections=[wire.OrganizationRecordWire(id=_COLLECTION, name=name, emoji="")]
    )


async def stale_collection_readback(result: ScenarioResult) -> None:
    rpc = GrpcFaultServer()
    server = HttpFaultServer()
    committed = []

    def commit(request):
        committed.append(request.mutations[0].properties.name)
        return organization_pb2.MutateLabelResponse()

    rpc.plan(
        GET_LABELS, reply(_collection("Old")), reply(_collection("Old")), reply(_collection("New"))
    )
    rpc.plan(MUTATE_LABEL, reply(commit))
    async with android_cohort(
        result,
        rpc,
        server,
        lambda: build_android_client(rpc, server_error_max_retries=0),
        release=lambda: None,
        tasks=[],
    ) as harness:
        error = None
        try:
            await harness.client.collections.rename(_COLLECTION, "New")
        except DecodingError as exc:
            error = exc
        result.require("stale_readback_rejected", error is not None)
        result.require(
            "stale_error_not_unsent",
            error is not None and error.commit_state is CommitState.UNKNOWN,
        )
        result.require("mutation_confirmed_independently", committed == ["New"])
        recovered = await harness.client.collections.get(_COLLECTION)
        result.require("collection_reconciled", recovered.name == "New")
        result.require(
            "stale_readback_no_replay",
            [row.method for row in rpc.requests]
            == [GET_LABELS, MUTATE_LABEL, GET_LABELS, GET_LABELS],
        )
        rpc.assert_consumed()
        result.record(
            "grpc_trace",
            methods=[row.method.rsplit("/", 1)[-1] for row in rpc.requests],
            commits=len(committed),
            error=type(error).__name__,
        )


async def repeated_chat_token(result: ScenarioResult) -> None:
    rpc = GrpcFaultServer()
    server = HttpFaultServer()
    sessions = chat_pb2.ListChatSessionsResponse(
        sessions=[common_pb2.ChatSession(chat_session_id="conversation-1")]
    )
    rpc.plan(LIST_CHAT_SESSIONS, reply(sessions), reply(sessions))
    rpc.plan(
        LIST_CHAT_TURNS,
        reply(chat_pb2.ListChatTurnsResponse(next_page_token="cycle-token")),
        reply(chat_pb2.ListChatTurnsResponse(next_page_token="cycle-token")),
        reply(chat_pb2.ListChatTurnsResponse()),
    )
    async with android_cohort(
        result,
        rpc,
        server,
        lambda: build_android_client(rpc, server_error_max_retries=0),
        release=lambda: None,
        tasks=[],
    ) as harness:
        error = None
        try:
            await harness.client.chat.get_conversation_turns(
                "notebook-1", "conversation-1", limit=2
            )
        except UnknownRPCMethodError as exc:
            error = exc
        result.require("token_cycle_rejected", error is not None)
        turns = [row for row in rpc.requests if row.method == LIST_CHAT_TURNS]
        result.require(
            "token_cycle_bounded", [row.request.page_token for row in turns] == ["", "cycle-token"]
        )
        recovered = await harness.client.chat.get_conversation_turns(
            "notebook-1", "conversation-1", limit=2
        )
        result.require(
            "chat_pagination_recovered", not recovered.chat_turns and not recovered.next_page_token
        )
        result.require("pagination_dispatch_bound", len(rpc.requests) == 5)
        rpc.assert_consumed()
        result.record(
            "grpc_trace",
            methods=[row.method.rsplit("/", 1)[-1] for row in rpc.requests],
            pages=len(turns),
            error=type(error).__name__,
        )


IMPLEMENTATIONS = {
    "consistency_stale_collection_readback": stale_collection_readback,
    "consistency_repeated_chat_token": repeated_chat_token,
}
REQUIRED_CHECKS = {
    "consistency_stale_collection_readback": (
        "stale_readback_rejected",
        "stale_error_not_unsent",
        "mutation_confirmed_independently",
        "collection_reconciled",
        "stale_readback_no_replay",
        "cleanup",
    ),
    "consistency_repeated_chat_token": (
        "token_cycle_rejected",
        "token_cycle_bounded",
        "chat_pagination_recovered",
        "pagination_dispatch_bound",
        "cleanup",
    ),
}

BUDGETS = {
    "consistency_stale_collection_readback": {
        "scenario_timeout_s": 8,
        "cleanup_timeout_s": 2,
        "max_requests": 4,
        "max_commits": 1,
    },
    "consistency_repeated_chat_token": {
        "scenario_timeout_s": 8,
        "cleanup_timeout_s": 2,
        "max_requests": 5,
        "max_commits": 0,
    },
}
