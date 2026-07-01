"""Unit tests for the research MCP tools.

Drives each tool through the in-memory FastMCP ``Client`` against a server bound
to the mocked ``NotebookLMClient``, asserting the serialized
``structured_content``. Covers each tool's happy path, name-vs-id resolution,
the start→status poll shape, the import workflow, and error projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "research-task-1"


class FakeResearchStatus(str, Enum):
    NO_RESEARCH = "no_research"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NOT_FOUND = "not_found"


@dataclass
class FakeResearchStart:
    task_id: str
    report_id: str | None = None
    notebook_id: str = NB_ID
    query: str = "q"
    mode: str = "fast"


@dataclass
class FakeSource:
    url: str
    title: str

    def to_public_dict(self) -> dict[str, str]:
        return {"url": self.url, "title": self.title}


@dataclass
class FakeResearchTask:
    status: FakeResearchStatus = FakeResearchStatus.IN_PROGRESS
    query: str = "my query"
    sources: list[FakeSource] = field(default_factory=list)
    summary: str = ""
    report: str = ""
    task_id: str = TASK_ID

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "query": self.query,
            "task_id": self.task_id,
        }


@dataclass
class FakeNotebook:
    id: str
    title: str


# ---------------------------------------------------------------------------
# research_start
# ---------------------------------------------------------------------------


async def test_research_start(mcp_call, mock_client) -> None:
    mock_client.research.start = AsyncMock(return_value=FakeResearchStart(task_id=TASK_ID))
    result = await mcp_call("research_start", {"notebook": NB_ID, "query": "quantum computing"})
    assert result.structured_content["task_id"] == TASK_ID
    mock_client.research.start.assert_awaited_once_with(NB_ID, "quantum computing", "web", "fast")


async def test_research_start_non_default_source_mode(mcp_call, mock_client) -> None:
    """Non-default but valid source/mode are forwarded (drive+fast; web+deep)."""
    mock_client.research.start = AsyncMock(return_value=FakeResearchStart(task_id=TASK_ID))
    await mcp_call(
        "research_start",
        {"notebook": NB_ID, "query": "q", "source": "drive", "mode": "fast"},
    )
    mock_client.research.start.assert_awaited_once_with(NB_ID, "q", "drive", "fast")
    mock_client.research.start.reset_mock()
    await mcp_call(
        "research_start",
        {"notebook": NB_ID, "query": "q", "source": "web", "mode": "deep"},
    )
    mock_client.research.start.assert_awaited_once_with(NB_ID, "q", "web", "deep")


async def test_research_start_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.research.start = AsyncMock(return_value=FakeResearchStart(task_id=TASK_ID))
    result = await mcp_call("research_start", {"notebook": "My Notebook", "query": "q"})
    assert result.structured_content["task_id"] == TASK_ID
    mock_client.research.start.assert_awaited_once_with(NB_ID, "q", "web", "fast")


# ---------------------------------------------------------------------------
# research_status
# ---------------------------------------------------------------------------


async def test_research_status_in_progress(mcp_call, mock_client) -> None:
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS)
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    assert result.structured_content["notebook_id"] == NB_ID
    assert result.structured_content["status"] == "in_progress"
    assert result.structured_content["kind"] == "in_progress"
    mock_client.research.poll.assert_awaited_once_with(NB_ID, None)


async def test_research_status_surfaces_task_id(mcp_call, mock_client) -> None:
    """status must surface ``task_id`` so an agent can later import that task."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED, task_id=TASK_ID)
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    assert result.structured_content["task_id"] == TASK_ID


