"""Unit tests for the research MCP tools.

Drives each tool through the in-memory FastMCP ``Client`` against a server bound
to the mocked ``NotebookLMClient``, asserting the serialized
``structured_content``. Covers each tool's happy path, name-vs-id resolution,
the start→status poll shape, the import workflow, and error projection.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402 - after importorskip guard
from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm import ResearchStartUnavailableError  # noqa: E402 - after importorskip guard
from notebooklm.types import (  # noqa: E402 - after importorskip guard
    DiscoveryMode,
    ResearchSource,
    ResearchStatus,
    ResearchTask,
)

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
    hint: str = ""

    def to_public_dict(self) -> dict[str, str]:
        # Mirror the real ``ResearchSource.to_public_dict``: ``report_markdown``
        # and ``hint`` are only emitted when truthy. NOTE this is a hand-mirror,
        # so a test asserting a key here proves the TOOL forwards it, not that
        # the real model emits it — that guarantee lives in
        # ``tests/unit/app/test_app_research.py`` and
        # ``tests/unit/test_research_task_parser.py``, both of which drive the
        # real ``ResearchSource``.
        public = {"url": self.url, "title": self.title}
        if self.report_markdown:
            public["report_markdown"] = self.report_markdown
        if self.hint:
            public["hint"] = self.hint
        return public


@dataclass
class FakeResearchTask:
    status: FakeResearchStatus = FakeResearchStatus.IN_PROGRESS
    query: str = "my query"
    sources: list[FakeSource] = field(default_factory=list)
    summary: str = ""
    report: str = ""
    task_id: str = TASK_ID
    status_code: int | None = None
    source_type: int | None = None
    # #2122 run metadata. Defaults are the "poll made no claim" state, so the
    # existing tests keep asserting the absent shape; the dedicated #2122 test
    # sets them from the live-observed values.
    discovery_mode: DiscoveryMode | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Delegate the #1964 derivations to a REAL ``ResearchTask`` rather than
    # restating them here, so this fake cannot drift from the shipped
    # reason/message/hint logic the tool depends on. Delegation (not
    # class-attribute aliasing of each property) keeps working when the real
    # model grows an internal helper the derivations call.
    @property
    def _derived(self) -> ResearchTask:
        """Real model carrying exactly the inputs the derivations read."""
        return ResearchTask(
            task_id=self.task_id,
            # Not read by any derived property, but a real value keeps the
            # model valid if that ever changes.
            status=ResearchStatus.FAILED,
            query=self.query,
            # Only emptiness matters (the code-3-with-sources guard).
            sources=tuple(ResearchSource(url=s.url, title=s.title) for s in self.sources),
            status_code=self.status_code,
            source_type=self.source_type,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @property
    def termination_reason(self) -> Any:
        return self._derived.termination_reason

    @property
    def reason_message(self) -> str | None:
        return self._derived.reason_message

    @property
    def hint(self) -> str | None:
        return self._derived.hint

    @property
    def duration(self) -> timedelta | None:
        return self._derived.duration

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
    assert result.structured_content["poll_task_id"] == TASK_ID
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
    assert result.structured_content["poll_task_id"] == TASK_ID
    mock_client.research.start.assert_awaited_once_with(NB_ID, "q", "web", "fast")


async def test_research_start_surfaces_poll_task_id_fast(mcp_call, mock_client) -> None:
    """Fast runs poll under task_id → poll_task_id mirrors it."""
    mock_client.research.start = AsyncMock(
        return_value=FakeResearchStart(task_id=TASK_ID, report_id=None)
    )
    result = await mcp_call("research_start", {"notebook": NB_ID, "query": "q"})
    sc = result.structured_content
    assert sc["poll_task_id"] == TASK_ID
    # #1909: the raw internal ids are dropped — poll_task_id is the only id field.
    assert "task_id" not in sc
    assert "report_id" not in sc


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


async def test_research_start_deep_unavailable_does_not_leak_rpc_method_id(
    mcp_call, mock_client
) -> None:
    """#1849: MCP must not expose the deep RPC method id as a fake pollable id."""
    mock_client.research.start = AsyncMock(
        side_effect=ResearchStartUnavailableError(
            NB_ID,
            "deep",
            method_id="QA9ei",
            found_ids=["QA9ei"],
        )
    )

    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "research_start", {"notebook": NB_ID, "query": "q", "source": "web", "mode": "deep"}
        )

    msg = str(excinfo.value)
    assert "RPC:" in msg
    assert "Deep research failed to start" in msg
    assert "NotebookLM returned no research run" in msg
    assert "QA9ei" not in msg
    assert "Found IDs" not in msg


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
    mock_client.research.import_sources_with_verification = AsyncMock(
        return_value=[{"id": "src-1", "title": "A"}]
    )
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
    # #1920: the import routes through the timeout-tolerant verification variant
    # (reconciles a committed partial import on timeout), not the naive one-shot.
    mock_client.research.import_sources_with_verification.assert_awaited_once()
    called = mock_client.research.import_sources_with_verification.await_args.args
    assert called[0] == NB_ID
    assert called[1] == TASK_ID


