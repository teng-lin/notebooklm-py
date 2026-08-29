"""Exact-package Android note request builders and public projection."""

from __future__ import annotations

import json
from typing import Any, cast

from ..._types.documents import utf16_len
from ...exceptions import DecodingError
from ...types import ChatReference, MindMap, MindMapKind, Note
from ..proto.google.internal.labs.tailwind.orchestration.v1 import chat_pb2, notes_pb2, read_pb2

_PROTO = cast(Any, notes_pb2)
_CHAT_PROTO = cast(Any, chat_pb2)
_READ_PROTO = cast(Any, read_pb2)


def _text_element(text: str, *, start: int, end: int) -> Any:
    return _CHAT_PROTO.StructuralElement(
        start_index=start,
        end_index=end,
        paragraph=_CHAT_PROTO.Paragraph(
            elements=[
                _CHAT_PROTO.ParagraphElement(
                    start_index=start,
                    end_index=end,
                    text_run=_CHAT_PROTO.TextRun(content=text),
                )
            ]
        ),
    )


def build_saved_response_payload(
    clean_answer: str,
    references: list[ChatReference],
    citation_anchors: list[tuple[ChatReference, int]],
) -> tuple[list[Any], Any]:
    """Build current-bundle source passages and TailwindDoc from one citation set."""
    clean_end = utf16_len(clean_answer)
    inline_locations = [
        _CHAT_PROTO.AnnotationMapEntry(
            object_id=_CHAT_PROTO.ObjectId(id=reference.chunk_id),
            content_range=_CHAT_PROTO.Range(start_index=0, end_index=position),
        )
        for reference, position in citation_anchors
        if reference.chunk_id is not None
    ]

    source_passages: list[Any] = []
    objects: list[Any] = []
    seen_chunks: set[str] = set()
    for reference in references:
        chunk_id = reference.chunk_id
        if not chunk_id or chunk_id in seen_chunks:
            continue
        seen_chunks.add(chunk_id)
        cited_text = reference.cited_text or ""
        cited_end = utf16_len(cited_text)
        source_start = reference.start_char if reference.start_char is not None else 0
        source_end = reference.end_char if reference.end_char is not None else cited_end
        if not cited_text:
            source_start = source_end = 0
        object_id = _CHAT_PROTO.ObjectId(id=chunk_id)
        citation = _CHAT_PROTO.Citation(
            ranges=[
                _CHAT_PROTO.Range(
                    start_index=source_start,
                    end_index=source_end,
                )
            ],
            fragment=_CHAT_PROTO.TailwindDocFragment(
                elements=[_text_element(cited_text, start=0, end=cited_end)]
            ),
            source_attribution=_CHAT_PROTO.CitationSource(
                ingested_source=_CHAT_PROTO.SourceRevision(
                    source=_READ_PROTO.SourceId(id=reference.source_id)
                )
            ),
            object_id=object_id,
        )
        source_passages.append(citation)
        objects.append(_CHAT_PROTO.DocumentObject(object_id=object_id, citation=citation))

    document = _CHAT_PROTO.TailwindDoc(
        body=_CHAT_PROTO.Body(
            content=[_text_element(clean_answer, start=0, end=clean_end)],
            inline_object_locations=inline_locations,
        ),
        objects=objects,
    )
    return source_passages, document


def build_create_note_request(
    notebook_id: str,
    *,
    title: str,
    content: str,
    note_type: int,
    source_passages: list[Any] | None = None,
    tailwind_doc_content: Any | None = None,
) -> Any:
    """Build the current-bundle CreateNote request shared with the chat hook."""
    request = _PROTO.CreateNoteRequest(
        project_id=notebook_id,
        content=content,
        metadata=_PROTO.NoteMetadata(type=note_type),
        source_passages=source_passages or (),
        name=title,
        tailwind_doc_content=tailwind_doc_content,
    )
    if tailwind_doc_content is not None:
        from ..upload import android_request_context

        request.request_context.CopyFrom(android_request_context())
    return request


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
    """Project exactly classified note-backed rows into the typed mind-map value.

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
    "build_saved_response_payload",
    "decode_note",
    "decode_note_by_id",
    "decode_note_backed_mind_map_rows",
    "decode_note_backed_mind_maps",
    "decode_note_entries",
    "is_note_backed_mind_map",
]
