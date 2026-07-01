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
from notebooklm.types import Note  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard


@dataclass
class FakeNote:
    id: str
    title: str
    content: str = ""


NB_ID = "11111111-1111-1111-1111-111111111111"
NOTE_ID = "55555555-5555-5555-5555-555555555555"


async def test_note_create(mcp_call, mock_client) -> None:
    # notes.create returns a typed Note (the facade trusts the contract and
    # reads note.id — no raw RPC-shape extraction above the facade).
    mock_client.notes.create = AsyncMock(
        return_value=FakeNote(id=NOTE_ID, title="Idea", content="body")
    )
    result = await mcp_call("note_create", {"notebook": NB_ID, "title": "Idea", "content": "body"})
    assert result.structured_content == {
        "status": "created",
        "notebook_id": NB_ID,
        "title": "Idea",
        "note_id": NOTE_ID,
        "created": True,
    }
    mock_client.notes.create.assert_awaited_once_with(NB_ID, "Idea", "body")


async def test_note_list(mcp_call, mock_client) -> None:
    mock_client.notes.list = AsyncMock(return_value=[FakeNote(id=NOTE_ID, title="N1", content="c")])
    result = await mcp_call("note_list", {"notebook": NB_ID})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "notes": [{"id": NOTE_ID, "title": "N1", "content": "c"}],
        "total": 1,
        "offset": 0,
        "has_more": False,
    }
    mock_client.notes.list.assert_awaited_once_with(NB_ID)


async def test_note_list_single_by_ref(mcp_call, mock_client) -> None:
    """note_list(note=…) fetches one note, returned in the 1-element list shape.

    Wires get_or_none → _app.execute_note_get → serialized note. ``execute_note_get``
    isinstance-checks the real Note type, so the mock must return one.
    """
    mock_client.notes.get_or_none = AsyncMock(
        return_value=Note(id=NOTE_ID, notebook_id=NB_ID, title="N1", content="full body")
    )
    result = await mcp_call("note_list", {"notebook": NB_ID, "note": NOTE_ID})
    sc = result.structured_content
    assert sc["notebook_id"] == NB_ID
    assert (sc["total"], sc["offset"], sc["has_more"]) == (1, 0, False)
    assert len(sc["notes"]) == 1
    note = sc["notes"][0]
    assert note["id"] == NOTE_ID
    assert note["title"] == "N1"
    assert note["content"] == "full body"
    mock_client.notes.get_or_none.assert_awaited_once_with(NB_ID, NOTE_ID)
    # The list RPC is NOT used on the single-fetch path.
    mock_client.notes.list.assert_not_called()


async def test_note_list_single_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    # A concrete-but-absent id (full-UUID fast-path skips the list) reaches the
    # tool as found=False → projected as the typed NOT_FOUND error (preserves the
    # old note_get semantics: a specific-ref miss is an error, not an empty list).
    mock_client.notes.get_or_none = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("note_list", {"notebook": NB_ID, "note": NOTE_ID})
    assert "NOT_FOUND" in str(excinfo.value)


@pytest.mark.parametrize(("arg", "value"), [("limit", 0), ("offset", -1)])
async def test_note_list_single_still_validates_pagination(
    mcp_call, mock_client, arg, value
) -> None:
    """`limit`/`offset` are validated even with `note` set (where they're ignored)."""
    mock_client.notes.get_or_none = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("note_list", {"notebook": NB_ID, "note": NOTE_ID, arg: value})
    assert arg in str(excinfo.value)
    mock_client.notes.get_or_none.assert_not_called()


async def test_note_update(mcp_call, mock_client) -> None:
    mock_client.notes.update = AsyncMock(return_value=None)
    result = await mcp_call(
        "note_update", {"notebook": NB_ID, "note": NOTE_ID, "content": "new body"}
    )
    assert result.structured_content == {
        "status": "updated",
        "notebook_id": NB_ID,
        "note_id": NOTE_ID,
    }
    mock_client.notes.update.assert_awaited_once_with(
        NB_ID, NOTE_ID, content="new body", title=None
    )


