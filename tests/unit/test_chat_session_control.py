"""Unit coverage for chat session status and generation cancellation (#2303)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from notebooklm import ChatSessionStatus
from notebooklm._web.chat import WebChatAPI
from notebooklm._web.params.chat_session import (
    build_cancel_generation_params,
    build_chat_session_status_params,
)
from notebooklm._web.rows.chat import unwrap_chat_session_status
from notebooklm.exceptions import AuthError, UnknownRPCMethodError
from notebooklm.rpc import RPCMethod


def _api() -> tuple[WebChatAPI, MagicMock, MagicMock]:
    rpc = MagicMock(rpc_call=AsyncMock())
    guard = MagicMock(spec=["assert_bound_loop"])
    api = WebChatAPI(
        rpc=rpc,
        transport=MagicMock(),
        reqid=MagicMock(),
        loop_guard=guard,
        notebooks=MagicMock(),
        chat_timeout=180.0,
    )
    return api, rpc, guard


def test_chat_session_param_builders_pin_web_positional_shapes() -> None:
    assert build_chat_session_status_params("conversation-1") == [None, "conversation-1"]
    assert build_cancel_generation_params("conversation-1") == [None, "conversation-1"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([None, 1], ChatSessionStatus(generating=False)),
        (["generation-token", 2], ChatSessionStatus(True, "generation-token")),
    ],
)
def test_web_status_decoder_maps_idle_and_generating(
    raw: list[object],
    expected: ChatSessionStatus,
) -> None:
    row = unwrap_chat_session_status(raw)
    assert (row.generating, row.token) == (expected.generating, expected.token)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        [None],
        [None, 1, "trailing-drift"],
        [None, 2],
        ["token", 1],
        ["token", 99],
        {"status": 1},
    ],
)
def test_web_status_decoder_rejects_drift(raw: object) -> None:
    with pytest.raises(UnknownRPCMethodError) as raised:
        unwrap_chat_session_status(raw)
    assert raised.value.method_id == RPCMethod.GET_CHAT_SESSION_STATUS.value


@pytest.mark.asyncio
async def test_web_session_status_sends_exact_request() -> None:
    api, rpc, guard = _api()
    rpc.rpc_call.return_value = ["generation-token", 2]

    status = await api.session_status("notebook-1", "conversation-1")

    assert status == ChatSessionStatus(True, "generation-token")
    guard.assert_bound_loop.assert_called_once_with()
    rpc.rpc_call.assert_awaited_once_with(
        RPCMethod.GET_CHAT_SESSION_STATUS,
        [None, "conversation-1"],
        source_path="/notebook/notebook-1",
    )


@pytest.mark.asyncio
async def test_web_session_control_resolves_latest_conversation_once_per_call() -> None:
    api, rpc, _ = _api()
    rpc.rpc_call.side_effect = [
        [[["conversation-1"]]],
        [None, 1],
        [[["conversation-1"]]],
        [],
    ]

    assert await api.session_status("notebook-1") == ChatSessionStatus(False)
    assert await api.cancel("notebook-1") is None

    assert rpc.rpc_call.await_args_list == [
        call(
            RPCMethod.GET_LAST_CONVERSATION_ID,
            [[], None, "notebook-1", 1],
            source_path="/notebook/notebook-1",
        ),
        call(
            RPCMethod.GET_CHAT_SESSION_STATUS,
            [None, "conversation-1"],
            source_path="/notebook/notebook-1",
        ),
        call(
            RPCMethod.GET_LAST_CONVERSATION_ID,
            [[], None, "notebook-1", 1],
            source_path="/notebook/notebook-1",
        ),
        call(
            RPCMethod.CANCEL_GENERATION,
            [None, "conversation-1"],
            source_path="/notebook/notebook-1",
        ),
    ]


@pytest.mark.asyncio
async def test_web_session_control_is_noop_without_a_conversation() -> None:
    api, rpc, _ = _api()
    rpc.rpc_call.side_effect = [[], []]

    assert await api.session_status("notebook-1") == ChatSessionStatus(False)
    assert await api.cancel("notebook-1") is None
    assert [args.args[0] for args in rpc.rpc_call.await_args_list] == [
        RPCMethod.GET_LAST_CONVERSATION_ID,
        RPCMethod.GET_LAST_CONVERSATION_ID,
    ]


@pytest.mark.asyncio
async def test_web_cancel_preserves_typed_permission_error() -> None:
    api, rpc, _ = _api()
    error = AuthError(
        "permission denied",
        method_id=RPCMethod.CANCEL_GENERATION.value,
        rpc_code=7,
    )
    rpc.rpc_call.side_effect = error

    with pytest.raises(AuthError) as raised:
        await api.cancel("notebook-1", "foreign-conversation")
    assert raised.value is error
