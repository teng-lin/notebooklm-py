"""The distinct chat outcomes behind the web UI's messages (#2541)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm import ChatHistoryTurn, ChatTurnStatus, EmptyAnswerReason
from notebooklm._types.documents import BlockKind, DocumentBlock, StructuredDocument
from notebooklm._web.chat import WebChatAPI
from notebooklm._web.rows.chat import AnswerRow, ConversationTurnRow
from notebooklm._web.rows.chat_stream import parse_streaming_chat_response
from notebooklm.exceptions import ChatError, RateLimitError, UnknownRPCMethodError
from notebooklm.outcomes import CommitState
from notebooklm.rpc import RPCMethod
from tests._fixtures.fake_core import make_fake_core

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("status", "age", "text", "state"),
    [
        (1, 0, "", "streaming"),
        (1, 1800, "", "streaming"),
        (1, 1800.001, "", "error"),
        (1, 7 * 86400, " \n", "error"),
        (1, 7 * 86400, "partial answer", "success"),
        (2, 0, "", "success"),
        (3, 0, "", "error"),
        (3, 0, "partial answer", "error"),
        (4, 0, "", "error"),
        (None, 0, "", "unknown"),
        (99, 7 * 86400, "", "unknown"),
    ],
)
def test_answer_display_state_matches_ui_boundary(status, age, text, state):
    """Distinguish stale, failed, completed, and unknown answer states."""
    turn = ChatHistoryTurn("id", 2, text, status, NOW - timedelta(seconds=age))
    assert turn.display_state(now=NOW) == state


def test_stale_document_answer_is_successful_and_missing_time_stays_streaming():
    """Recognize visible document content without inventing a missing timestamp."""
    document = StructuredDocument(blocks=(DocumentBlock(0, 1, kind=BlockKind.IMAGE),))
    turn = ChatHistoryTurn(
        "id", 2, "", 1, NOW - timedelta(days=7), answer_document=document, has_document_content=True
    )
    assert turn.display_state(now=NOW) == "success"
    assert not turn.is_empty
    assert ChatHistoryTurn("id", 2, "", 1).display_state(now=NOW) == "streaming"


def test_successful_empty_answer_is_distinct_from_failure_and_question():
    """Only a successful contentless answer represents the empty-answer outcome."""
    assert ChatHistoryTurn("id", 2, "", ChatTurnStatus.COMPLETED).is_empty
    assert not ChatHistoryTurn("id", 2, "", ChatTurnStatus.FAILED).is_empty
    assert ChatHistoryTurn("id", 1, "question", ChatTurnStatus.FAILED).state == "success"


@pytest.mark.parametrize(
    ("element", "visible"),
    [
        ([0, 1, [[[0, 1, [" \n"]]]]], False),
        ([0, 1, [[[0, 1, ["answer"]]]]], True),
        ([0, 1, [[[0, 1, None, []]]]], True),
        ([0, 1, [[[0, 1, None, None, []]]]], True),
        ([0, 1, None, None, []], True),
        ([0, 1, None, None, None, []], True),
        ([0, 1, None, None, None, None, [" \n"]], False),
        ([0, 1, None, None, None, None, ["print(1)"]], True),
        ([0, 1, None, None, None, None, None, None, []], False),
        ([0, 1, None, None, None, None, None, None, None, []], False),
        ([0, 1, None, None, None, None, None, None, None, None, []], False),
        ([0, 1, None, None, None, None, None, None, None, None, None, []], False),
    ],
)
def test_web_visible_document_content_matches_ui(element, visible):
    """Mirror the web UI content predicate across supported JSON document variants."""
    row = AnswerRow(["", None, None, None, [[[element]], None, None, None, 1]])
    assert row.has_document_content is visible
    thought = AnswerRow(["", None, None, None, [[[element]], None, None, None, 2]])
    assert not thought.has_document_content


def _web_api():
    """Build an isolated Web adapter with a recording RPC seam."""
    rpc = MagicMock(rpc_call=AsyncMock())
    api = WebChatAPI(
        rpc=rpc,
        supervisor=make_fake_core(),
        transport=MagicMock(),
        reqid=MagicMock(),
        loop_guard=MagicMock(spec=["assert_bound_loop"]),
        notebooks=MagicMock(),
    )
    return api, rpc


@pytest.mark.asyncio
async def test_public_web_history_turns_decodes_failed_and_empty_answers():
    """Expose independent failure and empty-answer metadata without changing row order."""
    api, rpc = _web_api()
    seconds = int(NOW.timestamp())
    rpc.rpc_call.return_value = [
        [
            ["empty", [seconds, 123456789], 2, None, [["", None, None, 1]], 2],
            ["q2", [seconds], 1, "unanswerable", None, 2],
            ["failed", [seconds - 7 * 86400], 2, None, [["", None, None, None]], 1],
            ["q1", [seconds - 7 * 86400], 1, "question", None, 2],
        ]
    ]
    turns = await api.get_history_turns("notebook", conversation_id="conversation", limit=4)
    assert [turn.message_id for turn in turns] == ["q1", "failed", "q2", "empty"]
    assert turns[1].status == ChatTurnStatus.IN_PROGRESS
    assert turns[1].state == "error"
    assert turns[3].is_empty
    assert turns[3].empty_answer_reason == EmptyAnswerReason.UNANSWERABLE
    assert turns[3].timestamp == NOW + timedelta(microseconds=123456)
    rpc.rpc_call.assert_awaited_once_with(
        RPCMethod.GET_CONVERSATION_TURNS,
        [[], None, None, "conversation", 4],
        source_path="/notebook/notebook",
    )


@pytest.mark.asyncio
async def test_history_turns_resolves_session_and_skips_io_for_nonpositive_limit():
    """Avoid history requests when no messages or conversation can be selected."""
    api, rpc = _web_api()
    api.get_conversation_id = AsyncMock(return_value=None)
    assert await api.get_history_turns("notebook", limit=0) == []
    api.get_conversation_id.assert_not_awaited()
    assert await api.get_history_turns("notebook") == []
    api.get_conversation_id.assert_awaited_once_with("notebook")
    rpc.rpc_call.assert_not_awaited()


@pytest.mark.parametrize("code", [None, 0, 1, 2, 99])
def test_empty_answer_reason_survives_textless_stream(code):
    """Retain known and future reason codes even when the stream has no answer text."""
    inner = [["", None, ["conversation", "turn", 7], code], None, None, None, True]
    frame = json.dumps([["wrb.fr", "method", json.dumps(inner)]])
    result = parse_streaming_chat_response(frame)
    assert result.answer == ""
    assert result.empty_answer_reason == (code or None)
    assert result.turn_key.turn_id == "turn"


@pytest.mark.parametrize("batched", [False, True], ids=["separate-lines", "same-line"])
def test_empty_answer_reason_survives_following_items_without_a_reason(batched):
    """Batching must not erase diagnostics that survive separate stream lines."""
    frames = [
        ["wrb.fr", "method", json.dumps([["", None, None, 2]])],
        ["wrb.fr", "method", json.dumps([["", None, None, None], None, None, None, True])],
    ]
    stream = json.dumps(frames) if batched else "\n".join(json.dumps([frame]) for frame in frames)
    parsed = parse_streaming_chat_response(stream)
    assert parsed.answer == ""
    assert parsed.empty_answer_reason == EmptyAnswerReason.FILTERED


@pytest.mark.parametrize("batched", [False, True], ids=["separate-lines", "same-line"])
def test_text_before_empty_answer_metadata_does_not_stop_envelope_processing(batched):
    """Earlier text must not hide a later textless envelope's reason code."""
    frames = [
        ["wrb.fr", "method", json.dumps([["partial answer", None, None, None]])],
        ["wrb.fr", "method", json.dumps([["", None, None, 2], None, None, None, True])],
    ]
    stream = json.dumps(frames) if batched else "\n".join(json.dumps([frame]) for frame in frames)
    parsed = parse_streaming_chat_response(stream)
    assert parsed.answer == "partial answer"
    assert parsed.empty_answer_reason == EmptyAnswerReason.FILTERED