async def test_research_import_reconciles_committed_partial_on_timeout(
    mcp_call, mock_client
) -> None:
    """#1920 Part 1: routing through import_sources_with_verification means a
    timeout that hid a committed import is reconciled (returned as imported), not
    re-raised as if nothing landed. The MCP tool just delegates to the variant;
    the reconciliation lives in (and is unit-tested at) the library layer, so
    here we assert the tool returns whatever the verification variant resolved."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[FakeSource(url="http://a", title="A")],
            task_id=TASK_ID,
        )
    )
    # The verification variant reconciled a committed source after a timeout.
    mock_client.research.import_sources_with_verification = AsyncMock(
        return_value=[{"id": "committed-1", "title": "A"}]
    )
    # The naive one-shot path must NOT be used.
    mock_client.research.import_sources = AsyncMock(return_value=[])
    result = await mcp_call("research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    assert result.structured_content["imported"] == [{"id": "committed-1", "title": "A"}]
    mock_client.research.import_sources_with_verification.assert_awaited_once()
    mock_client.research.import_sources.assert_not_called()


async def test_research_import_max_sources_caps(mcp_call, mock_client) -> None:
    """#1920 Part 2: max_sources bounds how many sources are imported."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[
                FakeSource(url="http://a", title="A"),
                FakeSource(url="http://b", title="B"),
                FakeSource(url="http://c", title="C"),
            ],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources_with_verification = AsyncMock(
        return_value=[{"id": "src-1", "title": "A"}, {"id": "src-2", "title": "B"}]
    )
    result = await mcp_call(
        "research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID, "max_sources": 2}
    )
    # Only the first two sources are handed to the importer.
    imported_sources = mock_client.research.import_sources_with_verification.await_args.args[2]
    assert len(imported_sources) == 2
    # ``sources_found`` stays the total the run discovered; ``sources_selected``
    # is the post-cap count handed to the importer.
    assert result.structured_content["sources_found"] == 3
    assert result.structured_content["sources_selected"] == 2


async def test_research_import_max_sources_rejects_zero(mcp_call, mock_client) -> None:
    """max_sources < 1 is rejected up front (before any poll/import)."""
    mock_client.research.poll = AsyncMock(return_value=FakeResearchTask())
    mock_client.research.import_sources_with_verification = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID, "max_sources": 0}
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.research.poll.assert_not_called()
    mock_client.research.import_sources_with_verification.assert_not_called()


