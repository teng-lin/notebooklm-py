"""Unit tests for the note MCP tools.

Drives each tool through the in-memory FastMCP ``Client`` against the mocked
``NotebookLMClient``, asserting the serialized ``structured_content``. Covers the
happy path, name-vs-id resolution (notebook + note) reaching the tool, the
confirm preview-then-delete flow, and error projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm.exceptions import NoteNotFoundError  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard


@dataclass
class FakeNote:
    id: str
    title: str
    content: str = ""


NB_ID = "11111111-1111-1111-1111-111111111111"
NOTE_ID = "55555555-5555-5555-5555-555555555555"


async def test_note_save_create(mcp_call, mock_client) -> None:
    # notes.create returns a typed Note (the facade trusts the contract and
    # reads note.id — no raw RPC-shape extraction above the facade).
    mock_client.notes.create = AsyncMock(
        return_value=FakeNote(id=NOTE_ID, title="Idea", content="body")
    )
    result = await mcp_call("note_save", {"notebook": NB_ID, "title": "Idea", "content": "body"})
    assert result.structured_content == {
        "status": "created",
        "notebook_id": NB_ID,
        "title": "Idea",
        "note_id": NOTE_ID,
        "created": True,
    }
    mock_client.notes.create.assert_awaited_once_with(NB_ID, "Idea", "body")


@pytest.mark.parametrize("payload", [{"title": "T"}, {"content": "C"}, {}])
async def test_note_save_create_requires_title_and_content(mcp_call, mock_client, payload) -> None:
    """Create mode (no ``note``) requires BOTH title and content; else a clean error."""
    mock_client.notes.create = AsyncMock()
    with pytest.raises(ToolError) as exc:
        await mcp_call("note_save", {"notebook": NB_ID, **payload})
    assert "required to create" in str(exc.value)
    mock_client.notes.create.assert_not_called()


async def test_note_save_update(mcp_call, mock_client) -> None:
    mock_client.notes.update = AsyncMock(return_value=None)
    result = await mcp_call(
        "note_save", {"notebook": NB_ID, "note": NOTE_ID, "content": "new body"}
    )
    assert result.structured_content == {
        "status": "updated",
        "notebook_id": NB_ID,
        "note_id": NOTE_ID,
    }
    mock_client.notes.update.assert_awaited_once_with(
        NB_ID, NOTE_ID, content="new body", title=None
    )


async def test_note_save_update_resolves_note_by_name(mcp_call, mock_client) -> None:
    """A non-id ``note`` ref resolves by exact title within the notebook."""
    mock_client.notes.list = AsyncMock(
        return_value=[FakeNote(id=NOTE_ID, title="My Note", content="x")]
    )
    mock_client.notes.update = AsyncMock(return_value=None)
    result = await mcp_call("note_save", {"notebook": NB_ID, "note": "My Note", "content": "y"})
    assert result.structured_content["note_id"] == NOTE_ID
    mock_client.notes.update.assert_awaited_once_with(NB_ID, NOTE_ID, content="y", title=None)


async def test_note_save_update_title_only_renames(mcp_call, mock_client) -> None:
    """Title-only update passes the title through (content stays None = unchanged)."""
    mock_client.notes.update = AsyncMock(return_value=None)
    result = await mcp_call("note_save", {"notebook": NB_ID, "note": NOTE_ID, "title": "Renamed"})
    assert result.structured_content == {
        "status": "updated",
        "notebook_id": NB_ID,
        "note_id": NOTE_ID,
    }
    mock_client.notes.update.assert_awaited_once_with(NB_ID, NOTE_ID, content=None, title="Renamed")


async def test_note_save_update_title_and_content(mcp_call, mock_client) -> None:
    mock_client.notes.update = AsyncMock(return_value=None)
    await mcp_call(
        "note_save",
        {"notebook": NB_ID, "note": NOTE_ID, "content": "body", "title": "T"},
    )
    mock_client.notes.update.assert_awaited_once_with(NB_ID, NOTE_ID, content="body", title="T")


async def test_note_save_update_no_fields_errors(mcp_call, mock_client) -> None:
    """Neither content nor title supplied → validation error, no RPC."""
    mock_client.notes.update = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("note_save", {"notebook": NB_ID, "note": NOTE_ID})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.notes.update.assert_not_called()


async def test_note_save_update_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise NoteNotFoundError(NOTE_ID)

    mock_client.notes.update = AsyncMock(side_effect=_raise)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("note_save", {"notebook": NB_ID, "note": NOTE_ID, "content": "z"})
    assert "NOT_FOUND" in str(excinfo.value)