@pytest.mark.parametrize("batched", [False, True], ids=["separate-lines", "same-line"])
def test_batched_final_answer_selection_matches_separate_lines(batched):
    """A shorter final answer must win over an earlier long partial in either carrier."""
    frames = [
        [
            "wrb.fr",
            "method",
            json.dumps([["long partial answer", None, None, None, [None, None, None, None, 1]]]),
        ],
        [
            "wrb.fr",
            "method",
            json.dumps(
                [["final", None, None, None, [None, None, None, None, 1]], None, None, None, True]
            ),
        ],
    ]
    stream = json.dumps(frames) if batched else "\n".join(json.dumps([frame]) for frame in frames)
    assert parse_streaming_chat_response(stream).answer == "final"


@pytest.mark.parametrize("batched", [False, True], ids=["separate-lines", "same-line"])
def test_later_quota_rejection_is_not_hidden_by_earlier_text(batched):
    """Partial text is not a successful answer when a later envelope rejects the ask."""
    details = [None] * 11 + [[]]
    payload = [8, None, [["type.googleapis.com/x.UserDisplayableError", details]]]
    frames = [
        ["wrb.fr", "method", json.dumps([["partial answer"]])],
        ["wrb.fr", "method", None, None, None, payload],
    ]
    stream = json.dumps(frames) if batched else "\n".join(json.dumps([frame]) for frame in frames)
    with pytest.raises(RateLimitError):
        parse_streaming_chat_response(stream)


