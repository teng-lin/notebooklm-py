"""Unit tests for the transport-neutral ``notebooklm._app.artifacts`` core.

These pin the relocated artifact business logic at the ``_app`` boundary
(independent of the Click adapter): the kind-aware mind-map dispatch for
``rename`` / ``delete``, the not-found raise on ``get``, the typed result
dataclasses (the CLI builds the ``--json`` envelope from their fields), and the
neutral :class:`ArtifactStatusView` DTO (``status_view`` projection + ``getattr``
defaults + duck-typed-source tolerance).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.artifacts import (
    ArtifactExportResult,
    ArtifactRenameResult,
    ArtifactStatusView,
    delete_artifact,
    export_artifact,
    get_artifact,
    poll_artifact,
    rename_artifact,
    retry_artifact,
    status_view,
    wait_for_artifact,
)
from notebooklm.exceptions import ArtifactNotFoundError
from notebooklm.types import (
    Artifact,
    ExportType,
    GenerationStatus,
    MindMap,
    MindMapKind,
)


def _client() -> MagicMock:
    client = MagicMock()
    client.artifacts = MagicMock()
    client.mind_maps = MagicMock()
    client.notes = MagicMock()
    return client


# ---------------------------------------------------------------------------
# get_artifact — fail-loud on a miss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_artifact_returns_artifact() -> None:
    client = _client()
    art = Artifact(id="art_1", title="T", _artifact_type=1, status=3)
    client.artifacts.get_or_none = AsyncMock(return_value=art)
    result = await get_artifact(client, "nb", "art_1")
    assert result is art
    client.artifacts.get_or_none.assert_awaited_once_with("nb", "art_1")


@pytest.mark.asyncio
async def test_get_artifact_raises_not_found() -> None:
    client = _client()
    client.artifacts.get_or_none = AsyncMock(return_value=None)
    client.artifacts.list = AsyncMock(return_value=[])
    with pytest.raises(ArtifactNotFoundError):
        await get_artifact(client, "nb", "art_gone")
    # No list call — the neutral get is a single get_or_none (the partial-id
    # resolution + full-id fast path live in the CLI resolver, not here).
    client.artifacts.list.assert_not_called()


# ---------------------------------------------------------------------------
# rename_artifact — kind-aware mind-map dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_regular_artifact_uses_artifacts_rename() -> None:
    client = _client()
    client.mind_maps.list = AsyncMock(return_value=[])
    client.mind_maps.rename = AsyncMock()
    client.artifacts.rename = AsyncMock()
    result = await rename_artifact(client, "nb", "art_1", "New Title")
    assert result == ArtifactRenameResult("art_1", "New Title", is_mind_map=False)
    client.artifacts.rename.assert_awaited_once_with(
        "nb", "art_1", "New Title", return_object=False
    )
    client.mind_maps.rename.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [MindMapKind.NOTE_BACKED, MindMapKind.INTERACTIVE])
async def test_rename_mind_map_dispatches_kind_aware(kind: MindMapKind) -> None:
    client = _client()
    client.mind_maps.list = AsyncMock(
        return_value=[MindMap(id="mm_1", notebook_id="nb", title="Old", kind=kind)]
    )
    client.mind_maps.rename = AsyncMock()
    client.artifacts.rename = AsyncMock()
    result = await rename_artifact(client, "nb", "mm_1", "New Title")
    assert result.is_mind_map is True
    client.mind_maps.rename.assert_awaited_once_with(
        "nb", "mm_1", "New Title", kind=kind, return_object=False
    )
    # Mind maps never fall through to the regular-artifact rename path.
    client.artifacts.rename.assert_not_called()


# ---------------------------------------------------------------------------
# delete_artifact — note-backed mind map vs regular artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_regular_artifact() -> None:
    client = _client()
    client.notes.list_mind_maps = AsyncMock(return_value=[])
    client.notes.delete = AsyncMock()
    client.artifacts.delete = AsyncMock()
    assert await delete_artifact(client, "nb", "art_1") is False
    client.artifacts.delete.assert_awaited_once_with("nb", "art_1")
    client.notes.delete.assert_not_called()


@pytest.mark.asyncio
async def test_delete_note_backed_mind_map_clears_via_notes() -> None:
    client = _client()
    client.notes.list_mind_maps = AsyncMock(
        return_value=[["mm_1", ["mm_1", "{}", None, None, "MM Title"]]]
    )
    client.notes.delete = AsyncMock()
    client.artifacts.delete = AsyncMock()
    assert await delete_artifact(client, "nb", "mm_1") is True
    client.notes.delete.assert_awaited_once_with("nb", "mm_1")
    client.artifacts.delete.assert_not_called()


# ---------------------------------------------------------------------------
# export_artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("export_type", "expected_enum"),
    [("docs", ExportType.DOCS), ("sheets", ExportType.SHEETS)],
)
async def test_export_maps_type_and_returns_typed_result(export_type, expected_enum) -> None:
    client = _client()
    client.artifacts.export = AsyncMock(return_value={"url": "https://x"})
    result = await export_artifact(client, "nb", "art_1", "My Title", export_type)
    assert isinstance(result, ArtifactExportResult)
    assert result.exported is True
    assert (result.artifact_id, result.title, result.export_type, result.result) == (
        "art_1",
        "My Title",
        export_type,
        {"url": "https://x"},
    )
    # content is None so the backend retrieves it from the artifact id.
    client.artifacts.export.assert_awaited_once_with("nb", "art_1", None, "My Title", expected_enum)


@pytest.mark.asyncio
async def test_export_falsy_result_marks_not_exported() -> None:
    client = _client()
    client.artifacts.export = AsyncMock(return_value=None)
    result = await export_artifact(client, "nb", "art_1", "T", "docs")
    assert result.exported is False
    assert result.result is None


# ---------------------------------------------------------------------------
# poll / wait / retry — raw GenerationStatus pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_returns_raw_status() -> None:
    client = _client()
    status = GenerationStatus(task_id="t", status="completed")
    client.artifacts.poll_status = AsyncMock(return_value=status)
    assert await poll_artifact(client, "nb", "t") is status


@pytest.mark.asyncio
async def test_poll_passes_non_status_payload_through_unprojected() -> None:
    # The CLI non-JSON path prints the object directly, so a non-status payload
    # must pass through untouched (no eager attribute projection).
    client = _client()
    payload = {"status": "completed", "artifact_id": "art_1"}
    client.artifacts.poll_status = AsyncMock(return_value=payload)
    assert await poll_artifact(client, "nb", "t") is payload


@pytest.mark.asyncio
async def test_retry_returns_raw_status() -> None:
    client = _client()
    status = GenerationStatus(task_id="t", status="in_progress")
    client.artifacts.retry_failed = AsyncMock(return_value=status)
    assert await retry_artifact(client, "nb", "art_1") is status


@pytest.mark.asyncio
async def test_wait_forwards_interval_and_timeout() -> None:
    client = _client()
    status = GenerationStatus(task_id="t", status="completed")
    client.artifacts.wait_for_completion = AsyncMock(return_value=status)
    assert await wait_for_artifact(client, "nb", "a", initial_interval=5.0, timeout=60.0) is status
    client.artifacts.wait_for_completion.assert_awaited_once_with(
        "nb", "a", initial_interval=5.0, timeout=60.0
    )


# ---------------------------------------------------------------------------
# ArtifactStatusView / status_view — the neutral DTO
# ---------------------------------------------------------------------------


def test_status_view_projects_full_generation_status() -> None:
    status = GenerationStatus(
        task_id="t",
        status="completed",
        url="https://x",
        error=None,
        error_code="CODE",
        metadata={"k": "v"},
    )
    view = status_view(status)
    assert view == ArtifactStatusView(
        task_id="t",
        status="completed",
        url="https://x",
        error=None,
        error_code="CODE",
        metadata={"k": "v"},
        is_complete=status.is_complete,
    )


def test_status_view_tolerates_duck_typed_source_without_optional_attrs() -> None:
    # A minimal duck-typed source missing error_code/metadata must default to
    # None via getattr rather than raising — the projector is defensive.
    class _Minimal:
        task_id = "t"
        status = "completed"
        url = None
        error = None
        is_complete = True

    view = status_view(_Minimal())
    assert view.error_code is None
    assert view.metadata is None
    assert view.is_complete is True
