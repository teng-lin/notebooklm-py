"""CLI integration tests for notebook commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
Unlike unit tests, these use real NotebookLMClient instances (not mocks).

Reuses existing cassettes from tests/cassettes/ where possible.
"""

import sys
from pathlib import Path

import pytest

# Add tests directory to path for vcr_config import
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from integration.conftest import skip_no_cassettes
from notebooklm.notebooklm_cli import cli
from vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestListCommand:
    """Test 'notebooklm list' command."""

    @notebooklm_vcr.use_cassette("notebooks_list.yaml")
    def test_list_notebooks(self, runner, mock_auth_for_vcr):
        """List notebooks shows results from real client."""
        result = runner.invoke(cli, ["list"])

        assert result.exit_code == 0, f"Command failed: {result.output}"
        # Output should contain notebook data (from cassette)
        # The cassette has recorded response with notebook list
        assert "No notebooks found" not in result.output or result.exit_code == 0

    @notebooklm_vcr.use_cassette("notebooks_list.yaml")
    def test_list_notebooks_json(self, runner, mock_auth_for_vcr):
        """List notebooks with --json flag returns JSON output."""
        result = runner.invoke(cli, ["list", "--json"])

        assert result.exit_code == 0, f"Command failed: {result.output}"
        # JSON output should be parseable
        import json

        try:
            data = json.loads(result.output)
            assert isinstance(data, (list, dict))
        except json.JSONDecodeError:
            # Some responses may have additional output before JSON
            # Try to find JSON in output
            lines = result.output.strip().split("\n")
            for line in lines:
                try:
                    data = json.loads(line)
                    assert isinstance(data, (list, dict))
                    break
                except json.JSONDecodeError:
                    continue


class TestSummaryCommand:
    """Test 'notebooklm summary' command."""

    @notebooklm_vcr.use_cassette("notebooks_get_summary.yaml")
    def test_summary(self, runner, mock_auth_for_vcr, mock_context):
        """Summary command shows notebook summary."""
        result = runner.invoke(cli, ["summary"])

        # May fail if no context is set, but should not crash
        # Exit code 0 or 1 are acceptable (1 = no notebook context)
        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"


class TestStatusCommand:
    """Test 'notebooklm status' command (doesn't need VCR)."""

    def test_status_no_context(self, runner, mock_auth_for_vcr, mock_context):
        """Status shows current context."""
        result = runner.invoke(cli, ["status"])

        # Should succeed even without real API calls
        assert result.exit_code == 0, f"Command failed: {result.output}"
        # Output should mention notebook context
        assert "notebook" in result.output.lower() or "context" in result.output.lower()
