"""Android backend implementation of the evidence-qualified B6 notes surface."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NoReturn, cast

from .._notes import NotesAPI
from ..exceptions import DecodingError, NoteNotFoundError, RPCError
from ..types import Note
from .codecs.notebooks import map_get_project_error
from .codecs.notes import (
    build_create_note_request,
    build_mutate_note_request,
    decode_note,
    decode_note_entries,
)
from .errors import unsupported_operation
from .proto.google.internal.labs.tailwind.orchestration.v1 import b6_notes_pb2
from .proto.notebooklm.android.wire.v1 import b6_sharing_pb2
from .session import AndroidSession

_PROTO = cast(Any, b6_notes_pb2)
_WIRE = cast(Any, b6_sharing_pb2)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_NOTES_METHOD = f"/{_SERVICE}/GetNotes"
CREATE_NOTE_METHOD = f"/{_SERVICE}/CreateNote"
MUTATE_NOTE_METHOD = f"/{_SERVICE}/MutateNote"
DELETE_NOTES_METHOD = f"/{_SERVICE}/DeleteNotes"
USER_WRITTEN_NOTE_TYPE = int(_PROTO.USER_WRITTEN)
SAVED_RESPONSE_NOTE_TYPE = int(_PROTO.SAVED_RESPONSE)


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


def _validate_read_back(note: Note, *, title: str, content: str, method_id: str) -> None:
    if note.title != title or note.content != content:
        raise DecodingError(
            "Android note write did not read back the requested title and content",
            method_id=method_id,
        )


async def create_note(
    session: AndroidSession,
    notebook_id: str,
    *,
    title: str,
    content: str,
    note_type: int = USER_WRITTEN_NOTE_TYPE,
) -> Note:
    """Send one non-replayed CreateNote call through the reusable B5/B6 seam.

    The parallel B5 AndroidChatAPI only needs to call this seam with
    ``SAVED_RESPONSE_NOTE_TYPE`` from its private save-note hook. This package
    intentionally does not define or duplicate that adapter.
    """
    request = build_create_note_request(
        notebook_id,
        title=title,
        content=content,
        note_type=note_type,
    )
    try:
        response = await session.unary(
            CREATE_NOTE_METHOD,
            request,
            replay_safe=False,
            response_type=_PROTO.CreateNoteResponse,
        )
    except RPCError as exc:
        mapped = map_get_project_error(notebook_id, exc, method_id=CREATE_NOTE_METHOD)
        if mapped is exc:
            raise
        raise mapped from exc
    return decode_note(response.note, notebook_id, method_id=CREATE_NOTE_METHOD)


class AndroidNotesAPI(NotesAPI):
    """Android note CRUD for the directly tested backend graph."""

    def __init__(
        self,
        session: AndroidSession,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        deletion_poll_delays: Sequence[float] = (0.0, 0.05, 0.1, 0.2, 0.4),
    ) -> None:
        self._transport = session
        self._sleep = sleep
        self._deletion_poll_delays = tuple(deletion_poll_delays)
        if not self._deletion_poll_delays:
            raise ValueError("deletion_poll_delays must contain at least one attempt")

    async def list(self, notebook_id: str) -> builtins.list[Note]:
        """List user notes while excluding evidenced mind-map rows."""
        request = _PROTO.GetNotesRequest(project_id=notebook_id)
        try:
            response = await self._transport.unary(
                GET_NOTES_METHOD,
                request,
                replay_safe=True,
                response_type=_PROTO.GetNotesResponse,
            )
        except RPCError as exc:
            mapped = map_get_project_error(notebook_id, exc, method_id=GET_NOTES_METHOD)
            if mapped is exc:
                raise
            raise mapped from exc
        return decode_note_entries(response, notebook_id, method_id=GET_NOTES_METHOD)

    async def get(self, notebook_id: str, note_id: str) -> Note:
        """Get a note or raise the public note-miss exception."""
        note = await self.get_or_none(notebook_id, note_id)
        if note is None:
            raise NoteNotFoundError(note_id, method_id=GET_NOTES_METHOD)
        return note

    async def get_or_none(self, notebook_id: str, note_id: str) -> Note | None:
        """Return one visible user note, or None for an evidenced absence."""
        return next((note for note in await self.list(notebook_id) if note.id == note_id), None)

    async def create(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
    ) -> Note:
        """Create once, then return the server-visible read-back."""
        created = await create_note(
            self._transport,
            notebook_id,
            title=title,
            content=content,
        )
        _validate_read_back(
            created,
            title=title,
            content=content,
            method_id=CREATE_NOTE_METHOD,
        )
        read_back = await self.get(notebook_id, created.id)
        _validate_read_back(
            read_back,
            title=title,
            content=content,
            method_id=CREATE_NOTE_METHOD,
        )
        return read_back

    async def update(
        self,
        notebook_id: str,
        note_id: str,
        content: str,
        title: str,
    ) -> None:
        """Mutate an existing note once and verify exact title/content read-back."""
        if await self.get_or_none(notebook_id, note_id) is None:
            raise NoteNotFoundError(note_id, method_id=GET_NOTES_METHOD)
        request = build_mutate_note_request(
            notebook_id,
            note_id,
            title=title,
            content=content,
        )
        try:
            response = await self._transport.unary(
                MUTATE_NOTE_METHOD,
                request,
                replay_safe=False,
                response_type=_PROTO.MutateNoteResponse,
            )
        except RPCError as exc:
            if exc.rpc_code == 5:
                raise NoteNotFoundError(
                    note_id,
                    method_id=MUTATE_NOTE_METHOD,
                    raw_response=exc.raw_response,
                ) from exc
            raise
        mutated = decode_note(response.note, notebook_id, method_id=MUTATE_NOTE_METHOD)
        if mutated.id != note_id:
            raise DecodingError(
                "Android MutateNote response changed note identity",
                method_id=MUTATE_NOTE_METHOD,
            )
        _validate_read_back(
            mutated,
            title=title,
            content=content,
            method_id=MUTATE_NOTE_METHOD,
        )
        read_back = await self.get(notebook_id, note_id)
        _validate_read_back(
            read_back,
            title=title,
            content=content,
            method_id=MUTATE_NOTE_METHOD,
        )

    async def delete(self, notebook_id: str, note_id: str) -> None:
        """Delete once, then poll bounded reads until eventual absence is visible."""
        if await self.get_or_none(notebook_id, note_id) is None:
            return
        request = _PROTO.DeleteNotesRequest(project_id=notebook_id, note_ids=[note_id])
        try:
            await self._transport.unary(
                DELETE_NOTES_METHOD,
                request,
                replay_safe=False,
                response_type=_WIRE.EmptyResponse,
            )
        except RPCError as exc:
            # The preflight proved the notebook and note existed. A concurrent
            # status-5 miss is therefore the idempotent note-delete outcome.
            if exc.rpc_code == 5:
                return
            raise

        for delay in self._deletion_poll_delays:
            if delay > 0:
                await self._sleep(delay)
            if await self.get_or_none(notebook_id, note_id) is None:
                return
        raise RPCError(
            "Android DeleteNotes succeeded but the note remained visible after bounded polling",
            method_id=DELETE_NOTES_METHOD,
        )

    async def list_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """Reject until the NotePromptType read kind is independently write-qualified."""
        _reject("notes.list_mind_maps")

    async def delete_mind_map(self, notebook_id: str, mind_map_id: str) -> None:
        """Reject before transport because delete-kind semantics are not qualified."""
        _reject("notes.delete_mind_map")


__all__ = [
    "AndroidNotesAPI",
    "CREATE_NOTE_METHOD",
    "DELETE_NOTES_METHOD",
    "GET_NOTES_METHOD",
    "MUTATE_NOTE_METHOD",
    "SAVED_RESPONSE_NOTE_TYPE",
    "USER_WRITTEN_NOTE_TYPE",
    "create_note",
]
