"""Exact-package Android note request builders and public projection."""

from __future__ import annotations

import json
from typing import Any, cast

from ...exceptions import DecodingError
from ...types import MindMap, MindMapKind, Note
from ..proto.google.internal.labs.tailwind.orchestration.v1 import notes_pb2

_PROTO = cast(Any, notes_pb2)


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
    """Decode user notes, skipping status-only rows and all evidenced map rows."""
    notes: list[Note] = []
    try:
        entries = response.notes
        for entry in entries:
            # NoteOrStatus #1 is unrecovered. A row without the evidenced note
            # arm is intentionally not interpreted as a deletion or an error.
            if not entry.HasField("note"):
                continue
            note = entry.note
            if is_note_backed_mind_map(note):
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


def decode_note_by_id(
    response: Any,
    notebook_id: str,
    note_id: str,
    *,
    method_id: str,
) -> Note | None:
    """Decode an exact persisted row without applying ``list`` kind filters.

    Web ``get_or_none`` scans the combined note/mind-map rows before parsing,
    so an exact map id is still a valid ``Note`` lookup even though ``list``
    excludes it. Update/delete preflights inherit that contract.
    """

    try:
        for entry in response.notes:
            if not entry.HasField("note") or entry.note.id != note_id:
                continue
            return decode_note(entry.note, notebook_id, method_id=method_id)
    except DecodingError:
        raise
    except Exception:
        raise DecodingError(
            "Could not decode Android note exact-id response",
            method_id=method_id,
        ) from None
    return None


def _parse_mind_map_tree(content: str) -> dict[str, Any] | None:
    """Parse an evidenced mind-map content string without inferring a shape."""
    if not content:
        return None
    try:
        tree = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return tree if isinstance(tree, dict) else None


def is_note_backed_mind_map(note: Any) -> bool:
    """Classify a ProjectNote with the union proven by both backends live.

    Android-created rows can carry the exact ``MIND_MAP`` prompt enum. Maps
    generated through the Web backend were observed twice with
    ``USER_WRITTEN`` and an unspecified prompt, while their JSON object carried
    ``children``. The legacy Web classifier also admits a top-level ``nodes``
    key. Either exact signal is therefore sufficient; neither is required to
    agree with the other.
    """
    if note.HasField("metadata") and note.metadata.note_prompt_type == _PROTO.MIND_MAP:
        return True
    tree = _parse_mind_map_tree(note.content)
    return tree is not None and ("children" in tree or "nodes" in tree)


def decode_note_backed_mind_map_rows(
    response: Any,
    *,
    method_id: str,
) -> list[list[str]]:
    """Return the smallest honest legacy raw row: ``[id, content]``.

    Those two values are exact Android ``ProjectNote`` fields and occupy the
    same leading slots consumed by the public Web mind-map callers. Android
    does not expose enough evidence to append Web metadata, so no further slots
    are synthesized.
    """
    rows: list[list[str]] = []
    try:
        for entry in response.notes:
            if not entry.HasField("note"):
                continue
            note = entry.note
            if not is_note_backed_mind_map(note):
                continue
            if not note.id:
                raise DecodingError(
                    "Android mind-map note response did not contain a note id",
                    method_id=method_id,
                )
            rows.append([note.id, note.content])
    except DecodingError:
        raise
    except Exception:
        raise DecodingError(
            "Could not decode Android note-backed mind-map rows",
            method_id=method_id,
        ) from None
    return rows


def decode_note_backed_mind_maps(
    response: Any,
    notebook_id: str,
    *,
    method_id: str,
) -> list[MindMap]:
    """Project exactly classified note-backed rows into the typed B7 value.

    This deliberately does not manufacture the raw positional rows returned by
    the Web notes RPC. The Android descriptor proves persisted id, content, and
    name; prompt metadata plus the twice-observed JSON signal prove the union
    classifier. Its only timestamp is ``last_edit_timestamp``, so the public
    creation time remains unknown instead of being guessed from it.
    """
    mind_maps: list[MindMap] = []
    try:
        for entry in response.notes:
            # NoteOrStatus #1 remains unrecovered. Status-only rows are not
            # interpreted as live mind maps or as a particular tombstone kind.
            if not entry.HasField("note"):
                continue
            note = entry.note
            if not is_note_backed_mind_map(note):
                continue
            if not note.id:
                raise DecodingError(
                    "Android mind-map note response did not contain a note id",
                    method_id=method_id,
                )
            mind_maps.append(
                MindMap(
                    id=note.id,
                    notebook_id=notebook_id,
                    title=note.name,
                    kind=MindMapKind.NOTE_BACKED,
                    created_at=None,
                    tree=_parse_mind_map_tree(note.content),
                )
            )
    except DecodingError:
        raise
    except Exception:
        raise DecodingError(
            "Could not decode Android note-backed mind maps response",
            method_id=method_id,
        ) from None
    return mind_maps


__all__ = [
    "build_create_note_request",
    "build_mutate_note_request",
    "decode_note",
    "decode_note_by_id",
    "decode_note_backed_mind_map_rows",
    "decode_note_backed_mind_maps",
    "decode_note_entries",
    "is_note_backed_mind_map",
]
