"""CLI integration tests for generate commands.

These tests exercise the full CLI → Client → RPC path using VCR cassettes.
"""

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import assert_command_success, notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestGenerateQuizCommand:
    """Test 'notebooklm generate quiz' command."""

    @notebooklm_vcr.use_cassette("artifacts_generate_quiz.yaml")
    def test_generate_quiz(self, runner, mock_auth_for_vcr, mock_context):
        """Generate quiz works with real client."""
        result = runner.invoke(cli, ["generate", "quiz"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestGenerateFlashcardsCommand:
    """Test 'notebooklm generate flashcards' command."""

    @notebooklm_vcr.use_cassette("artifacts_generate_flashcards.yaml")
    def test_generate_flashcards(self, runner, mock_auth_for_vcr, mock_context):
        """Generate flashcards works with real client."""
        result = runner.invoke(cli, ["generate", "flashcards"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)


class TestGenerateReportCommand:
    """Test 'notebooklm generate report' command."""

    @notebooklm_vcr.use_cassette("artifacts_generate_report.yaml")
    def test_generate_report(self, runner, mock_auth_for_vcr, mock_context):
        """Generate report works with real client."""
        result = runner.invoke(cli, ["generate", "report", "--format", "briefing-doc"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)

    @notebooklm_vcr.use_cassette("artifacts_generate_study_guide.yaml")
    def test_generate_study_guide(self, runner, mock_auth_for_vcr, mock_context):
        """Generate study guide works with real client."""
        result = runner.invoke(cli, ["generate", "report", "--format", "study-guide"])
        # allow_no_context=True: cassette may not match mock notebook ID
        assert_command_success(result)
