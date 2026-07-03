"""Unit tests for the chat MCP tools.

Drives ``chat_ask`` / ``chat_configure`` through the in-memory FastMCP ``Client``
against the mocked ``NotebookLMClient``, asserting the serialized
``structured_content``. Covers the happy path, conversation-id passthrough,
name-vs-id resolution, the configure goal/length dispatch, and error projection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm.exceptions import (  # noqa: E402 - after importorskip guard
    ChatError,
    RPCError,
)

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard


@dataclass
class FakeNotebook:
    id: str
    title: str


@dataclass
class FakeReference:
    source_id: str
    citation_number: int | None = None
    cited_text: str | None = None
    chunk_id: str | None = None
    start_char: int | None = None
    score: float | None = None


@dataclass
class FakeAskResult:
    answer: str
    conversation_id: str
    turn_number: int = 1
    is_follow_up: bool = False
    references: list[Any] = field(default_factory=list)
    raw_response: str = ""


NB_ID = "11111111-1111-1111-1111-111111111111"
CONV_ID = "conv-abc"


async def test_chat_ask(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?"})
    assert result.structured_content["answer"] == "42"
    assert result.structured_content["conversation_id"] == CONV_ID
    mock_client.chat.ask.assert_awaited_once_with(
        NB_ID, "what?", source_ids=None, conversation_id=None
    )


async def test_chat_ask_passes_conversation_id(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="ok", conversation_id=CONV_ID, is_follow_up=True)
    )
    await mcp_call(
        "chat_ask",
        {"notebook": NB_ID, "question": "follow up", "conversation_id": CONV_ID},
    )
    mock_client.chat.ask.assert_awaited_once_with(
        NB_ID, "follow up", source_ids=None, conversation_id=CONV_ID
    )


async def test_chat_ask_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="hi", conversation_id=CONV_ID)
    )
    await mcp_call("chat_ask", {"notebook": "My Notebook", "question": "q"})
    mock_client.chat.ask.assert_awaited_once_with(NB_ID, "q", source_ids=None, conversation_id=None)


# Full-UUID source ids take resolve_source's fast path (no listing needed).
_SRC_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SRC_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


async def test_chat_ask_omitting_source_ids_uses_all(mcp_call, mock_client) -> None:
    """Omitting ``source_ids`` => None (=> all sources, client.chat.ask's contract)."""
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?"})
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] is None


async def test_chat_ask_source_ids_list(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    await mcp_call(
        "chat_ask",
        {"notebook": NB_ID, "question": "what?", "source_ids": [_SRC_A, _SRC_B]},
    )
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] == [_SRC_A, _SRC_B]


async def test_chat_ask_source_ids_json_string(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    await mcp_call(
        "chat_ask",
        {"notebook": NB_ID, "question": "what?", "source_ids": f'["{_SRC_A}"]'},
    )
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] == [_SRC_A]


async def test_chat_ask_source_ids_comma_string(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    await mcp_call(
        "chat_ask",
        {"notebook": NB_ID, "question": "what?", "source_ids": f"{_SRC_A},{_SRC_B}"},
    )
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] == [_SRC_A, _SRC_B]


async def test_chat_ask_source_ids_scalar_string(mcp_call, mock_client) -> None:
    """A bare scalar-string source_ids resolves/passes a single id (coerce_list)."""
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?", "source_ids": _SRC_A})
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] == [_SRC_A]


async def test_chat_ask_empty_source_ids_uses_all(mcp_call, mock_client) -> None:
    """An explicit empty list => None (all sources), never [] (zero sources)."""
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?", "source_ids": []})
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] is None


async def test_chat_ask_whitespace_source_ids_uses_all(mcp_call, mock_client) -> None:
    """A whitespace-only string coerces to [] => collapses to None (all sources)."""
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?", "source_ids": "   "})
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] is None


