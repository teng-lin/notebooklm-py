"""MCP Studio mutating-tool VCR tests.

Full-stack coverage (MCP tool -> ``artifacts.py`` Studio adapter -> real
``NotebookLMClient`` -> VCR-replayed RPC) for every Studio mutating/read op that
was unit-only before #1733: ``studio_retry``, ``studio_get_prompt``,
``studio_rename``, and ``studio_delete`` (both cross-type routes). Replay only —
``NOTEBOOKLM_VCR_RECORD`` is deliberately NOT set here.

Two cassette provenances:

* ``studio_retry`` / ``studio_get_prompt`` REUSE cassettes the CLI ``artifact``
  VCR suite already recorded (``artifacts_retry_failed.yaml`` / ``artifacts_list.yaml``).
* ``studio_rename`` / ``studio_delete`` needed a bespoke recording — their
  merged-list / kind-probe preflight issues ``GET_NOTES_AND_MIND_MAPS`` (``cFji9``)
  **twice or thrice** + ``LIST_ARTIFACTS`` (``gArtLc``) before the mutation RPC, a
  sequence no CLI cassette holds. The ``mcp_studio_*.yaml`` cassettes were recorded
  against a throwaway scratch notebook by ``tests/scripts/record_mcp_studio_cassettes.py``.

Every tool is invoked with the FULL canonical UUIDs recorded in each cassette so
:func:`resolve_notebook` / :func:`resolve_artifact` take their full-UUID fast path.
For ``studio_delete`` / ``studio_rename`` the id VALUES are load-bearing (not
decorative): the tool resolves the ``item`` over the merged list response, so the
ref must match an id actually recorded there.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# artifacts_retry_failed.yaml — the recorded ``Rytqqe`` body carries this artifact
# id; the notebook id is decorative (lives in the URL, which the matcher ignores).
RETRY_NOTEBOOK_ID = "f66923f0-1df4-4ffe-9822-3ed63c558b1c"
RETRY_ARTIFACT_ID = "11111111-2222-3333-4444-555555555555"

# artifacts_list.yaml — a completed REPORT artifact whose ``gArtLc`` row carries a
# stored generation prompt (decoded from the recorded response).
PROMPT_NOTEBOOK_ID = "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e"
PROMPT_ARTIFACT_ID = "fdd20d4a-f422-42b3-896c-60997035f4ca"

# mcp_studio_*.yaml — recorded (scratch-notebook) ids. The notebook holds ONE note
# and ONE report; studio_delete/rename resolve these ids over the merged list, so
# the values must match the ids the recorded LIST responses carry.
STUDIO_NOTEBOOK_ID = "06d9adf2-f2d7-41e8-8587-5a92bd1f1a53"
STUDIO_REPORT_ID = "5acb7fe5-800e-4dc7-89a6-5cedd55c1ad9"
STUDIO_NOTE_ID = "d1706a9f-b959-413d-ae6d-52e0d35c6e72"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("artifacts_retry_failed.yaml")
async def test_mcp_studio_retry_over_vcr() -> None:
    """``studio_retry`` re-kicks a failed artifact through the real client over VCR.

    End-to-end: tool -> ``resolve_artifact`` (full UUID, no list) ->
    ``client.artifacts.retry_failed`` -> ``RETRY_ARTIFACT`` (``Rytqqe``). Pins the
    non-blocking wire shape (``task_id`` + ``status``) — the mutating retry RPC a
    mocked test cannot validate.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "studio_retry",
            {"notebook": RETRY_NOTEBOOK_ID, "artifact": RETRY_ARTIFACT_ID},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == RETRY_NOTEBOOK_ID
    assert structured["artifact_id"] == RETRY_ARTIFACT_ID
    assert structured["task_id"], "retry must return a resume task_id"
    assert isinstance(structured["status"], str)


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("artifacts_list.yaml")
async def test_mcp_studio_get_prompt_over_vcr() -> None:
    """``studio_get_prompt`` reads an artifact's generation prompt over VCR.

    End-to-end: tool -> ``resolve_artifact`` (full UUID, no list) ->
    ``get_artifact_prompt`` -> studio listing (``gArtLc`` + the mind-map facade
    ``cFji9``) -> the row's stored prompt. Pins the ``{"notebook_id",
    "artifact_id", "prompt"}`` wire shape with a real, non-null prompt (the pinned
    id is a completed report whose row carries one).
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "studio_get_prompt",
            {"notebook": PROMPT_NOTEBOOK_ID, "artifact": PROMPT_ARTIFACT_ID},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == PROMPT_NOTEBOOK_ID
    assert structured["artifact_id"] == PROMPT_ARTIFACT_ID
    # This artifact records a prompt — a real string, not the valid-but-empty None.
    assert isinstance(structured["prompt"], str)
    assert structured["prompt"]


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("mcp_studio_rename.yaml")
async def test_mcp_studio_rename_over_vcr() -> None:
    """``studio_rename`` retitles a regular artifact through the real client over VCR.

    End-to-end: tool -> cross-type ``resolve_studio_item`` (merged notes+artifacts
    preflight: ``GET_NOTES_AND_MIND_MAPS`` ``cFji9`` + ``LIST_ARTIFACTS`` ``gArtLc``)
    resolves the report as an artifact -> ``rename_artifact`` -> a kind-aware
    ``mind_maps.list`` probe (``list_note_backed`` ``cFji9`` + ``artifacts.list``
    ``gArtLc`` + facade ``cFji9``; the id is NOT a mind map) ->
    ``client.artifacts.rename`` -> ``RENAME_ARTIFACT`` (``rc3d8d``). Pins the
    ``{"status": "renamed", "type": "report", ..., "is_mind_map": False}`` wire
    shape — the mutating rename RPC a mocked test cannot validate.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "studio_rename",
            {
                "notebook": STUDIO_NOTEBOOK_ID,
                "item": STUDIO_REPORT_ID,
                "new_title": "Renamed by VCR",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["status"] == "renamed"
    assert structured["notebook_id"] == STUDIO_NOTEBOOK_ID
    assert structured["item_id"] == STUDIO_REPORT_ID
    assert structured["type"] == "report"
    assert structured["new_title"] == "Renamed by VCR"
    assert structured["is_mind_map"] is False


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("mcp_studio_delete_note.yaml")
async def test_mcp_studio_delete_note_over_vcr() -> None:
    """``studio_delete`` of a NOTE routes to the note system (cross-type routing).

    The headline #1733 proof: end-to-end, the merged notes+artifacts preflight
    (``GET_NOTES_AND_MIND_MAPS`` ``cFji9`` ×2 + ``LIST_ARTIFACTS`` ``gArtLc``)
    resolves ``item`` as a ``note`` and routes it to ``execute_note_delete`` ->
    ``DELETE_NOTE`` (``AH0mwd``) — NOT the artifact delete path. ``confirm=True`` is
    required; the default preview does not touch the wire.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "studio_delete",
            {"notebook": STUDIO_NOTEBOOK_ID, "item": STUDIO_NOTE_ID, "confirm": True},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["status"] == "deleted"
    assert structured["notebook_id"] == STUDIO_NOTEBOOK_ID
    assert structured["item_id"] == STUDIO_NOTE_ID
    # Resolved as a note -> note-delete route (proves the cross-type discriminator).
    assert structured["type"] == "note"
    assert structured["was_note_backed"] is False


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("mcp_studio_delete_artifact.yaml")
async def test_mcp_studio_delete_artifact_over_vcr() -> None:
    """``studio_delete`` of a regular artifact routes to the artifact delete RPC.

    The other cross-type branch: the merged preflight (``cFji9`` ×2 + ``gArtLc``)
    resolves ``item`` as a ``report``, then ``delete_artifact``'s note-backed probe
    (``list_note_backed`` ``cFji9``) finds no match and routes to
    ``client.artifacts.delete`` -> ``DELETE_ARTIFACT`` (``V5N4be``).
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "studio_delete",
            {"notebook": STUDIO_NOTEBOOK_ID, "item": STUDIO_REPORT_ID, "confirm": True},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["status"] == "deleted"
    assert structured["notebook_id"] == STUDIO_NOTEBOOK_ID
    assert structured["item_id"] == STUDIO_REPORT_ID
    assert structured["type"] == "report"
    # A regular artifact, not a note-backed mind map -> artifacts.delete path.
    assert structured["was_note_backed"] is False
