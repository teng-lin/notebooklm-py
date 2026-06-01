"""Tests for SectionedGroup CLI help formatting."""

import click
import pytest
from click.testing import CliRunner

from notebooklm.cli.grouped import SectionedGroup
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


class TestNoOrphanCommands:
    """Enforce: every top-level command lands in a primary section.

    The "Other" safety-net bin in :class:`SectionedGroup` is reserved for
    commands explicitly tagged ``category="misc"``. Any unbinned-and-untagged
    command is a discoverability regression.
    """

    def _binned_names(self) -> set[str]:
        """All command names that appear in any explicit primary section."""
        binned: set[str] = set()
        for cmd_names in SectionedGroup.command_sections.values():
            binned.update(cmd_names)
        for group_names in SectionedGroup.command_groups.values():
            binned.update(group_names)
        return binned

    def test_no_command_falls_into_other_unless_misc_tagged(self):
        """Walk the assembled CLI; assert every visible top-level command is
        either explicitly binned or explicitly tagged ``category="misc"``."""
        ctx = click.Context(cli)
        binned = self._binned_names()

        orphans: list[str] = []
        for name in cli.list_commands(ctx):
            cmd = cli.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            if name in binned:
                continue
            if getattr(cmd, "category", None) == "misc":
                continue
            orphans.append(name)

        assert orphans == [], (
            f"Top-level command(s) {orphans!r} fell into the 'Other' safety-net "
            "bin. Either add them to a primary section in "
            "src/notebooklm/cli/grouped.py (command_sections or "
            "command_groups), or explicitly tag them with `cmd.category = "
            '"misc"` to opt into the Other bin.'
        )

    def test_help_has_no_other_section(self):
        """Verify the rendered --help output has no 'Other:' header (since no
        top-level command is currently tagged ``category="misc"``)."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # Match a section header only — avoid false positives on substrings
        # like "Other" that may appear inside command help text.
        assert "\nOther:\n" not in result.output, (
            "Top-level help renders an 'Other:' section, which means at least "
            "one command is unbinned. See test_no_command_falls_into_other_"
            "unless_misc_tagged for details.\n\n" + result.output
        )

    def test_misc_tagged_command_lands_in_other(self):
        """Sanity-check the contract: a command tagged ``category="misc"`` is
        the *only* legitimate inhabitant of the 'Other' bin, and it does
        render there when present."""

        @click.group(cls=SectionedGroup)
        def fake_cli():
            pass

        @fake_cli.command("login")
        def _login():
            """Log in."""

        @fake_cli.command("oddball")
        def _oddball():
            """Doesn't fit anywhere."""

        # Tag oddball as misc; the SectionedGroup safety-net should still
        # render it under "Other", and our orphan-test logic should accept it.
        oddball_cmd = fake_cli.commands["oddball"]
        oddball_cmd.category = "misc"  # type: ignore[attr-defined]

        runner = CliRunner()
        result = runner.invoke(fake_cli, ["--help"])
        assert result.exit_code == 0
        assert "Other:" in result.output
        assert "oddball" in result.output

        # And the orphan-detection helper accepts it
        ctx = click.Context(fake_cli)
        binned = self._binned_names()
        orphans = [
            name
            for name in fake_cli.list_commands(ctx)
            if name not in binned
            and getattr(fake_cli.get_command(ctx, name), "category", None) != "misc"
        ]
        assert orphans == []