async def test_chat_ask_recall_only(mcp_call, mock_client) -> None:
    """Empty question + history>0 recalls pairs (no ask) and echoes the conversation."""
    mock_client.chat.get_conversation_id = AsyncMock(return_value=CONV_ID)
    mock_client.chat.get_history = AsyncMock(return_value=[("q1", "a1"), ("q2", "a2")])
    mock_client.chat.ask = AsyncMock()
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "history": 5})
    assert result.structured_content["history"] == [
        {"question": "q1", "answer": "a1"},
        {"question": "q2", "answer": "a2"},
    ]
    # Recall-only echoes the resolved conversation; no answer is produced.
    assert result.structured_content["conversation_id"] == CONV_ID
    assert "answer" not in result.structured_content
    mock_client.chat.ask.assert_not_awaited()
    # ``limit`` is doubled (role-rows ~2 per pair) and pinned to the resolved id.
    mock_client.chat.get_history.assert_awaited_once_with(NB_ID, limit=10, conversation_id=CONV_ID)


async def test_chat_ask_recall_only_empty_conversation(mcp_call, mock_client) -> None:
    """Recall against a notebook with no conversation => empty history, no echo, no fetch."""
    mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
    mock_client.chat.get_history = AsyncMock(return_value=[])
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "history": 5})
    assert result.structured_content["history"] == []
    assert "conversation_id" not in result.structured_content
    # No conversation => skip get_history (it would just re-resolve the absent id).
    mock_client.chat.get_history.assert_not_awaited()


async def test_chat_ask_history_explicit_conversation_id_skips_resolve(
    mcp_call, mock_client
) -> None:
    """An explicit conversation_id with history>0 must not pay the resolve round-trip."""
    mock_client.chat.get_history = AsyncMock(return_value=[("q1", "a1")])
    result = await mcp_call(
        "chat_ask", {"notebook": NB_ID, "history": 2, "conversation_id": CONV_ID}
    )
    assert result.structured_content["history"] == [{"question": "q1", "answer": "a1"}]
    assert result.structured_content["conversation_id"] == CONV_ID
    mock_client.chat.get_conversation_id.assert_not_called()
    mock_client.chat.get_history.assert_awaited_once_with(NB_ID, limit=4, conversation_id=CONV_ID)


async def test_chat_ask_question_with_history_no_conversation(mcp_call, mock_client) -> None:
    """Question + history>0 with no prior conversation: fresh ask, empty history, no fetch."""
    mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
    mock_client.chat.get_history = AsyncMock()
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "question": "q", "history": 3})
    assert result.structured_content["answer"] == "42"
    assert result.structured_content["history"] == []
    mock_client.chat.get_history.assert_not_awaited()
    # No conversation resolved => the ask starts a fresh one.
    assert mock_client.chat.ask.await_args.kwargs["conversation_id"] is None


async def test_chat_ask_negative_history_rejected(mcp_call, mock_client) -> None:
    """A negative history (even with a question) is invalid input, not a silent ask."""
    mock_client.chat.ask = AsyncMock()
    with pytest.raises(ToolError):
        await mcp_call("chat_ask", {"notebook": NB_ID, "question": "q", "history": -1})
    mock_client.chat.ask.assert_not_awaited()


async def test_chat_ask_with_history_includes_both(mcp_call, mock_client) -> None:
    """Question + history>0 returns the answer and prior pairs, pinned to one id."""
    mock_client.chat.get_conversation_id = AsyncMock(return_value=CONV_ID)
    mock_client.chat.get_history = AsyncMock(return_value=[("q1", "a1")])
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?", "history": 3})
    assert result.structured_content["answer"] == "42"
    assert result.structured_content["history"] == [{"question": "q1", "answer": "a1"}]
    # History fetched with doubled limit, and both reads/writes pin the same id.
    mock_client.chat.get_history.assert_awaited_once_with(NB_ID, limit=6, conversation_id=CONV_ID)
    assert mock_client.chat.ask.await_args.kwargs["conversation_id"] == CONV_ID


async def test_chat_ask_zero_history_skips_recall(mcp_call, mock_client) -> None:
    """A plain ask (history defaults to 0) never touches the history/resolve path."""
    mock_client.chat.get_history = AsyncMock()
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="x", conversation_id=CONV_ID)
    )
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "question": "q"})
    assert "history" not in result.structured_content
    mock_client.chat.get_history.assert_not_awaited()
    # The pure-ask hot path must not pay an extra get_conversation_id round-trip.
    mock_client.chat.get_conversation_id.assert_not_called()


async def test_chat_ask_requires_question_or_history(mcp_call, mock_client) -> None:
    """Neither a question nor history>0 is a validation error, not a silent no-op."""
    with pytest.raises(ToolError):
        await mcp_call("chat_ask", {"notebook": NB_ID})


