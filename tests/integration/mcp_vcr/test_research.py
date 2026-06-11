"""MCP research-tool VCR tests (reuse-only).

Full-stack coverage (FastMCP ``Client`` → research tool → ``_app`` →
real ``NotebookLMClient`` → VCR-replayed RPC) for the three research tools:

* ``research_start`` over ``research_start_fast.yaml`` (single
  ``START_FAST_RESEARCH`` ``Ljjv0c`` POST) and ``research_start_deep.yaml``
  (single ``START_DEEP_RESEARCH`` ``QA9ei`` POST). Non-blocking: the tool returns
  the started task, so the assertions pin the ``ResearchStart`` wire shape
  (``task_id`` / ``report_id`` / ``query`` / ``mode``) — no poll-to-completion.
* ``research_status`` over ``research_poll.yaml`` (a single in-flight task →
  ``in_progress``) and ``research_poll_empty.yaml`` (no task → ``no_research``).
  ``research_status`` drives ``_app.research.poll_and_classify`` → one
  ``POLL_RESEARCH`` (``e3bVqc``) POST. (``research_poll.yaml`` also recorded a
  leading ``START_FAST_RESEARCH`` leg; the status tool never starts research, so
  that recorded leg is simply unused on replay — VCR ``record_mode='none'`` does
  not require every interaction to play.)
* ``research_import`` over two cassettes:
  - ``research_import_sources.yaml`` (empty path): the recorded poll returns
    in-flight tasks carrying zero sources, so ``import_sources`` short-circuits on
    its empty-sources guard and issues NO ``IMPORT_RESEARCH`` (``LBwxtb``) RPC —
    covering the poll→empty-import→empty-result wiring and the
    ``{imported: [], sources_found: 0}`` wire shape.
  - ``research_import_sources_populated.yaml`` (import leg, issue #1541): the
    recorded poll returns a COMPLETED task with 10 importable url-bearing sources,
    so ``import_sources`` issues the real ``IMPORT_RESEARCH`` (``LBwxtb``) RPC and
    the tool returns a populated ``{imported: [...], sources_found: 10}`` — the
    actual import RPC and its decode, end-to-end.

Each cassette was recorded against a notebook UUID; the tools are invoked with
that full UUID so the resolver skips its ``LIST_NOTEBOOKS`` preflight. The
``freq`` body matcher's batchexecute path is structural (leaf values collapse),
so the query / mode / notebook-id leaf values do not need to match the recording.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# ``research_start_*.yaml`` / ``research_poll.yaml`` / ``research_import_sources.yaml``
# were recorded against this notebook.
RESEARCH_NOTEBOOK_ID = "06f0c5bd-108f-4c8b-8911-34b2acc656de"
# ``research_poll_empty.yaml`` was recorded against this (empty-research) notebook.
EMPTY_RESEARCH_NOTEBOOK_ID = "4d79940d-5f20-4d77-a918-5d04d08ce789"
# A task id present in ``research_poll.yaml`` / ``research_import_sources.yaml``'s
# recorded poll (the "Python programming best practices" task). Pinning it makes
# the otherwise-ambiguous two-task poll select one task deterministically.
PINNED_TASK_ID = "ac0bc757-fa42-4a0d-8c22-755a9ff075a3"

# ``research_import_sources_populated.yaml`` was recorded (issue #1541) from a
# COMPLETED fast/web research task that yielded 10 importable url-bearing sources,
# so it pairs a sources-bearing poll (``e3bVqc``) with the real ``IMPORT_RESEARCH``
# (``LBwxtb``) leg. The pinned task id matches the sources' ``research_task_id`` so
# ``import_sources``'s provenance check passes.
POPULATED_RESEARCH_NOTEBOOK_ID = "c8ef7832-c110-4e56-a8fb-ab3e1968fc0e"
POPULATED_TASK_ID = "8755b4ce-fa7f-4147-b1e6-665404d9097a"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("research_start_fast.yaml")
async def test_mcp_research_start_fast_over_vcr() -> None:
    """``research_start`` (fast/web) returns the started task through the real client.

    End-to-end: FastMCP ``Client`` → ``research_start`` tool →
    ``client.research.start`` → recorded ``START_FAST_RESEARCH`` (``Ljjv0c``) RPC.
    Asserts the ``ResearchStart`` wire shape (``{notebook_id, task_id, report_id,
    query, mode}``); the tool is non-blocking, so it never polls to completion.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "research_start",
            {
                "notebook": RESEARCH_NOTEBOOK_ID,
                "query": "Python programming best practices",
                "source": "web",
                "mode": "fast",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == RESEARCH_NOTEBOOK_ID
    assert structured["task_id"], "expected a server-recorded research task id"
    assert structured["mode"] == "fast"
    # ``report_id`` is None for fast research; ``query`` echoes the request.
    assert structured["report_id"] is None
    assert structured["query"] == "Python programming best practices"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("research_start_deep.yaml")
async def test_mcp_research_start_deep_over_vcr() -> None:
    """``research_start`` (deep/web) returns the started task + report id.

    End-to-end: FastMCP ``Client`` → ``research_start`` tool →
    ``client.research.start`` → recorded ``START_DEEP_RESEARCH`` (``QA9ei``) RPC.
    Deep research carries a ``report_id`` alongside the ``task_id``.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "research_start",
            {
                "notebook": RESEARCH_NOTEBOOK_ID,
                "query": "Artificial intelligence history",
                "source": "web",
                "mode": "deep",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == RESEARCH_NOTEBOOK_ID
    assert structured["task_id"], "expected a server-recorded research task id"
    assert structured["mode"] == "deep"
    # Deep research records a separate report id.
    assert structured["report_id"], "expected a deep-research report id"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("research_poll.yaml")
async def test_mcp_research_status_in_progress_over_vcr() -> None:
    """``research_status`` reports the pinned in-flight task as ``in_progress``.

    End-to-end: FastMCP ``Client`` → ``research_status`` tool →
    ``_app.research.poll_and_classify`` → ``client.research.poll`` → recorded
    ``POLL_RESEARCH`` (``e3bVqc``) RPC. The recorded poll carries two in-flight
    tasks, so a ``task_id`` is pinned to select one deterministically (an
    unpinned poll would be ambiguous). Asserts the classified status wire shape.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "research_status",
            {"notebook": RESEARCH_NOTEBOOK_ID, "task_id": PINNED_TASK_ID},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == RESEARCH_NOTEBOOK_ID
    assert structured["task_id"] == PINNED_TASK_ID
    assert structured["kind"] == "in_progress"
    assert structured["status"] == "in_progress"
    # The pinned task carries its recorded query; no sources/report yet.
    assert structured["query"], "expected the recorded research query"
    assert structured["sources"] == []
    assert structured["report"] == ""


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("research_poll_empty.yaml")
async def test_mcp_research_status_no_research_over_vcr() -> None:
    """``research_status`` reports ``no_research`` for an empty-research notebook.

    End-to-end: FastMCP ``Client`` → ``research_status`` tool →
    ``poll_and_classify`` → recorded ``POLL_RESEARCH`` (``e3bVqc``) RPC returning
    no tasks. The unfiltered empty poll classifies to ``no_research`` with an
    empty ``task_id`` (the ``ResearchTask.empty()`` sentinel) — no ambiguity.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "research_status",
            {"notebook": EMPTY_RESEARCH_NOTEBOOK_ID},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == EMPTY_RESEARCH_NOTEBOOK_ID
    assert structured["kind"] == "no_research"
    assert structured["status"] == "no_research"
    assert structured["task_id"] == ""
    assert structured["sources"] == []


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("research_import_sources.yaml")
async def test_mcp_research_import_empty_sources_over_vcr() -> None:
    """``research_import`` polls the pinned task then imports its (empty) sources.

    End-to-end: FastMCP ``Client`` → ``research_import`` tool →
    ``poll_and_classify`` (recorded ``POLL_RESEARCH`` ``e3bVqc``) →
    ``client.research.import_sources``. The pinned task's recorded poll returns
    zero sources, so ``import_sources`` short-circuits on its empty-sources guard
    and issues NO ``IMPORT_RESEARCH`` (``LBwxtb``) RPC. Asserts the import wire
    shape (``{imported: [], sources_found: 0}``) — the poll→empty-import wiring.
    The populated import leg (the real ``LBwxtb`` RPC) is covered by
    ``test_mcp_research_import_populated_sources_over_vcr``.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "research_import",
            {"notebook": RESEARCH_NOTEBOOK_ID, "task_id": PINNED_TASK_ID},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == RESEARCH_NOTEBOOK_ID
    assert structured["task_id"] == PINNED_TASK_ID
    assert structured["imported"] == []
    assert structured["sources_found"] == 0


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("research_import_sources_populated.yaml")
async def test_mcp_research_import_populated_sources_over_vcr() -> None:
    """``research_import`` imports a completed task's sources via the real LBwxtb leg.

    Closes the #1541 gap. The recorded poll (``POLL_RESEARCH`` ``e3bVqc``) returns a
    COMPLETED task carrying 10 url-bearing sources, so ``import_sources`` issues the
    real ``IMPORT_RESEARCH`` (``LBwxtb``) RPC (not the empty-sources short-circuit)
    and the tool returns a populated ``{imported: [...], sources_found: N}`` shape —
    exercising the import RPC and its decode end-to-end.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "research_import",
            {"notebook": POPULATED_RESEARCH_NOTEBOOK_ID, "task_id": POPULATED_TASK_ID},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == POPULATED_RESEARCH_NOTEBOOK_ID
    assert structured["task_id"] == POPULATED_TASK_ID
    assert structured["sources_found"] == 10
    imported = structured["imported"]
    # The LBwxtb import returned real, decoded sources (the leg the empty cassette
    # never reached). The API may return fewer rows than were imported (documented),
    # so assert non-empty + well-formed rather than an exact count.
    assert isinstance(imported, list) and imported
    for src in imported:
        assert src.get("id") and src.get("title")
