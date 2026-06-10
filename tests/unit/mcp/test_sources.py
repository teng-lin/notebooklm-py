"""Unit tests for the source MCP tools.

Drives each tool through the in-memory FastMCP ``Client`` against a server bound
to the mocked ``NotebookLMClient``, asserting the serialized
``structured_content``. Covers each tool's happy path, name-vs-id resolution
reaching the tool, the per-``type`` ``source_add`` dispatch, the confirm
preview-then-delete flow, and error projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm.exceptions import SourceNotFoundError  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard


@dataclass
class FakeSource:
    id: str
    title: str | None = None


NB_ID = "11111111-1111-1111-1111-111111111111"
SRC_ID = "33333333-3333-3333-3333-333333333333"
SRC2_ID = "44444444-4444-4444-4444-444444444444"


async def test_source_list(mcp_call, mock_client) -> None:
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Doc")])
    result = await mcp_call("source_list", {"notebook": NB_ID})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "sources": [{"id": SRC_ID, "title": "Doc"}],
    }
    mock_client.sources.list.assert_awaited_once_with(NB_ID)


async def test_source_list_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    @dataclass
    class FakeNotebook:
        id: str
        title: str

    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.sources.list = AsyncMock(return_value=[])
    result = await mcp_call("source_list", {"notebook": "My Notebook"})
    assert result.structured_content["notebook_id"] == NB_ID
    mock_client.sources.list.assert_awaited_with(NB_ID)


async def test_source_get_content(mcp_call, mock_client) -> None:
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    result = await mcp_call("source_get_content", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "source_id": SRC_ID,
        "source": {"id": SRC_ID, "title": "Doc"},
    }
    mock_client.sources.get_or_none.assert_awaited_once_with(NB_ID, SRC_ID)


async def test_source_get_content_resolves_source_by_name(mcp_call, mock_client) -> None:
    """A non-id ``source`` ref resolves by exact title within the notebook."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Paper")])
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Paper"))
    result = await mcp_call("source_get_content", {"notebook": NB_ID, "source": "Paper"})
    assert result.structured_content["source_id"] == SRC_ID
    mock_client.sources.get_or_none.assert_awaited_once_with(NB_ID, SRC_ID)


async def test_source_rename(mcp_call, mock_client) -> None:
    mock_client.sources.rename = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Renamed"))
    result = await mcp_call(
        "source_rename", {"notebook": NB_ID, "source": SRC_ID, "new_title": "Renamed"}
    )
    assert result.structured_content == {
        "source": {"id": SRC_ID, "title": "Renamed"},
        "notebook_id": NB_ID,
    }
    mock_client.sources.rename.assert_awaited_once_with(NB_ID, SRC_ID, "Renamed")


async def test_source_delete_without_confirm_previews(mcp_call, mock_client) -> None:
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID, title="Doomed")])
    mock_client.sources.delete = AsyncMock(return_value=None)
    result = await mcp_call("source_delete", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content == {
        "status": "needs_confirmation",
        "preview": {
            "action": "delete_source",
            "notebook_id": NB_ID,
            "source_id": SRC_ID,
            "title": "Doomed",
        },
    }
    mock_client.sources.delete.assert_not_called()


async def test_source_delete_with_confirm_deletes(mcp_call, mock_client) -> None:
    mock_client.sources.delete = AsyncMock(return_value=None)
    result = await mcp_call("source_delete", {"notebook": NB_ID, "source": SRC_ID, "confirm": True})
    assert result.structured_content == {
        "status": "deleted",
        "notebook_id": NB_ID,
        "source_id": SRC_ID,
    }
    mock_client.sources.delete.assert_awaited_once_with(NB_ID, SRC_ID)


async def test_source_wait_all_sources(mcp_call, mock_client) -> None:
    mock_client.sources.list = AsyncMock(
        return_value=[FakeSource(id=SRC_ID), FakeSource(id=SRC2_ID)]
    )
    mock_client.sources.wait_for_sources = AsyncMock(
        return_value=[FakeSource(id=SRC_ID, title="A"), FakeSource(id=SRC2_ID, title="B")]
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "ready": [{"id": SRC_ID, "title": "A"}, {"id": SRC2_ID, "title": "B"}],
    }
    mock_client.sources.wait_for_sources.assert_awaited_once_with(
        NB_ID, [SRC_ID, SRC2_ID], timeout=120.0, initial_interval=1.0
    )


async def test_source_wait_all_sources_forwards_interval(mcp_call, mock_client) -> None:
    """The all-sources branch honors the advertised ``interval`` (was dropped)."""
    mock_client.sources.list = AsyncMock(return_value=[FakeSource(id=SRC_ID)])
    mock_client.sources.wait_for_sources = AsyncMock(
        return_value=[FakeSource(id=SRC_ID, title="A")]
    )
    await mcp_call("source_wait", {"notebook": NB_ID, "timeout": 30.0, "interval": 3.0})
    mock_client.sources.wait_for_sources.assert_awaited_once_with(
        NB_ID, [SRC_ID], timeout=30.0, initial_interval=3.0
    )


async def test_source_wait_single_source_ready(mcp_call, mock_client) -> None:
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Ready")
    )
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "status": "ready",
        "source": {"id": SRC_ID, "title": "Ready"},
    }


