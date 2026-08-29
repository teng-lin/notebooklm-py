"""Offline fake-server orchestration tests for B6 Android notes and sharing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

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
    b6_notes_pb2,
)
from notebooklm._android.proto.notebooklm.android.wire.v1 import b6_sharing_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.sharing import (
    GET_PROJECT_DETAILS_METHOD,
    SHARE_PROJECT_METHOD,
    AndroidSharingAPI,
)
from notebooklm._notes import NotesAPI
from notebooklm._sharing import SharingAPI
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
from notebooklm.types import ShareAccess, SharePermission, ShareViewLevel


class FakeB6Server:
    """Stateful unary fake that models the measured disposable lifecycle."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.notes: dict[str, b6_notes_pb2.ProjectNote] = {
            "note-existing": b6_notes_pb2.ProjectNote(
                id="note-existing",
                content="Existing body",
                name="Existing title",
                metadata=b6_notes_pb2.NoteMetadata(type=b6_notes_pb2.USER_WRITTEN),
            ),
            "mind-map": b6_notes_pb2.ProjectNote(
                id="mind-map",
                content='{"nodes": []}',
                name="Map",
                metadata=b6_notes_pb2.NoteMetadata(
                    type=b6_notes_pb2.CUSTOM,
                    note_prompt_type=b6_notes_pb2.MIND_MAP,
                ),
            ),
        }
        self.pending_deletion_reads: dict[str, int] = {}
        self.public = False

    def _get_notes(self) -> b6_notes_pb2.GetNotesResponse:
        entries = [b6_notes_pb2.NoteOrStatus()]  # unrecovered status-only arm
        for note_id, note in list(self.notes.items()):
            pending = self.pending_deletion_reads.get(note_id)
            if pending is not None:
                if pending == 0:
                    del self.notes[note_id]
                    del self.pending_deletion_reads[note_id]
                    continue
                self.pending_deletion_reads[note_id] = pending - 1
            entries.append(b6_notes_pb2.NoteOrStatus(note=note))
        return b6_notes_pb2.GetNotesResponse(notes=entries)

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        if method == GET_NOTES_METHOD:
            return self._get_notes()
        if method == CREATE_NOTE_METHOD:
            note = b6_notes_pb2.ProjectNote(
                id="note-created",
                content=request.content,
                name=request.name,
                metadata=request.metadata,
            )
            self.notes[note.id] = note
            return b6_notes_pb2.CreateNoteResponse(note=note)
        if method == MUTATE_NOTE_METHOD:
            edit = request.mutations[0].edit_note_mutation
            note = self.notes[request.note_id]
            note.content = edit.content
            note.name = edit.name
            return b6_notes_pb2.MutateNoteResponse(note=note)
        if method == DELETE_NOTES_METHOD:
            for note_id in request.note_ids:
                if note_id in self.notes:
                    # The first post-delete GetNotes still sees the row; the
                    # second excludes it, matching the disposable live probe.
                    self.pending_deletion_reads[note_id] = 1
            return b6_sharing_pb2.EmptyResponse()
        if method == GET_PROJECT_DETAILS_METHOD:
            return b6_sharing_pb2.GetProjectDetailsResponse(
                public_settings=b6_sharing_pb2.ProjectPublicSettings(
                    is_publicly_readable=self.public,
                    is_discoverable=False,
                ),
                max_individuals_share_limit=1000,
                is_public_sharing_allowed=False,
            )
        if method == SHARE_PROJECT_METHOD:
            self.public = request.project[0].public_document_settings.is_publicly_readable
            return b6_sharing_pb2.EmptyResponse()
        raise AssertionError(f"unexpected method: {method}")


class SequencedSession:
    """Recording session with per-method response/exception queues."""

    def __init__(self, outcomes: dict[str, list[Any]]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        outcome = self.outcomes[method].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _session(fake: object) -> AndroidSession:
    return cast(AndroidSession, fake)


def _visible(note_id: str = "note-1") -> b6_notes_pb2.GetNotesResponse:
    return b6_notes_pb2.GetNotesResponse(
        notes=[
            b6_notes_pb2.NoteOrStatus(
                note=b6_notes_pb2.ProjectNote(
                    id=note_id,
                    content="body",
                    name="title",
                    metadata=b6_notes_pb2.NoteMetadata(type=b6_notes_pb2.USER_WRITTEN),
                )
            )
        ]
    )


def test_backend_contracts_are_split_and_android_adapters_are_concrete() -> None:
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
        CREATE_NOTE_METHOD: b6_notes_pb2.CreateNoteResponse,
        MUTATE_NOTE_METHOD: b6_notes_pb2.MutateNoteResponse,
        DELETE_NOTES_METHOD: b6_sharing_pb2.EmptyResponse,
    }
    for method, _request, kwargs in server.calls:
        if method in writes:
            assert kwargs == {"replay_safe": False, "response_type": writes[method]}
        elif method == GET_NOTES_METHOD:
            assert kwargs == {
                "replay_safe": True,
                "response_type": b6_notes_pb2.GetNotesResponse,
            }


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
async def test_note_unsupported_operations_reject_before_transport() -> None:
    server = FakeB6Server()
    notes = AndroidNotesAPI(_session(server))

    with pytest.raises(UnsupportedOperationError):
        await notes.list_mind_maps("project-1")
    with pytest.raises(UnsupportedOperationError):
        await notes.delete_mind_map("project-1", "mind-map")
    assert server.calls == []


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
    wrong = b6_notes_pb2.ProjectNote(
        id="note-1",
        content="wrong",
        name="title",
        metadata=b6_notes_pb2.NoteMetadata(type=b6_notes_pb2.USER_WRITTEN),
    )
    session = SequencedSession(
        {
            CREATE_NOTE_METHOD: [b6_notes_pb2.CreateNoteResponse(note=wrong)],
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
            DELETE_NOTES_METHOD: [b6_sharing_pb2.EmptyResponse()],
        }
    )
    with pytest.raises(RPCError, match="remained visible"):
        await AndroidNotesAPI(
            _session(session),
            deletion_poll_delays=(0.0, 0.0),
        ).delete("project-1", "note-1")
    assert len(session.calls) == 4


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
    share_request = server.calls[1][1]
    assert share_request.project[0].public_document_settings.is_publicly_readable is True
    assert server.calls[1][2] == {
        "replay_safe": False,
        "response_type": b6_sharing_pb2.EmptyResponse,
    }


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