@pytest.mark.parametrize("field", [12, 13])
@pytest.mark.parametrize("namespace", ["labs.tailwind", "cloud.notebooklm"])
def test_quota_presence_raises_rate_limit_with_rejected_commit_state(field, namespace):
    """Recognize both quota markers and namespaces while retaining rejection metadata."""
    detail = [None] * field
    detail[field - 1] = []  # An empty message is still present, as in the web UI.
    payload = [
        8,
        None,
        [
            [
                f"type.googleapis.com/google.internal.{namespace}.orchestration.v1.UserDisplayableError",
                detail,
            ]
        ],
    ]
    frame = json.dumps([["wrb.fr", "method", None, None, None, payload]])
    with pytest.raises(RateLimitError) as caught:
        parse_streaming_chat_response(frame)
    assert caught.value.commit_state == CommitState.REJECTED
    assert caught.value.rpc_code == 8


def test_nonquota_user_displayable_error_remains_chat_error():
    """Do not reinterpret every displayable rejection as account quota exhaustion."""
    payload = [8, None, [["type.googleapis.com/x.UserDisplayableError", [None] * 13]]]
    frame = json.dumps([["wrb.fr", "method", None, None, None, payload]])
    with pytest.raises(ChatError) as caught:
        parse_streaming_chat_response(frame)
    assert caught.value.commit_state == CommitState.REJECTED


def test_optional_history_metadata_is_not_fabricated_and_drift_raises():
    """Keep omitted metadata absent and reject malformed values explicitly."""
    row = ConversationTurnRow(["id", None, 2, None, None])
    assert row.status is None
    assert row.timestamp is None
    assert row.answer is None
    with pytest.raises(UnknownRPCMethodError):
        _ = ConversationTurnRow(["id", [0], 2, None, None, True]).status
    with pytest.raises(UnknownRPCMethodError):
        _ = ConversationTurnRow(["id", [0, -1], 2]).timestamp
    with pytest.raises(UnknownRPCMethodError):
        _ = AnswerRow(["", None, None, "filtered"]).empty_answer_reason


def test_skipped_history_rows_emit_bounded_drift_diagnostics(caplog):
    """A malformed snapshot must be distinguishable from an empty conversation."""
    api, _ = _web_api()
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat.api"):
        turns = api._decode_history_turns([[None, ["id", None, "unexpected" * 1000]]])
    assert turns == []
    messages = [record.message for record in caplog.records]
    assert any("malformed turn" in message for message in messages)
    assert any("role-slot drift" in message for message in messages)
    assert all(len(message) < 300 for message in messages)
