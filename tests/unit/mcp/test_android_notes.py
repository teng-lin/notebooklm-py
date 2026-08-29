"""MCP Notes partial-update parity against the real Android adapter."""

from __future__ import annotations

from typing import cast

import pytest

pytest.importorskip("fastmcp")

from notebooklm._android.notes import AndroidNotesAPI  # noqa: E402
from notebooklm._android.session import AndroidSession  # noqa: E402
from tests._helpers.android_notes import StatefulAndroidNotesTransport  # noqa: E402

NOTEBOOK_ID = "11111111-1111-1111-1111-111111111111"
NOTE_ID = "55555555-5555-5555-5555-555555555555"


@pytest.mark.parametrize(
    ("payload", "expected_title", "expected_content"),
    [
        ({"title": "Updated title"}, "Updated title", "Original content"),
        ({"content": "Updated content"}, "Original title", "Updated content"),
    ],
)
async def test_note_save_preserves_omitted_android_field_and_mcp_envelope(
    mcp_call,
    mock_client,
    payload: dict[str, str],
    expected_title: str,
    expected_content: str,
) -> None:
    transport = StatefulAndroidNotesTransport(notebook_id=NOTEBOOK_ID, note_id=NOTE_ID)
    mock_client.notes = AndroidNotesAPI(cast(AndroidSession, transport))

    result = await mcp_call(
        "note_save",
        {"notebook": NOTEBOOK_ID, "note": NOTE_ID, **payload},
    )

    assert result.structured_content == {
        "status": "updated",
        "notebook_id": NOTEBOOK_ID,
        "note_id": NOTE_ID,
    }
    assert transport.note is not None
    assert transport.note.name == expected_title
    assert transport.note.content == expected_content
