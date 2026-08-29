"""Offline fake-server orchestration tests for B6 Android notes and sharing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from notebooklm._android.notes import (
    CREATE_NOTE_METHOD,
    DELETE_NOTES_METHOD,
    GET_NOTES_METHOD,
    MUTATE_NOTE_METHOD,
    SAVED_RESPONSE_NOTE_TYPE,
    AndroidNotesAPI,
    create_note,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    notes_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import common_pb2
from notebooklm._android.proto.labs.language.tailwind.sharing import (
    sharing_pb2 as exact_sharing_pb2,
)
from notebooklm._android.proto.notebooklm.android.wire.v1 import sharing_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.sharing import (
    GET_PROJECT_DETAILS_METHOD,
    SHARE_PROJECT_METHOD,
    AndroidSharingAPI,
)
from notebooklm._client_metrics import ClientMetrics
from notebooklm._notes import NotesAPI
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._sharing import SharingAPI
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm.exceptions import (
    AuthError,
    DecodingError,
    NotebookNotFoundError,
    NoteNotFoundError,
    RateLimitError,
    RPCError,
    ServerError,
    UnsupportedOperationError,
)
from notebooklm.types import MindMapKind, ShareAccess, SharePermission, ShareViewLevel


@dataclass(frozen=True)
class _OperationLease:
    epoch: int


class _OperationScopedSession:
    epoch = 7

    def _initialize_operation_scopes(self) -> None:
        self.operation_scopes: list[tuple[str, int | None]] = []

    @asynccontextmanager
    async def operation_scope(
        self,
        label: str,
        *,
        expected_epoch: int | None = None,
    ) -> AsyncIterator[_OperationLease]:
        self.operation_scopes.append((label, expected_epoch))
        yield _OperationLease(self.epoch)


class FakeB6Server(_OperationScopedSession):
    """Stateful unary fake that models the measured disposable lifecycle."""

    def __init__(self) -> None:
        self._initialize_operation_scopes()
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.notes: dict[str, notes_pb2.ProjectNote] = {
            "note-existing": notes_pb2.ProjectNote(
                id="note-existing",
                content="Existing body",
                name="Existing title",
                metadata=notes_pb2.NoteMetadata(type=notes_pb2.USER_WRITTEN),
            ),
            # Live Web-generated maps retain USER_WRITTEN/UNSPECIFIED on the
            # Android wire; the top-level tree key is the cross-backend kind
            # signal.
            "mind-map": notes_pb2.ProjectNote(
                id="mind-map",
                content='{"children": []}',
                name="Map",
                metadata=notes_pb2.NoteMetadata(
                    type=notes_pb2.USER_WRITTEN,
                    note_prompt_type=notes_pb2.NOTE_PROMPT_TYPE_UNSPECIFIED,
                    last_edit_timestamp=Timestamp(seconds=1_700_000_000),
                ),
            ),
        }
        self.pending_deletion_reads: dict[str, int] = {}
        self.public = False

    def _get_notes(self) -> notes_pb2.GetNotesResponse:
        entries = [notes_pb2.NoteOrStatus()]  # unrecovered status-only arm
        for note_id, note in list(self.notes.items()):
            pending = self.pending_deletion_reads.get(note_id)
            if pending is not None:
                if pending == 0:
                    del self.notes[note_id]
                    del self.pending_deletion_reads[note_id]
                    continue
                self.pending_deletion_reads[note_id] = pending - 1
            entries.append(notes_pb2.NoteOrStatus(note=note))
        return notes_pb2.GetNotesResponse(notes=entries)

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        if method == GET_NOTES_METHOD:
            return self._get_notes()
        if method == CREATE_NOTE_METHOD:
            note = notes_pb2.ProjectNote(
                id="note-created",
                content=request.content,
                name=request.name,
                metadata=request.metadata,
            )
            self.notes[note.id] = note
            return notes_pb2.CreateNoteResponse(note=note)
        if method == MUTATE_NOTE_METHOD:
            edit = request.mutations[0].edit_note_mutation
            note = self.notes[request.note_id]
            note.content = edit.content
            note.name = edit.name
            return notes_pb2.MutateNoteResponse(note=note)
        if method == DELETE_NOTES_METHOD:
            for note_id in request.note_ids:
                if note_id in self.notes:
                    # The first post-delete GetNotes still sees the row; the
                    # second excludes it, matching the disposable live probe.
                    self.pending_deletion_reads[note_id] = 1
            return notes_pb2.DeleteNotesResponse()
        if method == GET_PROJECT_DETAILS_METHOD:
            return sharing_pb2.GetProjectDetailsResponse(
                public_settings=common_pb2.ProjectPublicSettings(
                    is_publicly_readable=self.public,
                    is_discoverable=False,
                ),
                max_individuals_share_limit=1000,
                is_public_sharing_allowed=False,
            )
        if method == SHARE_PROJECT_METHOD:
            self.public = request.project[0].public_document_settings.is_publicly_readable
            return exact_sharing_pb2.ShareProjectResponse()
        raise AssertionError(f"unexpected method: {method}")


class SequencedSession(_OperationScopedSession):
    """Recording session with per-method response/exception queues."""

    def __init__(self, outcomes: dict[str, list[Any]]) -> None:
        self._initialize_operation_scopes()
        self.outcomes = outcomes
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        outcome = self.outcomes[method].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class SupervisedSharingSession:
    """Controlled transport that exercises real nested supervisor admission."""

    def __init__(self) -> None:
        self.supervisor = CallSupervisor(
            metrics=ClientMetrics(),
            drain_tracker=TransportDrainTracker(),
            max_concurrent_rpcs=None,
        )
        self.supervisor.set_bound_loop(asyncio.get_running_loop())
        self.supervisor.reset_after_open()
        self.supervisor.prepare_generation(1)
        self.supervisor.start_accepting(1)
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.mutation_started = asyncio.Event()
        self.mutation_release = asyncio.Event()
        self.public = False

    def operation_scope(self, label: str, **kwargs: Any) -> Any:
        return self.supervisor.operation_scope(label, **kwargs)

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        expected_epoch = kwargs["expected_epoch"]
        async with self.supervisor.call_scope(
            method,
            None,
            None,
            expected_epoch=expected_epoch,
        ):
            self.calls.append((method, request, kwargs))
            if method == SHARE_PROJECT_METHOD:
                self.mutation_started.set()
                await self.mutation_release.wait()
                self.public = request.project[0].public_document_settings.is_publicly_readable
                return exact_sharing_pb2.ShareProjectResponse()
            if method == GET_PROJECT_DETAILS_METHOD:
                return sharing_pb2.GetProjectDetailsResponse(
                    public_settings=common_pb2.ProjectPublicSettings(
                        is_publicly_readable=self.public,
                        is_discoverable=False,
                    )
                )
            raise AssertionError(f"unexpected method: {method}")


class SupervisedNotesSession:
    """Stateful Notes transport backed by the real workflow supervisor."""

    def __init__(self, *, block_method: str) -> None:
        self.supervisor = CallSupervisor(
            metrics=ClientMetrics(),
            drain_tracker=TransportDrainTracker(),
            max_concurrent_rpcs=None,
        )
        self.supervisor.set_bound_loop(asyncio.get_running_loop())
        self.supervisor.reset_after_open()
        self.supervisor.prepare_generation(1)
        self.supervisor.start_accepting(1)
        self.block_method = block_method
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.mutation_started = asyncio.Event()
        self.mutation_release = asyncio.Event()
        self.notes: dict[str, notes_pb2.ProjectNote] = {
            "note-1": notes_pb2.ProjectNote(
                id="note-1",
                content="body",
                name="title",
                metadata=notes_pb2.NoteMetadata(type=notes_pb2.USER_WRITTEN),
            ),
            "map-1": notes_pb2.ProjectNote(
                id="map-1",
                content='{"children": []}',
                name="Map",
                metadata=notes_pb2.NoteMetadata(type=notes_pb2.USER_WRITTEN),
            ),
        }

    def operation_scope(self, label: str, **kwargs: Any) -> Any:
        return self.supervisor.operation_scope(label, **kwargs)

    def _get_notes(self) -> notes_pb2.GetNotesResponse:
        return notes_pb2.GetNotesResponse(
            notes=[notes_pb2.NoteOrStatus(note=note) for note in self.notes.values()]
        )

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        expected_epoch = kwargs["expected_epoch"]
        async with self.supervisor.call_scope(
            method,
            None,
            None,
            expected_epoch=expected_epoch,
        ):
            self.calls.append((method, request, kwargs))
            if method == GET_NOTES_METHOD:
                return self._get_notes()
            if method == self.block_method:
                self.mutation_started.set()
                await self.mutation_release.wait()
            if method == CREATE_NOTE_METHOD:
                note = notes_pb2.ProjectNote(
                    id="note-created",
                    content=request.content,
                    name=request.name,
                    metadata=request.metadata,
                )
                self.notes[note.id] = note
                return notes_pb2.CreateNoteResponse(note=note)
            if method == MUTATE_NOTE_METHOD:
                note = self.notes[request.note_id]
                edit = request.mutations[0].edit_note_mutation
                note.content = edit.content
                note.name = edit.name
                return notes_pb2.MutateNoteResponse(note=note)
            if method == DELETE_NOTES_METHOD:
                for note_id in request.note_ids:
                    self.notes.pop(note_id, None)
                return notes_pb2.DeleteNotesResponse()
            raise AssertionError(f"unexpected method: {method}")


def _session(fake: object) -> AndroidSession:
    return cast(AndroidSession, fake)


def _visible(note_id: str = "note-1") -> notes_pb2.GetNotesResponse:
    return notes_pb2.GetNotesResponse(
        notes=[
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(
                    id=note_id,
                    content="body",
                    name="title",
                    metadata=notes_pb2.NoteMetadata(type=notes_pb2.USER_WRITTEN),
                )
            )
        ]
    )


def test_backend_contracts_are_split_and_android_adapters_are_concrete() -> None:
    import inspect

    assert NotesAPI.__abstractmethods__ == frozenset(
        {
            "create",
            "delete",
            "delete_mind_map",
            "get",
            "get_or_none",
            "list",
            "list_mind_maps",
            "update",
        }
    )
    assert SharingAPI.__abstractmethods__ == frozenset(
        {"get_status", "remove_user", "set_public", "set_users", "set_view_level"}
    )
    assert AndroidNotesAPI.__abstractmethods__ == frozenset()
    assert AndroidSharingAPI.__abstractmethods__ == frozenset()
    assert {
        name
        for name, value in AndroidNotesAPI.__dict__.items()
        if not name.startswith("_") and inspect.iscoroutinefunction(value)
    } == {
        "list",
        "get",
        "get_or_none",
        "create",
        "update",
        "delete",
        "list_mind_maps",
        "delete_mind_map",
    }
    assert {
        name: str(inspect.signature(getattr(AndroidNotesAPI, name)))
        for name in NotesAPI.__abstractmethods__
    } == {
        "list": "(self, notebook_id: 'str') -> 'builtins.list[Note]'",
        "get": "(self, notebook_id: 'str', note_id: 'str') -> 'Note'",
        "get_or_none": "(self, notebook_id: 'str', note_id: 'str') -> 'Note | None'",
        "create": (
            "(self, notebook_id: 'str', title: 'str' = 'New Note', content: 'str' = '') -> 'Note'"
        ),
        "update": (
            "(self, notebook_id: 'str', note_id: 'str', content: 'str', title: 'str') -> 'None'"
        ),
        "delete": "(self, notebook_id: 'str', note_id: 'str') -> 'None'",
        "list_mind_maps": "(self, notebook_id: 'str') -> 'builtins.list[Any]'",
        "delete_mind_map": ("(self, notebook_id: 'str', mind_map_id: 'str') -> 'None'"),
    }


@pytest.mark.asyncio
async def test_fake_server_runs_complete_note_lifecycle_with_exact_orchestration() -> None:
    server = FakeB6Server()
    sleep = AsyncMock()
    notes = AndroidNotesAPI(
        _session(server),
        sleep=sleep,
        deletion_poll_delays=(0.0, 0.0),
    )

    assert [note.id for note in await notes.list("project-1")] == ["note-existing"]
    created = await notes.create("project-1", "Pinned title", "Pinned body")
    assert (created.id, created.title, created.content) == (
        "note-created",
        "Pinned title",
        "Pinned body",
    )

    await notes.update("project-1", created.id, "Edited body", "Edited title")
    updated = await notes.get("project-1", created.id)
    assert (updated.title, updated.content) == ("Edited title", "Edited body")

    await notes.delete("project-1", created.id)
    assert await notes.get_or_none("project-1", created.id) is None
    sleep.assert_not_awaited()

    methods = [method for method, _request, _kwargs in server.calls]
    assert methods == [
        GET_NOTES_METHOD,
        CREATE_NOTE_METHOD,
        GET_NOTES_METHOD,
        GET_NOTES_METHOD,
        MUTATE_NOTE_METHOD,
        GET_NOTES_METHOD,
        GET_NOTES_METHOD,
        GET_NOTES_METHOD,
        DELETE_NOTES_METHOD,
        GET_NOTES_METHOD,
        GET_NOTES_METHOD,
        GET_NOTES_METHOD,
    ]
    writes = {
        CREATE_NOTE_METHOD: notes_pb2.CreateNoteResponse,
        MUTATE_NOTE_METHOD: notes_pb2.MutateNoteResponse,
        DELETE_NOTES_METHOD: notes_pb2.DeleteNotesResponse,
    }
    for method, _request, kwargs in server.calls:
        if method in writes:
            assert kwargs == {
                "replay_safe": False,
                "response_type": writes[method],
                "expected_epoch": 7,
            }
        elif method == GET_NOTES_METHOD:
            assert kwargs == {
                "replay_safe": True,
                "response_type": notes_pb2.GetNotesResponse,
                "expected_epoch": 7,
            }
    assert server.operation_scopes == [
        ("notes.list", None),
        ("notes.create", None),
        ("notes.update", None),
        ("notes.get", None),
        ("notes.delete", None),
        ("notes.get_or_none", None),
    ]


@pytest.mark.asyncio
async def test_reusable_create_seam_encodes_saved_response_for_parallel_chat_hook() -> None:
    server = FakeB6Server()
    saved = await create_note(
        _session(server),
        "project-1",
        title="Saved response",
        content="Answer text",
        note_type=SAVED_RESPONSE_NOTE_TYPE,
    )
    assert saved.id == "note-created"
    method, request, kwargs = server.calls[0]
    assert method == CREATE_NOTE_METHOD
    assert request.metadata.type == SAVED_RESPONSE_NOTE_TYPE
    assert kwargs["replay_safe"] is False


@pytest.mark.asyncio
async def test_note_partial_updates_preserve_the_omitted_field_inside_one_scope() -> None:
    server = FakeB6Server()
    notes = AndroidNotesAPI(_session(server))

    await notes.update("project-1", "note-existing", None, "Renamed title")
    renamed = await notes.get("project-1", "note-existing")
    assert (renamed.title, renamed.content) == ("Renamed title", "Existing body")

    await notes.update("project-1", "note-existing", "Replaced body", None)
    replaced = await notes.get("project-1", "note-existing")
    assert (replaced.title, replaced.content) == ("Renamed title", "Replaced body")

    mutation_requests = [
        request for method, request, _kwargs in server.calls if method == MUTATE_NOTE_METHOD
    ]
    assert [
        (
            request.mutations[0].edit_note_mutation.name,
            request.mutations[0].edit_note_mutation.content,
        )
        for request in mutation_requests
    ] == [
        ("Renamed title", "Existing body"),
        ("Renamed title", "Replaced body"),
    ]
    assert server.operation_scopes == [
        ("notes.update", None),
        ("notes.get", None),
        ("notes.update", None),
        ("notes.get", None),
    ]


@pytest.mark.asyncio
async def test_private_typed_mind_map_read_uses_exact_kind_without_web_rows() -> None:
    server = FakeB6Server()
    mind_maps = await AndroidNotesAPI(_session(server))._list_note_backed_mind_maps("project-1")

    assert len(mind_maps) == 1
    mind_map = mind_maps[0]
    assert (
        mind_map.id,
        mind_map.notebook_id,
        mind_map.title,
        mind_map.kind,
        mind_map.created_at,
        mind_map.tree,
    ) == (
        "mind-map",
        "project-1",
        "Map",
        MindMapKind.NOTE_BACKED,
        None,
        {"children": []},
    )
    assert len(server.calls) == 1
    method, request, kwargs = server.calls[0]
    assert method == GET_NOTES_METHOD
    assert request.project_id == "project-1"
    assert kwargs == {
        "replay_safe": True,
        "response_type": notes_pb2.GetNotesResponse,
        "expected_epoch": 7,
    }
    assert server.operation_scopes == [("notes.list_note_backed_mind_maps", None)]


@pytest.mark.asyncio
async def test_private_typed_mind_map_read_softens_non_object_json_tree_only() -> None:
    response = notes_pb2.GetNotesResponse(
        notes=[
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(
                    id="map-malformed",
                    content="not-json",
                    name="Malformed",
                    metadata=notes_pb2.NoteMetadata(
                        note_prompt_type=notes_pb2.MIND_MAP,
                    ),
                )
            ),
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(
                    id="map-array",
                    content="[]",
                    name="Array",
                    metadata=notes_pb2.NoteMetadata(
                        note_prompt_type=notes_pb2.MIND_MAP,
                    ),
                )
            ),
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(
                    id="ordinary",
                    content='{"name": "Not a map"}',
                    name="Ordinary",
                    metadata=notes_pb2.NoteMetadata(type=notes_pb2.USER_WRITTEN),
                )
            ),
            notes_pb2.NoteOrStatus(),
        ]
    )
    session = SequencedSession({GET_NOTES_METHOD: [response]})

    mind_maps = await AndroidNotesAPI(_session(session))._list_note_backed_mind_maps("project-1")
    assert [(item.id, item.tree) for item in mind_maps] == [
        ("map-malformed", None),
        ("map-array", None),
    ]


@pytest.mark.asyncio
async def test_private_typed_mind_map_read_rejects_missing_evidenced_id() -> None:
    response = notes_pb2.GetNotesResponse(
        notes=[
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(
                    content='{"nodes": []}',
                    name="Missing id",
                    metadata=notes_pb2.NoteMetadata(
                        note_prompt_type=notes_pb2.MIND_MAP,
                    ),
                )
            )
        ]
    )
    session = SequencedSession({GET_NOTES_METHOD: [response]})

    with pytest.raises(DecodingError, match="did not contain a note id"):
        await AndroidNotesAPI(_session(session))._list_note_backed_mind_maps("project-1")


@pytest.mark.asyncio
async def test_public_mind_map_rows_are_minimal_exact_web_compatible_shape() -> None:
    server = FakeB6Server()
    notes = AndroidNotesAPI(_session(server))

    assert await notes.list_mind_maps("project-1") == [["mind-map", '{"children": []}']]
    assert [method for method, _request, _kwargs in server.calls] == [GET_NOTES_METHOD]
    assert server.operation_scopes == [("notes.list_mind_maps", None)]


@pytest.mark.asyncio
async def test_public_note_and_map_lists_share_the_exact_union_classifier() -> None:
    response = notes_pb2.GetNotesResponse(
        notes=[
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(
                    id="prompt-map",
                    content="not-json",
                    metadata=notes_pb2.NoteMetadata(note_prompt_type=notes_pb2.MIND_MAP),
                )
            ),
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(id="children-map", content='{"children": []}')
            ),
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(id="nodes-map", content='{"nodes": []}')
            ),
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(
                    id="ordinary-nested",
                    content='{"wrapper": {"children": []}}',
                    name="Nested key",
                )
            ),
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(
                    id="ordinary-array",
                    content='[{"nodes": []}]',
                    name="Array",
                )
            ),
        ]
    )
    session = SequencedSession({GET_NOTES_METHOD: [response, response]})
    notes = AndroidNotesAPI(_session(session))

    ordinary = await notes.list("project-1")
    assert [item.id for item in ordinary] == ["ordinary-nested", "ordinary-array"]
    assert await notes.list_mind_maps("project-1") == [
        ["prompt-map", "not-json"],
        ["children-map", '{"children": []}'],
        ["nodes-map", '{"nodes": []}'],
    ]


@pytest.mark.asyncio
async def test_exact_id_get_update_and_delete_preserve_web_map_row_semantics() -> None:
    server = FakeB6Server()
    notes = AndroidNotesAPI(_session(server), deletion_poll_delays=(0.0, 0.0))

    fetched = await notes.get("project-1", "mind-map")
    assert (fetched.id, fetched.title, fetched.content) == (
        "mind-map",
        "Map",
        '{"children": []}',
    )
    assert "mind-map" not in {item.id for item in await notes.list("project-1")}

    updated_content = '{"children": [{"name": "Updated"}]}'
    await notes.update("project-1", "mind-map", updated_content, "Updated map")
    updated = await notes.get_or_none("project-1", "mind-map")
    assert updated is not None
    assert (updated.title, updated.content) == ("Updated map", updated_content)

    await notes.delete("project-1", "mind-map")
    assert await notes.get_or_none("project-1", "mind-map") is None


@pytest.mark.asyncio
async def test_mind_map_delete_is_kind_safe_idempotent_and_eventually_confirmed() -> None:
    server = FakeB6Server()
    sleep = AsyncMock()
    notes = AndroidNotesAPI(
        _session(server),
        sleep=sleep,
        deletion_poll_delays=(0.0, 0.0),
    )

    # An ordinary sibling can never be deleted through the map-specific API.
    await notes.delete_mind_map("project-1", "note-existing")
    assert [method for method, _request, _kwargs in server.calls] == [GET_NOTES_METHOD]

    await notes.delete_mind_map("project-1", "mind-map")
    assert "mind-map" not in server.notes
    assert "note-existing" in server.notes
    delete_calls = [call for call in server.calls if call[0] == DELETE_NOTES_METHOD]
    assert len(delete_calls) == 1
    method, request, kwargs = delete_calls[0]
    assert method == DELETE_NOTES_METHOD
    assert list(request.note_ids) == ["mind-map"]
    assert kwargs == {
        "replay_safe": False,
        "response_type": notes_pb2.DeleteNotesResponse,
        "expected_epoch": 7,
    }
    sleep.assert_not_awaited()

    # A second delete is a read-only idempotent success.
    await notes.delete_mind_map("project-1", "mind-map")
    assert len([call for call in server.calls if call[0] == DELETE_NOTES_METHOD]) == 1
    assert server.operation_scopes == [
        ("notes.delete_mind_map", None),
        ("notes.delete_mind_map", None),
        ("notes.delete_mind_map", None),
    ]


@pytest.mark.asyncio
async def test_mind_map_delete_status_five_after_preflight_is_idempotent() -> None:
    map_response = notes_pb2.GetNotesResponse(
        notes=[
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(
                    id="map-1",
                    content='{"nodes": []}',
                    metadata=notes_pb2.NoteMetadata(type=notes_pb2.USER_WRITTEN),
                )
            )
        ]
    )
    session = SequencedSession(
        {
            GET_NOTES_METHOD: [map_response],
            DELETE_NOTES_METHOD: [RPCError("already absent", rpc_code=5)],
        }
    )

    await AndroidNotesAPI(_session(session)).delete_mind_map("project-1", "map-1")
    assert session.calls[-1][2]["replay_safe"] is False


@pytest.mark.asyncio
async def test_mind_map_delete_bounded_confirmation_fails_loud() -> None:
    visible = notes_pb2.GetNotesResponse(
        notes=[
            notes_pb2.NoteOrStatus(
                note=notes_pb2.ProjectNote(id="map-1", content='{"children": []}')
            )
        ]
    )
    session = SequencedSession(
        {
            GET_NOTES_METHOD: [visible, visible, visible],
            DELETE_NOTES_METHOD: [notes_pb2.DeleteNotesResponse()],
        }
    )

    with pytest.raises(RPCError, match="mind map remained visible"):
        await AndroidNotesAPI(
            _session(session),
            deletion_poll_delays=(0.0, 0.0),
        ).delete_mind_map("project-1", "map-1")
    assert len(session.calls) == 4


@pytest.mark.asyncio
async def test_missing_note_and_notebook_status_mapping_is_bounded() -> None:
    missing = RPCError("missing", rpc_code=5)
    session = SequencedSession({GET_NOTES_METHOD: [missing]})
    with pytest.raises(NotebookNotFoundError):
        await AndroidNotesAPI(_session(session)).list("missing-project")

    update_session = SequencedSession(
        {
            GET_NOTES_METHOD: [_visible()],
            MUTATE_NOTE_METHOD: [RPCError("missing", rpc_code=5)],
        }
    )
    with pytest.raises(NoteNotFoundError):
        await AndroidNotesAPI(_session(update_session)).update(
            "project-1", "note-1", "new body", "new title"
        )
    assert [method for method, _request, _kwargs in update_session.calls] == [
        GET_NOTES_METHOD,
        MUTATE_NOTE_METHOD,
    ]

    create_session = SequencedSession({CREATE_NOTE_METHOD: [RPCError("missing", rpc_code=5)]})
    with pytest.raises(NotebookNotFoundError):
        await AndroidNotesAPI(_session(create_session)).create("missing-project", "title", "body")
    assert create_session.calls[0][2]["replay_safe"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [AuthError, RateLimitError, ServerError])
async def test_note_update_preserves_typed_transport_error(error_type: type[RPCError]) -> None:
    error = error_type("typed transport failure")
    session = SequencedSession(
        {
            GET_NOTES_METHOD: [_visible()],
            MUTATE_NOTE_METHOD: [error],
        }
    )

    with pytest.raises(error_type) as caught:
        await AndroidNotesAPI(_session(session)).update(
            "project-1", "note-1", "new body", "new title"
        )

    assert caught.value is error


@pytest.mark.asyncio
async def test_delete_status_five_after_preflight_is_idempotent_and_not_replayed() -> None:
    session = SequencedSession(
        {
            GET_NOTES_METHOD: [_visible()],
            DELETE_NOTES_METHOD: [RPCError("already absent", rpc_code=5)],
        }
    )
    await AndroidNotesAPI(_session(session)).delete("project-1", "note-1")
    assert session.calls[-1][2]["replay_safe"] is False


@pytest.mark.asyncio
async def test_note_write_read_back_drift_fails_loud() -> None:
    wrong = notes_pb2.ProjectNote(
        id="note-1",
        content="wrong",
        name="title",
        metadata=notes_pb2.NoteMetadata(type=notes_pb2.USER_WRITTEN),
    )
    session = SequencedSession(
        {
            CREATE_NOTE_METHOD: [notes_pb2.CreateNoteResponse(note=wrong)],
        }
    )
    with pytest.raises(DecodingError, match="read back"):
        await AndroidNotesAPI(_session(session)).create("project-1", "title", "body")
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_eventual_delete_is_bounded_when_row_never_disappears() -> None:
    session = SequencedSession(
        {
            GET_NOTES_METHOD: [_visible(), _visible(), _visible()],
            DELETE_NOTES_METHOD: [notes_pb2.DeleteNotesResponse()],
        }
    )
    with pytest.raises(RPCError, match="remained visible"):
        await AndroidNotesAPI(
            _session(session),
            deletion_poll_delays=(0.0, 0.0),
        ).delete("project-1", "note-1")
    assert len(session.calls) == 4


@pytest.mark.asyncio
async def test_note_create_readback_completes_in_one_epoch_during_graceful_drain() -> None:
    session = SupervisedNotesSession(block_method=CREATE_NOTE_METHOD)
    notes = AndroidNotesAPI(_session(session))
    task = asyncio.create_task(notes.create("project-1", "Created title", "Created body"))
    await session.mutation_started.wait()

    await session.supervisor.stop_accepting(1)
    session.mutation_release.set()

    created = await task
    assert (created.id, created.title, created.content) == (
        "note-created",
        "Created title",
        "Created body",
    )
    assert [method for method, _request, _kwargs in session.calls] == [
        CREATE_NOTE_METHOD,
        GET_NOTES_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in session.calls] == [1, 1]
    generation = session.supervisor._current
    assert generation is not None
    assert generation.in_flight == 0
    assert generation.drain._in_flight_posts == 0


@pytest.mark.asyncio
async def test_note_delete_cancellation_settles_scope_without_polling() -> None:
    session = SupervisedNotesSession(block_method=DELETE_NOTES_METHOD)
    notes = AndroidNotesAPI(_session(session), deletion_poll_delays=(0.0,))
    task = asyncio.create_task(notes.delete("project-1", "note-1"))
    await session.mutation_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [method for method, _request, _kwargs in session.calls] == [
        GET_NOTES_METHOD,
        DELETE_NOTES_METHOD,
    ]
    await session.supervisor.wait_for_idle(1, 0.1)
    generation = session.supervisor._current
    assert generation is not None
    assert generation.in_flight == 0
    assert generation.drain._in_flight_posts == 0


@pytest.mark.asyncio
async def test_map_delete_old_workflow_cannot_poll_after_forced_close_reopen() -> None:
    session = SupervisedNotesSession(block_method=DELETE_NOTES_METHOD)
    notes = AndroidNotesAPI(_session(session), deletion_poll_delays=(0.0,))
    task = asyncio.create_task(notes.delete_mind_map("project-1", "map-1"))
    await session.mutation_started.wait()

    old_generation = session.supervisor._current
    assert old_generation is not None
    await session.supervisor.begin_closing(1)
    session.supervisor.mark_closed(1)
    session.supervisor.reset_after_open()
    session.supervisor.prepare_generation(2)
    session.supervisor.start_accepting(2)
    session.mutation_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in session.calls] == [
        GET_NOTES_METHOD,
        DELETE_NOTES_METHOD,
    ]
    assert old_generation.in_flight == 0
    current_generation = session.supervisor._current
    assert current_generation is not None
    assert current_generation.epoch == 2
    assert current_generation.in_flight == 0


@pytest.mark.asyncio
async def test_fake_server_sharing_set_public_then_reads_status() -> None:
    server = FakeB6Server()
    sharing = AndroidSharingAPI(_session(server))

    private = await sharing.get_status("project-1")
    assert private.is_public is False
    assert private.access is ShareAccess.RESTRICTED
    assert private.max_individuals_share_limit == 1000
    assert private.is_public_sharing_allowed is False
    assert private.shared_users == []

    public = await sharing.set_public("project-1", True)
    assert public.is_public is True
    assert public.access is ShareAccess.ANYONE_WITH_LINK
    assert public.view_level is ShareViewLevel.FULL_NOTEBOOK
    assert public.share_url == "https://notebook.google.com/notebook/project-1"
    assert [method for method, _request, _kwargs in server.calls] == [
        GET_PROJECT_DETAILS_METHOD,
        SHARE_PROJECT_METHOD,
        GET_PROJECT_DETAILS_METHOD,
    ]
    assert isinstance(server.calls[0][1], exact_sharing_pb2.GetProjectDetailsRequest)
    share_request = server.calls[1][1]
    assert isinstance(share_request, exact_sharing_pb2.ShareProjectRequest)
    assert share_request.project[0].public_document_settings.is_publicly_readable is True
    assert server.operation_scopes == [("sharing.set_public", None)]
    assert server.calls[1][2] == {
        "replay_safe": False,
        "response_type": exact_sharing_pb2.ShareProjectResponse,
        "expected_epoch": 7,
    }
    assert server.calls[2][2] == {
        "replay_safe": True,
        "response_type": sharing_pb2.GetProjectDetailsResponse,
        "expected_epoch": 7,
    }


@pytest.mark.asyncio
async def test_sharing_set_public_readback_completes_during_graceful_drain() -> None:
    session = SupervisedSharingSession()
    sharing = AndroidSharingAPI(_session(session))
    task = asyncio.create_task(sharing.set_public("project-1", True))
    await session.mutation_started.wait()

    await session.supervisor.stop_accepting(1)
    session.mutation_release.set()

    status = await task
    assert status.is_public is True
    assert [method for method, _request, _kwargs in session.calls] == [
        SHARE_PROJECT_METHOD,
        GET_PROJECT_DETAILS_METHOD,
    ]
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in session.calls] == [1, 1]
    generation = session.supervisor._current
    assert generation is not None
    assert generation.in_flight == 0
    assert generation.drain._in_flight_posts == 0


@pytest.mark.asyncio
async def test_sharing_set_public_cancellation_settles_operation_without_readback() -> None:
    session = SupervisedSharingSession()
    sharing = AndroidSharingAPI(_session(session))
    task = asyncio.create_task(sharing.set_public("project-1", True))
    await session.mutation_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [method for method, _request, _kwargs in session.calls] == [SHARE_PROJECT_METHOD]
    await session.supervisor.wait_for_idle(1, 0.1)
    generation = session.supervisor._current
    assert generation is not None
    assert generation.in_flight == 0
    assert generation.drain._in_flight_posts == 0


@pytest.mark.asyncio
async def test_sharing_set_public_old_workflow_cannot_read_back_after_close_reopen() -> None:
    session = SupervisedSharingSession()
    sharing = AndroidSharingAPI(_session(session))
    task = asyncio.create_task(sharing.set_public("project-1", True))
    await session.mutation_started.wait()

    old_generation = session.supervisor._current
    assert old_generation is not None
    await session.supervisor.begin_closing(1)
    session.supervisor.mark_closed(1)
    session.supervisor.reset_after_open()
    session.supervisor.prepare_generation(2)
    session.supervisor.start_accepting(2)
    session.mutation_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in session.calls] == [SHARE_PROJECT_METHOD]
    assert old_generation.in_flight == 0
    current_generation = session.supervisor._current
    assert current_generation is not None
    assert current_generation.epoch == 2
    assert current_generation.in_flight == 0


@pytest.mark.asyncio
async def test_collaborator_and_view_mutations_reject_before_transport() -> None:
    server = FakeB6Server()
    sharing = AndroidSharingAPI(_session(server))
    operations: tuple[Callable[[], Awaitable[Any]], ...] = (
        lambda: sharing.set_view_level("project-1", ShareViewLevel.CHAT_ONLY),
        lambda: sharing.set_users("project-1", [("person@example.test", SharePermission.VIEWER)]),
        lambda: sharing.remove_user("project-1", "person@example.test"),
        # Base intent wrappers must reach the unsupported set_users seam.
        lambda: sharing.add_user("project-1", "person@example.test"),
        lambda: sharing.update_user("project-1", "person@example.test", SharePermission.EDITOR),
    )
    for operation in operations:
        with pytest.raises(UnsupportedOperationError):
            await operation()
    assert server.calls == []


@pytest.mark.asyncio
async def test_sharing_status_five_maps_to_notebook_not_found() -> None:
    session = SequencedSession({GET_PROJECT_DETAILS_METHOD: [RPCError("missing", rpc_code=5)]})
    with pytest.raises(NotebookNotFoundError):
        await AndroidSharingAPI(_session(session)).get_status("missing-project")

    mutation_session = SequencedSession({SHARE_PROJECT_METHOD: [RPCError("missing", rpc_code=5)]})
    with pytest.raises(NotebookNotFoundError):
        await AndroidSharingAPI(_session(mutation_session)).set_public("missing-project", True)
    assert mutation_session.calls[0][2]["replay_safe"] is False
    assert mutation_session.calls[0][2]["expected_epoch"] == 7
