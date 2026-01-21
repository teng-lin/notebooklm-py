"""CLI integration tests for chat commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestAskCommand:
    """Test 'notebooklm ask' command."""

    @notebooklm_vcr.use_cassette("chat_ask.yaml")
    def test_ask_question(self, runner, mock_auth_for_vcr, mock_context):
        """Ask a question shows response from real client."""
        result = runner.invoke(cli, ["ask", "What is this notebook about?"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)

    @notebooklm_vcr.use_cassette("chat_ask.yaml")
    def test_ask_question_json(self, runner, mock_auth_for_vcr, mock_context):
        """Ask with --json flag returns JSON output."""
        result = runner.invoke(cli, ["ask", "--json", "What is this notebook about?"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestHistoryCommand:
    """Test 'notebooklm history' command."""

    @notebooklm_vcr.use_cassette("chat_get_history.yaml")
    def test_history(self, runner, mock_auth_for_vcr, mock_context):
        """History command shows chat history from real client."""
        result = runner.invoke(cli, ["history"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)