async def test_chat_ask_whitespace_question_rejected(mcp_call, mock_client) -> None:
    """A whitespace-only question with no history is rejected, not sent as a blank ask."""
    mock_client.chat.ask = AsyncMock()
    with pytest.raises(ToolError):
        await mcp_call("chat_ask", {"notebook": NB_ID, "question": "   "})
    mock_client.chat.ask.assert_not_awaited()


@dataclass
class FakePromptSuggestion:
    title: str
    prompt: str


async def test_chat_ask_default_no_suggest_followups(mcp_call, mock_client) -> None:
    """Default chat_ask omits suggested_prompts and never calls suggest_prompts."""
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    mock_client.notebooks.suggest_prompts = AsyncMock()
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?"})
    assert "suggested_prompts" not in result.structured_content
    mock_client.notebooks.suggest_prompts.assert_not_called()


async def test_chat_ask_suggest_followups_with_question(mcp_call, mock_client) -> None:
    """suggest_followups=True with a question returns the answer AND suggestions."""
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    mock_client.notebooks.suggest_prompts = AsyncMock(
        return_value=[
            FakePromptSuggestion(title="T1", prompt="P1"),
            FakePromptSuggestion(title="T2", prompt="P2"),
        ]
    )
    result = await mcp_call(
        "chat_ask", {"notebook": NB_ID, "question": "what?", "suggest_followups": True}
    )
    sc = result.structured_content
    assert sc["answer"] == "42"
    assert sc["suggested_prompts"] == [
        {"title": "T1", "prompt": "P1"},
        {"title": "T2", "prompt": "P2"},
    ]
    # Keyword-only args (positional would TypeError against the real signature).
    mock_client.notebooks.suggest_prompts.assert_awaited_once_with(
        NB_ID, source_ids=None, mode=4, query="what?"
    )


async def test_chat_ask_suggest_only_no_question(mcp_call, mock_client) -> None:
    """No question + suggest_followups=True returns suggestions without raising."""
    mock_client.chat.ask = AsyncMock()
    mock_client.notebooks.suggest_prompts = AsyncMock(
        return_value=[FakePromptSuggestion(title="T", prompt="P")]
    )
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "suggest_followups": True})
    sc = result.structured_content
    assert sc["suggested_prompts"] == [{"title": "T", "prompt": "P"}]
    assert "answer" not in sc
    # No question => no ask, and the suggest query is unsteered (None).
    mock_client.chat.ask.assert_not_awaited()
    mock_client.notebooks.suggest_prompts.assert_awaited_once_with(
        NB_ID, source_ids=None, mode=4, query=None
    )


async def test_chat_ask_all_three_absent_still_rejected(mcp_call, mock_client) -> None:
    """No question, history=0, suggest_followups=False remains a validation error."""
    mock_client.notebooks.suggest_prompts = AsyncMock()
    with pytest.raises(ToolError):
        await mcp_call("chat_ask", {"notebook": NB_ID, "suggest_followups": False})
    mock_client.notebooks.suggest_prompts.assert_not_called()


async def test_chat_ask_cancels_sibling_on_error(mcp_call, mock_client) -> None:
    """When ask + suggest run concurrently (question + suggest_followups) and one
    raises, the still-running sibling is cancelled + drained (no leaked coroutine)
    and the error propagates as ToolError (#1760)."""
    sibling_cancelled = asyncio.Event()

    async def _slow_ask(*_a: Any, **_k: Any) -> Any:
        try:
            await asyncio.sleep(30)  # the slow sibling — should be cancelled
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        # never reached (cancelled first); return a real fake, not Ellipsis
        return FakeAskResult(answer="unused", conversation_id=CONV_ID)  # pragma: no cover

    async def _raise_suggest(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(0)  # let the slow sibling start first
        raise RPCError("unexpected boom")

    mock_client.chat.ask = _slow_ask
    mock_client.notebooks.suggest_prompts = _raise_suggest

    with pytest.raises(ToolError):
        await mcp_call(
            "chat_ask",
            {"notebook": NB_ID, "question": "what?", "suggest_followups": True},
        )
    assert sibling_cancelled.is_set(), "slow sibling read was not cancelled/drained"


@dataclass
class FakeSource:
    id: str
    title: str | None


async def test_chat_ask_two_title_refs_list_once_order_preserved(mcp_call, mock_client) -> None:
    """Two non-UUID refs resolve via a single ``sources.list`` snapshot, in input order."""
    mock_client.sources.list = AsyncMock(
        return_value=[
            FakeSource(id=_SRC_A, title="Alpha"),
            FakeSource(id=_SRC_B, title="Beta"),
        ]
    )
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    await mcp_call(
        "chat_ask",
        {"notebook": NB_ID, "question": "what?", "source_ids": ["Beta", "Alpha"]},
    )
    mock_client.sources.list.assert_awaited_once_with(NB_ID)
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] == [_SRC_B, _SRC_A]


