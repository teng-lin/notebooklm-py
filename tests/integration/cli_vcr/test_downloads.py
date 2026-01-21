"""CLI integration tests for download commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestDownloadCommands:
    """Test 'notebooklm download' commands."""

    @pytest.mark.parametrize(
        ("command", "filename", "cassette", "extra_args"),
        [
            ("quiz", "quiz.json", "artifacts_download_quiz.yaml", []),
            ("quiz", "quiz.md", "artifacts_download_quiz_markdown.yaml", ["--format", "markdown"]),
            ("flashcards", "flashcards.json", "artifacts_download_flashcards.yaml", []),
            (
                "flashcards",
                "flashcards.md",
                "artifacts_download_flashcards_markdown.yaml",
                ["--format", "markdown"],
            ),
            ("report", "report.md", "artifacts_download_report.yaml", []),
            ("mind-map", "mindmap.json", "artifacts_download_mind_map.yaml", []),
            ("data-table", "data.csv", "artifacts_download_data_table.yaml", []),
        ],
    )
    def test_download(
        self,
        runner,
        mock_auth_for_vcr,
        mock_context,
        tmp_path,
        command,
        filename,
        cassette,
        extra_args,
    ):
        """Download commands work with real client."""
        output_file = tmp_path / filename
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, ["download", command, *extra_args, str(output_file)])
            assert_command_success(result)