async def test_research_import_cited_only_selects_cited(mcp_call, mock_client) -> None:
    """#1920 Part 2: cited_only imports only sources the report cites."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            report="See [A](http://a) for details.",
            sources=[
                FakeSource(url="http://a", title="A"),
                FakeSource(url="http://b", title="B"),
            ],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources_with_verification = AsyncMock(
        return_value=[{"id": "src-a", "title": "A"}]
    )
    result = await mcp_call(
        "research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID, "cited_only": True}
    )
    imported_sources = mock_client.research.import_sources_with_verification.await_args.args[2]
    urls = {src["url"] for src in imported_sources}
    assert urls == {"http://a"}
    assert result.structured_content["sources_found"] == 2
    assert result.structured_content["sources_selected"] == 1
    assert "cited_only_fallback" not in result.structured_content


async def test_research_import_cited_only_fallback_when_none_cited(mcp_call, mock_client) -> None:
    """cited_only with an uncited report falls back to all sources and flags it."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            report="No links here.",
            sources=[
                FakeSource(url="http://a", title="A"),
                FakeSource(url="http://b", title="B"),
            ],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources_with_verification = AsyncMock(
        return_value=[{"id": "src-a", "title": "A"}, {"id": "src-b", "title": "B"}]
    )
    result = await mcp_call(
        "research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID, "cited_only": True}
    )
    imported_sources = mock_client.research.import_sources_with_verification.await_args.args[2]
    assert len(imported_sources) == 2
    assert result.structured_content["sources_found"] == 2
    assert result.structured_content["sources_selected"] == 2
    assert result.structured_content["cited_only_fallback"] is True


async def test_research_import_cited_only_then_max_sources_order(mcp_call, mock_client) -> None:
    """cited_only applies first, then max_sources caps the cited subset (#1920)."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            report="See [A](http://a) and [B](http://b).",
            sources=[
                FakeSource(url="http://a", title="A"),
                FakeSource(url="http://b", title="B"),
                FakeSource(url="http://c", title="C"),
            ],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources_with_verification = AsyncMock(
        return_value=[{"id": "src-a", "title": "A"}]
    )
    result = await mcp_call(
        "research_import",
        {"notebook": NB_ID, "poll_task_id": TASK_ID, "cited_only": True, "max_sources": 1},
    )
    imported_sources = mock_client.research.import_sources_with_verification.await_args.args[2]
    # cited_only narrows {a,b,c} → cited {a,b}; max_sources=1 then keeps the first
    # cited source (a), never an uncited one (c).
    assert [src["url"] for src in imported_sources] == ["http://a"]
    assert result.structured_content["sources_found"] == 3
    assert result.structured_content["sources_selected"] == 1


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


# ---------------------------------------------------------------------------
# F10 (#1922): research_status surfaces the raw backend status code
# ---------------------------------------------------------------------------


async def test_research_status_surfaces_status_code(mcp_call, mock_client) -> None:
    """The raw ``task_info[4]`` code is surfaced so an agent can tell a
    "no matches" failure sub-code from a genuine error (the coarse ``status``
    flattens both to ``failed``)."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.FAILED, status_code=7)
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["status_code"] == 7


async def test_research_status_status_code_none_when_absent(mcp_call, mock_client) -> None:
    """A poll carrying no code surfaces ``status_code: None`` (not omitted)."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS)
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    assert result.structured_content["status_code"] is None


# ---------------------------------------------------------------------------
# #1964: an empty Drive search is differentiated from a genuine failure
# ---------------------------------------------------------------------------


async def test_research_status_empty_drive_search_reports_no_results(mcp_call, mock_client) -> None:
    """The reported bug: a Drive run that matched nothing arrived as an
    undifferentiated ``failed`` with no reason and no remediation."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.FAILED,
            query="Example Document.md",
            status_code=3,
            source_type=2,
        )
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    content = result.structured_content
    assert content["status"] == "failed"
    assert content["termination_reason"] == "no_results"
    # Full-text, not substring: a substring still passes on a message that has
    # gone ungrammatical or dropped the corpus name.
    assert content["reason_message"] == (
        "The search of Google Drive found no matches for 'Example Document.md'."
    )
    assert content["hint"] == (
        "Try the exact Drive filename, the document URL, or add the "
        "document directly by its document id."
    )
    # A query that found nothing is not a cancelled run.
    assert "cancelled" not in content


