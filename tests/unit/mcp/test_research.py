"""Unit tests for the research MCP tools.

Drives each tool through the in-memory FastMCP ``Client`` against a server bound
to the mocked ``NotebookLMClient``, asserting the serialized
``structured_content``. Covers each tool's happy path, name-vs-id resolution,
the start→status poll shape, the import workflow, and error projection.
"""

from __future__ import annotations

import warnings
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
    FAILED = "failed"
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
    report_markdown: str = ""

    def to_public_dict(self) -> dict[str, str]:
        # Mirror the real ``ResearchSource.to_public_dict``: ``report_markdown``
        # is only emitted when truthy.
        public = {"url": self.url, "title": self.title}
        if self.report_markdown:
            public["report_markdown"] = self.report_markdown
        return public


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
    # Deep runs return a report_id (fast/drive does not) — supply it so the
    # deep-mode poll_task_id guard is satisfied.
    mock_client.research.start = AsyncMock(
        return_value=FakeResearchStart(task_id=TASK_ID, report_id="report-1", mode="deep")
    )
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


async def test_research_start_surfaces_poll_task_id_fast(mcp_call, mock_client) -> None:
    """Fast runs poll under task_id → poll_task_id mirrors it."""
    mock_client.research.start = AsyncMock(
        return_value=FakeResearchStart(task_id=TASK_ID, report_id=None)
    )
    result = await mcp_call("research_start", {"notebook": NB_ID, "query": "q"})
    sc = result.structured_content
    assert sc["poll_task_id"] == TASK_ID
    # The raw start fields are still present (purely additive).
    assert sc["task_id"] == TASK_ID
    assert sc["report_id"] is None


async def test_research_start_poll_task_id_prefers_report_id_deep(mcp_call, mock_client) -> None:
    """Deep runs poll under report_id — poll_task_id must be report_id, not the
    (unpollable) sessionId task_id."""
    mock_client.research.start = AsyncMock(
        return_value=FakeResearchStart(task_id="session-x", report_id="report-y", mode="deep")
    )
    result = await mcp_call(
        "research_start", {"notebook": NB_ID, "query": "q", "source": "web", "mode": "deep"}
    )
    assert result.structured_content["poll_task_id"] == "report-y"


async def test_research_start_fast_ignores_report_id(mcp_call, mock_client) -> None:
    """poll_task_id is mode-chosen: a fast run uses task_id even if the backend
    ever set a report_id (never `report_id or task_id`)."""
    mock_client.research.start = AsyncMock(
        return_value=FakeResearchStart(task_id=TASK_ID, report_id="stray-report", mode="fast")
    )
    result = await mcp_call("research_start", {"notebook": NB_ID, "query": "q", "mode": "fast"})
    assert result.structured_content["poll_task_id"] == TASK_ID


async def test_research_start_deep_without_report_id_rejected(mcp_call, mock_client) -> None:
    """Deep start with no report_id is unpollable — reject rather than hand back
    the sessionId trap."""
    mock_client.research.start = AsyncMock(
        return_value=FakeResearchStart(task_id="session-x", report_id=None, mode="deep")
    )
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "research_start", {"notebook": NB_ID, "query": "q", "source": "web", "mode": "deep"}
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    # The raw session id is surfaced for traceability (the run started server-side).
    assert "session-x" in msg


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


async def test_research_status_pins_poll_task_id_when_given(mcp_call, mock_client) -> None:
    """A supplied ``poll_task_id`` is threaded through ``poll`` as the discriminator."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED, task_id=TASK_ID)
    )
    result = await mcp_call("research_status", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    assert result.structured_content["task_id"] == TASK_ID
    # No deprecated alias was used → no deprecation note.
    assert "deprecation" not in result.structured_content
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


async def test_research_status_omits_report_by_default(mcp_call, mock_client) -> None:
    """The report is omitted by default; its size is surfaced and, because a
    report exists but isn't returned, report_truncated is True."""
    long_report = "x" * 500
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED, report=long_report, task_id=TASK_ID
        )
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    sc = result.structured_content
    assert sc["report"] is None
    assert sc["report_char_count"] == 500
    assert sc["report_truncated"] is True
    assert sc["poll_task_id"] == TASK_ID


