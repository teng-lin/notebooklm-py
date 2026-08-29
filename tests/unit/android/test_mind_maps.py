"""Offline B7 Android mind-map composition and evidence-gate tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._android.mind_maps import AndroidMindMapsAPI
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._notes import NotesAPI
from notebooklm.exceptions import DecodingError, MindMapNotFoundError, UnsupportedOperationError
from notebooklm.types import Artifact, ArtifactType, MindMap, MindMapKind


def _note_map(
    mind_map_id: str = "note-map",
    *,
    title: str = "Note map",
) -> MindMap:
    return MindMap(
        id=mind_map_id,
        notebook_id="notebook-1",
        title=title,
        kind=MindMapKind.NOTE_BACKED,
        tree={"name": "Root", "children": []},
    )


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
    note_maps: list[Any] | None = None,
    artifacts: list[Artifact] | None = None,
) -> tuple[AndroidMindMapsAPI, MagicMock, MagicMock]:
    notes = MagicMock(spec=NotesAPI)
    notes.list_mind_maps = AsyncMock(return_value=note_maps or [])
    notes.update = AsyncMock()
    notes.delete_mind_map = AsyncMock()

    artifact_api = MagicMock(spec=ArtifactsAPI)
    artifact_api.list = AsyncMock(return_value=artifacts or [])
    artifact_api.rename = AsyncMock()
    artifact_api.delete = AsyncMock()

    return (
        AndroidMindMapsAPI(artifacts=artifact_api, notes=notes),
        artifact_api,
        notes,
    )


def test_direct_graph_requires_and_retains_exact_base_collaborators() -> None:
    import inspect

    api, artifacts, notes = _graph()
    parameters = inspect.signature(AndroidMindMapsAPI).parameters

    assert parameters["artifacts"].default is inspect.Parameter.empty
    assert parameters["notes"].default is inspect.Parameter.empty
    assert api._artifacts is artifacts
    assert api._notes is notes


@pytest.mark.asyncio
async def test_list_composes_decoded_note_maps_then_interactive_artifacts() -> None:
    note_map = _note_map()
    interactive = _interactive_artifact()
    api, artifacts, notes = _graph(note_maps=[note_map], artifacts=[interactive])

    result = await api.list("notebook-1")

    assert result == [
        note_map,
        MindMap(
            id="interactive",
            notebook_id="notebook-1",
            title="Interactive",
            kind=MindMapKind.INTERACTIVE,
        ),
    ]
    notes.list_mind_maps.assert_awaited_once_with("notebook-1")
    artifacts.list.assert_awaited_once_with("notebook-1", ArtifactType.MIND_MAP)


@pytest.mark.asyncio
async def test_list_note_backed_uses_only_decoded_note_kind_evidence() -> None:
    note_map = _note_map()
    api, artifacts, notes = _graph(
        note_maps=[note_map],
        artifacts=[_interactive_artifact()],
    )

    assert await api.list_note_backed("notebook-1") == [note_map]
    notes.list_mind_maps.assert_awaited_once_with("notebook-1")
    artifacts.list.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_note_backed_does_not_infer_a_raw_row_kind() -> None:
    api, artifacts, _ = _graph(
        note_maps=[["note-map", '{"name":"Root","children":[]}']],
    )

    with pytest.raises(DecodingError, match="decoded note-backed mind maps"):
        await api.list_note_backed("notebook-1")

    artifacts.list.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_and_get_or_none_preserve_public_lookup_semantics() -> None:
    note_map = _note_map()
    api, _, _ = _graph(note_maps=[note_map])
    assert await api.get("notebook-1", "note-map") is note_map

    missing_api, _, _ = _graph()
    assert await missing_api.get_or_none("notebook-1", "missing") is None
    with pytest.raises(MindMapNotFoundError, match="missing"):
        await missing_api.get("notebook-1", "missing")


@pytest.mark.asyncio
async def test_rename_composes_through_notes_and_artifacts() -> None:
    note_map = _note_map()
    interactive = _interactive_artifact()
    api, artifacts, notes = _graph(note_maps=[note_map], artifacts=[interactive])

    assert (
        await api.rename(
            "notebook-1",
            "note-map",
            "Renamed note map",
            kind=MindMapKind.NOTE_BACKED,
            return_object=False,
        )
        is None
    )
    notes.update.assert_awaited_once_with(
        "notebook-1",
        "note-map",
        '{"name":"Root","children":[]}',
        "Renamed note map",
    )

    assert (
        await api.rename(
            "notebook-1",
            "interactive",
            "Renamed interactive",
            kind=MindMapKind.INTERACTIVE,
            return_object=False,
        )
        is None
    )
    artifacts.rename.assert_awaited_once_with(
        "notebook-1",
        "interactive",
        "Renamed interactive",
        return_object=False,
    )


@pytest.mark.asyncio
async def test_delete_composes_through_notes_and_artifacts() -> None:
    api, artifacts, notes = _graph()

    await api.delete("notebook-1", "note-map", kind=MindMapKind.NOTE_BACKED)
    await api.delete("notebook-1", "interactive", kind=MindMapKind.INTERACTIVE)

    notes.delete_mind_map.assert_awaited_once_with("notebook-1", "note-map")
    artifacts.delete.assert_awaited_once_with("notebook-1", "interactive")


UnsupportedCall = Callable[[AndroidMindMapsAPI], Awaitable[object]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
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
                "note-map",
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
    api, artifacts, notes = _graph(
        note_maps=[_note_map()],
        artifacts=[_interactive_artifact()],
    )

    with pytest.raises(UnsupportedOperationError, match="web backend"):
        await invoke(api)

    notes.list_mind_maps.assert_not_awaited()
    notes.update.assert_not_awaited()
    notes.delete_mind_map.assert_not_awaited()
    artifacts.list.assert_not_awaited()
    artifacts.rename.assert_not_awaited()
    artifacts.delete.assert_not_awaited()
