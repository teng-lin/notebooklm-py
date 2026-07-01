"""MCP notebook-tool VCR tests (reuse-only).

Full-stack coverage (MCP tool -> ``_app`` -> real ``NotebookLMClient`` ->
VCR-replayed RPC) for the notebook mutation/describe tools, reusing the SAME
cassettes the CLI / comprehensive VCR suites recorded. ``NOTEBOOKLM_VCR_RECORD``
is deliberately NOT set — no cassette is ever re-recorded here.

Every tool is invoked with a FULL canonical UUID (the cassette's recorded
notebook id, decoded from its ``f.req`` body) so :func:`resolve_notebook` takes
its full-UUID fast path and never adds an extra ``LIST_NOTEBOOKS`` RPC the
cassette lacks. The ``notebooklm_vcr`` body matcher is shape-only for
batchexecute requests (leaf UUIDs collapse to a sentinel), so the specific id
value is decorative — reusing the recorded id keeps intent obvious.

The point of these tests is to PIN the serialized ``structured_content`` wire
shape an MCP client actually receives — which differs per tool:

* ``notebook_create`` is flat: the created notebook's fields at the top level
  with its id exposed as ``notebook_id`` (#1540) — matching ``note_create``.
* ``notebook_describe`` is flat: ``{"notebook_id", "description": {...}}``.
* ``notebook_rename`` is flat: ``{"notebook_id", "new_title"}``.
* ``notebook_delete`` (confirmed) is flat: ``{"status", "notebook_id"}``.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Recorded notebook ids decoded from each cassette's ``f.req`` body (the value is
# decorative under the shape-only matcher, but reusing the recording's id keeps
# the full-UUID fast path obvious and the intent self-documenting).
CREATE_TITLE = "VCR Test Notebook"
DESCRIBE_NOTEBOOK_ID = "167481cd-23a3-4331-9a45-c8948900bf91"  # notebooks_get_description.yaml
RENAME_NOTEBOOK_ID = "f66923f0-1df4-4ffe-9822-3ed63c558b1c"  # notebooks_rename.yaml
DELETE_NOTEBOOK_ID = "fc9cc125-fc20-439b-9f3d-d801c5b0de38"  # notebooks_delete.yaml


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("notebooks_create.yaml")
async def test_mcp_notebook_create_over_vcr() -> None:
    """``notebook_create`` returns the created notebook with non-null timestamps.

    End-to-end: FastMCP ``Client`` -> ``notebook_create`` tool ->
    ``execute_notebook_create`` -> ``client.notebooks.create`` -> recorded
    ``CREATE_NOTEBOOK`` (``CCqFvf``) RPC, THEN the #1699 best-effort re-read
    ``client.notebooks.get`` -> recorded ``GET_NOTEBOOK`` (``rLM1Ne``) RPC whose
    response carries populated ``meta[5]``/``meta[8]`` timestamps.

    Pins the *flat* wire shape: the created notebook's fields land at the top
    level with its id exposed as ``notebook_id`` (matching ``note_create`` and
    ``notebook_delete``), NOT nested under a ``notebook`` key (#1540).
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool("notebook_create", {"title": CREATE_TITLE})

    structured = result.structured_content
    assert isinstance(structured, dict)
    # Flat: the id is exposed as ``notebook_id`` and the record is NOT nested.
    assert structured.get("notebook_id"), "created notebook is missing a notebook_id"
    # Pin the EXACT flat key set — every ``Notebook`` dataclass field, with
    # ``id`` renamed to ``notebook_id``. Asserting the exact set (not just
    # ``in`` checks) is the load-bearing guard: it catches BOTH a regression
    # back to the nested ``{"notebook": ...}`` shape AND a silently dropped
    # field such as ``modified_at`` ("no metadata is dropped"). A new Notebook
    # field will fail this assertion by design, forcing a conscious wire-shape
    # decision.
    assert set(structured) == {
        "status",
        "notebook_id",
        "title",
        "created_at",
        "sources_count",
        "is_owner",
        "modified_at",
    }
    # #1699: CREATE_NOTEBOOK returns null created_at/modified_at; the tool's
    # GET_NOTEBOOK re-read populates them. Asserting NON-null is the load-bearing
    # guard that the enrichment actually ran — a regression to the create result
    # (or a silent fallback) would leave these null and fail here.
    assert structured["created_at"] is not None, "created_at should be populated by the re-read"
    assert structured["modified_at"] is not None, "modified_at should be populated by the re-read"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("notebooks_get_description.yaml")
async def test_mcp_notebook_describe_over_vcr() -> None:
    """``notebook_describe`` returns the AI description through the real client.

    End-to-end: ``notebook_describe`` tool -> ``resolve_notebook`` (full UUID,
    no list) -> ``execute_notebook_describe`` -> ``client.notebooks.get_description``
    -> recorded ``SUMMARIZE`` (``VfAZjd``) RPC. (``notebooks_get.yaml`` would NOT
    match — describe issues ``SUMMARIZE``, not ``GET_NOTEBOOK``.)

    Pins the FLAT wire shape ``{"notebook_id", "description": {...}}``.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool("notebook_describe", {"notebook": DESCRIBE_NOTEBOOK_ID})

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == DESCRIBE_NOTEBOOK_ID
    description = structured["description"]
    assert isinstance(description, dict)
    # NotebookDescription dataclass fields.
    assert "summary" in description
    assert "suggested_topics" in description
    assert isinstance(description["suggested_topics"], list)


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("notebooks_rename.yaml")
async def test_mcp_notebook_rename_over_vcr() -> None:
    """``notebook_rename`` renames through the real client over VCR.

    End-to-end: ``notebook_rename`` tool -> ``resolve_notebook`` (full UUID, no
    list) -> ``execute_notebook_rename`` -> ``client.notebooks.rename`` which
    issues ``RENAME_NOTEBOOK`` (``s0tc2d``) THEN re-fetches via ``GET_NOTEBOOK``
    (``rLM1Ne``) — both recorded in ``notebooks_rename.yaml``.

    Pins the FLAT wire shape ``{"notebook_id", "new_title"}``.
    """
    new_title = "VCR Test Renamed"
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "notebook_rename",
            {"notebook": RENAME_NOTEBOOK_ID, "new_title": new_title},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == RENAME_NOTEBOOK_ID
    assert structured["new_title"] == new_title


@pytest.mark.asyncio
async def test_mcp_notebook_delete_two_step_confirm_over_vcr() -> None:
    """``notebook_delete`` confirm-gate: preview-then-delete over real cassettes.

    Step 1 (``confirm`` omitted): the tool resolves the notebook (full UUID, no
    list) then lists notebooks for the preview title (``LIST_NOTEBOOKS`` ->
    ``wXbhsf``, replayed from ``notebooks_list.yaml``) and returns a
    ``needs_confirmation`` envelope WITHOUT issuing ``DELETE_NOTEBOOK``.

    Step 2 (``confirm=True``): the tool issues the real ``DELETE_NOTEBOOK``
    (``WWINqb``) mutation, replayed from ``notebooks_delete.yaml`` (whose leading
    ``CREATE_NOTEBOOK`` interactions go unused — VCR ``record_mode="none"`` does
    not require every recorded interaction to be played back).

    Two separate cassettes because the preview path needs the notebook-list RPC
    (which the delete cassette lacks) while the confirmed path needs the delete
    RPC (which the list cassette lacks).
    """
    # Step 1 — preview only: title lookup lists notebooks, no delete RPC.
    with notebooklm_vcr.use_cassette("notebooks_list.yaml"):
        async with build_mcp_client() as mcp_client:
            preview = await mcp_client.call_tool(
                "notebook_delete",
                {"notebook": DELETE_NOTEBOOK_ID},
            )

    preview_structured = preview.structured_content
    assert isinstance(preview_structured, dict)
    assert preview_structured["status"] == "needs_confirmation"
    inner = preview_structured["preview"]
    assert inner["action"] == "delete_notebook"
    assert inner["notebook_id"] == DELETE_NOTEBOOK_ID
    # ``title`` is present in the preview (None when the id isn't in the list).
    assert "title" in inner

    # Step 2 — confirmed delete replays the real DELETE_NOTEBOOK mutation.
    with notebooklm_vcr.use_cassette("notebooks_delete.yaml"):
        async with build_mcp_client() as mcp_client:
            deleted = await mcp_client.call_tool(
                "notebook_delete",
                {"notebook": DELETE_NOTEBOOK_ID, "confirm": True},
            )

    deleted_structured = deleted.structured_content
    assert isinstance(deleted_structured, dict)
    assert deleted_structured["status"] == "deleted"
    assert deleted_structured["notebook_id"] == DELETE_NOTEBOOK_ID