async def test_research_status_reason_distinguishes_cancel_from_no_results(
    mcp_call, mock_client
) -> None:
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.FAILED, query="q", status_code=4, source_type=1
        )
    )
    result = await mcp_call("research_status", {"notebook": NB_ID})
    assert result.structured_content["termination_reason"] == "cancelled"
    # The wire code alone is enough to report the cancel — no prior
    # ``research_cancel`` call was made in this process.
    assert result.structured_content["cancelled"] is True


async def test_research_status_unknown_terminal_code_is_not_guessed(mcp_call, mock_client) -> None:
    """An uncaptured code must surface as ``unknown`` — never folded into
    ``no_results``, which would tell an agent to rewrite a fine query."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.FAILED, query="q", status_code=7)
    )
    content = (await mcp_call("research_status", {"notebook": NB_ID})).structured_content
    assert content["termination_reason"] == "unknown"
    assert content["status_code"] == 7
    # Assert the UNKNOWN texts specifically — `is not None` passes on the Drive
    # hint just as happily, which would hide a mis-routed reason.
    assert content["reason_message"] == (
        "The research run for 'q' reported an unrecognised backend status code (7)."
    )
    assert content["hint"] == (
        "This client treats an unrecognised code as terminal — start a new run."
    )
    # An unnameable failure is not a cancel.
    assert "cancelled" not in content


async def test_research_status_success_omits_reason_message_and_hint(mcp_call, mock_client) -> None:
    """A successful poll's payload is unchanged — the explanation keys appear
    only when there is something to explain."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED, status_code=2)
    )
    content = (await mcp_call("research_status", {"notebook": NB_ID})).structured_content
    assert content["termination_reason"] == "completed"
    assert "reason_message" not in content
    assert "hint" not in content


async def test_research_import_empty_drive_search_error_is_actionable(
    mcp_call, mock_client
) -> None:
    """Importing a run that matched nothing must explain why rather than
    telling the caller their run broke."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.FAILED,
            query="Example Document.md",
            status_code=3,
            source_type=2,
        )
    )
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    assert "no matches" in str(excinfo.value)


# ---------------------------------------------------------------------------
# F9 (#1922): a cancelled run's later ``failed`` poll is annotated ``cancelled``
# ---------------------------------------------------------------------------


async def test_research_cancel_then_failed_status_is_annotated_cancelled(
    server_factory, mock_client
) -> None:
    """After a successful cancel, a later ``failed`` poll for that run is flagged
    ``cancelled: true`` (cancel intent is tracked client-side; the backend
    surfaces a cancelled run as a generic FAILED). Both tool calls share one
    open client so they hit the same lifespan ``AppState``."""
    server = server_factory()
    mock_client.research.cancel = AsyncMock(return_value=None)
    async with Client(server) as client:
        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS, task_id=TASK_ID)
        )
        await client.call_tool("research_cancel", {"notebook": NB_ID, "poll_task_id": TASK_ID})

        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.FAILED, task_id=TASK_ID)
        )
        result = await client.call_tool(
            "research_status", {"notebook": NB_ID, "poll_task_id": TASK_ID}
        )
    assert result.structured_content["status"] == "failed"
    assert result.structured_content["cancelled"] is True


async def test_research_cancel_then_failed_status_annotated_unfiltered_poll(
    server_factory, mock_client
) -> None:
    """The annotation keys off the polled ``task_id`` too, so an unfiltered
    ``research_status`` (no pin) after a cancel is still flagged."""
    server = server_factory()
    mock_client.research.cancel = AsyncMock(return_value=None)
    async with Client(server) as client:
        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS, task_id=TASK_ID)
        )
        await client.call_tool("research_cancel", {"notebook": NB_ID, "poll_task_id": TASK_ID})

        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.FAILED, task_id=TASK_ID)
        )
        result = await client.call_tool("research_status", {"notebook": NB_ID})
    assert result.structured_content["cancelled"] is True


async def test_research_cancel_intent_evicted_on_terminal_poll(server_factory, mock_client) -> None:
    """The cancel intent is evicted once the run reaches a terminal poll, so the
    tracker cannot grow without bound: the FIRST failed poll is annotated, a
    SECOND poll of the same terminal run is no longer annotated (#1922, F9)."""
    server = server_factory()
    mock_client.research.cancel = AsyncMock(return_value=None)
    async with Client(server) as client:
        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS, task_id=TASK_ID)
        )
        await client.call_tool("research_cancel", {"notebook": NB_ID, "poll_task_id": TASK_ID})

        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.FAILED, task_id=TASK_ID)
        )
        first = await client.call_tool(
            "research_status", {"notebook": NB_ID, "poll_task_id": TASK_ID}
        )
        second = await client.call_tool(
            "research_status", {"notebook": NB_ID, "poll_task_id": TASK_ID}
        )
    assert first.structured_content["cancelled"] is True
    assert "cancelled" not in second.structured_content


