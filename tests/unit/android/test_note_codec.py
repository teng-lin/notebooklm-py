"""Unit coverage for the Android note codec's admission and failure branches.

The adapter-level suites drive these through ``AndroidNotesAPI``. These cases
call the codec directly to reach the id guards, the mind-map classifier's
fallbacks, and the ``DecodingError`` wrapping that turns an unexpected shape
into a named drift signal instead of an ``AttributeError``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from notebooklm._android.codecs.notes import (
    build_saved_response_payload,
    decode_note,
    decode_note_backed_mind_map_rows,
    decode_note_backed_mind_maps,
    decode_note_by_id,
    decode_note_entries,
    is_note_backed_mind_map,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    notes_pb2,
)
from notebooklm.exceptions import DecodingError
from notebooklm.types import ChatReference, MindMapKind

METHOD_ID = "test-method"
NB = "notebook-1"

_MIND_MAP_JSON = json.dumps({"name": "root", "children": [{"name": "leaf"}]})


def _note(
    note_id: str = "note-1",
    *,
    name: str = "Title",
    content: str = "Body",
    prompt_type: int | None = None,
) -> Any:
    note = notes_pb2.ProjectNote(id=note_id, name=name, content=content)
    if prompt_type is not None:
        note.metadata.note_prompt_type = prompt_type
    return note


def _response(*notes: Any, include_status_row: bool = False) -> Any:
    response = notes_pb2.GetNotesResponse()
    if include_status_row:
        # A ``NoteOrStatus`` without the note arm: neither a deletion nor an error.
        response.notes.add()
    for note in notes:
        response.notes.add().note.CopyFrom(note)
    return response


class _ExplodingResponse:
    """Stands in for a response whose shape drifted out from under the codec."""

    @property
    def notes(self) -> Any:
        raise AttributeError("notes")


# ---------------------------------------------------------------------------
# decode_note
# ---------------------------------------------------------------------------


def test_decode_note_projects_the_evidenced_fields() -> None:
    decoded = decode_note(_note(), NB, method_id=METHOD_ID)

    assert (decoded.id, decoded.notebook_id, decoded.title, decoded.content) == (
        "note-1",
        NB,
        "Title",
        "Body",
    )
    # ``last_edit_timestamp`` is not creation time, so it is left unknown.
    assert decoded.created_at is None


def test_decode_note_rejects_a_row_without_an_id() -> None:
    with pytest.raises(DecodingError, match="did not contain a note id"):
        decode_note(_note(""), NB, method_id=METHOD_ID)


def test_decode_note_wraps_an_unexpected_shape() -> None:
    with pytest.raises(DecodingError, match="Could not decode Android note response"):
        decode_note(object(), NB, method_id=METHOD_ID)


# ---------------------------------------------------------------------------
# decode_note_entries
# ---------------------------------------------------------------------------


def test_decode_note_entries_skips_status_rows_and_mind_maps() -> None:
    response = _response(
        _note("note-1"),
        _note("map-1", content=_MIND_MAP_JSON),
        include_status_row=True,
    )

    decoded = decode_note_entries(response, NB, method_id=METHOD_ID)

    assert [note.id for note in decoded] == ["note-1"]


def test_decode_note_entries_propagates_a_row_level_decoding_error() -> None:
    response = _response(_note(""))

    with pytest.raises(DecodingError, match="did not contain a note id"):
        decode_note_entries(response, NB, method_id=METHOD_ID)


def test_decode_note_entries_wraps_an_unexpected_shape() -> None:
    with pytest.raises(DecodingError, match="Could not decode Android notes response"):
        decode_note_entries(_ExplodingResponse(), NB, method_id=METHOD_ID)


# ---------------------------------------------------------------------------
# decode_note_by_id
# ---------------------------------------------------------------------------


def test_decode_note_by_id_returns_an_exact_match_including_a_mind_map() -> None:
    """``list`` filters mind maps out; an exact id lookup does not."""
    response = _response(_note("note-1"), _note("map-1", content=_MIND_MAP_JSON))

    decoded = decode_note_by_id(response, NB, "map-1", method_id=METHOD_ID)

    assert decoded is not None
    assert decoded.id == "map-1"


def test_decode_note_by_id_returns_none_when_absent() -> None:
    response = _response(_note("note-1"), include_status_row=True)

    assert decode_note_by_id(response, NB, "absent", method_id=METHOD_ID) is None


def test_decode_note_by_id_propagates_a_row_level_decoding_error() -> None:
    """A matching row with a blank id cannot be silently reported as absent."""

    class _BlankIdMatch:
        class _Entry:
            def HasField(self, _name: str) -> bool:
                return True

            note = _note("")

        notes = (_Entry(),)

    with pytest.raises(DecodingError):
        decode_note_by_id(_BlankIdMatch(), NB, "", method_id=METHOD_ID)


def test_decode_note_by_id_wraps_an_unexpected_shape() -> None:
    with pytest.raises(DecodingError, match="exact-id response"):
        decode_note_by_id(_ExplodingResponse(), NB, "note-1", method_id=METHOD_ID)


# ---------------------------------------------------------------------------
# is_note_backed_mind_map
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "prompt_type", "expected"),
    [
        pytest.param("", notes_pb2.MIND_MAP, True, id="android-prompt-enum"),
        pytest.param(_MIND_MAP_JSON, None, True, id="children-key"),
        pytest.param('{"nodes": []}', None, True, id="legacy-nodes-key"),
        pytest.param('{"name": "x"}', None, False, id="object-without-either-key"),
        pytest.param("[1, 2]", None, False, id="non-object-json"),
        pytest.param("not json", None, False, id="unparseable"),
        pytest.param("", None, False, id="empty-content"),
    ],
)
def test_mind_map_classifier_admits_either_exact_signal(
    content: str, prompt_type: int | None, expected: bool
) -> None:
    assert is_note_backed_mind_map(_note(content=content, prompt_type=prompt_type)) is expected


# ---------------------------------------------------------------------------
# decode_note_backed_mind_map_rows / decode_note_backed_mind_maps
# ---------------------------------------------------------------------------


def test_mind_map_rows_carry_only_the_two_evidenced_slots() -> None:
    response = _response(
        _note("note-1"),
        _note("map-1", content=_MIND_MAP_JSON),
        include_status_row=True,
    )

    assert decode_note_backed_mind_map_rows(response, method_id=METHOD_ID) == [
        ["map-1", _MIND_MAP_JSON]
    ]


def test_mind_map_rows_reject_a_row_without_an_id() -> None:
    response = _response(_note("", content=_MIND_MAP_JSON))

    with pytest.raises(DecodingError, match="did not contain a note id"):
        decode_note_backed_mind_map_rows(response, method_id=METHOD_ID)


def test_mind_map_rows_wrap_an_unexpected_shape() -> None:
    with pytest.raises(DecodingError, match="mind-map rows"):
        decode_note_backed_mind_map_rows(_ExplodingResponse(), method_id=METHOD_ID)


def test_mind_maps_project_the_parsed_tree_without_guessing_creation_time() -> None:
    response = _response(
        _note("note-1"),
        _note("map-1", name="Concepts", content=_MIND_MAP_JSON),
        include_status_row=True,
    )

    [mind_map] = decode_note_backed_mind_maps(response, NB, method_id=METHOD_ID)

    assert (mind_map.id, mind_map.title) == ("map-1", "Concepts")
    assert mind_map.kind is MindMapKind.NOTE_BACKED
    assert mind_map.created_at is None
    assert mind_map.tree == json.loads(_MIND_MAP_JSON)


def test_mind_maps_leave_an_unparseable_tree_unset() -> None:
    response = _response(_note("map-1", content="", prompt_type=notes_pb2.MIND_MAP))

    [mind_map] = decode_note_backed_mind_maps(response, NB, method_id=METHOD_ID)

    assert mind_map.tree is None


def test_mind_maps_reject_a_row_without_an_id() -> None:
    response = _response(_note("", content=_MIND_MAP_JSON))

    with pytest.raises(DecodingError, match="did not contain a note id"):
        decode_note_backed_mind_maps(response, NB, method_id=METHOD_ID)


def test_mind_maps_wrap_an_unexpected_shape() -> None:
    with pytest.raises(DecodingError, match="note-backed mind maps response"):
        decode_note_backed_mind_maps(_ExplodingResponse(), NB, method_id=METHOD_ID)


# ---------------------------------------------------------------------------
# build_saved_response_payload
# ---------------------------------------------------------------------------


def _reference(**kwargs: Any) -> ChatReference:
    kwargs.setdefault("source_id", "src-1")
    return ChatReference(**kwargs)


def test_saved_response_payload_skips_unusable_and_duplicate_chunks() -> None:
    references = [
        _reference(chunk_id=None, cited_text="no chunk id"),
        _reference(chunk_id="", cited_text="empty chunk id"),
        _reference(chunk_id="c1", cited_text="first", start_char=3, end_char=8),
        _reference(chunk_id="c1", cited_text="duplicate"),
    ]

    passages, _document = build_saved_response_payload(references[2].cited_text, references, [])

    assert len(passages) == 1


def test_saved_response_payload_zeroes_the_range_of_an_empty_citation() -> None:
    """A structural anchor carries no text, so its source range collapses."""
    reference = _reference(chunk_id="c1", cited_text=None, start_char=5, end_char=9)

    _passages, document = build_saved_response_payload("answer", [reference], [])

    [item] = document.objects
    [range_] = item.citation.ranges
    assert (range_.start_index, range_.end_index) == (0, 0)


def test_saved_response_payload_anchors_only_chunks_it_can_key_on() -> None:
    anchored = _reference(chunk_id="c1", cited_text="cited")
    unkeyed = _reference(chunk_id=None, cited_text="cited")

    _passages, document = build_saved_response_payload(
        "answer", [anchored], [(anchored, 4), (unkeyed, 6)]
    )

    assert [entry.object_id.id for entry in document.body.inline_object_locations] == ["c1"]
