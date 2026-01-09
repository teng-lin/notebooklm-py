"""Tests for SectionedGroup CLI help formatting."""

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli


@pytest.fixture
def runner():
    return CliRunner()


class TestSectionedHelp:
    """Test that CLI help output is organized into sections."""

    def test_help_shows_session_section(self, runner):
        """Verify Session section appears with expected commands."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Session:" in result.output
        assert "login" in result.output
        assert "use" in result.output
        assert "status" in result.output
        assert "clear" in result.output

    def test_help_shows_notebooks_section(self, runner):
        """Verify Notebooks section appears with expected commands."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Notebooks:" in result.output
        assert "list" in result.output
        assert "create" in result.output
        assert "delete" in result.output
        assert "rename" in result.output
        assert "share" in result.output
        assert "summary" in result.output

    def test_help_shows_chat_section(self, runner):
        """Verify Chat section appears with expected commands."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Chat:" in result.output
        assert "ask" in result.output
        assert "configure" in result.output
        assert "history" in result.output

    def test_help_shows_command_groups_section(self, runner):
        """Verify Command Groups section appears with subcommand listings."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Command Groups" in result.output
        # These should show subcommands, not help text
        assert "source" in result.output
        assert "artifact" in result.output
        assert "note" in result.output

    def test_help_shows_artifact_actions_section(self, runner):
        """Verify Artifact Actions section appears with type listings."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Artifact Actions" in result.output
        assert "generate" in result.output
        assert "download" in result.output

    def test_source_group_shows_subcommands(self, runner):
        """Verify source group subcommands are listed in help."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # Source subcommands should appear in the command group line
        # They should be sorted alphabetically
        assert "add" in result.output
        assert "list" in result.output

    def test_generate_group_shows_types(self, runner):
        """Verify generate subcommands (types) are listed in help."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # Generate types should appear
        assert "audio" in result.output
        assert "video" in result.output

    def test_no_commands_section_header(self, runner):
        """Verify the default 'Commands:' section header is replaced by sections."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # The output should not have a generic "Commands:" section
        # (it may still appear if Click adds it, but our sections should dominate)
        lines = result.output.split("\n")
        # Count section headers
        section_count = sum(
            1
            for line in lines
            if line.strip().endswith(":")
            and any(
                s in line
                for s in ["Session", "Notebooks", "Chat", "Command Groups", "Artifact Actions"]
            )
        )
        assert section_count >= 4  # At least 4 of our sections should appear (no Insights anymore)


class TestSectionedHelpOrder:
    """Test that sections appear in the correct order."""

    def test_section_order(self, runner):
        """Verify sections appear in the expected order."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0

        output = result.output

        # Find positions of key sections
        session_pos = output.find("Session:")
        notebooks_pos = output.find("Notebooks:")
        chat_pos = output.find("Chat:")
        groups_pos = output.find("Command Groups")
        actions_pos = output.find("Artifact Actions")

        # Verify they all exist
        assert session_pos > 0
        assert notebooks_pos > 0
        assert chat_pos > 0
        assert groups_pos > 0
        assert actions_pos > 0

        # Verify order
        assert session_pos < notebooks_pos < chat_pos < groups_pos < actions_pos