async def test_research_status_failed_without_cancel_not_annotated(mcp_call, mock_client) -> None:
    """A genuine failure (never cancelled) is NOT annotated — absence of the
    ``cancelled`` key means "not a tracked cancel"."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.FAILED, task_id=TASK_ID)
    )
    result = await mcp_call("research_status", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    assert result.structured_content["status"] == "failed"
    assert "cancelled" not in result.structured_content


async def test_research_cancel_does_not_annotate_different_task(
    server_factory, mock_client
) -> None:
    """Cancel intent for one run does not leak onto a DIFFERENT failed run."""
    other = "research-task-2"
    server = server_factory()
    mock_client.research.cancel = AsyncMock(return_value=None)
    async with Client(server) as client:
        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS, task_id=TASK_ID)
        )
        await client.call_tool("research_cancel", {"notebook": NB_ID, "poll_task_id": TASK_ID})

        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.FAILED, task_id=other)
        )
        result = await client.call_tool(
            "research_status", {"notebook": NB_ID, "poll_task_id": other}
        )
    assert "cancelled" not in result.structured_content


async def test_research_cancel_then_completed_status_not_annotated(
    server_factory, mock_client
) -> None:
    """``cancelled`` only annotates a ``failed`` outcome — a cancel that lost the
    race and the run completed anyway is not mislabelled."""
    server = server_factory()
    mock_client.research.cancel = AsyncMock(return_value=None)
    async with Client(server) as client:
        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.IN_PROGRESS, task_id=TASK_ID)
        )
        await client.call_tool("research_cancel", {"notebook": NB_ID, "poll_task_id": TASK_ID})

        mock_client.research.poll = AsyncMock(
            return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED, task_id=TASK_ID)
        )
        result = await client.call_tool(
            "research_status", {"notebook": NB_ID, "poll_task_id": TASK_ID}
        )
    assert "cancelled" not in result.structured_content


async def test_research_start_then_status_poll_shape(mcp_call, mock_client) -> None:
    """start→status: start returns poll_task_id, status polls the notebook."""
    mock_client.research.start = AsyncMock(return_value=FakeResearchStart(task_id=TASK_ID))
    started = await mcp_call("research_start", {"notebook": NB_ID, "query": "q"})
    assert started.structured_content["poll_task_id"] == TASK_ID

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
    mock_client.research.import_sources_with_verification = AsyncMock(
        return_value=[{"id": "src-1", "title": "A"}]
    )
    result = await mcp_call("research_import", {"notebook": NB_ID, "task_id": TASK_ID})
    sc = result.structured_content
    assert sc["imported"] == [{"id": "src-1", "title": "A"}]
    assert sc["poll_task_id"] == TASK_ID
    mock_client.research.poll.assert_awaited_once_with(NB_ID, TASK_ID)
    assert mock_client.research.import_sources_with_verification.await_args.args[1] == TASK_ID
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


# ---------------------------------------------------------------------------
# research_import idempotency (#1961)
# ---------------------------------------------------------------------------


async def test_research_import_already_imported_when_all_present(mcp_call, mock_client) -> None:
    """A repeat import (everything already present) adds nothing and reports it."""
    from notebooklm._research import _imported_result

    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[FakeSource(url="http://a", title="A")],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources_with_verification = AsyncMock(
        return_value=_imported_result([], [{"id": "existing-1", "title": "A", "url": "http://a"}])
    )

    result = await mcp_call("research_import", {"notebook": NB_ID, "poll_task_id": TASK_ID})
    sc = result.structured_content
    assert sc["status"] == "already_imported"
    assert sc["newly_imported"] == []
    assert sc["imported"] == []  # historical alias for newly_imported
    assert sc["newly_imported_count"] == 0
    assert sc["already_present"] == [{"id": "existing-1", "title": "A", "url": "http://a"}]
    assert sc["already_present_count"] == 1


async def test_research_import_allow_duplicate_threads_through(mcp_call, mock_client) -> None:
    """allow_duplicate=True re-adds and is forwarded to the import wrapper."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            sources=[FakeSource(url="http://a", title="A")],
            task_id=TASK_ID,
        )
    )
    mock_client.research.import_sources_with_verification = AsyncMock(
        return_value=[{"id": "dup-1", "title": "A"}]
    )

    result = await mcp_call(
        "research_import",
        {"notebook": NB_ID, "poll_task_id": TASK_ID, "allow_duplicate": True},
    )
    sc = result.structured_content
    assert sc["status"] == "imported"
    assert sc["newly_imported"] == [{"id": "dup-1", "title": "A"}]
    assert sc["already_present"] == []
    assert sc["already_present_count"] == 0
    _, kwargs = mock_client.research.import_sources_with_verification.await_args
    assert kwargs.get("allow_duplicate") is True


