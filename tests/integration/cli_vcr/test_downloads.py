"""CLI integration tests for download commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestDownloadQuizCommand:
    """Test 'notebooklm download quiz' command."""

    @notebooklm_vcr.use_cassette("artifacts_download_quiz.yaml")
    def test_download_quiz(self, runner, mock_auth_for_vcr, mock_context, tmp_path):
        """Download quiz works with real client."""
        output_file = tmp_path / "quiz.json"
        result = runner.invoke(cli, ["download", "quiz", str(output_file)])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)

    @notebooklm_vcr.use_cassette("artifacts_download_quiz_markdown.yaml")
    def test_download_quiz_markdown(self, runner, mock_auth_for_vcr, mock_context, tmp_path):
        """Download quiz as markdown works with real client."""
        output_file = tmp_path / "quiz.md"
        result = runner.invoke(cli, ["download", "quiz", "--format", "markdown", str(output_file)])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestDownloadFlashcardsCommand:
    """Test 'notebooklm download flashcards' command."""

    @notebooklm_vcr.use_cassette("artifacts_download_flashcards.yaml")
    def test_download_flashcards(self, runner, mock_auth_for_vcr, mock_context, tmp_path):
        """Download flashcards works with real client."""
        output_file = tmp_path / "flashcards.json"
        result = runner.invoke(cli, ["download", "flashcards", str(output_file)])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)

    @notebooklm_vcr.use_cassette("artifacts_download_flashcards_markdown.yaml")
    def test_download_flashcards_markdown(self, runner, mock_auth_for_vcr, mock_context, tmp_path):
        """Download flashcards as markdown works with real client."""
        output_file = tmp_path / "flashcards.md"
        result = runner.invoke(
            cli, ["download", "flashcards", "--format", "markdown", str(output_file)]
        )
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestDownloadReportCommand:
    """Test 'notebooklm download report' command."""

    @notebooklm_vcr.use_cassette("artifacts_download_report.yaml")
    def test_download_report(self, runner, mock_auth_for_vcr, mock_context, tmp_path):
        """Download report works with real client."""
        output_file = tmp_path / "report.md"
        result = runner.invoke(cli, ["download", "report", str(output_file)])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestDownloadMindMapCommand:
    """Test 'notebooklm download mind-map' command."""

    @notebooklm_vcr.use_cassette("artifacts_download_mind_map.yaml")
    def test_download_mind_map(self, runner, mock_auth_for_vcr, mock_context, tmp_path):
        """Download mind map works with real client."""
        output_file = tmp_path / "mindmap.json"
        result = runner.invoke(cli, ["download", "mind-map", str(output_file)])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestDownloadDataTableCommand:
    """Test 'notebooklm download data-table' command."""

    @notebooklm_vcr.use_cassette("artifacts_download_data_table.yaml")
    def test_download_data_table(self, runner, mock_auth_for_vcr, mock_context, tmp_path):
        """Download data table works with real client."""
        output_file = tmp_path / "data.csv"
        result = runner.invoke(cli, ["download", "data-table", str(output_file)])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)