async def test_research_status_no_report_not_truncated(mcp_call, mock_client) -> None:
    """When there is no report at all, report_truncated is False (nothing omitted)."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS, report="")
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    sc = result.structured_content
    assert sc["report"] is None
    assert sc["report_char_count"] == 0
    assert sc["report_truncated"] is False


async def test_research_status_strips_report_markdown_by_default(mcp_call, mock_client) -> None:
    """A report source's report_markdown must NOT leak when include_report=False."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[FakeSource(url="http://r", title="R", report_markdown="B" * 400)],
        )
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    src = result.structured_content["sources"][0]
    assert "report_markdown" not in src
    assert src == {"url": "http://r", "title": "R"}


async def test_research_status_include_report_truncates(mcp_call, mock_client) -> None:
    """include_report=True truncates both the report and source report_markdown."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            report="R" * 50,
            sources=[FakeSource(url="http://r", title="R", report_markdown="M" * 50)],
        )
    )
    result = await mcp_call(
        "research_status",
        {"notebook": NB_ID, "include_report": True, "report_max_chars": 10},
    )
    sc = result.structured_content
    assert sc["report"] == "R" * 10
    assert sc["report_truncated"] is True
    assert sc["report_char_count"] == 50
    assert sc["sources"][0]["report_markdown"] == "M" * 10


async def test_research_status_include_report_full(mcp_call, mock_client) -> None:
    """A short report is returned whole and not marked truncated."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED, report="short")
    )
    result = await mcp_call("research_status", {"notebook": NB_ID, "include_report": True})
    sc = result.structured_content
    assert sc["report"] == "short"
    assert sc["report_truncated"] is False


async def test_research_status_paginates_sources(mcp_call, mock_client) -> None:
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[
                FakeSource(url="http://a", title="A"),
                FakeSource(url="http://b", title="B"),
                FakeSource(url="http://c", title="C"),
            ],
        )
    )
    result = await mcp_call(
        "research_status", {"notebook": NB_ID, "source_limit": 1, "source_offset": 1}
    )
    sc = result.structured_content
    assert sc["sources_total"] == 3
    assert sc["sources_returned"] == 1
    assert sc["sources_offset"] == 1
    assert sc["sources"] == [{"url": "http://b", "title": "B"}]