async def test_chat_ask_suggest_followups_resolves_source_ids(mcp_call, mock_client) -> None:
    """suggest_prompts is called with resolved source ids, not the raw title."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=_SRC_A, title="Alpha")])
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    mock_client.notebooks.suggest_prompts = AsyncMock(
        return_value=[FakePromptSuggestion(title="T", prompt="P")]
    )
    await mcp_call(
        "chat_ask",
        {
            "notebook": NB_ID,
            "question": "what?",
            "source_ids": "Alpha",
            "suggest_followups": True,
        },
    )
    assert mock_client.notebooks.suggest_prompts.await_args.kwargs["source_ids"] == [_SRC_A]
    # The ask path shares the same resolved ids (resolution happens once, up front).
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] == [_SRC_A]
    # Resolve-once: a single sources.list snapshot feeds both the ask + suggest paths.
    mock_client.sources.list.assert_awaited_once_with(NB_ID)


async def test_chat_ask_recall_only_ignores_source_ids(mcp_call, mock_client) -> None:
    """A recall-only turn (history>0, no question, no suggest) does NOT resolve
    source_ids — no sources.list round-trip, and a stale ref can't fail the recall."""
    mock_client.sources.list = AsyncMock(return_value=[])
    mock_client.chat.get_conversation_id = AsyncMock(return_value=CONV_ID)
    mock_client.chat.get_history = AsyncMock(return_value=[("q", "a")])
    result = await mcp_call(
        "chat_ask",
        {"notebook": NB_ID, "history": 1, "source_ids": "Nonexistent-Title"},
    )
    assert result.structured_content["history"] == [{"question": "q", "answer": "a"}]
    mock_client.sources.list.assert_not_called()  # source refs ignored in pure recall


async def test_chat_configure_goal_and_length(mcp_call, mock_client) -> None:
    mock_client.chat.configure = AsyncMock(return_value=None)
    result = await mcp_call(
        "chat_configure",
        {"notebook": NB_ID, "goal": "Explain like I'm five", "response_length": "longer"},
    )
    sc = result.structured_content
    assert sc["notebook_id"] == NB_ID
    assert sc["persona"] == "Explain like I'm five"
    assert sc["response_length"] == "longer"
    assert sc["goal_name"] == "custom"
    mock_client.chat.configure.assert_awaited_once()


