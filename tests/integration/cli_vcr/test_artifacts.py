"""CLI integration tests for artifact commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, parse_json_output, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestArtifactListCommand:
    """Test 'notebooklm artifact list' command."""

    @pytest.mark.parametrize("json_flag", [False, True])
    @notebooklm_vcr.use_cassette("artifacts_list.yaml")
    def test_artifact_list(self, runner, mock_auth_for_vcr, mock_context, json_flag):
        """List artifacts with optional --json flag."""
        args = ["artifact", "list"]
        if json_flag:
            args.append("--json")

        result = runner.invoke(cli, args)
        assert_command_success(result)

        if json_flag and result.exit_code == 0:
            data = parse_json_output(result.output)
            assert data is not None, "Expected valid JSON output"
            assert isinstance(data, (list, dict))


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
            assert_command_success(result)


class TestArtifactSuggestionsCommand:
    """Test 'notebooklm artifact suggestions' command."""

    @notebooklm_vcr.use_cassette("artifacts_suggest_reports.yaml")
    def test_artifact_suggestions(self, runner, mock_auth_for_vcr, mock_context):
        """Get artifact suggestions works with real client."""
        result = runner.invoke(cli, ["artifact", "suggestions"])
        assert_command_success(result)