async def test_research_status_source_limit_zero(mcp_call, mock_client) -> None:
    """source_limit=0 returns no rows but still reports the total."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[
                FakeSource(url="http://a", title="A"),
                FakeSource(url="http://b", title="B"),
                FakeSource(url="http://c", title="C"),
            ],
        )
    )
    result = await mcp_call("research_status", {"notebook": NB_ID, "source_limit": 0})
    sc = result.structured_content
    assert sc["sources"] == []
    assert sc["sources_total"] == 3
    assert sc["sources_returned"] == 0


@pytest.mark.parametrize(
    "bad_args",
    [
        {"report_max_chars": 0},
        {"source_limit": -1},
        {"source_offset": -1},
        {"poll_task_id": "  "},
        {"poll_task_id": ""},
    ],
)
async def test_research_status_rejects_bad_bounds(bad_args, mcp_call, mock_client) -> None:
    """Windowing/pin bounds are validated BEFORE the poll (no wasted RPC)."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED)
    )
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("research_status", {"notebook": NB_ID, **bad_args})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.poll.assert_not_called()


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
    result = await mcp_call("research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    assert result.structured_content["notebook_id"] == NB_ID
    assert result.structured_content["imported"] == [{"id": "src-1", "title": "A"}]
    # The response echoes the id under both the canonical and legacy keys.
    assert result.structured_content["poll_task_id"] == TASK_ID
    assert result.structured_content["task_id"] == TASK_ID
    assert "deprecation" not in result.structured_content
    # The requested id is threaded through ``poll`` as the discriminator so the
    # freshly-polled sources belong to that task (not the notebook's current task).
    mock_client.research.poll.assert_awaited_once_with(NB_ID, TASK_ID)
    mock_client.research.import_sources.assert_awaited_once()
    called = mock_client.research.import_sources.await_args.args
    assert called[0] == NB_ID
    assert called[1] == TASK_ID


async def test_research_import_empty_poll_task_id_rejected(mcp_call, mock_client) -> None:
    """An empty/whitespace poll_task_id is rejected before any poll or import (the
    falsy-id unfiltered-poll cross-wire trap)."""
    mock_client.research.poll = AsyncMock(return_value=FakeResearchTask())
    mock_client.research.import_sources = AsyncMock(return_value=[])
    for bad in ("", "   "):
        with pytest.raises(ToolError) as excinfo:
            await mcp_call("research_import", {"notebook": NB_ID, "poll_task_id": bad})
        assert "VALIDATION" in str(excinfo.value)
    # Omitting the id entirely is likewise rejected (neither canonical nor alias).
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("research_import", {"notebook": NB_ID})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.poll.assert_not_called()
    mock_client.research.import_sources.assert_not_called()


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
        await mcp_call("research_import", {"notebook": NB_ID, "poll_task_id": other_task})
    # A clean validation/not-found projection — never a silent cross-wire import.
    msg = str(excinfo.value)
    assert "VALIDATION" in msg or "NOT_FOUND" in msg
    mock_client.research.import_sources.assert_not_called()


# ---------------------------------------------------------------------------
# research_cancel
# ---------------------------------------------------------------------------


async def test_research_cancel(mcp_call, mock_client) -> None:
    """An in-progress run is preflighted then cancelled."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS, task_id=TASK_ID)
    )
    mock_client.research.cancel = AsyncMock(return_value=None)
    result = await mcp_call("research_cancel", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    assert result.structured_content == {
        "status": "cancel_requested",
        "notebook_id": NB_ID,
        "poll_task_id": TASK_ID,
        "run_id": TASK_ID,
        "cancel_requested": True,
        "run_status_before": "in_progress",
    }
    mock_client.research.poll.assert_awaited_once_with(NB_ID, TASK_ID)
    mock_client.research.cancel.assert_awaited_once_with(NB_ID, TASK_ID)


async def test_research_cancel_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS, task_id=TASK_ID)
    )
    mock_client.research.cancel = AsyncMock(return_value=None)
    result = await mcp_call("research_cancel", {"notebook": "My Notebook", "poll_task_id": TASK_ID})
    assert result.structured_content["cancel_requested"] is True
    mock_client.research.cancel.assert_awaited_once_with(NB_ID, TASK_ID)


@pytest.mark.parametrize("status", [FakeResearchStatus.NOT_FOUND, FakeResearchStatus.NO_RESEARCH])
async def test_research_cancel_absent_run_still_cancelled(status, mcp_call, mock_client) -> None:
    """A run that preflights absent (not_found / no_research) is STILL cancelled:
    a poll right after research_start can transiently miss a valid just-started
    run (replication lag), so suppressing the cancel would leave it running."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=status, task_id="just-started")
    )
    mock_client.research.cancel = AsyncMock(return_value=None)
    result = await mcp_call("research_cancel", {"notebook": NB_ID, "poll_task_id": "just-started"})
    sc = result.structured_content
    assert sc["cancel_requested"] is True
    assert sc["status"] == "cancel_requested"
    # ``run_status_before`` surfaces the unconfirmed preflight observation.
    assert sc["run_status_before"] == status.value
    mock_client.research.cancel.assert_awaited_once_with(NB_ID, "just-started")


