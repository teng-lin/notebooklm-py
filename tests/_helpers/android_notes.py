"""Small stateful Android Notes transport shared by frontend parity tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from notebooklm._android.notes import DELETE_NOTES_METHOD, GET_NOTES_METHOD, MUTATE_NOTE_METHOD
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import notes_pb2


@dataclass(frozen=True)
class OperationLease:
    epoch: int


class StatefulAndroidNotesTransport:
    """Model exact reads, edits, and immediate delete visibility without network I/O."""

    epoch = 7

    def __init__(
        self,
        *,
        notebook_id: str,
        note_id: str,
        title: str = "Original title",
        content: str = "Original content",
        malformed_first: bool = False,
    ) -> None:
        self.notebook_id = notebook_id
        self.note_id = note_id
        self.malformed_first = malformed_first
        self.note: notes_pb2.ProjectNote | None = notes_pb2.ProjectNote(
            id=note_id,
            name=title,
            content=content,
            metadata=notes_pb2.NoteMetadata(type=notes_pb2.USER_WRITTEN),
        )
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.operation_scopes: list[tuple[str, int | None]] = []

    @asynccontextmanager
    async def operation_scope(
        self,
        label: str,
        *,
        expected_epoch: int | None = None,
    ) -> AsyncIterator[OperationLease]:
        self.operation_scopes.append((label, expected_epoch))
        yield OperationLease(self.epoch)

    def _response(self) -> notes_pb2.GetNotesResponse:
        rows: list[notes_pb2.NoteOrStatus] = []
        if self.malformed_first:
            # An unrelated note arm with no id cannot be projected as a Note.
            # Exact-id reads must skip it before decoding the requested row.
            rows.append(
                notes_pb2.NoteOrStatus(
                    note=notes_pb2.ProjectNote(name="Malformed", content="unrelated")
                )
            )
        if self.note is not None:
            rows.append(notes_pb2.NoteOrStatus(note=self.note))
        return notes_pb2.GetNotesResponse(notes=rows)

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        if method == GET_NOTES_METHOD:
            assert request.project_id == self.notebook_id
            return self._response()
        if method == MUTATE_NOTE_METHOD:
            assert request.project_id == self.notebook_id
            assert request.note_id == self.note_id
            assert self.note is not None
            edit = request.mutations[0].edit_note_mutation
            self.note.name = edit.name
            self.note.content = edit.content
            return notes_pb2.MutateNoteResponse(note=self.note)
        if method == DELETE_NOTES_METHOD:
            assert request.project_id == self.notebook_id
            assert list(request.note_ids) == [self.note_id]
            self.note = None
            return notes_pb2.DeleteNotesResponse()
        raise AssertionError(f"unexpected Android Notes method: {method}")


__all__ = ["StatefulAndroidNotesTransport"]
