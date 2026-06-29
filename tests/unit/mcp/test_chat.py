"""Unit tests for the chat MCP tools.

Drives ``chat_ask`` / ``chat_configure`` through the in-memory FastMCP ``Client``
against the mocked ``NotebookLMClient``, asserting the serialized
``structured_content``. Covers the happy path, conversation-id passthrough,
name-vs-id resolution, the configure goal/length dispatch, and error projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm.exceptions import ChatError  # noqa: E402 - after importorskip guard

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


async def test_chat_configure_no_goal(mcp_call, mock_client) -> None:
    mock_client.chat.configure = AsyncMock(return_value=None)
    result = await mcp_call("chat_configure", {"notebook": NB_ID})
    sc = result.structured_content
    assert sc["notebook_id"] == NB_ID
    assert sc["goal_name"] is None
    mock_client.chat.configure.assert_awaited_once()


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
