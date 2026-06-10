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

The point is to PIN the serialized ``structured_content`` wire shape — which is
INCONSISTENT with the notebook tools:

* ``note_create`` is FLAT with a ``created: true`` flag
  (``{"notebook_id", "title", "note_id", "created"}``) — note the contrast with
  ``notebook_create``, which nests under a ``notebook`` key.
* ``note_list`` is ``{"notebook_id", "notes": [...]}``.
* ``note_delete`` (confirmed) is ``{"status", "notebook_id", "note_id"}``.

``note_update`` is NOT covered — see the module-level skip note below.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Recorded ids decoded from each cassette's ``f.req`` body.
NOTE_CREATE_NOTEBOOK_ID = "f66923f0-1df4-4ffe-9822-3ed63c558b1c"  # notes_create.yaml
NOTE_LIST_NOTEBOOK_ID = "167481cd-23a3-4331-9a45-c8948900bf91"  # notes_list.yaml
NOTE_DELETE_NOTEBOOK_ID = "06f0c5bd-108f-4c8b-8911-34b2acc656de"  # notes_delete.yaml
NOTE_DELETE_NOTE_ID = "7027c957-5230-4fc1-adf1-3ea5c3041d5a"  # notes_delete.yaml


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("notes_create.yaml")
async def test_mcp_note_create_over_vcr() -> None:
    """``note_create`` creates a note through the real client over VCR.

    End-to-end: ``note_create`` tool -> ``resolve_notebook`` (full UUID, no
    list) -> ``execute_note_create`` -> ``client.notes.create`` which issues
    ``CREATE_NOTE`` (``CYK0Xb``) THEN finalizes title/content via ``UPDATE_NOTE``
    (``cYAfTb``) — both recorded in ``notes_create.yaml``.

    Pins the FLAT ``created``-flag wire shape — the load-bearing asymmetry vs.
    ``notebook_create``, which nests under a ``notebook`` key. The note tool
    builds its dict by hand (not ``to_jsonable`` over a result dataclass), so the
    shape is hand-authored and worth pinning verbatim.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "note_create",
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
@notebooklm_vcr.use_cassette("notes_list.yaml")
async def test_mcp_note_list_over_vcr() -> None:
    """``note_list`` returns the notebook's notes through the real client.

    End-to-end: ``note_list`` tool -> ``resolve_notebook`` (full UUID, no list)
    -> ``client.notes.list`` -> recorded ``GET_NOTES_AND_MIND_MAPS`` (``cFji9``)
    RPC.

    Pins the ``{"notebook_id", "notes": [...]}`` wire shape. The recorded
    notebook happens to hold zero text notes, so this asserts the shape +
    ``notes`` being a list rather than its non-emptiness.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool("note_list", {"notebook": NOTE_LIST_NOTEBOOK_ID})

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == NOTE_LIST_NOTEBOOK_ID
    notes = structured["notes"]
    assert isinstance(notes, list)


@pytest.mark.asyncio
async def test_mcp_note_delete_two_step_confirm_over_vcr() -> None:
    """``note_delete`` confirm-gate: preview-then-delete over real cassettes.

    Step 1 (``confirm`` omitted): the tool resolves the note (full UUID, no
    list) then lists notes for the preview title
    (``GET_NOTES_AND_MIND_MAPS`` -> ``cFji9``, replayed from ``notes_list.yaml``)
    and returns a ``needs_confirmation`` envelope WITHOUT issuing ``DELETE_NOTE``.

    Step 2 (``confirm=True``): the tool issues the real ``DELETE_NOTE``
    (``AH0mwd``) mutation, replayed from ``notes_delete.yaml`` (whose leading
    ``CREATE_NOTE`` / ``UPDATE_NOTE`` interactions go unused — VCR
    ``record_mode="none"`` does not require every recorded interaction to be
    played back).

    Two separate cassettes because the preview path needs the note-list RPC
    (which the delete cassette lacks) while the confirmed path needs the delete
    RPC (which the list cassette lacks).
    """
    # Step 1 — preview only: title lookup lists notes, no delete RPC.
    with notebooklm_vcr.use_cassette("notes_list.yaml"):
        async with build_mcp_client() as mcp_client:
            preview = await mcp_client.call_tool(
                "note_delete",
                {"notebook": NOTE_LIST_NOTEBOOK_ID, "note": NOTE_DELETE_NOTE_ID},
            )

    preview_structured = preview.structured_content
    assert isinstance(preview_structured, dict)
    assert preview_structured["status"] == "needs_confirmation"
    inner = preview_structured["preview"]
    assert inner["action"] == "delete_note"
    assert inner["notebook_id"] == NOTE_LIST_NOTEBOOK_ID
    assert inner["note_id"] == NOTE_DELETE_NOTE_ID
    assert "title" in inner

    # Step 2 — confirmed delete replays the real DELETE_NOTE mutation.
    with notebooklm_vcr.use_cassette("notes_delete.yaml"):
        async with build_mcp_client() as mcp_client:
            deleted = await mcp_client.call_tool(
                "note_delete",
                {
                    "notebook": NOTE_DELETE_NOTEBOOK_ID,
                    "note": NOTE_DELETE_NOTE_ID,
                    "confirm": True,
                },
            )

    deleted_structured = deleted.structured_content
    assert isinstance(deleted_structured, dict)
    assert deleted_structured["status"] == "deleted"
    assert deleted_structured["notebook_id"] == NOTE_DELETE_NOTEBOOK_ID
    assert deleted_structured["note_id"] == NOTE_DELETE_NOTE_ID


# ---------------------------------------------------------------------------
# note_update is intentionally NOT covered (reuse-only constraint).
#
# The MCP ``note_update`` tool drives the public ``client.notes.update`` facade,
# which since v0.8.0 (#1362) runs an existence preflight — ``get_or_none`` ->
# ``GET_NOTES_AND_MIND_MAPS`` (``cFji9``) — BEFORE issuing ``UPDATE_NOTE``
# (``cYAfTb``). The candidate cassette ``notes_create_and_update.yaml`` was
# recorded PRE-flip and holds only ``CREATE_NOTE`` + ``UPDATE_NOTE`` — no
# ``cFji9`` interaction — so the preflight RPC has nothing to replay against, and
# NO existing cassette pairs ``cFji9`` with ``cYAfTb``. The comprehensive VCR
# test dodges this by monkeypatching ``client.notes.get_or_none``, but an
# integration test that mocks the facade would defeat the whole purpose of
# exercising the real path. Covering it would require re-recording a cassette,
# which is forbidden here.
# ---------------------------------------------------------------------------