async def test_chat_configure_empty_rejected(mcp_call, mock_client) -> None:
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_configure", {"notebook": NB_ID})
    assert "at least one setting" in str(excinfo.value)
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_goal_only_rejected(mcp_call, mock_client) -> None:
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_configure", {"notebook": NB_ID, "goal": "tutor"})
    assert "Pass BOTH goal and response_length" in str(excinfo.value)
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_length_only_rejected(mcp_call, mock_client) -> None:
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_configure", {"notebook": NB_ID, "response_length": "longer"})
    assert "Pass BOTH goal and response_length" in str(excinfo.value)
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_length_default_only_rejected(mcp_call, mock_client) -> None:
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_configure", {"notebook": NB_ID, "response_length": "default"})
    assert "Pass BOTH goal and response_length" in str(excinfo.value)
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_empty_goal_only_rejected(mcp_call, mock_client) -> None:
    """An empty goal is not-supplied, so goal: "" with no length is a bare/empty call."""
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_configure", {"notebook": NB_ID, "goal": ""})
    assert "at least one setting" in str(excinfo.value)
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_empty_goal_with_length_rejected(mcp_call, mock_client) -> None:
    """goal: "" + a length is a length-ONLY (partial) write, not a both-supplied call.

    Pins ``bool(goal)`` (not ``goal is not None``): an empty goal counts as
    not-supplied, so pairing it with a response_length is still the partial-reset
    footgun and must be rejected.
    """
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "chat_configure", {"notebook": NB_ID, "goal": "", "response_length": "longer"}
        )
    assert "Pass BOTH goal and response_length" in str(excinfo.value)
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_rejects_before_resolving_notebook(mcp_call, mock_client) -> None:
    """The guard runs BEFORE resolve_notebook: an invalid call by *title* never lists.

    ``NB_ID`` is a full UUID that fast-paths without a ``notebooks.list`` round-trip,
    so the other rejection tests can't prove ordering. A title forces resolution to
    call ``client.notebooks.list`` — asserting it stays uncalled pins that the
    fail-loud guard short-circuits ahead of any network work.
    """
    mock_client.chat.configure = AsyncMock(return_value=None)
    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    with pytest.raises(ToolError):
        await mcp_call("chat_configure", {"notebook": "My Notebook", "goal": "tutor"})
    mock_client.notebooks.list.assert_not_called()
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_mode_applies_preset(mcp_call, mock_client) -> None:
    """A chat_mode selects the predefined preset via set_mode (not the custom branch)."""
    mock_client.chat.set_mode = AsyncMock(return_value=None)
    mock_client.chat.configure = AsyncMock(return_value=None)
    result = await mcp_call("chat_configure", {"notebook": NB_ID, "chat_mode": "learning-guide"})
    sc = result.structured_content
    assert sc["notebook_id"] == NB_ID
    assert sc["mode"] == "learning-guide"
    assert sc["persona"] is None and sc["response_length"] is None
    mock_client.chat.set_mode.assert_awaited_once()
    # The preset path must not also write the custom settings block.
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_mode_rejects_goal_combination(mcp_call, mock_client) -> None:
    """chat_mode + a (truthy) goal is rejected, not silently dropped."""
    mock_client.chat.set_mode = AsyncMock(return_value=None)
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_configure", {"notebook": NB_ID, "chat_mode": "concise", "goal": "x"})
    assert "chat_mode" in str(excinfo.value)
    mock_client.chat.set_mode.assert_not_called()
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_mode_rejects_response_length_combination(
    mcp_call, mock_client
) -> None:
    """chat_mode + response_length (a real setting, incl. 'default') is rejected."""
    mock_client.chat.set_mode = AsyncMock(return_value=None)
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "chat_configure",
            {"notebook": NB_ID, "chat_mode": "detailed", "response_length": "longer"},
        )
    assert "chat_mode" in str(excinfo.value)
    mock_client.chat.set_mode.assert_not_called()
    mock_client.chat.configure.assert_not_called()


async def test_chat_configure_mode_with_empty_goal_ok(mcp_call, mock_client) -> None:
    """An empty goal ("") is a no-op, so it does NOT block a chat_mode preset."""
    mock_client.chat.set_mode = AsyncMock(return_value=None)
    result = await mcp_call(
        "chat_configure", {"notebook": NB_ID, "chat_mode": "concise", "goal": ""}
    )
    assert result.structured_content["mode"] == "concise"
    mock_client.chat.set_mode.assert_awaited_once()


async def test_chat_configure_rejects_bad_mode(mcp_call, mock_client) -> None:
    """An out-of-enum chat_mode is rejected at the Literal schema boundary, no RPC."""
    mock_client.chat.set_mode = AsyncMock(return_value=None)
    with pytest.raises(ToolError):
        await mcp_call("chat_configure", {"notebook": NB_ID, "chat_mode": "podcast"})
    mock_client.chat.set_mode.assert_not_called()


async def test_chat_ask_strips_raw_response_and_lite_references(mcp_call, mock_client) -> None:
    """raw_response is never returned; default references are the lite subset."""
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(
            answer="42",
            conversation_id=CONV_ID,
            raw_response='[["wrb.fr", ... internal wire blob ...]]',
            references=[
                FakeReference(
                    source_id="s1",
                    citation_number=1,
                    cited_text="quote",
                    chunk_id="c1",
                    start_char=10,
                    score=0.9,
                )
            ],
        )
    )
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?"})
    sc = result.structured_content
    assert "raw_response" not in sc
    assert sc["references"] == [{"source_id": "s1", "citation_number": 1, "cited_text": "quote"}]


