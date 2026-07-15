"""Client-visible JSON-first contract tests for source read/wait MCP tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from notebooklm._types.sources import SourceType  # noqa: E402 - after importorskip guard
from notebooklm.exceptions import SourceTimeoutError  # noqa: E402 - after importorskip guard
from notebooklm.rpc.types import SourceStatus  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
SRC_ID = "22222222-2222-2222-2222-222222222222"


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
        return SourceType.PASTED_TEXT

    @property
    def status(self) -> SourceStatus:
        return SourceStatus.READY


@dataclass
class FakeFulltext:
    content: str
    char_count: int


@dataclass
class FakeGuide:
    summary: str
    keywords: list[str]


def assert_json_first_content(result: Any) -> None:
    """The model-visible content block is one parseable JSON text block."""
    assert len(result.content) == 1
    block = result.content[0]
    assert block.type == "text"
    assert block.model_dump(mode="json", exclude_none=True) == {
        "type": "text",
        "text": block.text,
    }
    assert json.loads(block.text) == result.structured_content


async def test_source_read_full_is_json_first(mcp_call, mock_client) -> None:
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_fulltext = AsyncMock(
        return_value=FakeFulltext(content="hello world", char_count=11)
    )

    result = await mcp_call("source_read", {"notebook": NB_ID, "source": SRC_ID})

    assert result.structured_content == {
        "notebook_id": NB_ID,
        "source_id": SRC_ID,
        "source": {
            "id": SRC_ID,
            "title": "Doc",
            "kind": "pasted_text",
            "status_label": "ready",
        },
        "content": "hello world",
        "char_count": 11,
        "truncated": False,
        "output_format": "text",
    }
    assert_json_first_content(result)


async def test_source_read_summary_is_json_first(mcp_call, mock_client) -> None:
    mock_client.sources.get_or_none = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Doc"))
    mock_client.sources.get_guide = AsyncMock(
        return_value=FakeGuide(summary="Short digest", keywords=["alpha", "beta"])
    )

    result = await mcp_call(
        "source_read",
        {"notebook": NB_ID, "source": SRC_ID, "detail": "summary"},
    )

    assert result.structured_content == {
        "notebook_id": NB_ID,
        "source_id": SRC_ID,
        "summary": "Short digest",
        "keywords": ["alpha", "beta"],
    }
    assert_json_first_content(result)


async def test_source_wait_is_json_first(mcp_call, mock_client) -> None:
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Doc")
    )

    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})

    assert result.structured_content == {
        "notebook_id": NB_ID,
        "ok": True,
        "ready": [{"id": SRC_ID, "title": "Doc", "kind": "pasted_text", "status_label": "ready"}],
        "timed_out": [],
        "failed": [],
        "not_found": [],
        "ready_count": 1,
        "timed_out_count": 0,
        "failed_count": 0,
        "not_found_count": 0,
        "total_count": 1,
    }
    assert_json_first_content(result)


async def test_source_wait_timeout_is_json_first(mcp_call, mock_client) -> None:
    err = SourceTimeoutError(SRC_ID, 5.0, last_status=1)
    mock_client.sources.wait_until_ready = AsyncMock(side_effect=err)

    result = await mcp_call("source_wait", {"notebook": NB_ID, "source": SRC_ID})

    assert result.structured_content == {
        "notebook_id": NB_ID,
        "ok": False,
        "ready": [],
        "timed_out": [{"source_id": SRC_ID, "error": str(err)}],
        "failed": [],
        "not_found": [],
        "ready_count": 0,
        "timed_out_count": 1,
        "failed_count": 0,
        "not_found_count": 0,
        "total_count": 1,
    }
    assert_json_first_content(result)


async def test_source_add_wait_is_json_first(mcp_call, mock_client) -> None:
    mock_client.sources.add_text = AsyncMock(return_value=FakeSource(id=SRC_ID, title="Notes"))
    mock_client.sources.wait_until_ready = AsyncMock(
        return_value=FakeSource(id=SRC_ID, title="Notes")
    )

    result = await mcp_call(
        "source_add",
        {
            "notebook": NB_ID,
            "source_type": "text",
            "text": "hello world",
            "title": "Notes",
            "wait": True,
        },
    )

    assert result.structured_content == {
        "notebook_id": NB_ID,
        "ok": True,
        "ready": [{"id": SRC_ID, "title": "Notes", "kind": "pasted_text", "status_label": "ready"}],
        "timed_out": [],
        "failed": [],
        "not_found": [],
        "ready_count": 1,
        "timed_out_count": 0,
        "failed_count": 0,
        "not_found_count": 0,
        "total_count": 1,
        "source_id": SRC_ID,
    }
    assert_json_first_content(result)