# ---------------------------------------------------------------------------
# run metadata on the research_status payload (#2122)
# ---------------------------------------------------------------------------


async def test_research_status_surfaces_run_metadata(mcp_call, mock_client) -> None:
    """An agent can confirm which mode a run is executing under and how long it
    has been going, from the poll it already makes."""
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.IN_PROGRESS,
            status_code=1,
            discovery_mode=DiscoveryMode.DEEP_RESEARCH,
            created_at=datetime(2026, 8, 13, 11, 12, 58, tzinfo=timezone.utc),
            updated_at=datetime(2026, 8, 13, 11, 13, 5, tzinfo=timezone.utc),
        )
    )
    content = (await mcp_call("research_status", {"notebook": NB_ID})).structured_content

    assert content["discovery_mode"] == "deep_research"
    assert content["created_at"] == "2026-08-13T11:12:58+00:00"
    assert content["updated_at"] == "2026-08-13T11:13:05+00:00"
    assert content["duration_seconds"] == 7.0


async def test_research_status_run_metadata_is_none_when_absent(mcp_call, mock_client) -> None:
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(status=FakeResearchStatus.COMPLETED, status_code=2)
    )
    content = (await mcp_call("research_status", {"notebook": NB_ID})).structured_content

    assert content["discovery_mode"] is None
    assert content["created_at"] is None
    assert content["updated_at"] is None
    assert content["duration_seconds"] is None


async def test_research_status_sources_carry_their_hints(mcp_call, mock_client) -> None:
    mock_client.research.poll = AsyncMock(
        return_value=FakeResearchTask(
            status=FakeResearchStatus.COMPLETED,
            status_code=2,
            sources=[FakeSource(url="https://example.com/1", title="S1", hint="why this one")],
        )
    )
    content = (await mcp_call("research_status", {"notebook": NB_ID})).structured_content

    assert content["sources"][0]["hint"] == "why this one"
