"""Offline mind-map Android mind-map composition and evidence-gate tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from tests._helpers.android_supervisor import SupervisedAndroidTransport

from notebooklm._android.artifacts import AndroidArtifactsAPI
from notebooklm._android.mind_maps import AndroidMindMapsAPI
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._mind_maps_api import MindMapsAPI
from notebooklm._notes import NotesAPI
from notebooklm._types.research import MindMapResult
from notebooklm.exceptions import ArtifactNotReadyError, MindMapNotFoundError, NoteNotFoundError
from notebooklm.types import Artifact, GenerationStatus, MindMap, MindMapKind, Note


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


class _Supervisor:
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.active_scopes: list[str] = []
        self.scope_events: list[tuple[str, str]] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        self.active_scopes.append(label)
        self.scope_events.append(("enter", label))
        try:
            yield _Lease()
        finally:
            assert self.active_scopes[-1] == label
            self.active_scopes.pop()
            self.scope_events.append(("exit", label))


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
    supervisor: _Supervisor | None = None,
) -> tuple[AndroidMindMapsAPI, MagicMock, MagicMock]:
    notes = MagicMock(spec=NotesAPI)
    notes._list_note_backed_mind_maps = AsyncMock(return_value=note_backed or [])
    # Notes' public method remains a raw ``list[Any]`` evidence gate. A raw-looking
    # value makes accidental use by mind-map fail the interaction assertions below.
    notes.list_mind_maps = AsyncMock(return_value=[["raw-note-row"]])
    notes.get = AsyncMock(
        return_value=Note(
            id="note-map",
            notebook_id="notebook-1",
            title="Note-backed",
            content='{"name":"Root","children":[]}',
        )
    )
    notes.update = AsyncMock()
    notes.delete_mind_map = AsyncMock(return_value=None)

    artifact_api = MagicMock(spec=AndroidArtifactsAPI)
    artifact_api.list = AsyncMock(return_value=artifacts or [])
    artifact_api._list_all_studio = AsyncMock(return_value=artifacts or [])
    artifact_api.rename = AsyncMock()
    artifact_api.delete = AsyncMock()
    artifact_api.wait_for_completion = AsyncMock(
        return_value=GenerationStatus(task_id="interactive", status="completed")
    )
    artifact_api.generate_mind_map = AsyncMock(
        return_value=MindMapResult(
            mind_map={"name": "Root", "children": []},
            note_id="note-generated",
        )
    )
    artifact_api._generate_interactive_mind_map = AsyncMock(
        return_value=GenerationStatus(task_id="interactive", status="pending")
    )
    artifact_api._get_interactive_mind_map_tree = AsyncMock(
        return_value={"name": "Interactive", "children": []}
    )

    return (
        AndroidMindMapsAPI(
            session=cast(Any, supervisor or _Supervisor()),
            artifacts=artifact_api,
            notes=notes,
        ),
        artifact_api,
        notes,
    )


def test_direct_graph_requires_and_retains_exact_base_collaborators() -> None:
    api, artifacts, notes = _graph()
    parameters = inspect.signature(AndroidMindMapsAPI).parameters

    assert parameters["session"].default is inspect.Parameter.empty
    assert parameters["artifacts"].default is inspect.Parameter.empty
    assert parameters["notes"].default is inspect.Parameter.empty
    assert api._artifacts is artifacts
    assert api._notes is notes


def test_public_callable_manifest_is_complete_without_compatibility_methods() -> None:
    expected = {
        "delete",
        "generate",
        "get",
        "get_or_none",
        "get_tree",
        "list",
        "list_note_backed",
        "rename",
    }

    for adapter in (MindMapsAPI, AndroidMindMapsAPI):
        assert {
            name
            for name, member in inspect.getmembers(adapter)
            if not name.startswith("_") and callable(member)
        } == expected
    assert AndroidMindMapsAPI.__abstractmethods__ == frozenset()


def test_direct_graph_requires_private_typed_notes_read_seam() -> None:
    artifacts = MagicMock(spec=ArtifactsAPI)
    notes = MagicMock(spec=NotesAPI)

    with pytest.raises(TypeError, match="private typed note-backed"):
        AndroidMindMapsAPI(
            session=cast(Any, _Supervisor()),
            artifacts=artifacts,
            notes=notes,
        )


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
    notes._list_note_backed_mind_maps.reset_mock()
    aggregate = await api.list("notebook-1")
    assert [item.id for item in aggregate] == ["note-map", "interactive"]
    assert aggregate[0] is note_map
    assert aggregate[1].kind is MindMapKind.INTERACTIVE
    assert aggregate[1].tree is None

    notes._list_note_backed_mind_maps.assert_awaited_once_with("notebook-1")
    notes.list_mind_maps.assert_not_awaited()
    artifacts._list_all_studio.assert_awaited_once_with("notebook-1")
    artifacts.list.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_and_get_or_none_use_typed_aggregate_read() -> None:
    api, artifacts, notes = _graph(note_backed=[_note_backed_mind_map()])

    found = await api.get("notebook-1", "note-map")
    assert found.id == "note-map"
    assert await api.get_or_none("notebook-1", "missing") is None

    assert notes._list_note_backed_mind_maps.await_count == 2
    notes.list_mind_maps.assert_not_awaited()
    assert artifacts._list_all_studio.await_count == 2
    artifacts.list.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_backed_generate_composes_native_artifact_result() -> None:
    api, artifacts, notes = _graph(artifacts=[_interactive_artifact()])

    result = await api.generate(
        "notebook-1",
        ["source-1"],
        kind=MindMapKind.NOTE_BACKED,
        language="fr",
        instructions="Group by theme",
    )

    assert result == MindMap(
        id="note-generated",
        notebook_id="notebook-1",
        title="Root",
        kind=MindMapKind.NOTE_BACKED,
        tree={"name": "Root", "children": []},
    )
    artifacts.generate_mind_map.assert_awaited_once_with(
        "notebook-1",
        ["source-1"],
        "fr",
        "Group by theme",
    )
    artifacts._generate_interactive_mind_map.assert_not_awaited()
    notes._list_note_backed_mind_maps.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_generate_uses_live_create_wait_list_and_tree_seams() -> None:
    interactive = _interactive_artifact()
    api, artifacts, notes = _graph(artifacts=[interactive])

    result = await api.generate(
        "notebook-1",
        ["source-1"],
        kind=MindMapKind.INTERACTIVE,
        language="en",
        instructions="focus",
        wait=True,
    )

    assert result == MindMap(
        id="interactive",
        notebook_id="notebook-1",
        title="Interactive",
        kind=MindMapKind.INTERACTIVE,
        tree={"name": "Interactive", "children": []},
    )
    artifacts._generate_interactive_mind_map.assert_awaited_once_with(
        "notebook-1",
        ["source-1"],
        language="en",
        instructions="focus",
    )
    artifacts.wait_for_completion.assert_awaited_once_with("notebook-1", "interactive")
    artifacts._list_all_studio.assert_awaited_once_with("notebook-1")
    artifacts._get_interactive_mind_map_tree.assert_awaited_once_with("notebook-1", "interactive")
    notes._list_note_backed_mind_maps.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_generate_without_wait_skips_poll_and_tree() -> None:
    api, artifacts, _ = _graph()

    result = await api.generate(
        "notebook-1",
        None,
        kind=MindMapKind.INTERACTIVE,
        wait=False,
    )

    assert result.id == "interactive"
    assert result.tree is None
    artifacts.wait_for_completion.assert_not_awaited()
    artifacts._get_interactive_mind_map_tree.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "removed"])
async def test_interactive_generate_rejects_unsuccessful_terminal_wait_status(
    terminal_status: str,
) -> None:
    api, artifacts, _ = _graph(artifacts=[_interactive_artifact()])
    artifacts.wait_for_completion.return_value = GenerationStatus(
        task_id="interactive",
        status=terminal_status,
        error="generation did not complete",
    )

    with pytest.raises(ArtifactNotReadyError, match=f"status: {terminal_status}"):
        await api.generate(
            "notebook-1",
            ["source-1"],
            kind=MindMapKind.INTERACTIVE,
            wait=True,
        )

    artifacts.list.assert_not_awaited()
    artifacts._list_all_studio.assert_not_awaited()
    artifacts._get_interactive_mind_map_tree.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_generate_holds_outer_scope_through_nested_tree_read() -> None:
    supervisor = _Supervisor()
    api, artifacts, _ = _graph(
        artifacts=[_interactive_artifact()],
        supervisor=supervisor,
    )
    tree_started = asyncio.Event()
    tree_release = asyncio.Event()

    async def _read_tree(_notebook_id: str, _artifact_id: str) -> dict[str, Any]:
        tree_started.set()
        await tree_release.wait()
        return {"name": "Interactive", "children": []}

    artifacts._get_interactive_mind_map_tree.side_effect = _read_tree
    task = asyncio.create_task(
        api.generate(
            "notebook-1",
            ["source-1"],
            kind=MindMapKind.INTERACTIVE,
            wait=True,
        )
    )
    await tree_started.wait()

    try:
        assert supervisor.active_scopes == ["mind_maps.generate", "mind_maps.get_tree"]
    finally:
        tree_release.set()

    assert (await task).tree == {"name": "Interactive", "children": []}
    assert supervisor.active_scopes == []
    assert supervisor.scope_events == [
        ("enter", "mind_maps.generate"),
        ("enter", "mind_maps.get_tree"),
        ("exit", "mind_maps.get_tree"),
        ("exit", "mind_maps.generate"),
    ]


@pytest.mark.asyncio
async def test_explicit_interactive_tree_passes_notebook_for_ownership_preflight() -> None:
    api, artifacts, notes = _graph()

    tree = await api.get_tree(
        "notebook-1",
        "interactive",
        kind=MindMapKind.INTERACTIVE,
    )

    assert tree == {"name": "Interactive", "children": []}
    artifacts._get_interactive_mind_map_tree.assert_awaited_once_with("notebook-1", "interactive")
    artifacts.list.assert_not_awaited()
    notes._list_note_backed_mind_maps.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_tree_holds_outer_scope_until_interactive_read_finishes() -> None:
    supervisor = _Supervisor()
    api, artifacts, _ = _graph(supervisor=supervisor)
    tree_started = asyncio.Event()
    tree_release = asyncio.Event()

    async def _read_tree(_notebook_id: str, _artifact_id: str) -> dict[str, Any]:
        tree_started.set()
        await tree_release.wait()
        return {"name": "Interactive", "children": []}

    artifacts._get_interactive_mind_map_tree.side_effect = _read_tree
    task = asyncio.create_task(
        api.get_tree(
            "notebook-1",
            "interactive",
            kind=MindMapKind.INTERACTIVE,
        )
    )
    await tree_started.wait()

    try:
        assert supervisor.active_scopes == ["mind_maps.get_tree"]
    finally:
        tree_release.set()

    assert await task == {"name": "Interactive", "children": []}
    assert supervisor.active_scopes == []
    assert supervisor.scope_events == [
        ("enter", "mind_maps.get_tree"),
        ("exit", "mind_maps.get_tree"),
    ]


@pytest.mark.asyncio
async def test_note_backed_rename_preserves_exact_persisted_content() -> None:
    note_map = _note_backed_mind_map()
    api, artifacts, notes = _graph(note_backed=[note_map])

    assert (
        await api.rename(
            "notebook-1",
            "note-map",
            "Renamed",
            kind=MindMapKind.NOTE_BACKED,
            return_object=False,
        )
        is None
    )

    notes._list_note_backed_mind_maps.assert_awaited_once_with("notebook-1")
    notes.get.assert_awaited_once_with("notebook-1", "note-map")
    notes.update.assert_awaited_once_with(
        "notebook-1",
        "note-map",
        '{"name":"Root","children":[]}',
        "Renamed",
    )
    artifacts.list.assert_not_awaited()


@pytest.mark.asyncio
async def test_note_backed_rename_maps_a_vanished_note_to_mind_map_not_found() -> None:
    api, artifacts, notes = _graph(note_backed=[_note_backed_mind_map()])
    notes.get.side_effect = NoteNotFoundError("note-map")

    with pytest.raises(MindMapNotFoundError, match="note-map"):
        await api.rename(
            "notebook-1",
            "note-map",
            "Renamed",
            kind=MindMapKind.NOTE_BACKED,
            return_object=False,
        )

    notes.update.assert_not_awaited()
    artifacts.list.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_detect_rename_hydrates_interactive_map() -> None:
    before = _interactive_artifact()
    after = Artifact(
        id="interactive",
        title="Renamed",
        _artifact_type=4,
        status=3,
        _variant=4,
    )
    api, artifacts, notes = _graph(artifacts=[before])
    artifacts._list_all_studio.side_effect = [[before], [after]]

    renamed = await api.rename("notebook-1", "interactive", "Renamed")

    assert renamed is not None
    assert (renamed.id, renamed.title, renamed.kind) == (
        "interactive",
        "Renamed",
        MindMapKind.INTERACTIVE,
    )
    assert notes._list_note_backed_mind_maps.await_count == 2
    assert artifacts._list_all_studio.await_count == 2
    artifacts.list.assert_not_awaited()
    artifacts.rename.assert_awaited_once_with(
        "notebook-1",
        "interactive",
        "Renamed",
        return_object=False,
    )


@pytest.mark.asyncio
async def test_auto_detect_delete_dispatches_and_missing_is_idempotent() -> None:
    api, artifacts, notes = _graph(
        note_backed=[_note_backed_mind_map()],
        artifacts=[_interactive_artifact()],
    )

    assert await api.delete("notebook-1", "note-map") is None
    notes.delete_mind_map.assert_awaited_once_with("notebook-1", "note-map")

    notes._list_note_backed_mind_maps.return_value = []
    assert await api.delete("notebook-1", "interactive") is None
    artifacts.delete.assert_awaited_once_with("notebook-1", "interactive")

    artifacts._list_all_studio.return_value = []
    assert await api.delete("notebook-1", "missing") is None
    assert artifacts.delete.await_count == 1


@pytest.mark.asyncio
async def test_get_tree_reads_note_backed_and_auto_detects_missing() -> None:
    tree = {"name": "Root", "children": []}
    api, artifacts, notes = _graph(note_backed=[_note_backed_mind_map()])

    assert (
        await api.get_tree(
            "notebook-1",
            "note-map",
            kind=MindMapKind.NOTE_BACKED,
        )
        == tree
    )
    assert await api.get_tree("notebook-1", "note-map") == tree

    notes._list_note_backed_mind_maps.return_value = []
    assert await api.get_tree("notebook-1", "missing") is None
    artifacts._list_all_studio.assert_awaited_once_with("notebook-1")


@pytest.mark.asyncio
async def test_get_tree_auto_detected_interactive_reads_exact_app_tree_after_detection() -> None:
    api, artifacts, notes = _graph(artifacts=[_interactive_artifact()])

    assert await api.get_tree("notebook-1", "interactive") == {
        "name": "Interactive",
        "children": [],
    }

    notes._list_note_backed_mind_maps.assert_awaited_once_with("notebook-1")
    artifacts._list_all_studio.assert_awaited_once_with("notebook-1")
    artifacts._get_interactive_mind_map_tree.assert_awaited_once_with("notebook-1", "interactive")


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

    artifacts._list_all_studio.assert_awaited_once_with("notebook-1")
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

    artifacts._list_all_studio.assert_awaited_once_with("notebook-1")
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
async def test_delete_holds_outer_scope_until_artifact_delete_finishes() -> None:
    supervisor = _Supervisor()
    api, artifacts, _ = _graph(supervisor=supervisor)
    delete_started = asyncio.Event()
    delete_release = asyncio.Event()

    async def _delete(_notebook_id: str, _artifact_id: str) -> None:
        delete_started.set()
        await delete_release.wait()

    artifacts.delete.side_effect = _delete
    task = asyncio.create_task(
        api.delete(
            "notebook-1",
            "interactive",
            kind=MindMapKind.INTERACTIVE,
        )
    )
    await delete_started.wait()

    try:
        assert supervisor.active_scopes == ["mind_maps.delete"]
    finally:
        delete_release.set()

    assert await task is None
    assert supervisor.active_scopes == []
    assert supervisor.scope_events == [
        ("enter", "mind_maps.delete"),
        ("exit", "mind_maps.delete"),
    ]


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


class _SupervisedNotes:
    def __init__(self, transport: SupervisedAndroidTransport) -> None:
        self._transport = transport

    async def _list_note_backed_mind_maps(self, notebook_id: str) -> list[MindMap]:
        return await self._transport.unary(
            "notes.list_note_backed",
            notebook_id,
            replay_safe=True,
            response_type=list,
        )


class _SupervisedArtifacts:
    def __init__(self, transport: SupervisedAndroidTransport) -> None:
        self._transport = transport

    async def list(self, notebook_id: str, artifact_type: Any = None) -> list[Artifact]:
        return await self._transport.unary(
            "artifacts.list",
            (notebook_id, artifact_type),
            replay_safe=True,
            response_type=list,
        )

    async def _list_all_studio(self, notebook_id: str) -> list[Artifact]:
        return await self._transport.unary(
            "artifacts.list",
            (notebook_id, None),
            replay_safe=True,
            response_type=list,
        )

    async def rename(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
        *,
        return_object: bool,
    ) -> None:
        await self._transport.unary(
            "artifacts.rename",
            (notebook_id, mind_map_id, new_title, return_object),
            replay_safe=False,
            response_type=type(None),
        )


def _supervised_api(transport: SupervisedAndroidTransport) -> AndroidMindMapsAPI:
    return AndroidMindMapsAPI(
        session=cast(Any, transport),
        artifacts=cast(Any, _SupervisedArtifacts(transport)),
        notes=cast(Any, _SupervisedNotes(transport)),
    )


@pytest.mark.asyncio
async def test_aggregate_list_finishes_during_graceful_drain() -> None:
    transport = SupervisedAndroidTransport()
    notes_started = asyncio.Event()
    notes_release = asyncio.Event()

    async def _notes(_request: Any, _kwargs: dict[str, Any]) -> Any:
        notes_started.set()
        await notes_release.wait()
        return [_note_backed_mind_map()]

    transport.handlers["notes.list_note_backed"] = _notes
    transport.handlers["artifacts.list"] = [_interactive_artifact()]
    task = asyncio.create_task(_supervised_api(transport).list("notebook-1"))
    await notes_started.wait()

    await transport.supervisor.stop_accepting(1)
    notes_release.set()

    assert [item.id for item in await task] == ["note-map", "interactive"]
    assert [method for method, _request, _kwargs in transport.calls] == [
        "notes.list_note_backed",
        "artifacts.list",
    ]
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_aggregate_list_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    notes_started = asyncio.Event()
    notes_release = asyncio.Event()

    async def _notes(_request: Any, _kwargs: dict[str, Any]) -> Any:
        notes_started.set()
        await notes_release.wait()
        return [_note_backed_mind_map()]

    transport.handlers["notes.list_note_backed"] = _notes
    transport.handlers["artifacts.list"] = [_interactive_artifact()]
    task = asyncio.create_task(_supervised_api(transport).list("notebook-1"))
    await notes_started.wait()

    old_generation = await transport.force_close_and_reopen()
    notes_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == ["notes.list_note_backed"]
    assert old_generation.in_flight == 0


@pytest.mark.asyncio
async def test_interactive_rename_finishes_during_graceful_drain() -> None:
    transport = SupervisedAndroidTransport()
    list_started = asyncio.Event()
    list_release = asyncio.Event()

    async def _artifacts(_request: Any, _kwargs: dict[str, Any]) -> Any:
        list_started.set()
        await list_release.wait()
        return [_interactive_artifact()]

    transport.handlers["artifacts.list"] = _artifacts
    transport.handlers["artifacts.rename"] = None
    task = asyncio.create_task(
        _supervised_api(transport).rename(
            "notebook-1",
            "interactive",
            "Renamed",
            kind=MindMapKind.INTERACTIVE,
            return_object=False,
        )
    )
    await list_started.wait()

    await transport.supervisor.stop_accepting(1)
    list_release.set()

    assert await task is None
    assert [method for method, _request, _kwargs in transport.calls] == [
        "artifacts.list",
        "artifacts.rename",
    ]


@pytest.mark.asyncio
async def test_interactive_rename_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    list_started = asyncio.Event()
    list_release = asyncio.Event()

    async def _artifacts(_request: Any, _kwargs: dict[str, Any]) -> Any:
        list_started.set()
        await list_release.wait()
        return [_interactive_artifact()]

    transport.handlers["artifacts.list"] = _artifacts
    transport.handlers["artifacts.rename"] = None
    task = asyncio.create_task(
        _supervised_api(transport).rename(
            "notebook-1",
            "interactive",
            "Renamed",
            kind=MindMapKind.INTERACTIVE,
            return_object=False,
        )
    )
    await list_started.wait()

    old_generation = await transport.force_close_and_reopen()
    list_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == ["artifacts.list"]
    assert old_generation.in_flight == 0
