"""Offline B7 Android mind-map composition and evidence-gate tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from notebooklm._android.mind_maps import AndroidMindMapsAPI
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._notes import NotesAPI
from notebooklm.exceptions import MindMapNotFoundError, UnsupportedOperationError
from notebooklm.types import Artifact, MindMap, MindMapKind


def _interactive_artifact(artifact_id: str = "interactive") -> Artifact:
    return Artifact(
        id=artifact_id,
        title="Interactive",
        _artifact_type=4,
        status=3,
        _variant=4,
    )


def _graph(
    *,
    artifacts: list[Artifact] | None = None,
    note_backed: list[MindMap] | None = None,
) -> tuple[AndroidMindMapsAPI, MagicMock, MagicMock]:
    notes = MagicMock(spec=NotesAPI)
    notes._list_note_backed_mind_maps = AsyncMock(return_value=note_backed or [])
    # B6's public method remains a raw ``list[Any]`` evidence gate. A raw-looking
    # value makes accidental use by B7 fail the interaction assertions below.
    notes.list_mind_maps = AsyncMock(return_value=[["raw-note-row"]])
    notes.update = AsyncMock()
    notes.delete_mind_map = AsyncMock(return_value=None)

    artifact_api = MagicMock(spec=ArtifactsAPI)
    artifact_api.list = AsyncMock(return_value=artifacts or [])
    artifact_api.rename = AsyncMock()
    artifact_api.delete = AsyncMock()

    return (
        AndroidMindMapsAPI(artifacts=artifact_api, notes=notes),
        artifact_api,
        notes,
    )


def _assert_no_dependency_io(artifacts: MagicMock, notes: MagicMock) -> None:
    notes._list_note_backed_mind_maps.assert_not_awaited()
    notes.list_mind_maps.assert_not_awaited()
    notes.update.assert_not_awaited()
    notes.delete_mind_map.assert_not_awaited()
    artifacts.list.assert_not_awaited()
    artifacts.rename.assert_not_awaited()
    artifacts.delete.assert_not_awaited()


def test_direct_graph_requires_and_retains_exact_base_collaborators() -> None:
    import inspect

    api, artifacts, notes = _graph()
    parameters = inspect.signature(AndroidMindMapsAPI).parameters

    assert parameters["artifacts"].default is inspect.Parameter.empty
    assert parameters["notes"].default is inspect.Parameter.empty
    assert api._artifacts is artifacts
    assert api._notes is notes


def test_direct_graph_requires_private_typed_notes_read_seam() -> None:
    artifacts = MagicMock(spec=ArtifactsAPI)
    notes = MagicMock(spec=NotesAPI)

    with pytest.raises(TypeError, match="private typed note-backed"):
        AndroidMindMapsAPI(artifacts=artifacts, notes=notes)


def _note_backed_mind_map(map_id: str = "note-map") -> MindMap:
    return MindMap(
        id=map_id,
        notebook_id="notebook-1",
        title="Note-backed",
        kind=MindMapKind.NOTE_BACKED,
        created_at=None,
        tree={"name": "Root", "children": []},
    )


@pytest.mark.asyncio
async def test_typed_note_backed_read_composes_aggregate_without_raw_note_rows() -> None:
    note_map = _note_backed_mind_map()
    api, artifacts, notes = _graph(
        note_backed=[note_map],
        artifacts=[_interactive_artifact()],
    )

    assert await api.list_note_backed("notebook-1") == [note_map]
    aggregate = await api.list("notebook-1")
    assert [item.id for item in aggregate] == ["note-map", "interactive"]
    assert aggregate[0] is note_map
    assert aggregate[1].kind is MindMapKind.INTERACTIVE
    assert aggregate[1].tree is None

    assert notes._list_note_backed_mind_maps.await_count == 2
    notes._list_note_backed_mind_maps.assert_awaited_with("notebook-1")
    notes.list_mind_maps.assert_not_awaited()
    artifacts.list.assert_awaited_once_with("notebook-1", ANY)


@pytest.mark.asyncio
async def test_get_and_get_or_none_use_typed_aggregate_read() -> None:
    api, artifacts, notes = _graph(note_backed=[_note_backed_mind_map()])

    found = await api.get("notebook-1", "note-map")
    assert found.id == "note-map"
    assert await api.get_or_none("notebook-1", "missing") is None

    assert notes._list_note_backed_mind_maps.await_count == 2
    notes.list_mind_maps.assert_not_awaited()
    assert artifacts.list.await_count == 2


UnsupportedCall = Callable[[AndroidMindMapsAPI], Awaitable[object]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(
            lambda api: api.rename(
                "notebook-1",
                "map",
                "Renamed",
                return_object=False,
            ),
            id="rename-auto-detect",
        ),
        pytest.param(
            lambda api: api.rename(
                "notebook-1",
                "map",
                "Renamed",
                kind=MindMapKind.NOTE_BACKED,
                return_object=False,
            ),
            id="rename-note-backed",
        ),
        pytest.param(
            lambda api: api.rename(
                "notebook-1",
                "interactive",
                "Renamed",
                kind=MindMapKind.INTERACTIVE,
            ),
            id="rename-interactive-with-hydration",
        ),
        pytest.param(lambda api: api.delete("notebook-1", "map"), id="delete-auto-detect"),
        pytest.param(
            lambda api: api.generate(
                "notebook-1",
                ["source-1"],
                kind=MindMapKind.NOTE_BACKED,
            ),
            id="generate-note-backed",
        ),
        pytest.param(
            lambda api: api.generate(
                "notebook-1",
                None,
                kind=MindMapKind.INTERACTIVE,
            ),
            id="generate-interactive",
        ),
        pytest.param(
            lambda api: api.get_tree(
                "notebook-1",
                "map",
                kind=MindMapKind.NOTE_BACKED,
            ),
            id="get-tree-note-backed",
        ),
        pytest.param(
            lambda api: api.get_tree(
                "notebook-1",
                "interactive",
                kind=MindMapKind.INTERACTIVE,
            ),
            id="get-tree-interactive",
        ),
    ],
)
async def test_evidence_gated_operations_fail_before_dependency_io(
    invoke: UnsupportedCall,
) -> None:
    api, artifacts, notes = _graph(artifacts=[_interactive_artifact()])

    with pytest.raises(UnsupportedOperationError, match="web backend"):
        await invoke(api)

    _assert_no_dependency_io(artifacts, notes)


@pytest.mark.asyncio
async def test_explicit_interactive_rename_composes_without_note_reads() -> None:
    api, artifacts, notes = _graph(artifacts=[_interactive_artifact()])

    assert (
        await api.rename(
            "notebook-1",
            "interactive",
            "Renamed",
            kind=MindMapKind.INTERACTIVE,
            return_object=False,
        )
        is None
    )

    artifacts.list.assert_awaited_once_with("notebook-1")
    artifacts.rename.assert_awaited_once_with(
        "notebook-1",
        "interactive",
        "Renamed",
        return_object=False,
    )
    notes.list_mind_maps.assert_not_awaited()
    notes.update.assert_not_awaited()
    notes.delete_mind_map.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_interactive_rename_preserves_missing_error() -> None:
    api, artifacts, notes = _graph()

    with pytest.raises(MindMapNotFoundError, match="missing"):
        await api.rename(
            "notebook-1",
            "missing",
            "Renamed",
            kind=MindMapKind.INTERACTIVE,
            return_object=False,
        )

    artifacts.list.assert_awaited_once_with("notebook-1")
    artifacts.rename.assert_not_awaited()
    notes.list_mind_maps.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_interactive_delete_composes_without_aggregate_reads() -> None:
    api, artifacts, notes = _graph()

    assert (
        await api.delete(
            "notebook-1",
            "interactive",
            kind=MindMapKind.INTERACTIVE,
        )
        is None
    )

    artifacts.delete.assert_awaited_once_with("notebook-1", "interactive")
    artifacts.list.assert_not_awaited()
    notes.list_mind_maps.assert_not_awaited()
    notes.delete_mind_map.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_backed_delete_delegates_to_notes_kind_safe_delete() -> None:
    api, artifacts, notes = _graph()

    assert (
        await api.delete(
            "notebook-1",
            "note-map",
            kind=MindMapKind.NOTE_BACKED,
        )
        is None
    )

    notes.delete_mind_map.assert_awaited_once_with("notebook-1", "note-map")
    notes.list_mind_maps.assert_not_awaited()
    artifacts.list.assert_not_awaited()
    artifacts.delete.assert_not_awaited()
