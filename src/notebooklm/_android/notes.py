"""Android backend implementation of the public Notes surface."""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from .._idempotency import mark_unconfirmed
from .._notes import NotesAPI
from ..exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    NotebookNotFoundError,
    NoteNotFoundError,
    RateLimitError,
    RPCError,
    ServerError,
)
from ..types import MindMap, Note
from .session import AndroidSession
from .write_safety import call_unconfirmed_on_transport_loss

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_NOTES_METHOD = f"/{_SERVICE}/GetNotes"
CREATE_NOTE_METHOD = f"/{_SERVICE}/CreateNote"
MUTATE_NOTE_METHOD = f"/{_SERVICE}/MutateNote"
DELETE_NOTES_METHOD = f"/{_SERVICE}/DeleteNotes"
# Exact enum values pinned by notes.proto and test_proto_contract.py. Keeping
# these scalar constants here lets explicit Android client construction remain
# dependency-free; protobuf is validated during open and imported on first use.
USER_WRITTEN_NOTE_TYPE = 1
SAVED_RESPONSE_NOTE_TYPE = 2

logger = logging.getLogger(__name__)


def _proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import notes_pb2

    return cast(Any, notes_pb2)


def _map_notebook_error(notebook_id: str, error: RPCError, *, method_id: str) -> RPCError:
    if error.rpc_code != 5:
        return error
    return NotebookNotFoundError(
        notebook_id,
        method_id=method_id,
        raw_response=error.raw_response,
        rpc_code=error.rpc_code,
        found_ids=error.found_ids,
        detail=str(error),
    )


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
    source_passages: list[Any] | None = None,
    tailwind_doc_content: Any | None = None,
    expected_epoch: int | None = None,
) -> Note:
    """Send one non-replayed CreateNote call through the reusable chat/note seam.

    The parallel chat AndroidChatAPI only needs to call this seam with
    ``SAVED_RESPONSE_NOTE_TYPE`` from its private save-note hook. This package
    intentionally does not define or duplicate that adapter.
    """
    from .codecs.notes import build_create_note_request, decode_note

    proto = _proto()
    request = build_create_note_request(
        notebook_id,
        title=title,
        content=content,
        note_type=note_type,
        source_passages=source_passages,
        tailwind_doc_content=tailwind_doc_content,
    )
    epoch_kwargs: dict[str, Any] = (
        {} if expected_epoch is None else {"expected_epoch": expected_epoch}
    )
    try:
        response = await call_unconfirmed_on_transport_loss(
            lambda: session.unary(
                CREATE_NOTE_METHOD,
                request,
                replay_safe=False,
                response_type=proto.CreateNoteResponse,
                **epoch_kwargs,
            )
        )
    except RPCError as exc:
        mapped = _map_notebook_error(notebook_id, exc, method_id=CREATE_NOTE_METHOD)
        if mapped is exc:
            raise
        raise mapped from exc
    try:
        return decode_note(response.note, notebook_id, method_id=CREATE_NOTE_METHOD)
    except DecodingError as error:
        # A successful CreateNote envelope proves dispatch, but an unusable
        # result cannot tell the caller which row (if any) was committed.
        raise mark_unconfirmed(error) from None


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

    async def _get_notes_response(self, notebook_id: str, *, expected_epoch: int) -> Any:
        """Issue the one exact safe read shared by the two typed projections."""
        proto = _proto()
        request = proto.GetNotesRequest(project_id=notebook_id)
        try:
            return await self._transport.unary(
                GET_NOTES_METHOD,
                request,
                replay_safe=True,
                response_type=proto.GetNotesResponse,
                expected_epoch=expected_epoch,
            )
        except RPCError as exc:
            mapped = _map_notebook_error(notebook_id, exc, method_id=GET_NOTES_METHOD)
            if mapped is exc:
                raise
            raise mapped from exc

    async def _list_notes(self, notebook_id: str, *, expected_epoch: int) -> builtins.list[Note]:
        from .codecs.notes import decode_note_entries

        response = await self._get_notes_response(notebook_id, expected_epoch=expected_epoch)
        return decode_note_entries(response, notebook_id, method_id=GET_NOTES_METHOD)

    async def _get_note_or_none(
        self,
        notebook_id: str,
        note_id: str,
        *,
        expected_epoch: int,
    ) -> Note | None:
        from .codecs.notes import decode_note_by_id

        response = await self._get_notes_response(notebook_id, expected_epoch=expected_epoch)
        return decode_note_by_id(
            response,
            notebook_id,
            note_id,
            method_id=GET_NOTES_METHOD,
        )

    async def _list_mind_map_rows(
        self,
        notebook_id: str,
        *,
        expected_epoch: int,
    ) -> builtins.list[Any]:
        from .codecs.notes import decode_note_backed_mind_map_rows

        response = await self._get_notes_response(notebook_id, expected_epoch=expected_epoch)
        return decode_note_backed_mind_map_rows(response, method_id=GET_NOTES_METHOD)

    async def list(self, notebook_id: str) -> builtins.list[Note]:
        """List user notes while excluding evidenced mind-map rows."""
        async with self._transport.operation_scope("notes.list") as lease:
            return await self._list_notes(notebook_id, expected_epoch=lease.epoch)

    async def _list_note_backed_mind_maps(self, notebook_id: str) -> builtins.list[MindMap]:
        """Return the private typed mind-map projection without fabricating Web rows."""
        from .codecs.notes import decode_note_backed_mind_maps

        async with self._transport.operation_scope("notes.list_note_backed_mind_maps") as lease:
            response = await self._get_notes_response(
                notebook_id,
                expected_epoch=lease.epoch,
            )
            return decode_note_backed_mind_maps(
                response,
                notebook_id,
                method_id=GET_NOTES_METHOD,
            )

    async def get(self, notebook_id: str, note_id: str) -> Note:
        """Get a note or raise the public note-miss exception."""
        async with self._transport.operation_scope("notes.get") as lease:
            note = await self._get_note_or_none(
                notebook_id,
                note_id,
                expected_epoch=lease.epoch,
            )
            if note is None:
                raise NoteNotFoundError(note_id, method_id=GET_NOTES_METHOD)
            return note

    async def get_or_none(self, notebook_id: str, note_id: str) -> Note | None:
        """Return one visible user note, or None for an evidenced absence."""
        async with self._transport.operation_scope("notes.get_or_none") as lease:
            return await self._get_note_or_none(
                notebook_id,
                note_id,
                expected_epoch=lease.epoch,
            )

    async def create(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
    ) -> Note:
        """Create once, then return the server-visible read-back."""
        async with self._transport.operation_scope("notes.create") as lease:
            created = await create_note(
                self._transport,
                notebook_id,
                title=title,
                content=content,
                expected_epoch=lease.epoch,
            )
            try:
                _validate_read_back(
                    created,
                    title=title,
                    content=content,
                    method_id=CREATE_NOTE_METHOD,
                )
            except DecodingError as error:
                raise mark_unconfirmed(error) from None
            try:
                read_back = await self._get_note_or_none(
                    notebook_id,
                    created.id,
                    expected_epoch=lease.epoch,
                )
            except (NetworkError, RPCError):
                # CreateNote returned a concrete, fully decoded note whose
                # title/content already match the request. An expected read failure
                # in the optional GetNotes verification cannot make that
                # confirmed mutation ambiguous; propagating it would invite a
                # caller to retry CreateNote and duplicate it. Programming defects
                # remain visible rather than being mistaken for read unavailability.
                logger.debug(
                    "Android CreateNote succeeded but GetNotes verification was unavailable; "
                    "returning the validated create response"
                )
                return created
            if read_back is None:
                logger.debug(
                    "Android CreateNote succeeded but its row was not yet visible; "
                    "returning the validated create response"
                )
                return created
            try:
                _validate_read_back(
                    read_back,
                    title=title,
                    content=content,
                    method_id=CREATE_NOTE_METHOD,
                )
            except DecodingError as error:
                raise mark_unconfirmed(error) from None
            return read_back

    async def update(
        self,
        notebook_id: str,
        note_id: str,
        content: str,
        title: str,
    ) -> None:
        """Mutate an existing note once and verify exact title/content read-back."""
        from .codecs.notes import build_mutate_note_request, decode_note

        proto = _proto()
        async with self._transport.operation_scope("notes.update") as lease:
            current = await self._get_note_or_none(
                notebook_id,
                note_id,
                expected_epoch=lease.epoch,
            )
            if current is None:
                raise NoteNotFoundError(note_id, method_id=GET_NOTES_METHOD)
            resolved_content = current.content if content is None else content
            resolved_title = current.title if title is None else title
            request = build_mutate_note_request(
                notebook_id,
                note_id,
                title=resolved_title,
                content=resolved_content,
            )
            try:
                response = await self._transport.unary(
                    MUTATE_NOTE_METHOD,
                    request,
                    replay_safe=False,
                    response_type=proto.MutateNoteResponse,
                    expected_epoch=lease.epoch,
                )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
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
                title=resolved_title,
                content=resolved_content,
                method_id=MUTATE_NOTE_METHOD,
            )
            read_back = await self._get_note_or_none(
                notebook_id,
                note_id,
                expected_epoch=lease.epoch,
            )
            if read_back is None:
                raise NoteNotFoundError(note_id, method_id=GET_NOTES_METHOD)
            _validate_read_back(
                read_back,
                title=resolved_title,
                content=resolved_content,
                method_id=MUTATE_NOTE_METHOD,
            )

    async def delete(self, notebook_id: str, note_id: str) -> None:
        """Delete once, then poll bounded reads until eventual absence is visible."""
        proto = _proto()
        async with self._transport.operation_scope("notes.delete") as lease:
            if (
                await self._get_note_or_none(
                    notebook_id,
                    note_id,
                    expected_epoch=lease.epoch,
                )
                is None
            ):
                return
            request = proto.DeleteNotesRequest(project_id=notebook_id, note_ids=[note_id])
            try:
                await self._transport.unary(
                    DELETE_NOTES_METHOD,
                    request,
                    replay_safe=False,
                    response_type=proto.DeleteNotesResponse,
                    expected_epoch=lease.epoch,
                )
            except RPCError as exc:
                # The preflight proved the notebook and note existed. A
                # concurrent status-5 miss is the idempotent delete outcome.
                if exc.rpc_code == 5:
                    return
                raise

            for delay in self._deletion_poll_delays:
                if delay > 0:
                    await self._sleep(delay)
                if (
                    await self._get_note_or_none(
                        notebook_id,
                        note_id,
                        expected_epoch=lease.epoch,
                    )
                    is None
                ):
                    return
            raise RPCError(
                "Android DeleteNotes succeeded but the note remained visible after bounded polling",
                method_id=DELETE_NOTES_METHOD,
            )

    async def list_mind_maps(self, notebook_id: str) -> builtins.list[Any]:
        """List map rows as the minimal Web-compatible ``[id, content]`` pair.

        Both slots come from exact Android fields. Optional Web-only metadata
        is deliberately absent rather than fabricated.
        """
        async with self._transport.operation_scope("notes.list_mind_maps") as lease:
            return await self._list_mind_map_rows(
                notebook_id,
                expected_epoch=lease.epoch,
            )

    async def delete_mind_map(self, notebook_id: str, mind_map_id: str) -> None:
        """Delete an evidenced map once and poll until that map is absent.

        The preflight is kind-safe: an ordinary note id is treated as an
        already-absent mind map and never reaches ``DeleteNotes``.
        """
        proto = _proto()
        async with self._transport.operation_scope("notes.delete_mind_map") as lease:
            rows = await self._list_mind_map_rows(
                notebook_id,
                expected_epoch=lease.epoch,
            )
            if not any(row_id == mind_map_id for row_id, _content in rows):
                return
            request = proto.DeleteNotesRequest(project_id=notebook_id, note_ids=[mind_map_id])
            try:
                await self._transport.unary(
                    DELETE_NOTES_METHOD,
                    request,
                    replay_safe=False,
                    response_type=proto.DeleteNotesResponse,
                    expected_epoch=lease.epoch,
                )
            except RPCError as exc:
                # A status-5 race after the kind preflight is the idempotent
                # concurrent-absence outcome, matching ordinary-note deletion.
                if exc.rpc_code == 5:
                    return
                raise

            for delay in self._deletion_poll_delays:
                if delay > 0:
                    await self._sleep(delay)
                rows = await self._list_mind_map_rows(
                    notebook_id,
                    expected_epoch=lease.epoch,
                )
                if not any(row_id == mind_map_id for row_id, _content in rows):
                    return
            raise RPCError(
                "Android DeleteNotes succeeded but the mind map remained visible "
                "after bounded polling",
                method_id=DELETE_NOTES_METHOD,
            )


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
