"""Offline B7 Android mind-map composition and evidence-gate tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from tests._helpers.android_supervisor import SupervisedAndroidTransport

from notebooklm._android.mind_maps import AndroidMindMapsAPI
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._notes import NotesAPI
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm.exceptions import MindMapNotFoundError, UnsupportedOperationError
from notebooklm.types import Artifact, MindMap, MindMapKind


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


class _Supervisor:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease()


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
        AndroidMindMapsAPI(
            supervisor=cast(CallSupervisor, _Supervisor()),
            artifacts=artifact_api,
            notes=notes,
        ),
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

    assert parameters["supervisor"].default is inspect.Parameter.empty
    assert parameters["artifacts"].default is inspect.Parameter.empty
    assert parameters["notes"].default is inspect.Parameter.empty
    assert api._artifacts is artifacts
    assert api._notes is notes


def test_direct_graph_requires_private_typed_notes_read_seam() -> None:
    artifacts = MagicMock(spec=ArtifactsAPI)
    notes = MagicMock(spec=NotesAPI)

    with pytest.raises(TypeError, match="private typed note-backed"):
        AndroidMindMapsAPI(
            supervisor=cast(CallSupervisor, _Supervisor()),
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
        supervisor=transport.supervisor,
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
