"""MCP artifact-tool VCR test (reuse-only).

``artifact_list`` over ``artifacts_list.yaml`` — the studio-artifact list wire
shape (``{"notebook_id", "artifacts": [...]}``). ``client.artifacts.list``
issues ``LIST_ARTIFACTS`` (``gArtLc``) + the note-backed mind-map merge
``GET_NOTES_AND_MIND_MAPS`` (``cFji9``), both recorded in the cassette.

``artifact_download`` over ``artifacts_download_report.yaml`` — the typed
``DownloadResult`` wire shape, end-to-end, with the report file actually written.
This pairing was originally DROPPED because the download path issued
``LIST_ARTIFACTS`` (``gArtLc``) *twice* (the executor listed to select, then
``download_report`` re-listed), which can't replay against a single-``gArtLc``
cassette. #1488 collapsed that to a single list (the executor threads the
already-fetched rows into the download method), so the shape now replays cleanly.

``artifact_status`` over ``artifacts_list.yaml`` — the stateless poll path
(``_app.artifacts.poll_artifact`` → ``client.artifacts.poll_status`` → a single
``LIST_ARTIFACTS`` ``gArtLc`` RPC, which scans the listed rows for the polled
task id). Reuses the SAME ``artifacts_list.yaml`` cassette as ``artifact_list``:
both consume exactly one ``gArtLc`` interaction, so each is its own
single-interaction test. The polled task id is a real artifact id recorded in
that list (a completed report), so the status resolves to ``completed`` with a
media url rather than ``not_found``.

``artifact_generate`` over ``artifacts_generate_report.yaml`` /
``artifacts_generate_quiz.yaml`` — the non-blocking generation path
(``_app.generate.execute_generation`` → ``client.artifacts.generate_*`` →
``CREATE_ARTIFACT`` ``R7cb6c``). The MCP tool sends ``source_ids`` straight
through (its pass-through source resolver), and the recorded ``R7cb6c`` body
carries the notebook's full source-id list. Because the ``freq`` batchexecute
matcher preserves LIST LENGTHS (only leaf VALUES collapse), the request must
carry the same NUMBER of source ids as the recording — so the tool is invoked
with the exact source ids decoded from the cassette's recorded ``R7cb6c`` body
(see :func:`_recorded_generate_source_ids`). An empty ``source_ids`` would send a
zero-length source list and fail the structural match. The recorded leading
``rLM1Ne`` (``GET_NOTEBOOK``) leg is unused here: the tool supplies explicit
source ids, so the client never resolves them via ``get_source_ids``.

The tools are invoked with a full-UUID notebook id so the resolver skips its
``LIST_NOTEBOOKS`` preflight.
"""

from __future__ import annotations

import json
import re
import urllib.parse

import pytest

from tests.integration.conftest import CASSETTES_DIR, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# ``artifacts_list.yaml`` was recorded against this notebook. Decorative — the
# matcher keys on rpcids + body shape, never the notebook id.
ARTIFACT_NOTEBOOK_ID = "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e"

# ``artifacts_generate_*.yaml`` were recorded against this (44-source) notebook.
GENERATE_NOTEBOOK_ID = "f66923f0-1df4-4ffe-9822-3ed63c558b1c"

# A completed report artifact id recorded in ``artifacts_list.yaml``'s ``gArtLc``
# response — pinned so ``artifact_status`` resolves it to ``completed`` (with a
# media url) instead of ``not_found``.
COMPLETED_ARTIFACT_ID = "575a9e5d-40fb-44a4-b2d3-21a573bdb547"


