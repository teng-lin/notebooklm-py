"""CLI Notes partial-update parity against the real Android adapter."""

from __future__ import annotations

import json
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
from notebooklm._android.notes import AndroidNotesAPI
from notebooklm._android.session import AndroidSession
from notebooklm.cli import helpers as helpers_module
from notebooklm.notebooklm_cli import cli
from tests._helpers.android_notes import StatefulAndroidNotesTransport

from .conftest import create_mock_client, inject_client

NOTEBOOK_ID = "11111111-1111-1111-1111-111111111111"
NOTE_ID = "55555555-5555-5555-5555-555555555555"


@pytest.mark.parametrize(
    ("option", "value", "expected_title", "expected_content"),
    [
        ("--title", "Updated title", "Updated title", "Original content"),
        ("--content", "Updated content", "Original title", "Updated content"),
    ],
)
def test_note_save_json_preserves_omitted_android_field_and_envelope(
    option: str,
    value: str,
    expected_title: str,
    expected_content: str,
) -> None:
    transport = StatefulAndroidNotesTransport(notebook_id=NOTEBOOK_ID, note_id=NOTE_ID)
    client = create_mock_client()
    client.notes = AndroidNotesAPI(cast(AndroidSession, transport))
    runner = CliRunner()

    with (
        patch.object(helpers_module, "load_auth_from_storage") as load_auth,
        patch.object(auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock) as fetch,
    ):
        load_auth.return_value = {"SID": "test", "HSID": "test", "SSID": "test"}
        fetch.return_value = ("csrf", "session")
        result = runner.invoke(
            cli,
            [
                "note",
                "save",
                NOTE_ID,
                option,
                value,
                "-n",
                NOTEBOOK_ID,
                "--json",
            ],
            obj=inject_client(client),
        )

    assert result.exit_code == 0, result.output
    field = "title" if option == "--title" else "content"
    assert json.loads(result.output) == {
        "id": NOTE_ID,
        "notebook_id": NOTEBOOK_ID,
        "saved": True,
        field: value,
    }
    assert transport.note is not None
    assert transport.note.name == expected_title
    assert transport.note.content == expected_content
