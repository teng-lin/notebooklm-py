"""CLI integration tests for source commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, parse_json_output, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestSourceListCommand:
    """Test 'notebooklm source list' command."""

    @notebooklm_vcr.use_cassette("sources_list.yaml")
    def test_source_list(self, runner, mock_auth_for_vcr, mock_context):
        """List sources shows results from real client."""
        result = runner.invoke(cli, ["source", "list"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)

    @notebooklm_vcr.use_cassette("sources_list.yaml")
    def test_source_list_json(self, runner, mock_auth_for_vcr, mock_context):
        """List sources with --json flag returns JSON output."""
        result = runner.invoke(cli, ["source", "list", "--json"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)

        if result.exit_code == 0:
            data = parse_json_output(result.output)
            assert data is not None, "Expected valid JSON output"
            assert isinstance(data, (list, dict))


class TestSourceAddCommand:
    """Test 'notebooklm source add' command."""

    @notebooklm_vcr.use_cassette("sources_add_url.yaml")
    def test_source_add_url(self, runner, mock_auth_for_vcr, mock_context):
        """Add URL source works with real client."""
        result = runner.invoke(
            cli, ["source", "add", "https://en.wikipedia.org/wiki/Artificial_intelligence"]
        )
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)

    @notebooklm_vcr.use_cassette("sources_add_text.yaml")
    def test_source_add_text(self, runner, mock_auth_for_vcr, mock_context):
        """Add text source works with real client."""
        result = runner.invoke(
            cli,
            ["source", "add", "--type", "text", "--title", "Test Source", "This is test content."],
        )
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestSourceGuideCommand:
    """Test 'notebooklm source guide' command."""

    @notebooklm_vcr.use_cassette("sources_get_guide.yaml")
    def test_source_guide(self, runner, mock_auth_for_vcr, mock_context):
        """Get source guide works with real client."""
        # Use a placeholder source ID - VCR will replay recorded response
        result = runner.invoke(cli, ["source", "guide", "test_source_id"])
        # allow_no_context=True: cassette may not match mock IDs
        assert_command_success(result)


class TestSourceFulltextCommand:
    """Test 'notebooklm source fulltext' command."""

    @notebooklm_vcr.use_cassette("sources_get_fulltext.yaml")
    def test_source_fulltext(self, runner, mock_auth_for_vcr, mock_context):
        """Get source fulltext works with real client."""
        result = runner.invoke(cli, ["source", "fulltext", "test_source_id"])
        # allow_no_context=True: cassette may not match mock IDs
        assert_command_success(result)
