"""CLI integration tests for chat commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, parse_json_output, skip_no_cassettes

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

        if result.exit_code == 0:
            data = parse_json_output(result.output)
            assert data is not None, "Expected valid JSON output"
            assert isinstance(data, list | dict)


class TestHistoryCommand:
    """Test 'notebooklm history' command."""

    @notebooklm_vcr.use_cassette("chat_get_history.yaml")
    def test_history(self, runner, mock_auth_for_vcr, mock_context):
        """History command shows Q&A turns from last conversation."""
        result = runner.invoke(cli, ["history"])
        assert_command_success(result)


class TestGetConversationTurnsCommand:
    """Test conversation turns fetching via GET_CONVERSATION_TURNS (khqZz) RPC.

    Cassette: chat_get_conversation_turns.yaml
    Notebook: f59447f4-2a13-4d64-9df8-bc89c615c7bd
    Conversation: b1556695-010e-4fe3-a841-a6efa7fe0697

    The cassette captures two sequential batchexecute calls:
      1. hPTbtc (GET_LAST_CONVERSATION_ID) → returns one conversation ID
      2. khqZz (GET_CONVERSATION_TURNS) → returns Q&A turns for that conversation
    """

    @notebooklm_vcr.use_cassette("chat_get_conversation_turns.yaml")
    def test_history_shows_qa_previews(self, runner, mock_auth_for_vcr, mock_context):
        """history command shows Q&A preview columns populated from khqZz turns API."""
        # Use the full UUID directly so resolve_notebook_id skips LIST_NOTEBOOKS
        result = runner.invoke(cli, ["history", "-n", "f59447f4-2a13-4d64-9df8-bc89c615c7bd"])
        assert result.exit_code == 0, result.output
        assert "What question should I" in result.output
        assert "Based on the sources" in result.output
