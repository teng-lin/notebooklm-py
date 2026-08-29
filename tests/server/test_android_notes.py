"""REST Notes full-update parity against the real Android adapter."""

from __future__ import annotations

from typing import cast

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from notebooklm._android.notes import AndroidNotesAPI  # noqa: E402
from notebooklm._android.session import AndroidSession  # noqa: E402
from tests._helpers.android_notes import StatefulAndroidNotesTransport  # noqa: E402

from .fakes import FakeClient  # noqa: E402

NOTEBOOK_ID = "11111111-1111-1111-1111-111111111111"
NOTE_ID = "55555555-5555-5555-5555-555555555555"


def test_put_updates_real_android_note_and_returns_canonical_readback(
    authed_client: TestClient,
    fake_client: FakeClient,
) -> None:
    transport = StatefulAndroidNotesTransport(notebook_id=NOTEBOOK_ID, note_id=NOTE_ID)
    fake_client.notes = AndroidNotesAPI(cast(AndroidSession, transport))

    response = authed_client.put(
        f"/v1/notebooks/{NOTEBOOK_ID}/notes/{NOTE_ID}",
        json={"title": "Updated title", "content": "Updated content"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": NOTE_ID,
        "notebook_id": NOTEBOOK_ID,
        "title": "Updated title",
        "content": "Updated content",
        "created_at": None,
    }
    assert transport.note is not None
    assert transport.note.name == "Updated title"
    assert transport.note.content == "Updated content"
