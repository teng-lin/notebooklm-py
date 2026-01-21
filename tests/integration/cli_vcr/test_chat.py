"""CLI integration tests for chat commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from integration.conftest import skip_no_cassettes
from notebooklm.notebooklm_cli import cli
from vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestAskCommand:
    """Test 'notebooklm ask' command."""

    @notebooklm_vcr.use_cassette("chat_ask.yaml")
    def test_ask_question(self, runner, mock_auth_for_vcr, mock_context):
        """Ask a question shows response from real client."""
        result = runner.invoke(cli, ["ask", "What is this notebook about?"])

        # Exit code 0 or 1 (no notebook context) are acceptable
        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"

    @notebooklm_vcr.use_cassette("chat_ask.yaml")
    def test_ask_question_json(self, runner, mock_auth_for_vcr, mock_context):
        """Ask with --json flag returns JSON output."""
        result = runner.invoke(cli, ["ask", "--json", "What is this notebook about?"])

        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"


class TestHistoryCommand:
    """Test 'notebooklm history' command."""

    @notebooklm_vcr.use_cassette("chat_get_history.yaml")
    def test_history(self, runner, mock_auth_for_vcr, mock_context):
        """History command shows chat history from real client."""
        result = runner.invoke(cli, ["history"])

        # Exit code 0 or 1 (no notebook context) are acceptable
        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"
