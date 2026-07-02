"""MCP artifact-tool VCR test (reuse-only).

``studio_list`` over ``mind_maps_interactive.yaml`` — the merged Studio-panel
wire shape (``{"notebook_id", "items": [...], "total", "offset", "has_more"}``).
The unified list reads notes AND artifacts, issuing ``GET_NOTES_AND_MIND_MAPS``
(``cFji9``, notes.list) + ``LIST_ARTIFACTS`` (``gArtLc``) + ``GET_NOTES_AND_MIND_MAPS``
(``cFji9``, the artifacts.list mind-map facade) — the exact three-RPC sequence that
cassette records.

``studio_download`` over ``artifacts_download_report.yaml`` — the typed
``DownloadResult`` wire shape, end-to-end, with the report file actually written.
This pairing was originally DROPPED because the download path issued
``LIST_ARTIFACTS`` (``gArtLc``) *twice* (the executor listed to select, then
``download_report`` re-listed), which can't replay against a single-``gArtLc``
cassette. #1488 collapsed that to a single list (the executor threads the
already-fetched rows into the download method), so the shape now replays cleanly.

``studio_status`` over ``artifacts_list.yaml`` — the stateless poll path
(``_app.artifacts.poll_artifact`` → ``client.artifacts.poll_status`` → a single
``LIST_ARTIFACTS`` ``gArtLc`` RPC, which scans the listed rows for the polled
task id). Reuses the SAME ``artifacts_list.yaml`` cassette as ``studio_list``:
both consume exactly one ``gArtLc`` interaction, so each is its own
single-interaction test. The polled task id is a real artifact id recorded in
that list (a completed report), so the status resolves to ``completed`` with a
media url rather than ``not_found``.

``studio_generate`` over ``artifacts_generate_report.yaml`` /
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
# response — pinned so ``studio_status`` resolves it to ``completed`` (with a
# media url) instead of ``not_found``.
COMPLETED_ARTIFACT_ID = "575a9e5d-40fb-44a4-b2d3-21a573bdb547"


def _recorded_generate_source_ids(cassette: str) -> list[str]:
    """Decode the source ids from a generate cassette's recorded ``R7cb6c`` body.

    ``studio_generate`` forwards ``source_ids`` verbatim (pass-through
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


#: ``mind_maps_interactive.yaml`` was recorded against this notebook and holds
#: exactly the merged-read RPC sequence ``cFji9`` (notes.list) + ``gArtLc`` +
#: ``cFji9`` (artifacts.list's mind-map facade) the unified ``studio_list`` needs.
STUDIO_LIST_NOTEBOOK_ID = "f7d1e2b6-2334-4016-b81d-aded7b3fa9b6"

#: The hyphenated ``type`` vocabulary a merged studio item may carry.
_STUDIO_TYPES = frozenset(
    {
        "note",
        "audio",
        "video",
        "report",
        "quiz",
        "flashcards",
        "mind-map",
        "infographic",
        "slide-deck",
        "data-table",
        "unknown",
    }
)


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("mind_maps_interactive.yaml")
async def test_mcp_studio_list_over_vcr() -> None:
    """``studio_list`` merges notes + artifacts through the real client over VCR.

    End-to-end: FastMCP ``Client`` → ``studio_list`` tool → concurrent
    ``client.notes.list`` (``GET_NOTES_AND_MIND_MAPS`` ``cFji9``) +
    ``client.artifacts.list`` (``LIST_ARTIFACTS`` ``gArtLc`` + the note-backed
    mind-map merge ``cFji9``) — the exact three-RPC sequence recorded in
    ``mind_maps_interactive.yaml``.

    Pins the merged ``items`` wire shape: the key is ``items`` (never
    ``notes``/``artifacts``), each item carries a hyphenated ``type`` discriminator,
    the pagination meta is present, and the notebook's interactive mind map surfaces
    as a ``mind-map`` item.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool("studio_list", {"notebook": STUDIO_LIST_NOTEBOOK_ID})

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == STUDIO_LIST_NOTEBOOK_ID
    items = structured["items"]
    assert isinstance(items, list)
    assert items, "expected at least one recorded studio item from the cassette"
    # Pagination meta rides alongside the merged list.
    assert structured["total"] == len(items) if not structured["has_more"] else True
    assert structured["offset"] == 0
    types = set()
    for it in items:
        assert isinstance(it, dict)
        assert it.get("id"), "studio item is missing an id"
        assert "title" in it
        assert it["type"] in _STUDIO_TYPES, f"unexpected studio type: {it['type']!r}"
        types.add(it["type"])
    # The recording is a mind-map notebook, so a ``mind-map`` item is present.
    assert "mind-map" in types, f"expected a mind-map item; got types {sorted(types)}"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("artifacts_download_report.yaml")
async def test_mcp_artifact_download_over_vcr(tmp_path) -> None:
    """``studio_download`` selects + writes the latest report through the real client.

    End-to-end: FastMCP ``Client`` → ``studio_download`` tool →
    ``execute_download`` (single ``LIST_ARTIFACTS`` post-#1488) →
    ``client.artifacts.download_report`` → recorded download RPC. Asserts the
    typed ``DownloadResult`` wire shape AND that the file was really written
    (a re-introduced double-list would fail the replay, not silently pass).
    """
    out = tmp_path / "report.md"
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "studio_download",
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
    """``studio_status`` polls one artifact's status through the real client.

    End-to-end: FastMCP ``Client`` → ``studio_status`` tool →
    ``_app.artifacts.poll_artifact`` → ``client.artifacts.poll_status`` → a single
    recorded ``LIST_ARTIFACTS`` (``gArtLc``) RPC (the poll lists the notebook's
    artifacts and finds the row matching ``task_id``). Reuses the same
    ``artifacts_list.yaml`` cassette as ``studio_list`` — each test consumes
    exactly one ``gArtLc`` interaction. The pinned task id is a completed report
    recorded in that list, so the status resolves to ``completed`` (not
    ``not_found``). Asserts the serialized ``ArtifactStatusView`` wire shape.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "studio_status",
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
    """``studio_generate`` (report) starts generation through the real client.

    End-to-end: FastMCP ``Client`` → ``studio_generate`` tool →
    ``_app.generate.execute_generation`` → ``client.artifacts.generate_report`` →
    recorded ``CREATE_ARTIFACT`` (``R7cb6c``) RPC. Non-blocking: the tool returns
    a pollable ``task_id`` immediately (``wait=False``); it does NOT poll to
    completion. The recorded source-id list is forwarded verbatim so the
    ``R7cb6c`` body matches the structural ``freq`` matcher.
    """
    source_ids = _recorded_generate_source_ids("artifacts_generate_report.yaml")
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "studio_generate",
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
    """``studio_generate`` (quiz) starts generation through the real client.

    Same path as the report variant but routed to ``client.artifacts.generate_quiz``
    over ``artifacts_generate_quiz.yaml`` — a second ``artifact_type`` confirms the
    per-kind routing + the (no second-source-block) quiz body shape both replay.
    """
    source_ids = _recorded_generate_source_ids("artifacts_generate_quiz.yaml")
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "studio_generate",
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
