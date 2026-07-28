"""Unit tests for the ``source_add_drive_file`` MCP tool (#1884).

Exercised through the in-memory FastMCP client against a mocked ``NotebookLMClient``
whose ``sources.add_drive_file`` is stubbed — so the tool's routing echo, error
projection, and (crucially) the remote-mode contract that it NEVER emits
``upload_required`` are pinned without any live Drive access.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm._types.sources import SourceType  # noqa: E402 - after importorskip guard
from notebooklm.exceptions import ValidationError  # noqa: E402 - after importorskip guard
from notebooklm.rpc.types import SourceStatus  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
SRC_ID = "44444444-4444-4444-4444-444444444444"
FILE_ID = "1W20RJpJUD2JqXSEiM9Il48_fsdOtZ5fD"


@dataclass
class FakeSource:
    id: str
    title: str | None = None

    @property
    def is_ready(self) -> bool:
        return True

    @property
    def is_error(self) -> bool:
        return False

    @property
    def kind(self) -> SourceType:
        return SourceType.EPUB

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.READY


async def test_source_add_drive_file_happy_path(mcp_call, mock_client) -> None:
    mock_client.sources.add_drive_file = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Book"))
    result = await mcp_call(
        "source_add_drive_file",
        {"notebook": NB_ID, "document_id": FILE_ID, "title": "Book"},
    )
    assert result.structured_content == {
        "status": "added",
        "notebook_id": NB_ID,
        "document_id": FILE_ID,
        "source": {"id": SRC_ID, "title": "Book", "kind": "epub", "status_label": "ready"},
    }
    mock_client.sources.add_drive_file.assert_awaited_once_with(
        NB_ID, FILE_ID, title="Book", wait=False, wait_timeout=120.0
    )


async def test_source_add_drive_file_passes_wait(mcp_call, mock_client) -> None:
    mock_client.sources.add_drive_file = AsyncMock(return_value=FakeSource(id=SRC_ID))
    await mcp_call(
        "source_add_drive_file",
        {"notebook": NB_ID, "document_id": FILE_ID, "wait": True},
    )
    mock_client.sources.add_drive_file.assert_awaited_once_with(
        NB_ID, FILE_ID, title=None, wait=True, wait_timeout=120.0
    )


async def test_unsupported_type_projects_validation_error(mcp_call, mock_client) -> None:
    mock_client.sources.add_drive_file = AsyncMock(
        side_effect=ValidationError("HTML isn't supported by NotebookLM upload")
    )
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "source_add_drive_file",
            {"notebook": NB_ID, "document_id": FILE_ID},
        )
    assert "VALIDATION" in str(excinfo.value)


async def test_remote_mode_never_emits_upload_required(mcp_call, mock_client) -> None:
    """Unlike source_add(source_type='file'), the Drive fetch is server-side, so the
    tool returns a normal ``added`` payload with no ``upload_required`` broker dict."""
    mock_client.sources.add_drive_file = AsyncMock(return_value=FakeSource(id=SRC_ID))
    result = await mcp_call(
        "source_add_drive_file",
        {"notebook": NB_ID, "document_id": FILE_ID},
    )
    assert result.structured_content["status"] == "added"
    assert "upload_required" not in result.structured_content