async def test_research_status_pins_task_id_when_given(mcp_call, mock_client) -> None:
    """A supplied ``task_id`` is threaded through ``poll`` as the discriminator."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED, task_id=TASK_ID)
    )
    result = await mcp_call("research_status", {"notebook": NB_ID, "task_id": TASK_ID})
    assert result.structured_content["task_id"] == TASK_ID
    mock_client.research.poll.assert_awaited_once_with(NB_ID, TASK_ID)


async def test_research_status_completed_with_sources(mcp_call, mock_client) -> None:
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[FakeSource(url="http://a", title="A")],
        )
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    assert result.structured_content["status"] == "completed"
    assert result.structured_content["sources"] == [{"url": "http://a", "title": "A"}]


# ---------------------------------------------------------------------------
# research_import
# ---------------------------------------------------------------------------


async def test_research_import(mcp_call, mock_client) -> None:
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[FakeSource(url="http://a", title="A")],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources = AsyncMock(return_value=[{"id": "src-1", "title": "A"}])
    result = await mcp_call("research_import", {"notebook": NB_ID, "task_id": TASK_ID})
    assert result.structured_content["notebook_id"] == NB_ID
    assert result.structured_content["imported"] == [{"id": "src-1", "title": "A"}]
    # The requested task_id is threaded through ``poll`` as the discriminator so
    # the freshly-polled sources belong to that task (not the notebook's current
    # task).
    mock_client.research.poll.assert_awaited_once_with(NB_ID, TASK_ID)
    mock_client.research.import_sources.assert_awaited_once()
    called = mock_client.research.import_sources.await_args.args
    assert called[0] == NB_ID
    assert called[1] == TASK_ID


async def test_research_import_non_current_task_fails_cleanly(mcp_call, mock_client) -> None:
    """Importing a task_id that is not among the polled tasks must NOT silently
    import the current task's sources — it raises a clean error instead."""
    other_task = "research-task-OTHER"
    # Polling the notebook with the requested (non-current) task_id yields a
    # NOT_FOUND sentinel carrying the requested id and no sources.
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.NOT_FOUND,
            sources=[],
            task_id=other_task,
        )
    )
    mock_client.research.import_sources = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("research_import", {"notebook": NB_ID, "task_id": other_task})
    # A clean validation/not-found projection — never a silent cross-wire import.
    msg = str(excinfo.value)
    assert "VALIDATION" in msg or "NOT_FOUND" in msg
    mock_client.research.import_sources.assert_not_called()


# ---------------------------------------------------------------------------
# research_cancel
# ---------------------------------------------------------------------------


async def test_research_cancel(mcp_call, mock_client) -> None:
    mock_client.research.cancel = AsyncMock(return_value=None)
    result = await mcp_call("research_cancel", {"notebook": NB_ID, "run_id": TASK_ID})
    assert result.structured_content == {
        "status": "cancelled",
        "notebook_id": NB_ID,
        "run_id": TASK_ID,
        "cancelled": True,
    }
    mock_client.research.cancel.assert_awaited_once_with(NB_ID, TASK_ID)


async def test_research_cancel_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.research.cancel = AsyncMock(return_value=None)
    result = await mcp_call("research_cancel", {"notebook": "My Notebook", "run_id": TASK_ID})
    assert result.structured_content["cancelled"] is True
    mock_client.research.cancel.assert_awaited_once_with(NB_ID, TASK_ID)


async def test_research_start_then_status_poll_shape(mcp_call, mock_client) -> None:
    """start→status: start returns a task_id, status polls the notebook."""
    mock_client.research.start = AsyncMock(return_value=FakeResearchStart(task_id=TASK_ID))
    started = await mcp_call("research_start", {"notebook": NB_ID, "query": "q"})
    assert started.structured_content["task_id"] == TASK_ID

    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED)
    )
    polled = await mcp_call("research_status", {"notebook": NB_ID})
    assert polled.structured_content["status"] == "completed"


# ---------------------------------------------------------------------------
# error projection
# ---------------------------------------------------------------------------


async def test_research_start_invalid_source_rejected_at_schema(mcp_call, mock_client) -> None:
    """``source`` is a Literal — an out-of-enum value is rejected before the RPC."""
    mock_client.research.start = AsyncMock(return_value=FakeResearchStart(task_id=TASK_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("research_start", {"notebook": NB_ID, "query": "q", "source": "ftp"})
    msg = str(excinfo.value).lower()
    assert "web" in msg and "drive" in msg
    mock_client.research.start.assert_not_called()


async def test_research_start_drive_deep_rejected(mcp_call, mock_client) -> None:
    """deep mode is web-only — drive+deep is rejected at the tool boundary, no RPC."""
    mock_client.research.start = AsyncMock(return_value=FakeResearchStart(task_id=TASK_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "research_start",
            {"notebook": NB_ID, "query": "q", "source": "drive", "mode": "deep"},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.start.assert_not_called()


async def test_research_import_in_progress_refused(mcp_call, mock_client) -> None:
    """An in-progress task is not importable — refuse rather than import a partial set."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.IN_PROGRESS,
            sources=[FakeSource(url="http://a", title="A")],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("research_import", {"notebook": NB_ID, "task_id": TASK_ID})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.import_sources.assert_not_called()


async def test_research_import_completed_but_empty_refused(mcp_call, mock_client) -> None:
    """A completed task with no sources is refused (no silent empty import)."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED, sources=[], task_id=TASK_ID
        )
    )
    mock_client.research.import_sources = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("research_import", {"notebook": NB_ID, "task_id": TASK_ID})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.import_sources.assert_not_called()
