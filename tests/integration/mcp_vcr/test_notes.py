"""MCP note-tool VCR tests (reuse-only).

Full-stack coverage (MCP tool -> ``_app`` -> real ``NotebookLMClient`` ->
VCR-replayed RPC) for the note tools, reusing the SAME cassettes the
comprehensive VCR suite recorded. ``NOTEBOOKLM_VCR_RECORD`` is deliberately NOT
set — no cassette is ever re-recorded here.

Every tool is invoked with FULL canonical UUIDs (the cassette's recorded
notebook/note ids, decoded from each ``f.req`` body) so :func:`resolve_notebook`
/ :func:`resolve_note` take their full-UUID fast path and never add an extra
``LIST_NOTEBOOKS`` / ``GET_NOTES_AND_MIND_MAPS`` RPC the cassette lacks. The
body matcher is shape-only for batchexecute requests, so the id value itself is
decorative.

The point is to PIN the serialized ``structured_content`` wire shape — each note
tool builds its dict BY HAND (not ``to_jsonable`` over a result dataclass):

* ``note_save`` create is FLAT with a ``created: true`` flag
  (``{"notebook_id", "title", "note_id", "created"}``).

Reading and deleting notes moved to the cross-type Studio surface (``studio_list``
merges notes with artifacts; ``studio_delete`` deletes a note or an artifact) —
that cross-type routing is covered by the unit tests with ``mock_client`` (no
merged-list+DELETE_NOTE cassette exists and none is recorded here).

``note_update`` IS covered by ``mcp_note_update.yaml`` — a cassette recorded
specifically for the MCP integration suite that pairs the post-#1362 existence
preflight (``GET_NOTES_AND_MIND_MAPS`` → ``cFji9``) with the ``UPDATE_NOTE``
(``cYAfTb``) mutation, the exact pair no pre-existing cassette holds.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Recorded ids decoded from each cassette's ``f.req`` body.
NOTE_CREATE_NOTEBOOK_ID = "f66923f0-1df4-4ffe-9822-3ed63c558b1c"  # notes_create.yaml
NOTE_UPDATE_NOTEBOOK_ID = "2bba3730-4547-48c7-b5f5-e631eb5332ca"  # mcp_note_update.yaml
NOTE_UPDATE_NOTE_ID = "39f5e968-5eab-4a8a-9bd7-d10756febe0c"  # mcp_note_update.yaml


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("notes_create.yaml")
async def test_mcp_note_save_create_over_vcr() -> None:
    """``note_create`` creates a note through the real client over VCR.

    End-to-end: ``note_create`` tool -> ``resolve_notebook`` (full UUID, no
    list) -> ``execute_note_create`` -> ``client.notes.create`` which issues
    ``CREATE_NOTE`` (``CYK0Xb``) THEN finalizes title/content via ``UPDATE_NOTE``
    (``cYAfTb``) — both recorded in ``notes_create.yaml``.

    Pins the FLAT ``created``-flag wire shape. The note tool builds its dict by
    hand (not ``to_jsonable`` over a result dataclass), so the shape is
    hand-authored and worth pinning verbatim.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "note_save",
            {
                "notebook": NOTE_CREATE_NOTEBOOK_ID,
                "title": "VCR Test Note",
                "content": "This is a test note created by VCR recording.",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    # Flat shape — NOT nested under a "note" key, with an explicit created flag.
    assert structured["notebook_id"] == NOTE_CREATE_NOTEBOOK_ID
    assert structured["title"] == "VCR Test Note"
    assert structured.get("note_id"), "created note is missing a note_id"
    assert structured["created"] is True


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("mcp_note_update.yaml")
async def test_mcp_note_save_update_over_vcr() -> None:
    """``note_update`` updates a note's content through the real client over VCR.

    End-to-end: ``note_update`` tool -> ``resolve_notebook`` / ``resolve_note``
    (full UUIDs, no list) -> ``execute_note_save`` -> ``client.notes.update``.
    Post-#1362 the public ``update`` facade runs an existence preflight via
    ``get_or_none`` -> ``GET_NOTES_AND_MIND_MAPS`` (``cFji9``) BEFORE issuing the
    real ``UPDATE_NOTE`` (``cYAfTb``) mutation. ``mcp_note_update.yaml`` is the
    first cassette to pair those two RPCs, so this exercises the real preflight
    path rather than monkeypatching ``get_or_none`` the way the comprehensive VCR
    suite does.

    Pins the flat ``{"status": "updated", "notebook_id", "note_id"}`` wire shape
    — hand-authored by the tool (not ``to_jsonable`` over a result dataclass).
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "note_save",
            {
                "notebook": NOTE_UPDATE_NOTEBOOK_ID,
                "note": NOTE_UPDATE_NOTE_ID,
                "content": "Updated content recorded by the MCP VCR suite.",
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["status"] == "updated"
    assert structured["notebook_id"] == NOTE_UPDATE_NOTEBOOK_ID
    assert structured["note_id"] == NOTE_UPDATE_NOTE_ID
