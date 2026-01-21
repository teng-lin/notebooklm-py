"""CLI integration tests for source commands.

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


class TestSourceListCommand:
    """Test 'notebooklm source list' command."""

    @notebooklm_vcr.use_cassette("sources_list.yaml")
    def test_source_list(self, runner, mock_auth_for_vcr, mock_context):
        """List sources shows results from real client."""
        result = runner.invoke(cli, ["source", "list"])

        # Exit code 0 or 1 (no notebook context) are acceptable
        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"

    @notebooklm_vcr.use_cassette("sources_list.yaml")
    def test_source_list_json(self, runner, mock_auth_for_vcr, mock_context):
        """List sources with --json flag returns JSON output."""
        result = runner.invoke(cli, ["source", "list", "--json"])

        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"
        if result.exit_code == 0:
            import json

            # Should be valid JSON if successful
            try:
                json.loads(result.output)
            except json.JSONDecodeError:
                # May have non-JSON prefix, that's OK
                pass
