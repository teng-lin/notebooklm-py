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
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "CODEX.md").write_text("# Repository Guidelines", encoding="utf-8")

        with (
            patch.object(agent_templates_module, "REPO_ROOT_AGENTS", tmp_path / "AGENTS.md"),
            patch.object(agent_templates_module.resources, "files", return_value=tmp_path),
        ):
            content = agent_templates_module.get_agent_source_content("codex")

        assert content is not None
        assert "Repository Guidelines" in content

    def test_claude_template_reads_package_data(self):
        """Test that claude content reads from packaged skill data."""
        content = agent_templates_module.get_agent_source_content("claude")

        assert content is not None
        assert "NotebookLM Automation" in content

    def test_codex_reads_repo_root_agents_when_present(self, tmp_path):
        """Codex prefers the repo-root AGENTS.md when running from a checkout."""
        agents_file = tmp_path / "AGENTS.md"
        agents_file.write_text("# Repository Guidelines\nlocal", encoding="utf-8")
        with patch.object(agent_templates_module, "REPO_ROOT_AGENTS", agents_file):
            content = agent_templates_module.get_agent_source_content("codex")

        assert content == "# Repository Guidelines\nlocal"

    def test_unknown_target_returns_none(self, tmp_path):
        """An unsupported agent name yields ``None`` (no template file)."""
        # Point the repo-root probes at non-existent paths so the lookup
        # falls through to the ``AGENT_TEMPLATE_FILES`` map, which has no
        # entry for an unknown target.
        with (
            patch.object(agent_templates_module, "REPO_ROOT_AGENTS", tmp_path / "AGENTS.md"),
            patch.object(agent_templates_module, "REPO_ROOT_CLAUDE_SKILL", tmp_path / "SKILL.md"),
        ):
            assert agent_templates_module.get_agent_source_content("gpt-9000") is None

    def test_read_package_data_returns_file_contents(self, tmp_path):
        """``_read_package_data`` reads a bundled template file.

        The packaged ``notebooklm/data/`` directory only exists in built
        wheels, not in a source checkout, so point ``resources.files`` at a
        temp tree that mirrors the bundled layout to exercise the read.
        """
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "BUNDLED.md").write_text("bundled body", encoding="utf-8")

        with patch.object(agent_templates_module.resources, "files", return_value=tmp_path):
            content = agent_templates_module._read_package_data("BUNDLED.md")

        assert content == "bundled body"

    def test_read_package_data_missing_file_returns_none(self, tmp_path):
        """A missing packaged file is swallowed and reported as ``None``."""
        # data/ dir exists but the requested file does not -> FileNotFoundError.
        (tmp_path / "data").mkdir()
        with patch.object(agent_templates_module.resources, "files", return_value=tmp_path):
            assert agent_templates_module._read_package_data("does-not-exist.md") is None
