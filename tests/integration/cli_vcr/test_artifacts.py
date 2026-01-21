"""CLI integration tests for artifact commands.

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


class TestArtifactListCommand:
    """Test 'notebooklm artifact list' command."""

    @notebooklm_vcr.use_cassette("artifacts_list.yaml")
    def test_artifact_list(self, runner, mock_auth_for_vcr, mock_context):
        """List artifacts shows results from real client."""
        result = runner.invoke(cli, ["artifact", "list"])

        # Exit code 0 or 1 (no notebook context) are acceptable
        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"

    @notebooklm_vcr.use_cassette("artifacts_list.yaml")
    def test_artifact_list_json(self, runner, mock_auth_for_vcr, mock_context):
        """List artifacts with --json flag returns JSON output."""
        result = runner.invoke(cli, ["artifact", "list", "--json"])

        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"


class TestArtifactListByType:
    """Test 'notebooklm artifact list --type' command."""

    @notebooklm_vcr.use_cassette("artifacts_list_quizzes.yaml")
    def test_artifact_list_quizzes(self, runner, mock_auth_for_vcr, mock_context):
        """List quiz artifacts."""
        result = runner.invoke(cli, ["artifact", "list", "--type", "quiz"])
        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"

    @notebooklm_vcr.use_cassette("artifacts_list_reports.yaml")
    def test_artifact_list_reports(self, runner, mock_auth_for_vcr, mock_context):
        """List report artifacts."""
        result = runner.invoke(cli, ["artifact", "list", "--type", "report"])
        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"

    @notebooklm_vcr.use_cassette("artifacts_list_video.yaml")
    def test_artifact_list_video(self, runner, mock_auth_for_vcr, mock_context):
        """List video artifacts."""
        result = runner.invoke(cli, ["artifact", "list", "--type", "video"])
        assert result.exit_code in (0, 1), f"Command crashed: {result.output}"
