"""CLI integration tests for artifact commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestArtifactListCommand:
    """Test 'notebooklm artifact list' command."""

    @notebooklm_vcr.use_cassette("artifacts_list.yaml")
    def test_artifact_list(self, runner, mock_auth_for_vcr, mock_context):
        """List artifacts shows results from real client."""
        result = runner.invoke(cli, ["artifact", "list"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)

    @notebooklm_vcr.use_cassette("artifacts_list.yaml")
    def test_artifact_list_json(self, runner, mock_auth_for_vcr, mock_context):
        """List artifacts with --json flag returns JSON output."""
        result = runner.invoke(cli, ["artifact", "list", "--json"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestArtifactListByType:
    """Test 'notebooklm artifact list --type' command."""

    @pytest.mark.parametrize(
        ("artifact_type", "cassette"),
        [
            ("quiz", "artifacts_list_quizzes.yaml"),
            ("report", "artifacts_list_reports.yaml"),
            ("video", "artifacts_list_video.yaml"),
            ("flashcard", "artifacts_list_flashcards.yaml"),
            ("infographic", "artifacts_list_infographics.yaml"),
            ("slide-deck", "artifacts_list_slide_decks.yaml"),
            ("data-table", "artifacts_list_data_tables.yaml"),
            ("mind-map", "artifacts_list_audio.yaml"),  # Uses audio cassette as fallback
        ],
    )
    def test_artifact_list_by_type(
        self, runner, mock_auth_for_vcr, mock_context, artifact_type, cassette
    ):
        """List artifacts filtered by type."""
        with notebooklm_vcr.use_cassette(cassette):
            result = runner.invoke(cli, ["artifact", "list", "--type", artifact_type])
            # allow_no_context=True: cassette may not match mock notebook ID
            assert_command_success(result)


class TestArtifactSuggestReportsCommand:
    """Test 'notebooklm artifact suggestions' command."""

    @notebooklm_vcr.use_cassette("artifacts_suggest_reports.yaml")
    def test_artifact_suggestions(self, runner, mock_auth_for_vcr, mock_context):
        """Get artifact suggestions works with real client."""
        result = runner.invoke(cli, ["artifact", "suggestions"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)
