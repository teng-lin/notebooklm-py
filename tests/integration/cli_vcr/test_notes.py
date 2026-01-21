"""CLI integration tests for note commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestNoteCommands:
    """Test 'notebooklm note' commands."""

    @pytest.mark.parametrize(
        ("cassette", "args"),
        [
            ("notes_list.yaml", ["note", "list"]),
            ("notes_create.yaml", ["note", "create", "-t", "Test Note", "This is test content."]),
        ],
    )
    def test_note_command(self, runner, mock_auth_for_vcr, mock_context, cassette, args):
        """Note commands work with real client."""
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, args)
            assert_command_success(result)
