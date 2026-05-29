"""Tests for agent CLI commands."""

import importlib
import inspect
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli

agent_module = importlib.import_module("notebooklm.cli.agent_cmd")
agent_templates_module = importlib.import_module("notebooklm.cli.agent_templates")


@pytest.fixture
def runner():
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        return CliRunner(mix_stderr=False)
    return CliRunner()


class TestAgentShow:
    """Tests for agent show command."""

    def test_agent_show_codex_displays_content(self, runner):
        """Test that agent show codex displays the bundled instructions."""
        with patch.object(
            agent_module, "get_agent_source_content", return_value="# Repository Guidelines"
        ):
            result = runner.invoke(cli, ["agent", "show", "codex"])

        assert result.exit_code == 0
        assert result.stderr == ""
        assert "Repository Guidelines" in result.output

    def test_agent_show_claude_displays_content(self, runner):
        """Test that agent show claude displays the bundled instructions."""
        with patch.object(agent_module, "get_agent_source_content", return_value="# Claude Skill"):
            result = runner.invoke(cli, ["agent", "show", "claude"])

        assert result.exit_code == 0
        assert result.stderr == ""
        assert "Claude Skill" in result.output

    def test_agent_show_missing_content_returns_error(self, runner):
        """Test missing bundled instructions report on stderr only."""
        with patch.object(agent_module, "get_agent_source_content", return_value=None):
            result = runner.invoke(cli, ["agent", "show", "codex"])

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "not found" in result.stderr.lower()


class TestAgentTemplates:
    """Tests for bundled agent template loading."""

    def test_codex_template_falls_back_to_package_data(self, tmp_path):
        """Test that codex content falls back to packaged data outside repo root."""
        with (
            patch.object(agent_templates_module, "REPO_ROOT_AGENTS", tmp_path / "AGENTS.md"),
            patch.object(
                agent_templates_module,
                "_read_package_data",
                return_value="# Repository Guidelines",
            ),
        ):
            content = agent_templates_module.get_agent_source_content("codex")

        assert content is not None
        assert "Repository Guidelines" in content

    def test_claude_template_reads_package_data(self):
        """Test that claude content reads from packaged skill data."""
        content = agent_templates_module.get_agent_source_content("claude")

        assert content is not None
        assert "NotebookLM Automation" in content
