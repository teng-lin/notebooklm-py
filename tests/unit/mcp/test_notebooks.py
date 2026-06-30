"""Unit tests for the notebook MCP tools.

Drives each tool through the in-memory FastMCP ``Client`` against a server bound
to the mocked ``NotebookLMClient`` (the ``mcp_call`` fixture), asserting the
serialized ``structured_content``. Covers the happy path, name-vs-id resolution
reaching the tool, the confirm preview-then-delete flow, and error projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm.exceptions import NotebookNotFoundError  # noqa: E402 - after importorskip guard

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard


@dataclass
class FakeNotebook:
    id: str
    title: str


@dataclass
class FakeNotebookFull:
    """A create-result-shaped notebook mirroring :class:`notebooklm.types.Notebook`.

    Carries the full field set so ``to_jsonable`` emits the flat shape (including
    ``created_at`` / ``modified_at``) the create tool surfaces. The timestamp
    backfill itself lives in the transport-neutral core (``execute_notebook_create``,
    #1705) and is unit-tested there; this fake just lets the MCP test assert the
    tool flattens and surfaces those fields end-to-end.
    """

    id: str
    title: str
    created_at: datetime | None = None
    sources_count: int = 0
    is_owner: bool = True
    modified_at: datetime | None = None


@dataclass
class FakeDescription:
    summary: str


NB_ID = "11111111-1111-1111-1111-111111111111"
NB2_ID = "22222222-2222-2222-2222-222222222222"
CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
MODIFIED_AT = datetime(2026, 1, 3, 4, 5, 6, tzinfo=timezone.utc)


async def test_notebook_list(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[FakeNotebook(id=NB_ID, title="Research")])
    result = await mcp_call("notebook_list")
    assert result.structured_content == {"notebooks": [{"id": NB_ID, "title": "Research"}]}
    mock_client.notebooks.list.assert_awaited_once_with()


async def test_notebook_create_surfaces_backfilled_timestamps(mcp_call, mock_client) -> None:
    """End-to-end wiring: the tool flattens the create result (#1540) and
    surfaces the core's timestamp backfill (#1699/#1705) at the top level.

    The backfill *semantics* (per-key, additive, best-effort fallback) are
    unit-tested against the core in ``tests/unit/app/test_app_notebooks.py``;
    here we only assert the MCP tool wires create → core → flat output, exposing
    the populated ``created_at`` / ``modified_at`` and the id as ``notebook_id``.
    """
    mock_client.notebooks.create = AsyncMock(
        return_value=FakeNotebookFull(id=NB_ID, title="New", sources_count=0, is_owner=True)
    )
    # The core re-reads via GET to backfill the null create timestamps; the GET
    # diverges on the non-timestamp fields to prove the create stays authoritative.
    mock_client.notebooks.get = AsyncMock(
        return_value=FakeNotebookFull(
            id=NB_ID,
            title="Stale",
            created_at=CREATED_AT,
            sources_count=9,
            is_owner=False,
            modified_at=MODIFIED_AT,
        )
    )
    result = await mcp_call("notebook_create", {"title": "New"})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "title": "New",  # from create, NOT the divergent GET
        "created_at": CREATED_AT.isoformat(),  # backfilled by the core
        "sources_count": 0,  # from create
        "is_owner": True,  # from create
        "modified_at": MODIFIED_AT.isoformat(),  # backfilled by the core
    }
    mock_client.notebooks.create.assert_awaited_once_with("New")
    mock_client.notebooks.get.assert_awaited_once_with(NB_ID)


async def test_notebook_describe_by_id(mcp_call, mock_client) -> None:
    mock_client.notebooks.get_description = AsyncMock(
        return_value=FakeDescription(summary="A summary")
    )
    result = await mcp_call("notebook_describe", {"notebook": NB_ID})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "description": {"summary": "A summary"},
    }
    mock_client.notebooks.get_description.assert_awaited_once_with(NB_ID)


async def test_notebook_describe_resolves_by_name(mcp_call, mock_client) -> None:
    """A non-id ``notebook`` ref resolves by exact title before the executor runs."""
    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.notebooks.get_description = AsyncMock(return_value=FakeDescription(summary="s"))
    result = await mcp_call("notebook_describe", {"notebook": "My Notebook"})
    assert result.structured_content["notebook_id"] == NB_ID
    mock_client.notebooks.get_description.assert_awaited_once_with(NB_ID)


async def test_notebook_rename(mcp_call, mock_client) -> None:
    mock_client.notebooks.rename = AsyncMock(return_value=None)
    result = await mcp_call("notebook_rename", {"notebook": NB_ID, "new_title": "Renamed"})
    assert result.structured_content == {"notebook_id": NB_ID, "new_title": "Renamed"}
    mock_client.notebooks.rename.assert_awaited_once_with(NB_ID, "Renamed")


async def test_notebook_delete_without_confirm_previews(mcp_call, mock_client) -> None:
    """confirm=False returns a needs_confirmation preview and does NOT delete."""
    mock_client.notebooks.list = AsyncMock(return_value=[FakeNotebook(id=NB_ID, title="Doomed")])
    mock_client.notebooks.delete = AsyncMock(return_value=None)
    result = await mcp_call("notebook_delete", {"notebook": NB_ID})
    assert result.structured_content == {
        "status": "needs_confirmation",
        "preview": {"action": "delete_notebook", "notebook_id": NB_ID, "title": "Doomed"},
    }
    mock_client.notebooks.delete.assert_not_called()


async def test_notebook_delete_with_confirm_deletes(mcp_call, mock_client) -> None:
    mock_client.notebooks.delete = AsyncMock(return_value=None)
    result = await mcp_call("notebook_delete", {"notebook": NB_ID, "confirm": True})
    assert result.structured_content == {"status": "deleted", "notebook_id": NB_ID}
    mock_client.notebooks.delete.assert_awaited_once_with(NB_ID)


async def test_notebook_delete_confirm_preview_then_delete(mcp_call, mock_client) -> None:
    """Two-step flow: preview first, then the confirmed delete runs."""
    mock_client.notebooks.list = AsyncMock(return_value=[FakeNotebook(id=NB2_ID, title="Target")])
    mock_client.notebooks.delete = AsyncMock(return_value=None)

    preview = await mcp_call("notebook_delete", {"notebook": "Target"})
    assert preview.structured_content["status"] == "needs_confirmation"
    assert preview.structured_content["preview"]["notebook_id"] == NB2_ID
    mock_client.notebooks.delete.assert_not_called()

    confirmed = await mcp_call("notebook_delete", {"notebook": "Target", "confirm": True})
    assert confirmed.structured_content == {"status": "deleted", "notebook_id": NB2_ID}
    mock_client.notebooks.delete.assert_awaited_once_with(NB2_ID)


async def test_notebook_describe_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise NotebookNotFoundError(NB_ID)

    mock_client.notebooks.get_description = AsyncMock(side_effect=_raise)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("notebook_describe", {"notebook": NB_ID})
    assert "NOT_FOUND" in str(excinfo.value)
