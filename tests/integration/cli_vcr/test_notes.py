"""CLI integration tests for note commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestNoteListCommand:
    """Test 'notebooklm note list' command."""

    @notebooklm_vcr.use_cassette("notes_list.yaml")
    def test_note_list(self, runner, mock_auth_for_vcr, mock_context):
        """List notes shows results from real client."""
        result = runner.invoke(cli, ["note", "list"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestNoteCreateCommand:
    """Test 'notebooklm note create' command."""

    @notebooklm_vcr.use_cassette("notes_create.yaml")
    def test_note_create(self, runner, mock_auth_for_vcr, mock_context):
        """Create note works with real client."""
        result = runner.invoke(
            cli,
            ["note", "create", "-t", "Test Note", "This is test content."],
        )
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)