@pytest.mark.parametrize("status", [FakeResearchStatus.COMPLETED, FakeResearchStatus.FAILED])
async def test_research_cancel_terminal_run_not_cancelled(status, mcp_call, mock_client) -> None:
    """An already-terminal run (completed/failed) is stable — cancel is a no-op,
    so don't send it; report cancel_requested=False with the observed status."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=status, task_id=TASK_ID)
    )
    mock_client.research.cancel = AsyncMock(return_value=None)
    result = await mcp_call("research_cancel", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    sc = result.structured_content
    assert sc["cancel_requested"] is False
    assert sc["status"] == status.value
    mock_client.research.cancel.assert_not_called()


async def test_research_cancel_empty_poll_task_id_rejected(mcp_call, mock_client) -> None:
    """An empty/whitespace/absent poll_task_id is rejected before any poll or cancel."""
    mock_client.research.poll = AsyncMock(return_value=FakeResearchTask())
    mock_client.research.cancel = AsyncMock(return_value=None)
    for bad in ("", "   "):
        with pytest.raises(ToolError) as excinfo:
            await mcp_call("research_cancel", {"notebook": NB_ID, "poll_task_id": bad})
        assert "VALIDATION" in str(excinfo.value)
    # Omitting the id entirely (neither canonical nor alias) is likewise rejected.
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("research_cancel", {"notebook": NB_ID})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.poll.assert_not_called()
    mock_client.research.cancel.assert_not_called()


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
        await mcp_call("research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID})
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
        await mcp_call("research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.import_sources.assert_not_called()


@pytest.mark.parametrize("status", [FakeResearchStatus.FAILED, FakeResearchStatus.NO_RESEARCH])
async def test_research_import_non_completed_status_refused(status, mcp_call, mock_client) -> None:
    """A failed / no_research task is not importable — refuse, never import."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=status,
            sources=[FakeSource(url="http://a", title="A")],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.import_sources.assert_not_called()


# ---------------------------------------------------------------------------
# deprecated id-param aliases (issue #1789): task_id / run_id → poll_task_id
#
# Each downstream tool renamed its id param to ``poll_task_id`` and keeps the old
# name as an accepted alias for one release. The alias must (a) resolve to the
# same behavior as ``poll_task_id``, (b) emit a ``DeprecationWarning``, and (c)
# surface a caller-visible ``deprecation`` note in the result. The Python-level
# ``DeprecationWarning`` is asserted directly on the resolution helper (FastMCP's
# tool-execution boundary swallows warnings, so ``pytest.warns`` around a tool
# call is unreliable); the agent-visible ``deprecation`` note is asserted through
# the full tool call.
# ---------------------------------------------------------------------------


def test_resolve_poll_task_id_alias_warns_and_notes() -> None:
    """The helper emits a ``DeprecationWarning`` and a note when only the alias is used."""
    from notebooklm.mcp.tools.research import _resolve_poll_task_id

    with pytest.warns(DeprecationWarning, match="research_import.*task_id.*poll_task_id"):
        resolved, note = _resolve_poll_task_id("research_import", "task_id", None, "abc")
    assert resolved == "abc"
    assert note is not None and "task_id" in note and "poll_task_id" in note


def test_resolve_poll_task_id_canonical_no_warning() -> None:
    """The canonical name (with no alias) warns not, notes not."""
    from notebooklm.mcp.tools.research import _resolve_poll_task_id

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        resolved, note = _resolve_poll_task_id("research_status", "task_id", "abc", None)
    assert resolved == "abc"
    assert note is None


def test_resolve_poll_task_id_same_value_prefers_canonical_no_warning() -> None:
    """Both names, same value → canonical wins silently (no warning, no note)."""
    from notebooklm.mcp.tools.research import _resolve_poll_task_id

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        resolved, note = _resolve_poll_task_id("research_cancel", "run_id", "abc", " abc ")
    assert resolved == "abc"
    assert note is None


def test_resolve_poll_task_id_blank_alias_no_warning() -> None:
    """A whitespace-only alias is handed back unwarned (the tool's empty-id guard
    rejects it) — no deprecation signal spent on a value about to be refused."""
    from notebooklm.mcp.tools.research import _resolve_poll_task_id

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        resolved, note = _resolve_poll_task_id("research_import", "task_id", None, "   ")
    assert resolved == "   "
    assert note is None


