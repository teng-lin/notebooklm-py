"""CLI integration tests for notebook commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
Unlike unit tests, these use real NotebookLMClient instances (not mocks).

Reuses existing cassettes from tests/cassettes/ where possible.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, parse_json_output, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestListCommand:
    """Test 'notebooklm list' command."""

    @notebooklm_vcr.use_cassette("notebooks_list.yaml")
    def test_list_notebooks(self, runner, mock_auth_for_vcr):
        """List notebooks shows results from real client."""
        result = runner.invoke(cli, ["list"])
        assert_command_success(result, allow_no_context=False)

    @notebooklm_vcr.use_cassette("notebooks_list.yaml")
    def test_list_notebooks_json(self, runner, mock_auth_for_vcr):
        """List notebooks with --json flag returns JSON output."""
        result = runner.invoke(cli, ["list", "--json"])
        assert_command_success(result, allow_no_context=False)

        data = parse_json_output(result.output)
        assert data is not None, "Expected valid JSON output"
        assert isinstance(data, (list, dict))


class TestSummaryCommand:
    """Test 'notebooklm summary' command."""

    @notebooklm_vcr.use_cassette("notebooks_get_summary.yaml")
    def test_summary(self, runner, mock_auth_for_vcr, mock_context):
        """Summary command shows notebook summary."""
        result = runner.invoke(cli, ["summary"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestStatusCommand:
    """Test 'notebooklm status' command (doesn't need VCR)."""

    def test_status_shows_context(self, runner, mock_auth_for_vcr, mock_context):
        """Status shows current context."""
        result = runner.invoke(cli, ["status"])
        assert_command_success(result, allow_no_context=False)
        assert "notebook" in result.output.lower() or "context" in result.output.lower()
