"""Behavioral parity pins shared by Android Notes frontend adapters."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests._helpers.android_notes import StatefulAndroidNotesTransport

from notebooklm._android.notes import (
    DELETE_NOTES_METHOD,
    GET_NOTES_METHOD,
    MUTATE_NOTE_METHOD,
    AndroidNotesAPI,
)
from notebooklm._android.session import AndroidSession
from notebooklm._app.notes import NoteSaveResult, execute_note_save

NOTEBOOK_ID = "11111111-1111-1111-1111-111111111111"
NOTE_ID = "55555555-5555-5555-5555-555555555555"


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
