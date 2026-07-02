"""MCP Studio mutating-tool VCR tests (reuse-only).

Full-stack coverage (MCP tool -> ``artifacts.py`` Studio adapter -> real
``NotebookLMClient`` -> VCR-replayed RPC) for the Studio ops that were unit-only
before #1733 and whose exact RPC sequence a recorded cassette already holds:
``studio_retry`` and ``studio_get_prompt``. Reuses the SAME cassettes the CLI
``artifact`` VCR suite recorded — ``NOTEBOOKLM_VCR_RECORD`` is deliberately NOT set.

Every tool is invoked with FULL canonical UUIDs (the cassette's recorded
notebook/artifact ids) so :func:`resolve_notebook` / :func:`resolve_artifact`
take their full-UUID fast path and never add a ``LIST_NOTEBOOKS`` /
``LIST_ARTIFACTS`` preflight the cassette lacks. The batchexecute body matcher is
shape-only, so the id VALUES are decorative — the RPC *sequence* and body *shape*
are what replay.

NOT covered here — both need a live-auth recording session to capture a RPC
sequence no existing cassette holds (see #1733):

* ``studio_delete``'s cross-type routing — its merged notes+artifacts preflight
  issues ``GET_NOTES_AND_MIND_MAPS`` (``cFji9``) **twice** + ``LIST_ARTIFACTS``
  (``gArtLc``) before the delete RPC.
* ``studio_rename`` — its kind-aware ``mind_maps.list`` probe likewise issues
  ``cFji9`` **twice** + ``gArtLc`` (``list_note_backed`` + the interactive-map
  ``artifacts.list``) before ``RENAME_ARTIFACT`` (``rc3d8d``); the CLI
  ``artifacts_rename.yaml`` predates that probe and carries a single ``cFji9``.

Both are otherwise unit-covered (``tests/unit/mcp/test_artifacts.py``) and their
underlying mutation RPCs (``V5N4be`` / ``AH0mwd`` / ``rc3d8d``) are already
VCR-exercised by the CLI suite, so neither introduces an un-tested wire shape.
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