async def test_source_wait_single_source_not_found(mcp_call, mock_client) -> None:
    mock_client.sources.wait_until_ready = AsyncMock(side_effect=SourceNotFoundError(SRC_ID))
    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})
    assert result.structured_content["status"] == "not_found"


async def test_source_add_text(mcp_call, mock_client) -> None:
    mock_client.sources.add_text = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Notes"))
    result = await mcp_call(
        "source_add",
        {"notebook": NB_ID, "source_type": "text", "text": "hello world", "title": "Notes"},
    )
    assert result.structured_content == {"source": {"id": SRC_ID, "title": "Notes"}}
    mock_client.sources.add_text.assert_awaited_once_with(NB_ID, "Notes", "hello world")


async def test_source_add_url(mcp_call, mock_client) -> None:
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Page"))
    result = await mcp_call(
        "source_add", {"notebook": NB_ID, "source_type": "url", "url": "https://example.com/a"}
    )
    assert result.structured_content == {"source": {"id": SRC_ID, "title": "Page"}}
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, "https://example.com/a")


async def test_source_add_drive(mcp_call, mock_client) -> None:
    mock_client.sources.add_drive = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Sheet"))
    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "drive",
            "document_id": "drivefile123",
            "title": "Sheet",
            "mime_type": "google-sheets",
        },
    )
    # SourceAddDriveResult carries the source plus the drive provenance fields.
    assert result.structured_content == {
        "source": {"id": SRC_ID, "title": "Sheet"},
        "notebook_id": NB_ID,
        "file_id": "drivefile123",
        "mime_type": "google-sheets",
    }
    mock_client.sources.add_drive.assert_awaited_once()
    called_args = mock_client.sources.add_drive.await_args.args
    assert called_args[0] == NB_ID
    assert called_args[1] == "drivefile123"


async def test_source_add_missing_input_is_validation_error(mcp_call, mock_client) -> None:
    """type=url with no url projects as a VALIDATION ToolError."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_add", {"notebook": NB_ID, "source_type": "url"})
    assert "VALIDATION" in str(excinfo.value)


async def test_source_add_drive_bad_mime_is_validation_error(mcp_call, mock_client) -> None:
    """A bogus drive mime_type projects as VALIDATION (not UNEXPECTED)."""
    mock_client.sources.add_drive = AsyncMock(return_value=FakeSource(id=SRC_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {
                "notebook": NB_ID,
                "source_type": "drive",
                "document_id": "drivefile123",
                "mime_type": "bogus",
            },
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_drive.assert_not_called()


async def test_source_get_content_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise SourceNotFoundError(SRC_ID)

    mock_client.sources.get_or_none = AsyncMock(side_effect=_raise)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_get_content", {"notebook": NB_ID, "source": SRC_ID})
    assert "NOT_FOUND" in str(excinfo.value)


async def test_source_get_content_missing_full_uuid_projects_not_found(
    mcp_call, mock_client
) -> None:
    """A full-UUID ref skips list resolution; a None get_or_none must NOT return
    {"source": null} as success — it projects NOT_FOUND."""
    mock_client.sources.get_or_none = AsyncMock(return_value=None)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("source_get_content", {"notebook": NB_ID, "source": SRC_ID})
    assert "NOT_FOUND" in str(excinfo.value)


async def test_source_add_youtube_rejects_non_youtube_url(mcp_call, mock_client) -> None:
    """type=youtube with a non-YouTube URL projects as VALIDATION."""
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Page"))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add",
            {"notebook": NB_ID, "source_type": "youtube", "url": "https://example.com/not-yt"},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.sources.add_url.assert_not_called()


async def test_source_add_youtube_accepts_youtube_url(mcp_call, mock_client) -> None:
    """type=youtube with a genuine YouTube URL is accepted."""
    yt = "https://www.youtube.com/watch?v=abc123"
    mock_client.sources.add_url = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Vid"))
    result = await mcp_call("source_add", {"notebook": NB_ID, "source_type": "youtube", "url": yt})
    assert result.structured_content == {"source": {"id": SRC_ID, "title": "Vid"}}
    mock_client.sources.add_url.assert_awaited_once_with(NB_ID, yt)
