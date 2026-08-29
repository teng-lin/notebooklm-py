"""Exact-package Android note request builders and public projection."""

from __future__ import annotations

from typing import Any, cast

from ...exceptions import DecodingError
from ...types import Note
from ..proto.google.internal.labs.tailwind.orchestration.v1 import notes_pb2

_PROTO = cast(Any, notes_pb2)


def _enum_name(enum: Any, value: int) -> str | None:
    try:
        return str(enum.Name(value))
    except ValueError:
        return None


def build_create_note_request(
    notebook_id: str,
    *,
    title: str,
    content: str,
    note_type: int,
) -> Any:
    """Build the byte-proven CreateNote request shared with the B5 chat hook."""
    return _PROTO.CreateNoteRequest(
        project_id=notebook_id,
        content=content,
        metadata=_PROTO.NoteMetadata(type=note_type),
        name=title,
    )


def build_mutate_note_request(
    notebook_id: str,
    note_id: str,
    *,
    title: str,
    content: str,
) -> Any:
    """Build one exact edit-note mutation."""
    return _PROTO.MutateNoteRequest(
        project_id=notebook_id,
        note_id=note_id,
        mutations=[
            _PROTO.NoteMutation(
                edit_note_mutation=_PROTO.NoteMutation_EditNoteMutation(
                    content=content,
                    name=title,
                )
            )
        ],
    )


def decode_note(note: Any, notebook_id: str, *, method_id: str) -> Note:
    """Project one evidenced ProjectNote without relabeling last-edit as creation."""
    try:
        if not note.id:
            raise DecodingError(
                "Android note response did not contain a note id",
                method_id=method_id,
            )
        return Note(
            id=note.id,
            notebook_id=notebook_id,
            title=note.name,
            content=note.content,
            # The only recovered timestamp is explicitly lastEditTimestamp.
            # Note.created_at therefore remains unknown instead of receiving a
            # semantically incorrect value.
            created_at=None,
        )
    except DecodingError:
        raise
    except Exception:
        raise DecodingError(
            "Could not decode Android note response",
            method_id=method_id,
        ) from None


def decode_note_entries(response: Any, notebook_id: str, *, method_id: str) -> list[Note]:
    """Decode user notes, skipping status-only rows and evidenced mind-map rows."""
    notes: list[Note] = []
    try:
        entries = response.notes
        for entry in entries:
            # NoteOrStatus #1 is unrecovered. A row without the evidenced note
            # arm is intentionally not interpreted as a deletion or an error.
            if not entry.HasField("note"):
                continue
            note = entry.note
            if note.HasField("metadata"):
                prompt_name = _enum_name(_PROTO.NotePromptType, note.metadata.note_prompt_type)
                if prompt_name == "MIND_MAP":
                    continue
            notes.append(decode_note(note, notebook_id, method_id=method_id))
    except DecodingError:
        raise
    except Exception:
        raise DecodingError(
            "Could not decode Android notes response",
            method_id=method_id,
        ) from None
    return notes


__all__ = [
    "build_create_note_request",
    "build_mutate_note_request",
    "decode_note",
    "decode_note_entries",
]
