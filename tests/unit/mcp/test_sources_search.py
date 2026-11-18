"""Unit tests for the ``source_search`` MCP tool."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm.types import RelevantChunk  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
SRC_A = "22222222-2222-2222-2222-222222222222"
SRC_B = "33333333-3333-3333-3333-333333333333"


@dataclass
class _FakeNotebook:
    id: str
    title: str


@dataclass
class _FakeSource:
    id: str
    title: str


def _chunk(
    source_id: str, text: str, rank: int, start: int | None = 10, end: int | None = 42
) -> RelevantChunk:
    return RelevantChunk(source_id=source_id, text=text, rank=rank, start=start, end=end)


async def test_source_search_all_sources(mcp_call, mock_client) -> None:
    mock_client.sources.search = AsyncMock(
        return_value=[
            _chunk(SRC_A, "Revenue grew 18% year over year.", 1, 10, 42),
            _chunk(SRC_B, "Operating margin expanded.", 2, None, None),
        ]
    )

    result = await mcp_call("source_search", {"notebook": NB_ID, "query": "  revenue growth  "})
    payload = result.structured_content
    assert payload == {
        "notebook_id": NB_ID,
        "query": "revenue growth",
        "count": 2,
        "chunks": [
            {
                "source_id": SRC_A,
                "text": "Revenue grew 18% year over year.",
                "rank": 1,
                "start": 10,
                "end": 42,
            },
            {
                "source_id": SRC_B,
                "text": "Operating margin expanded.",
                "rank": 2,
                "start": None,
                "end": None,
            },
        ],
    }
    mock_client.sources.search.assert_awaited_once_with(
        NB_ID,
        "revenue growth",
        source_ids=None,
        limit=None,
    )


async def test_source_search_resolves_notebook_and_sources_with_limit(
    mcp_call, mock_client
) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[_FakeNotebook(id=NB_ID, title="Earnings")])
    mock_client.sources.list = AsyncMock(
        return_value=[
            _FakeSource(id=SRC_A, title="Q1 Filing"),
            _FakeSource(id=SRC_B, title="Q2 Call Transcript"),
        ]
    )
    mock_client.sources.search = AsyncMock(
        return_value=[_chunk(SRC_A, "Q1 revenue segment breakdown.", 1)]
    )

    result = await mcp_call(
        "source_search",
        {
            "notebook": "Earnings",
            "query": "segment breakdown",
            "source_ids": "Q1 Filing, 33333333",
            "limit": 5,
        },
    )
    payload = result.structured_content
    assert payload["notebook_id"] == NB_ID
    assert payload["source_ids"] == [SRC_A, SRC_B]
    assert payload["count"] == 1
    mock_client.sources.search.assert_awaited_once_with(
        NB_ID,
        "segment breakdown",
        source_ids=[SRC_A, SRC_B],
        limit=5,
    )


@pytest.mark.parametrize("bad_query", ["", "   "])
async def test_source_search_rejects_blank_query(bad_query: str, mcp_call, mock_client) -> None:
    mock_client.sources.search = AsyncMock()
    with pytest.raises(ToolError, match="query must be a non-empty string"):
        await mcp_call("source_search", {"notebook": NB_ID, "query": bad_query})
    mock_client.sources.search.assert_not_awaited()


@pytest.mark.parametrize("bad_limit", [0, -3])
async def test_source_search_rejects_non_positive_limit(
    bad_limit: int, mcp_call, mock_client
) -> None:
    mock_client.sources.search = AsyncMock()
    with pytest.raises(ToolError, match="limit must be a positive integer"):
        await mcp_call(
            "source_search",
            {"notebook": NB_ID, "query": "revenue", "limit": bad_limit},
        )
    mock_client.sources.search.assert_not_awaited()
