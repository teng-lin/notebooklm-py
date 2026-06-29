"""Live-only contract tests for the MCP tools — the behaviours that mocked unit
tests structurally cannot prove, against the real NotebookLM API.

The headline case is the ``source_ids`` collapse (#1652): the ``artifact_generate``
tool deliberately maps BOTH an omitted ``source_ids`` AND an explicit
``source_ids=[]`` to ``None`` ("all sources"), shielding callers from the raw
backend behaviour where ``[]`` means "zero sources" (which the backend refuses).
A mock only proves the resolver *received* ``None``; only a LIVE call proves the
backend actually ACCEPTS the collapsed all-sources request and starts generating.

Also covers not-found resolution, destructive confirm-gating (the entity must
survive a no-confirm call), and the MCP error projection shape. All ``e2e``
(light) so they run nightly.

Requires auth and the ``mcp`` extra (``importorskip``); auto-marked ``e2e`` by
``conftest.pytest_itemcollected``.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

# Require the `mcp` extra; skip the whole module cleanly when fastmcp is absent.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from ._mcp_live_helpers import call_tool as _call  # noqa: E402 - after importorskip guard
from .conftest import requires_auth  # noqa: E402 - after importorskip guard

pytestmark = pytest.mark.e2e


@requires_auth
class TestSourceIdsContract:
    """#1652: omitted ``source_ids`` == ``[]`` == all sources, identically."""

    @pytest.mark.asyncio
    async def test_omitted_and_empty_source_ids_both_generate_from_all(
        self, client, generation_notebook_id
    ):
        """Both omitting ``source_ids`` and passing ``[]`` start generation and
        return a ``task_id`` — the tool collapses ``[]`` → ``None`` (all sources),
        never forwarding the backend-refused empty selection.

        This is the highest-value live catch: a regression that stopped
        collapsing ``[]`` would surface here (the ``[]`` call would fail with the
        backend's "… generation is unavailable") while passing every mock.
        """
        omitted = await _call(
            client,
            "artifact_generate",
            {"notebook": generation_notebook_id, "artifact_type": "report"},
        )
        assert omitted.get("task_id"), f"omitted source_ids did not generate: {omitted}"

        # Brief pause so the second submit isn't racing the first on the same
        # notebook — if the backend serializes generation it can reject a
        # back-to-back submit with FAILED_PRECONDITION (not a RateLimitError, so
        # the skip wrapper wouldn't catch it). The contract under test is source-id
        # resolution, not concurrent submits.
        await asyncio.sleep(3)

        empty = await _call(
            client,
            "artifact_generate",
            {
                "notebook": generation_notebook_id,
                "artifact_type": "report",
                "source_ids": [],
            },
        )
        assert empty.get("task_id"), f"empty source_ids did not generate: {empty}"


@requires_auth
class TestNotFoundResolution:
    """A bogus id surfaces a clean not-found error, never a silent null success."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_describe_bogus_notebook_errors(self, client):
        bogus = f"nonexistent-{uuid4().hex}"
        with pytest.raises(ToolError) as excinfo:
            await _call(client, "notebook_describe", {"notebook": bogus})
        # The MCP error projection carries a structured CODE in the message.
        assert "NOT_FOUND" in str(excinfo.value) or "not found" in str(excinfo.value).lower()


@requires_auth
class TestConfirmGating:
    """Destructive tools without ``confirm`` preview-only; the entity survives."""

    @pytest.mark.asyncio
    async def test_note_delete_without_confirm_does_not_delete(self, client, temp_notebook):
        nb = temp_notebook.id
        created = await _call(
            client,
            "note_create",
            {"notebook": nb, "title": "Confirm-Gate Note", "content": "Body."},
        )
        note_id = created["note_id"]

        # No confirm → preview only.
        preview = await _call(client, "note_delete", {"notebook": nb, "note": note_id})
        assert preview["status"] == "needs_confirmation"

        # The note must still exist (the no-confirm call did NOT delete).
        listing = await _call(client, "note_list", {"notebook": nb})
        assert note_id in [n["id"] for n in listing["notes"]]

        # Clean up with an explicit confirm.
        deleted = await _call(
            client, "note_delete", {"notebook": nb, "note": note_id, "confirm": True}
        )
        assert deleted["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_notebook_delete_without_confirm_does_not_delete(
        self, client, created_notebooks, cleanup_notebooks
    ):
        created = await _call(client, "notebook_create", {"title": f"Confirm-{uuid4().hex[:8]}"})
        nb_id = created["notebook_id"]
        created_notebooks.append(nb_id)

        preview = await _call(client, "notebook_delete", {"notebook": nb_id})
        assert preview["status"] == "needs_confirmation"

        # Still resolvable → not deleted.
        described = await _call(client, "notebook_describe", {"notebook": nb_id})
        assert described["notebook_id"] == nb_id


@requires_auth
class TestErrorProjection:
    """An invalid call surfaces as the MCP error shape, not a raw traceback."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_invalid_artifact_type_is_validation_error(self, client, read_only_notebook_id):
        with pytest.raises(ToolError) as excinfo:
            await _call(
                client,
                "artifact_generate",
                {"notebook": read_only_notebook_id, "artifact_type": "not-a-real-type"},
            )
        assert "VALIDATION" in str(excinfo.value)
