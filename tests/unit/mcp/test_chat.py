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
class FakeAskResult:
    answer: str
    conversation_id: str
    turn_number: int = 1
    is_follow_up: bool = False
    references: list[Any] = field(default_factory=list)


NB_ID = "11111111-1111-1111-1111-111111111111"
CONV_ID = "conv-abc"


async def test_chat_ask(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="42", conversation_id=CONV_ID)
    )
    result = await mcp_call("chat_ask", {"notebook": NB_ID, "question": "what?"})
    assert result.structured_content["answer"] == "42"
    assert result.structured_content["conversation_id"] == CONV_ID
    mock_client.chat.ask.assert_awaited_once_with(NB_ID, "what?", conversation_id=None)


async def test_chat_ask_passes_conversation_id(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="ok", conversation_id=CONV_ID, is_follow_up=True)
    )
    await mcp_call(
        "chat_ask",
        {"notebook": NB_ID, "question": "follow up", "conversation_id": CONV_ID},
    )
    mock_client.chat.ask.assert_awaited_once_with(NB_ID, "follow up", conversation_id=CONV_ID)


async def test_chat_ask_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="hi", conversation_id=CONV_ID)
    )
    await mcp_call("chat_ask", {"notebook": "My Notebook", "question": "q"})
    mock_client.chat.ask.assert_awaited_once_with(NB_ID, "q", conversation_id=None)


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


async def test_chat_configure_rejects_bad_response_length(mcp_call, mock_client) -> None:
    """An invalid response_length is rejected up front as VALIDATION_ERROR, no RPC."""
    mock_client.chat.configure = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_configure", {"notebook": NB_ID, "response_length": "huge"})
    assert "VALIDATION" in str(excinfo.value)
    assert "response_length" in str(excinfo.value)
    mock_client.chat.configure.assert_not_called()


async def test_chat_ask_error_projects_tool_error(mcp_call, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(side_effect=ChatError("no conversation recorded"))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_ask", {"notebook": NB_ID, "question": "q"})
    # ChatError classifies under the LIBRARY ladder -> the generic ERROR code.
    assert "ERROR" in str(excinfo.value)
