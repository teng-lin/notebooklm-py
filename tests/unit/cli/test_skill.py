"""Tests for skill CLI commands."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli

from .conftest import get_cli_module

# Get the actual skill module (not the click group that shadows it)
skill_module = get_cli_module("skill")


@pytest.fixture
def runner():
    return CliRunner()


class TestSkillInstall:
    """Tests for skill install command."""

    def test_skill_install_creates_directory_and_file(self, runner, tmp_path):
        """Test that install creates the skill file."""
        skill_dest = tmp_path / "skills" / "notebooklm" / "SKILL.md"
        mock_source_content = "---\nname: notebooklm\n---\n# Test"

        with (
            patch.object(skill_module, "SKILL_DEST", skill_dest),
            patch.object(skill_module, "SKILL_DEST_DIR", skill_dest.parent),
            patch.object(
                skill_module, "get_skill_source_content", return_value=mock_source_content
            ),
        ):
            result = runner.invoke(cli, ["skill", "install"])

            assert result.exit_code == 0
            assert "installed" in result.output.lower()
            assert skill_dest.exists()

    def test_skill_install_source_not_found(self, runner, tmp_path):
        """Test error when source file doesn't exist."""
        skill_dest = tmp_path / "skills" / "notebooklm" / "SKILL.md"

        with (
            patch.object(skill_module, "SKILL_DEST", skill_dest),
            patch.object(skill_module, "SKILL_DEST_DIR", skill_dest.parent),
            patch.object(skill_module, "get_skill_source_content", return_value=None),
        ):
            result = runner.invoke(cli, ["skill", "install"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()


class TestSkillStatus:
    """Tests for skill status command."""

    def test_skill_status_not_installed(self, runner, tmp_path):
        """Test status when skill is not installed."""
        skill_dest = tmp_path / "skills" / "notebooklm" / "SKILL.md"

        with patch.object(skill_module, "SKILL_DEST", skill_dest):
            result = runner.invoke(cli, ["skill", "status"])

        assert result.exit_code == 0
        assert "not installed" in result.output.lower()

    def test_skill_status_installed(self, runner, tmp_path):
        """Test status when skill is installed."""
        skill_dest = tmp_path / "skills" / "notebooklm" / "SKILL.md"
        skill_dest.parent.mkdir(parents=True)
        skill_dest.write_text("<!-- notebooklm-py v0.1.0 -->\n# Test")

        with patch.object(skill_module, "SKILL_DEST", skill_dest):
            result = runner.invoke(cli, ["skill", "status"])

        assert result.exit_code == 0
        assert "installed" in result.output.lower()


class TestSkillUninstall:
    """Tests for skill uninstall command."""

    def test_skill_uninstall_removes_file(self, runner, tmp_path):
        """Test that uninstall removes the skill file."""
        skill_dest = tmp_path / "skills" / "notebooklm" / "SKILL.md"
        skill_dest.parent.mkdir(parents=True)
        skill_dest.write_text("# Test")

        with (
            patch.object(skill_module, "SKILL_DEST", skill_dest),
            patch.object(skill_module, "SKILL_DEST_DIR", skill_dest.parent),
        ):
            result = runner.invoke(cli, ["skill", "uninstall"])

        assert result.exit_code == 0
        assert not skill_dest.exists()

    def test_skill_uninstall_not_installed(self, runner, tmp_path):
        """Test uninstall when skill doesn't exist."""
        skill_dest = tmp_path / "skills" / "notebooklm" / "SKILL.md"

        with patch.object(skill_module, "SKILL_DEST", skill_dest):
            result = runner.invoke(cli, ["skill", "uninstall"])

        assert result.exit_code == 0
        assert "not installed" in result.output.lower()


class TestSkillShow:
    """Tests for skill show command."""

    def test_skill_show_displays_content(self, runner, tmp_path):
        """Test that show displays skill content."""
        skill_dest = tmp_path / "skills" / "notebooklm" / "SKILL.md"
        skill_dest.parent.mkdir(parents=True)
        skill_dest.write_text("# NotebookLM Skill\nTest content")

        with patch.object(skill_module, "SKILL_DEST", skill_dest):
            result = runner.invoke(cli, ["skill", "show"])

        assert result.exit_code == 0
        assert "NotebookLM Skill" in result.output

    def test_skill_show_not_installed(self, runner, tmp_path):
        """Test show when skill doesn't exist."""
        skill_dest = tmp_path / "skills" / "notebooklm" / "SKILL.md"

        with patch.object(skill_module, "SKILL_DEST", skill_dest):
            result = runner.invoke(cli, ["skill", "show"])

        assert result.exit_code == 0
        assert "not installed" in result.output.lower()


class TestSkillVersionExtraction:
    """Tests for version extraction logic."""

    def test_get_skill_version_extracts_version(self, tmp_path):
        """Test version extraction from skill file."""
        from notebooklm.cli.skill import get_skill_version

        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("---\nname: test\n---\n<!-- notebooklm-py v1.2.3 -->\n# Test")

        version = get_skill_version(skill_file)
        assert version == "1.2.3"

    def test_get_skill_version_no_version(self, tmp_path):
        """Test version extraction when no version present."""
        from notebooklm.cli.skill import get_skill_version

        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("# Test\nNo version here")

        version = get_skill_version(skill_file)
        assert version is None

    def test_get_skill_version_file_not_exists(self, tmp_path):
        """Test version extraction when file doesn't exist."""
        from notebooklm.cli.skill import get_skill_version

        skill_file = tmp_path / "nonexistent.md"
        version = get_skill_version(skill_file)
        assert version is None