def _recorded_generate_source_ids(cassette: str) -> list[str]:
    """Decode the source ids from a generate cassette's recorded ``R7cb6c`` body.

    ``artifact_generate`` forwards ``source_ids`` verbatim (pass-through
    resolver), and the ``freq`` batchexecute matcher compares LIST LENGTHS, so
    the replayed request must carry the same number of source ids the cassette
    recorded. Reading them from the cassette (rather than hard-coding the 40+
    UUIDs) keeps the test resilient to a future re-record while still producing a
    structurally-matching ``CREATE_ARTIFACT`` body.
    """
    text = (CASSETTES_DIR / cassette).read_text(encoding="utf-8")
    for body in re.findall(r"body: (f\.req=[^\n]+)", text):
        f_req = urllib.parse.parse_qs(body).get("f.req", [])
        if not f_req:
            continue
        for batch in json.loads(f_req[0]):
            for entry in batch:
                if entry[0] == "R7cb6c":
                    inner = json.loads(entry[1])
                    # inner[2][3] is the list of ``[[source_id]]`` sublists.
                    return [sublist[0][0] for sublist in inner[2][3]]
    raise AssertionError(f"no recorded R7cb6c source ids found in {cassette}")


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("artifacts_list.yaml")
async def test_mcp_artifact_list_over_vcr() -> None:
    """``artifact_list`` returns the recorded artifacts through the real client.

    End-to-end: FastMCP ``Client`` → ``artifact_list`` tool →
    ``client.artifacts.list()`` → recorded ``LIST_ARTIFACTS`` (``gArtLc``) +
    ``GET_NOTES_AND_MIND_MAPS`` (``cFji9``) RPCs.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool("artifact_list", {"notebook": ARTIFACT_NOTEBOOK_ID})

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == ARTIFACT_NOTEBOOK_ID
    artifacts = structured["artifacts"]
    assert isinstance(artifacts, list)
    assert artifacts, "expected at least one recorded artifact from the cassette"
    first = artifacts[0]
    assert isinstance(first, dict)
    # ``to_jsonable`` serializes the declared ``Artifact`` dataclass fields (the
    # user-facing ``type_id`` / ``kind`` are ``@property``, so they are NOT on
    # the wire). Pin the real serialized fields decoded from the positional row:
    # a non-empty id, a title, the integer artifact-type code, and the status.
    assert first.get("id"), "recorded artifact is missing an id"
    assert "title" in first
    assert isinstance(first.get("_artifact_type"), int), "missing decoded artifact-type code"
    assert isinstance(first.get("status"), int), "missing decoded status code"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("artifacts_download_report.yaml")
async def test_mcp_artifact_download_over_vcr(tmp_path) -> None:
    """``artifact_download`` selects + writes the latest report through the real client.

    End-to-end: FastMCP ``Client`` → ``artifact_download`` tool →
    ``execute_download`` (single ``LIST_ARTIFACTS`` post-#1488) →
    ``client.artifacts.download_report`` → recorded download RPC. Asserts the
    typed ``DownloadResult`` wire shape AND that the file was really written
    (a re-introduced double-list would fail the replay, not silently pass).
    """
    out = tmp_path / "report.md"
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "artifact_download",
            {
                "notebook": ARTIFACT_NOTEBOOK_ID,
                "artifact_type": "report",
                "path": str(out),
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["outcome"] == "single_downloaded", structured
    assert not structured.get("is_failure"), structured
    assert structured.get("error") is None, structured
    assert structured.get("output_path"), structured
    assert out.exists() and out.stat().st_size > 0, "the report file was not written"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("artifacts_list.yaml")
async def test_mcp_artifact_status_over_vcr() -> None:
    """``artifact_status`` polls one artifact's status through the real client.

    End-to-end: FastMCP ``Client`` → ``artifact_status`` tool →
    ``_app.artifacts.poll_artifact`` → ``client.artifacts.poll_status`` → a single
    recorded ``LIST_ARTIFACTS`` (``gArtLc``) RPC (the poll lists the notebook's
    artifacts and finds the row matching ``task_id``). Reuses the same
    ``artifacts_list.yaml`` cassette as ``artifact_list`` — each test consumes
    exactly one ``gArtLc`` interaction. The pinned task id is a completed report
    recorded in that list, so the status resolves to ``completed`` (not
    ``not_found``). Asserts the serialized ``ArtifactStatusView`` wire shape.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "artifact_status",
            {"notebook": ARTIFACT_NOTEBOOK_ID, "task_id": COMPLETED_ARTIFACT_ID},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    # ``{"notebook_id", **status_view}`` where the view is
    # ``{task_id, status, url, error, error_code, metadata, is_complete}``.
    assert structured["notebook_id"] == ARTIFACT_NOTEBOOK_ID
    assert structured["task_id"] == COMPLETED_ARTIFACT_ID
    assert structured["status"] == "completed"
    assert structured["is_complete"] is True
    assert structured["error"] is None
    # A completed artifact carries a media url decoded from the listed row.
    assert structured["url"], "expected a media url for the completed artifact"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("artifacts_generate_report.yaml")
async def test_mcp_artifact_generate_report_over_vcr() -> None:
    """``artifact_generate`` (report) starts generation through the real client.

    End-to-end: FastMCP ``Client`` → ``artifact_generate`` tool →
    ``_app.generate.execute_generation`` → ``client.artifacts.generate_report`` →
    recorded ``CREATE_ARTIFACT`` (``R7cb6c``) RPC. Non-blocking: the tool returns
    a pollable ``task_id`` immediately (``wait=False``); it does NOT poll to
    completion. The recorded source-id list is forwarded verbatim so the
    ``R7cb6c`` body matches the structural ``freq`` matcher.
    """
    source_ids = _recorded_generate_source_ids("artifacts_generate_report.yaml")
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "artifact_generate",
            {
                "notebook": GENERATE_NOTEBOOK_ID,
                "artifact_type": "report",
                "source_ids": source_ids,
                "report_format": "briefing-doc",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    # ``_generation_payload`` → ``{notebook_id, kind, task_id, status, url, error}``.
    assert structured["notebook_id"] == GENERATE_NOTEBOOK_ID
    assert structured["kind"] == "report"
    assert structured["task_id"], "expected a pollable generation task id"
    assert structured["status"], "expected a generation status"
    assert "url" in structured
    assert structured["error"] is None


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("artifacts_generate_quiz.yaml")
async def test_mcp_artifact_generate_quiz_over_vcr() -> None:
    """``artifact_generate`` (quiz) starts generation through the real client.

    Same path as the report variant but routed to ``client.artifacts.generate_quiz``
    over ``artifacts_generate_quiz.yaml`` — a second ``artifact_type`` confirms the
    per-kind routing + the (no second-source-block) quiz body shape both replay.
    """
    source_ids = _recorded_generate_source_ids("artifacts_generate_quiz.yaml")
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "artifact_generate",
            {
                "notebook": GENERATE_NOTEBOOK_ID,
                "artifact_type": "quiz",
                "source_ids": source_ids,
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == GENERATE_NOTEBOOK_ID
    assert structured["kind"] == "quiz"
    assert structured["task_id"], "expected a pollable generation task id"
    assert structured["status"], "expected a generation status"
    assert structured["error"] is None
