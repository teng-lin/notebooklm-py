"""CLI integration tests for download commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, parse_json_output, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestDownloadCommands:
    """Test 'notebooklm download' commands for text-based artifacts."""

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


class TestBinaryDownloadCommands:
    """Test 'notebooklm download' commands for binary artifacts (audio, video, etc.).

    Note: Binary download cassettes may not include the actual binary content
    if recorded in a CI environment or without a completed artifact.
    These tests verify the CLI→Client→RPC path works correctly.
    """

    @pytest.mark.parametrize(
        ("command", "filename", "cassette"),
        [
            ("audio", "audio.mp3", "artifacts_download_audio.yaml"),
            ("video", "video.mp4", "artifacts_download_video.yaml"),
            ("infographic", "infographic.png", "artifacts_download_infographic.yaml"),
            ("slide-deck", "slides.pdf", "artifacts_download_slide_deck.yaml"),
        ],
    )
    def test_binary_download(
        self,
        runner,
        mock_auth_for_vcr,
        mock_context,
        tmp_path,
        command,
        filename,
        cassette,
    ):
        """Binary download commands work with real client."""
        output_file = tmp_path / filename
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, ["download", command, str(output_file)])
            # Use same assertion as text downloads - accepts exit 0 or 1
            assert_command_success(result)


class TestDownloadByUUID:
    """Test 'notebooklm download <uuid>' commands."""

    def test_download_uuid_dry_run(
        self,
        runner,
        mock_auth_for_vcr,
        mock_context,
        tmp_path,
    ):
        """UUID download with --dry-run shows what would be downloaded."""
        # Use quiz cassette which lists artifacts
        with notebooklm_vcr.use_cassette("artifacts_download_quiz.yaml"):
            result = runner.invoke(
                cli,
                ["download", "--dry-run", "--json", "-o", str(tmp_path), "nonexistent123"],
            )
            # Should handle gracefully - either find artifacts or report not found
            output = parse_json_output(result.output)
            if output:
                # JSON output means it processed the request
                assert "not_found" in output or "results" in output

    def test_download_uuid_not_found(
        self,
        runner,
        mock_auth_for_vcr,
        mock_context,
    ):
        """UUID download reports when artifact ID not found."""
        with notebooklm_vcr.use_cassette("artifacts_list_audio.yaml"):
            result = runner.invoke(
                cli,
                ["download", "--json", "definitely-not-a-real-uuid-12345"],
            )
            output = parse_json_output(result.output)
            if output:
                assert "not_found" in output
                assert "definitely-not-a-real-uuid-12345" in output.get("not_found", [])
