"""Behavioral parity pins shared by Android Notes frontend adapters."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests._helpers.android_notes import StatefulAndroidNotesTransport

from notebooklm._android.codecs.notes import (
    decode_note,
    decode_note_backed_mind_map_rows,
    decode_note_by_id,
)
from notebooklm._android.notes import (
    DELETE_NOTES_METHOD,
    GET_NOTES_METHOD,
    MUTATE_NOTE_METHOD,
    AndroidNotesAPI,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import notes_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._app.notes import NoteSaveResult, execute_note_save
from notebooklm._web.rows.notes import NoteRow

NOTEBOOK_ID = "11111111-1111-1111-1111-111111111111"
NOTE_ID = "55555555-5555-5555-5555-555555555555"


def test_android_notes_satisfy_public_nullable_raw_and_absence_contracts() -> None:
    """Pin public semantics, not incidental Web soft-delete storage details."""
    note = notes_pb2.ProjectNote(
        id=NOTE_ID,
        name="Title",
        content="Body",
        metadata=notes_pb2.NoteMetadata(type=notes_pb2.USER_WRITTEN),
    )
    note.metadata.last_edit_timestamp.FromSeconds(1_700_000_000)

    decoded = decode_note(note, NOTEBOOK_ID, method_id=GET_NOTES_METHOD)
    # ``Note.created_at`` is explicitly optional. The only Android timestamp
    # is last-edit time, so preserving None is the honest public projection.
    assert decoded.created_at is None

    map_note = notes_pb2.ProjectNote(
        id="map-1",
        name="Map title",
        content='{"children": []}',
        metadata=notes_pb2.NoteMetadata(note_prompt_type=notes_pb2.MIND_MAP),
    )
    response = notes_pb2.GetNotesResponse(notes=[notes_pb2.NoteOrStatus(note=map_note)])
    raw_rows = decode_note_backed_mind_map_rows(response, method_id=GET_NOTES_METHOD)
    # The public return is opaque ``list[Any]``; this exact two-slot row is
    # also the established Web legacy shape and remains consumable by NoteRow.
    assert raw_rows == [["map-1", '{"children": []}']]
    assert (NoteRow(raw_rows[0]).id, NoteRow(raw_rows[0]).content) == (
        "map-1",
        '{"children": []}',
    )

    # A status-only/absent Android projection is a genuine public miss. The
    # contract for get_or_none is None, not Web's private persisted tombstone.
    absent = notes_pb2.GetNotesResponse(notes=[notes_pb2.NoteOrStatus()])
    assert (
        decode_note_by_id(
            absent,
            NOTEBOOK_ID,
            NOTE_ID,
            method_id=GET_NOTES_METHOD,
        )
        is None
    )


def _api(transport: StatefulAndroidNotesTransport) -> AndroidNotesAPI:
    return AndroidNotesAPI(cast(AndroidSession, transport), deletion_poll_delays=(0.0,))


async def _resolve_notebook(
    _client: Any,
    notebook_id: str,
    *,
    json_output: bool = False,
) -> str:
    return notebook_id


async def _resolve_note(
    _client: Any,
    _notebook_id: str,
    note_id: str,
    *,
    json_output: bool = False,
) -> str:
    return note_id


@pytest.mark.parametrize("operation", ["get", "update", "delete"])
async def test_unrelated_malformed_row_does_not_break_exact_id_workflows(operation: str) -> None:
    transport = StatefulAndroidNotesTransport(
        notebook_id=NOTEBOOK_ID,
        note_id=NOTE_ID,
        malformed_first=True,
    )
    notes = _api(transport)

    if operation == "get":
        note = await notes.get(NOTEBOOK_ID, NOTE_ID)
        assert note.id == NOTE_ID
        assert note.title == "Original title"
    elif operation == "update":
        await notes.update(
            NOTEBOOK_ID,
            NOTE_ID,
            title="Updated title",
            content="Updated content",
        )
        assert transport.note is not None
        assert transport.note.name == "Updated title"
        assert transport.note.content == "Updated content"
    else:
        await notes.delete(NOTEBOOK_ID, NOTE_ID)
        assert transport.note is None

    assert [method for method, _request, _kwargs in transport.calls] == {
        "get": [GET_NOTES_METHOD],
        "update": [GET_NOTES_METHOD, MUTATE_NOTE_METHOD, GET_NOTES_METHOD],
        "delete": [GET_NOTES_METHOD, DELETE_NOTES_METHOD, GET_NOTES_METHOD],
    }[operation]


@pytest.mark.parametrize(
    ("title", "content", "expected_title", "expected_content"),
    [
        ("Updated title", None, "Updated title", "Original content"),
        (None, "Updated content", "Original title", "Updated content"),
    ],
)
async def test_app_note_save_runtime_none_preserves_omitted_android_field_and_result(
    title: str | None,
    content: str | None,
    expected_title: str,
    expected_content: str,
) -> None:
    transport = StatefulAndroidNotesTransport(notebook_id=NOTEBOOK_ID, note_id=NOTE_ID)
    client = SimpleNamespace(notes=_api(transport))

    result = await execute_note_save(
        client,
        NOTEBOOK_ID,
        NOTE_ID,
        title=title,
        content=content,
        resolve_notebook_id=_resolve_notebook,
        resolve_note_id=_resolve_note,
    )

    assert result == NoteSaveResult(notebook_id=NOTEBOOK_ID, note_id=NOTE_ID)
    assert transport.note is not None
    assert transport.note.name == expected_title
    assert transport.note.content == expected_content