async def test_note_update_resolves_note_by_name(mcp_call, mock_client) -> None:
    """A non-id ``note`` ref resolves by exact title within the notebook."""
    mock_client.notes.list = AsyncMock(
        return_value=[FakeNote(id=NOTE_ID, title="My Note", content="x")]
    )
    mock_client.notes.update = AsyncMock(return_value=None)
    result = await mcp_call("note_update", {"notebook": NB_ID, "note": "My Note", "content": "y"})
    assert result.structured_content["note_id"] == NOTE_ID
    mock_client.notes.update.assert_awaited_once_with(NB_ID, NOTE_ID, content="y", title=None)


async def test_note_update_title_only_renames(mcp_call, mock_client) -> None:
    """Title-only update passes the title through (content stays None = unchanged)."""
    mock_client.notes.update = AsyncMock(return_value=None)
    result = await mcp_call("note_update", {"notebook": NB_ID, "note": NOTE_ID, "title": "Renamed"})
    assert result.structured_content == {
        "status": "updated",
        "notebook_id": NB_ID,
        "note_id": NOTE_ID,
    }
    mock_client.notes.update.assert_awaited_once_with(NB_ID, NOTE_ID, content=None, title="Renamed")


async def test_note_update_title_and_content(mcp_call, mock_client) -> None:
    mock_client.notes.update = AsyncMock(return_value=None)
    await mcp_call(
        "note_update",
        {"notebook": NB_ID, "note": NOTE_ID, "content": "body", "title": "T"},
    )
    mock_client.notes.update.assert_awaited_once_with(NB_ID, NOTE_ID, content="body", title="T")


async def test_note_update_no_fields_errors(mcp_call, mock_client) -> None:
    """Neither content nor title supplied → validation error, no RPC."""
    mock_client.notes.update = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("note_update", {"notebook": NB_ID, "note": NOTE_ID})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.notes.update.assert_not_called()


async def test_note_delete_without_confirm_previews(mcp_call, mock_client) -> None:
    mock_client.notes.list = AsyncMock(
        return_value=[FakeNote(id=NOTE_ID, title="Doomed", content="c")]
    )
    mock_client.notes.delete = AsyncMock(return_value=None)
    result = await mcp_call("note_delete", {"notebook": NB_ID, "note": NOTE_ID})
    assert result.structured_content == {
        "status": "needs_confirmation",
        "preview": {
            "action": "delete_note",
            "notebook_id": NB_ID,
            "note_id": NOTE_ID,
            "title": "Doomed",
        },
    }
    mock_client.notes.delete.assert_not_called()


async def test_note_delete_with_confirm_deletes(mcp_call, mock_client) -> None:
    mock_client.notes.delete = AsyncMock(return_value=None)
    result = await mcp_call("note_delete", {"notebook": NB_ID, "note": NOTE_ID, "confirm": True})
    assert result.structured_content == {
        "status": "deleted",
        "notebook_id": NB_ID,
        "note_id": NOTE_ID,
    }
    mock_client.notes.delete.assert_awaited_once_with(NB_ID, NOTE_ID)


async def test_note_delete_confirm_preview_then_delete(mcp_call, mock_client) -> None:
    """Two-step flow: preview by name first, then the confirmed delete runs."""
    mock_client.notes.list = AsyncMock(
        return_value=[FakeNote(id=NOTE_ID, title="Target", content="c")]
    )
    mock_client.notes.delete = AsyncMock(return_value=None)

    preview = await mcp_call("note_delete", {"notebook": NB_ID, "note": "Target"})
    assert preview.structured_content["status"] == "needs_confirmation"
    assert preview.structured_content["preview"]["note_id"] == NOTE_ID
    mock_client.notes.delete.assert_not_called()

    confirmed = await mcp_call(
        "note_delete", {"notebook": NB_ID, "note": "Target", "confirm": True}
    )
    assert confirmed.structured_content == {
        "status": "deleted",
        "notebook_id": NB_ID,
        "note_id": NOTE_ID,
    }
    mock_client.notes.delete.assert_awaited_once_with(NB_ID, NOTE_ID)


async def test_note_update_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise NoteNotFoundError(NOTE_ID)

    mock_client.notes.update = AsyncMock(side_effect=_raise)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("note_update", {"notebook": NB_ID, "note": NOTE_ID, "content": "z"})
    assert "NOT_FOUND" in str(excinfo.value)