async def test_research_import_blank_alias_rejected_without_warning(mcp_call, mock_client) -> None:
    """A whitespace-only ``task_id`` alias is rejected as VALIDATION and emits no
    DeprecationWarning through the tool (the empty-id guard fires, not the alias)."""
    mock_client.research.poll = AsyncMock(return_value=FakeResearchTask())
    mock_client.research.import_sources = AsyncMock(return_value=[])
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with pytest.raises(ToolError) as excinfo:
            await mcp_call("research_import", {"notebook": NB_ID, "task_id": "   "})
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.poll.assert_not_called()
    mock_client.research.import_sources.assert_not_called()


def test_resolve_poll_task_id_conflict_raises() -> None:
    """Both names, different values → ValidationError."""
    from notebooklm.exceptions import ValidationError
    from notebooklm.mcp.tools.research import _resolve_poll_task_id

    with pytest.raises(ValidationError):
        _resolve_poll_task_id("research_status", "task_id", "a", "b")


async def test_research_status_task_id_alias_matches_poll_task_id(mcp_call, mock_client) -> None:
    """The deprecated ``task_id`` pin resolves exactly like ``poll_task_id``."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED, task_id=TASK_ID)
    )
    result = await mcp_call("research_status", {"notebook": NB_ID, "task_id": TASK_ID})
    sc = result.structured_content
    assert sc["poll_task_id"] == TASK_ID
    # The alias is threaded through ``poll`` identically to the canonical name.
    mock_client.research.poll.assert_awaited_once_with(NB_ID, TASK_ID)
    # A caller-visible note names the old param and its replacement.
    assert "task_id" in sc["deprecation"] and "poll_task_id" in sc["deprecation"]


async def test_research_import_task_id_alias_matches_poll_task_id(mcp_call, mock_client) -> None:
    """The deprecated ``task_id`` import arg resolves exactly like ``poll_task_id``."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[FakeSource(url="http://a", title="A")],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources = AsyncMock(return_value=[{"id": "src-1", "title": "A"}])
    result = await mcp_call("research_import", {"notebook": NB_ID, "task_id": TASK_ID})
    sc = result.structured_content
    assert sc["imported"] == [{"id": "src-1", "title": "A"}]
    assert sc["poll_task_id"] == TASK_ID
    mock_client.research.poll.assert_awaited_once_with(NB_ID, TASK_ID)
    assert mock_client.research.import_sources.await_args.args[1] == TASK_ID
    assert "task_id" in sc["deprecation"]


async def test_research_cancel_run_id_alias_matches_poll_task_id(mcp_call, mock_client) -> None:
    """The deprecated ``run_id`` cancel arg resolves exactly like ``poll_task_id``."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS, task_id=TASK_ID)
    )
    mock_client.research.cancel = AsyncMock(return_value=None)
    result = await mcp_call("research_cancel", {"notebook": NB_ID, "run_id": TASK_ID})
    sc = result.structured_content
    assert sc["cancel_requested"] is True
    assert sc["poll_task_id"] == TASK_ID
    # The legacy ``run_id`` response key is retained for one release.
    assert sc["run_id"] == TASK_ID
    mock_client.research.cancel.assert_awaited_once_with(NB_ID, TASK_ID)
    assert "run_id" in sc["deprecation"]


async def test_research_alias_and_canonical_same_value_no_note(mcp_call, mock_client) -> None:
    """Passing both names with the SAME value is accepted (canonical wins, no note)."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED, task_id=TASK_ID)
    )
    result = await mcp_call(
        "research_status",
        {"notebook": NB_ID, "poll_task_id": TASK_ID, "task_id": TASK_ID},
    )
    assert "deprecation" not in result.structured_content
    mock_client.research.poll.assert_awaited_once_with(NB_ID, TASK_ID)


async def test_research_alias_and_canonical_conflict_rejected(mcp_call, mock_client) -> None:
    """Passing both names with DIFFERENT values is rejected up front (no poll)."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED)
    )
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "research_status",
            {"notebook": NB_ID, "poll_task_id": "a", "task_id": "b"},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.poll.assert_not_called()
