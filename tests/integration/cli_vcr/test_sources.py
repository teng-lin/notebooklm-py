"""CLI integration tests for source commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, parse_json_output, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestSourceListCommand:
    """Test 'notebooklm source list' command."""

    @pytest.mark.parametrize("json_flag", [False, True])
    @notebooklm_vcr.use_cassette("sources_list.yaml")
    def test_source_list(self, runner, mock_auth_for_vcr, mock_context, json_flag):
        """List sources with optional --json flag."""
        args = ["source", "list"]
        if json_flag:
            args.append("--json")

        result = runner.invoke(cli, args)
        assert_command_success(result)

        if json_flag and result.exit_code == 0:
            data = parse_json_output(result.output)
            assert data is not None, "Expected valid JSON output"
            assert isinstance(data, (list, dict))


class TestSourceAddCommand:
    """Test 'notebooklm source add' command."""

    @pytest.mark.parametrize(
        ("cassette", "args"),
        [
            (
                "sources_add_url.yaml",
                ["source", "add", "https://en.wikipedia.org/wiki/Artificial_intelligence"],
            ),
            (
                "sources_add_text.yaml",
                [
                    "source",
                    "add",
                    "--type",
                    "text",
                    "--title",
                    "Test Source",
                    "This is test content.",
                ],
            ),
        ],
    )
    def test_source_add(self, runner, mock_auth_for_vcr, mock_context, cassette, args):
        """Add source (URL or text) works with real client."""
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, args)
            assert_command_success(result)


class TestSourceContentCommands:
    """Test source content retrieval commands (guide, fulltext)."""

    @pytest.mark.parametrize(
        ("command", "cassette"),
        [
            ("guide", "sources_get_guide.yaml"),
            ("fulltext", "sources_get_fulltext.yaml"),
        ],
    )
    def test_source_content(self, runner, mock_auth_for_vcr, mock_context, command, cassette):
        """Get source content works with real client."""
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, ["source", command, "test_source_id"])
            assert_command_success(result)