async def test_chat_ask_tolerates_null_references(mcp_call, mock_client) -> None:
    """A null references value (not a list) must not crash the lite projection."""
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID, references=None)
    )
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?"})
    assert result.structured_content["references"] == []


async def test_chat_ask_full_references_keep_chunk_detail(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(
            answer="42",
            conversation_id=CONV_ID,
            references=[FakeReference(source_id="s1", citation_number=1, chunk_id="c1", score=0.9)],
        )
    )
    result = await mcp_call(
        "chat_ask", {"notebook": NB_ID, "question": "what?", "references": "full"}
    )
    assert result.structured_content["references"][0]["chunk_id"] == "c1"


async def test_chat_configure_rejects_bad_response_length(mcp_call, mock_client) -> None:
    """An out-of-enum response_length is rejected at the Literal schema boundary, no RPC."""
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_configure", {"notebook": NB_ID, "response_length": "huge"})
    msg = str(excinfo.value).lower()
    assert "response_length" in msg and "shorter" in msg
    mock_client.chat.configure.assert_not_called()


async def test_chat_ask_error_projects_tool_error(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(side_effect=ChatError("no conversation recorded"))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_ask", {"notebook": NB_ID, "question": "q"})
    # ChatError classifies under the LIBRARY ladder -> the generic ERROR code.
    assert "ERROR" in str(excinfo.value)


# ---------------------------------------------------------------------------
# suggest_prompts (dedicated tool)
# ---------------------------------------------------------------------------


async def test_suggest_prompts_default_ask(mcp_call, mock_client) -> None:
    """Default surface=ask maps to mode 4 and returns {suggestions:[...]}."""
    mock_client.notebooks.suggest_prompts = AsyncMock(
        return_value=[FakePromptSuggestion(title="T1", prompt="P1")]
    )
    result = await mcp_call("suggest_prompts", {"notebook": NB_ID})
    assert result.structured_content["suggestions"] == [{"title": "T1", "prompt": "P1"}]
    mock_client.notebooks.suggest_prompts.assert_awaited_once_with(
        NB_ID, source_ids=None, mode=4, query=None
    )


@pytest.mark.parametrize(
    ("surface", "mode"),
    [
        ("ask", 4),
        ("audio-deep-dive", 1),
        ("audio-brief", 2),
        ("audio-critique", 5),
        ("audio-debate", 6),
        ("video-explainer", 3),
        ("video-short", 10),
        ("quiz", 8),
        ("flashcards", 9),
    ],
)
async def test_suggest_prompts_surface_maps_to_mode(mcp_call, mock_client, surface, mode) -> None:
    """Each surface Literal maps to its verified otmP3b mode int (incl. video-short=10)."""
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=[])
    await mcp_call("suggest_prompts", {"notebook": NB_ID, "surface": surface})
    assert mock_client.notebooks.suggest_prompts.await_args.kwargs["mode"] == mode


async def test_suggest_prompts_source_ids_and_query(mcp_call, mock_client) -> None:
    """source_ids resolved once; empty query normalizes to None."""
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=[])
    await mcp_call(
        "suggest_prompts",
        {"notebook": NB_ID, "surface": "quiz", "source_ids": [_SRC_A], "query": "risks"},
    )
    kwargs = mock_client.notebooks.suggest_prompts.await_args.kwargs
    assert kwargs["source_ids"] == [_SRC_A]
    assert kwargs["query"] == "risks"
    # Omitted source_ids => None (all); explicit null query is accepted at the
    # schema boundary (query is str | None) and reaches the client as None.
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=[])
    await mcp_call("suggest_prompts", {"notebook": NB_ID, "query": None})
    kwargs = mock_client.notebooks.suggest_prompts.await_args.kwargs
    assert kwargs["source_ids"] is None and kwargs["query"] is None


async def test_suggest_prompts_rejects_bad_surface(mcp_call, mock_client) -> None:
    """An out-of-enum surface is rejected at the Literal schema boundary, no RPC."""
    mock_client.notebooks.suggest_prompts = AsyncMock()
    with pytest.raises(ToolError):
        await mcp_call("suggest_prompts", {"notebook": NB_ID, "surface": "podcast"})
    mock_client.notebooks.suggest_prompts.assert_not_called()
