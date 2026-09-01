"""Unit tests for the Play Books MCP tools (#2292).

Exercised through the in-memory FastMCP client against a mocked ``NotebookLMClient``
whose ``sources.list_play_books`` / ``sources.add_play_book`` are stubbed.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

pytest.importorskip("fastmcp")

from notebooklm.exceptions import (  # noqa: E402 - after importorskip guard
    PlayBookNotExportableError,
    UnsupportedOperationError,
)
from notebooklm.types import (  # noqa: E402 - after importorskip guard
    PlayBook,
    SourceStatus,
    SourceType,
)

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
SRC_ID = "44444444-4444-4444-4444-444444444444"


@dataclass
class FakeSource:
    id: str
    title: str | None = None

    @property
    def kind(self) -> SourceType:
        return SourceType.EXPERT_INTELLIGENCE

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.READY

    @property
    def drive_status(self):  # noqa: ANN201 - fake
        return None

    @property
    def is_drive_degraded(self) -> bool:
        return False


def _book(content_id: str, *, disabled: bool = False) -> PlayBook:
    return PlayBook(
        content_id=content_id,
        title="The Art of War",
        authors=("Sun Tzu",),
        description_html="<p>…</p>",
        cover_url="https://cover",
        export_disabled=disabled,
        reason=None,
        field_type=4.6,
        updated_at=None,
    )


async def test_list_play_books(mcp_call, mock_client) -> None:
    mock_client.sources.list_play_books = AsyncMock(
        return_value=[_book("QhsZEAAAQBAJ"), _book("kLrxEQAAQBAJ", disabled=True)]
    )
    result = await mcp_call("source_list_play_books", {})
    payload = result.structured_content
    assert payload["total"] == 2
    assert [b["content_id"] for b in payload["play_books"]] == ["QhsZEAAAQBAJ", "kLrxEQAAQBAJ"]
    assert payload["play_books"][0]["store_url"].startswith("https://play.google.com/store/books")


async def test_add_play_book_happy_path(mcp_call, mock_client) -> None:
    mock_client.sources.add_play_book = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="The Art of War")
    )
    result = await mcp_call(
        "source_add_play_book",
        {"notebook": NB_ID, "content_id": "QhsZEAAAQBAJ", "wait": True},
    )
    payload = result.structured_content
    assert payload["status"] == "added"
    assert payload["notebook_id"] == NB_ID
    assert payload["content_id"] == "QhsZEAAAQBAJ"
    assert payload["source"]["kind"] == "expert_intelligence"
    mock_client.sources.add_play_book.assert_awaited_once_with(
        NB_ID, "QhsZEAAAQBAJ", wait=True, wait_timeout=120.0
    )


async def test_add_play_book_refusal_is_tool_error(mcp_call, mock_client) -> None:
    from fastmcp.exceptions import ToolError

    mock_client.sources.add_play_book = AsyncMock(
        side_effect=PlayBookNotExportableError("kLrxEQAAQBAJ")
    )
    with pytest.raises(ToolError):
        await mcp_call(
            "source_add_play_book",
            {"notebook": NB_ID, "content_id": "kLrxEQAAQBAJ"},
        )


async def test_list_play_books_android_unsupported_is_tool_error(mcp_call, mock_client) -> None:
    from fastmcp.exceptions import ToolError

    mock_client.sources.list_play_books = AsyncMock(
        side_effect=UnsupportedOperationError("web only")
    )
    with pytest.raises(ToolError):
        await mcp_call("source_list_play_books", {})
