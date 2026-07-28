"""Tests for skill CLI commands."""

import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli

# Get the actual skill module (not the click group that shadows it)
skill_module = importlib.import_module("notebooklm.cli.skill_cmd")


@pytest.fixture
def runner():
    return CliRunner()


class TestSkillInstall:
    """Tests for skill install command.

    ``install`` reads the whole packaged skill tree through
    ``get_skill_source_tree`` (``{relative_posix_path: content}``), not the
    single-file ``get_skill_source_content`` (that seam stays reserved for
    ``skill show``) -- so these tests patch the tree seam with a small dict
    fixture instead of a single source string.
    """

    def test_skill_install_creates_all_default_targets(self, runner, tmp_path):
        """Test that install writes both supported user targets by default."""
        home = tmp_path / "home"
        mock_files = {"SKILL.md": "---\nname: notebooklm\n---\n# Test"}

        with (
            patch.object(skill_module, "get_skill_source_tree", return_value=mock_files),
            patch.object(skill_module.Path, "home", return_value=home),
        ):
            result = runner.invoke(cli, ["skill", "install"])

        assert result.exit_code == 0
        assert "installed" in result.output.lower()
        assert (home / ".claude" / "skills" / "notebooklm" / "SKILL.md").exists()
        assert (home / ".agents" / "skills" / "notebooklm" / "SKILL.md").exists()

    def test_skill_install_project_agents_target_only(self, runner, tmp_path):
        """Test project-scope installs into the universal .agents path only."""
        home = tmp_path / "home"
        project = tmp_path / "project"
        mock_files = {"SKILL.md": "---\nname: notebooklm\n---\n# Test"}

        with (
            patch.object(skill_module, "get_skill_source_tree", return_value=mock_files),
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module.Path, "cwd", return_value=project),
        ):
            result = runner.invoke(
                cli, ["skill", "install", "--scope", "project", "--target", "agents"]
            )

        assert result.exit_code == 0
        assert (project / ".agents" / "skills" / "notebooklm" / "SKILL.md").exists()
        assert not (project / ".claude" / "skills" / "notebooklm" / "SKILL.md").exists()

    def test_skill_install_project_scope_all_targets(self, runner, tmp_path):
        """Test project-scope installs both targets under cwd when target=all."""
        project = tmp_path / "project"
        mock_files = {"SKILL.md": "---\nname: notebooklm\n---\n# Test"}

        with (
            patch.object(skill_module, "get_skill_source_tree", return_value=mock_files),
            patch.object(skill_module.Path, "cwd", return_value=project),
        ):
            result = runner.invoke(cli, ["skill", "install", "--scope", "project"])

        assert result.exit_code == 0
        assert (project / ".claude" / "skills" / "notebooklm" / "SKILL.md").exists()
        assert (project / ".agents" / "skills" / "notebooklm" / "SKILL.md").exists()

    def test_skill_install_source_not_found(self, runner, tmp_path):
        """Test error when the packaged skill tree isn't found."""
        with patch.object(skill_module, "get_skill_source_tree", return_value=None):
            result = runner.invoke(cli, ["skill", "install"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_skill_install_partial_failure_reports_both(self, runner, tmp_path):
        """Test that a per-target write failure is reported but other targets still install."""
        home = tmp_path / "home"
        mock_files = {"SKILL.md": "---\nname: notebooklm\n---\n# Test"}

        # Make the claude target's install *directory* a plain file, so it
        # can't be diffed (classify_target -> OVERWRITE) and the write loop's
        # rmtree(ignore_errors=True) leaves it in place, so the first
        # atomic_write_text's mkdir(parents=True) raises FileExistsError.
        claude_dir = home / ".claude" / "skills" / "notebooklm"
        claude_dir.parent.mkdir(parents=True)
        claude_dir.write_text("blocker")

        with (
            patch.object(skill_module, "get_skill_source_tree", return_value=mock_files),
            patch.object(skill_module.Path, "home", return_value=home),
        ):
            result = runner.invoke(cli, ["skill", "install"])

        assert result.exit_code == 1
        assert "failed" in result.output.lower()
        # agents target should still have succeeded
        assert (home / ".agents" / "skills" / "notebooklm" / "SKILL.md").exists()

    def test_skill_install_chmods_scripts_executable(self, runner, tmp_path):
        """Files under scripts/ land 0o755 (executable) after install."""
        home = tmp_path / "home"
        mock_files = {
            "SKILL.md": "---\nname: notebooklm\n---\n# Test",
            "references/setup.md": "setup body",
            "scripts/nlm": "#!/bin/sh\necho nlm\n",
            "scripts/helper.py": "#!/usr/bin/env python3\n",
        }

        with (
            patch.object(skill_module, "get_skill_source_tree", return_value=mock_files),
            patch.object(skill_module.Path, "home", return_value=home),
        ):
            result = runner.invoke(cli, ["skill", "install", "--target", "claude"])

        assert result.exit_code == 0, result.output
        claude_dir = home / ".claude" / "skills" / "notebooklm"
        assert (claude_dir / "references" / "setup.md").read_text(encoding="utf-8") == (
            "setup body"
        )
        for script_name in ("nlm", "helper.py"):
            mode = (claude_dir / "scripts" / script_name).stat().st_mode & 0o777
            assert mode == 0o755, f"{script_name} mode was {oct(mode)}"


class TestSkillInstallProjectHardening:
    """Tests for the project-scope hardening flags (--dry-run / --no-clobber / --force).

    Every test in this class uses ``--scope project`` and patches ``Path.cwd()``
    so the install rooted under ``tmp_path``.
    """

    SOURCE_CONTENT = "---\nname: notebooklm\n---\n# Source body v1"

    def _stamped(self, version: str = "1.0.0") -> str:
        return skill_module.add_version_comment(self.SOURCE_CONTENT, version)

    def _seed(self, project: Path, *, claude: str | None, agents: str | None) -> None:
        """Pre-create one or both target files with the supplied content."""
        if claude is not None:
            path = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(claude, encoding="utf-8")
        if agents is not None:
            path = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(agents, encoding="utf-8")

    def _invoke(self, runner, project: Path, *extra_args: str):
        """Run ``skill install --scope project`` with the patched cwd."""
        with (
            patch.object(
                skill_module,
                "get_skill_source_tree",
                return_value={"SKILL.md": self.SOURCE_CONTENT},
            ),
            patch.object(skill_module, "get_package_version", return_value="1.0.0"),
            patch.object(skill_module.Path, "cwd", return_value=project),
        ):
            return runner.invoke(cli, ["skill", "install", "--scope", "project", *extra_args])

    # --- fresh install (no existing files) -----------------------------------

    def test_fresh_install_creates_both_targets(self, runner, tmp_path):
        """No existing files: install writes both targets with stamped content."""
        project = tmp_path / "project"
        result = self._invoke(runner, project)

        assert result.exit_code == 0, result.output
        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        assert claude.read_text(encoding="utf-8") == self._stamped()
        assert agents.read_text(encoding="utf-8") == self._stamped()
        assert "installed" in result.output.lower()

    # --- unchanged content (no-op) -------------------------------------------

    def test_unchanged_content_is_noop(self, runner, tmp_path):
        """Both targets already contain stamped content: no write, exit 0, 'up to date'."""
        project = tmp_path / "project"
        stamped = self._stamped()
        self._seed(project, claude=stamped, agents=stamped)

        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        claude_mtime = claude.stat().st_mtime_ns
        agents_mtime = agents.stat().st_mtime_ns

        result = self._invoke(runner, project)

        assert result.exit_code == 0, result.output
        # No write happened: mtime is preserved (atomic_write would have replaced inode).
        assert claude.stat().st_mtime_ns == claude_mtime
        assert agents.stat().st_mtime_ns == agents_mtime
        assert "up to date" in result.output.lower()

    # --- differing content, default mode (refuse + exit 1) -------------------

    def test_default_refuses_to_clobber_differing_targets(self, runner, tmp_path):
        """Differing content + no flags: exit 1, list differing files, no writes."""
        project = tmp_path / "project"
        self._seed(project, claude="old claude content", agents="old agents content")

        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"

        result = self._invoke(runner, project)

        assert result.exit_code == 1, result.output
        # Files are not modified.
        assert claude.read_text(encoding="utf-8") == "old claude content"
        assert agents.read_text(encoding="utf-8") == "old agents content"
        # Error mentions both targets and surfaces the canonical flag hint.
        assert "claude code" in result.output.lower()
        assert "agent skills" in result.output.lower()
        assert "--force" in result.output
        assert "--no-clobber" in result.output

    # --- --dry-run -----------------------------------------------------------

    def test_dry_run_on_fresh_project_writes_nothing(self, runner, tmp_path):
        """--dry-run on a fresh project prints intended creates without writing."""
        project = tmp_path / "project"
        result = self._invoke(runner, project, "--dry-run")

        assert result.exit_code == 0, result.output
        assert "dry run" in result.output.lower()
        assert "would create" in result.output.lower()
        # No filesystem changes -- the skill files do not exist.
        assert not (project / ".claude" / "skills" / "notebooklm" / "SKILL.md").exists()
        assert not (project / ".agents" / "skills" / "notebooklm" / "SKILL.md").exists()

    def test_dry_run_with_differing_files_writes_nothing(self, runner, tmp_path):
        """--dry-run with differing files: announces refuse, exit 0, no writes."""
        project = tmp_path / "project"
        self._seed(project, claude="old claude content", agents=self._stamped())

        result = self._invoke(runner, project, "--dry-run")

        assert result.exit_code == 0, result.output
        output = result.output.lower()
        assert "dry run" in output
        # The differing target is flagged as would-refuse (or similar wording).
        assert "would refuse" in output
        # The matching target appears as up-to-date.
        assert "up to date" in output
        # Differing file is unchanged.
        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        assert claude.read_text(encoding="utf-8") == "old claude content"

    def test_dry_run_with_force_previews_overwrite(self, runner, tmp_path):
        """--dry-run --force: previews overwrites without writing."""
        project = tmp_path / "project"
        self._seed(project, claude="old", agents=None)

        result = self._invoke(runner, project, "--dry-run", "--force")

        assert result.exit_code == 0, result.output
        output = result.output.lower()
        assert "would overwrite" in output
        assert "would create" in output
        # Nothing actually written.
        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        assert claude.read_text(encoding="utf-8") == "old"
        assert not agents.exists()

    def test_dry_run_with_no_clobber_previews_skip(self, runner, tmp_path):
        """--dry-run --no-clobber: previews which differing files would be skipped."""
        project = tmp_path / "project"
        self._seed(project, claude="old", agents=None)

        result = self._invoke(runner, project, "--dry-run", "--no-clobber")

        assert result.exit_code == 0, result.output
        output = result.output.lower()
        assert "would skip" in output
        assert "would create" in output
        # Nothing actually written.
        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        assert claude.read_text(encoding="utf-8") == "old"
        assert not agents.exists()

    def test_dry_run_all_up_to_date_reports_each_target(self, runner, tmp_path):
        """--dry-run with both targets already stamped reports the no-op targets."""
        project = tmp_path / "project"
        stamped = self._stamped()
        self._seed(project, claude=stamped, agents=stamped)

        result = self._invoke(runner, project, "--dry-run")

        assert result.exit_code == 0, result.output
        output = result.output.lower()
        assert "dry run" in output
        assert output.count("up to date") >= 2
        assert "claude code" in output
        assert "agent skills" in output

    # --- --no-clobber --------------------------------------------------------

    def test_no_clobber_skips_differing_creates_missing(self, runner, tmp_path):
        """--no-clobber: skip differing files, still create missing targets."""
        project = tmp_path / "project"
        self._seed(project, claude="old claude content", agents=None)

        result = self._invoke(runner, project, "--no-clobber")

        assert result.exit_code == 0, result.output
        # Existing differing file is preserved.
        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        assert claude.read_text(encoding="utf-8") == "old claude content"
        # Missing target was created with stamped content.
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        assert agents.read_text(encoding="utf-8") == self._stamped()
        # Informative summary surfaces the skip count.
        assert "skipped" in result.output.lower()
        assert "--no-clobber" in result.output

    def test_no_clobber_all_differing_writes_nothing(self, runner, tmp_path):
        """--no-clobber with both targets differing: no writes, exit 0, summary printed."""
        project = tmp_path / "project"
        self._seed(project, claude="old claude", agents="old agents")

        result = self._invoke(runner, project, "--no-clobber")

        assert result.exit_code == 0, result.output
        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        assert claude.read_text(encoding="utf-8") == "old claude"
        assert agents.read_text(encoding="utf-8") == "old agents"
        assert "skipped" in result.output.lower()
        assert "2" in result.output  # count surfaced

    # --- --force -------------------------------------------------------------

    def test_force_overwrites_differing_targets(self, runner, tmp_path):
        """--force: overwrites differing content unconditionally."""
        project = tmp_path / "project"
        self._seed(project, claude="old claude", agents="old agents")

        result = self._invoke(runner, project, "--force")

        assert result.exit_code == 0, result.output
        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        assert claude.read_text(encoding="utf-8") == self._stamped()
        assert agents.read_text(encoding="utf-8") == self._stamped()
        assert "installed" in result.output.lower()

    # --- mixed (per-target diff detection) -----------------------------------

    def test_mixed_one_identical_one_differing_default_refuses(self, runner, tmp_path):
        """Mixed targets: identical claude + differing agents -> default refuses, lists agents only."""
        project = tmp_path / "project"
        self._seed(project, claude=self._stamped(), agents="old agents")

        result = self._invoke(runner, project)

        assert result.exit_code == 1, result.output
        # The error lists the differing target.
        assert "agent skills" in result.output.lower()
        # The identical target should NOT appear in the differing list.
        differing_section = (
            result.output.split("Refusing")[1] if "Refusing" in result.output else ""
        )
        assert "Claude Code:" not in differing_section
        # No mutation.
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        assert agents.read_text(encoding="utf-8") == "old agents"

    def test_mixed_no_clobber_skips_differing_only(self, runner, tmp_path):
        """Mixed targets: --no-clobber preserves the differing one, leaves identical untouched."""
        project = tmp_path / "project"
        self._seed(project, claude=self._stamped(), agents="old agents")
        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        claude_mtime = claude.stat().st_mtime_ns

        result = self._invoke(runner, project, "--no-clobber")

        assert result.exit_code == 0, result.output
        # Identical target: byte-equal, mtime preserved (no rewrite).
        assert claude.read_text(encoding="utf-8") == self._stamped()
        assert claude.stat().st_mtime_ns == claude_mtime
        # Differing target preserved as-is.
        assert agents.read_text(encoding="utf-8") == "old agents"
        assert "skipped" in result.output.lower()
        assert "up to date" in result.output.lower()

    def test_mixed_force_overwrites_differing_only(self, runner, tmp_path):
        """Mixed targets: --force overwrites differing, identical target is a no-op."""
        project = tmp_path / "project"
        self._seed(project, claude=self._stamped(), agents="old agents")
        claude = project / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        agents = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        claude_mtime = claude.stat().st_mtime_ns

        result = self._invoke(runner, project, "--force")

        assert result.exit_code == 0, result.output
        # Identical target untouched (mtime preserved).
        assert claude.stat().st_mtime_ns == claude_mtime
        # Differing target overwritten.
        assert agents.read_text(encoding="utf-8") == self._stamped()

    # --- stale extra file (directory-tree classification) --------------------

    def test_stale_extra_file_forces_overwrite_and_force_cleans_it_up(self, runner, tmp_path):
        """A leftover file not in the packaged tree (e.g. from an old skill
        version, or an old single-file install occupying the same directory)
        makes the target 'differing' even though SKILL.md itself matches --
        refused by default, cleaned up by --force."""
        project = tmp_path / "project"
        claude_dir = project / ".claude" / "skills" / "notebooklm"
        claude_dir.mkdir(parents=True)
        (claude_dir / "SKILL.md").write_text(self._stamped(), encoding="utf-8")
        stale = claude_dir / "old-reference.md"
        stale.write_text("leftover from a previous skill version", encoding="utf-8")

        # Default: refuses because of the stale extra file.
        result = self._invoke(runner, project)
        assert result.exit_code == 1, result.output
        assert stale.exists()

        # --force: overwrites the whole tree, so the stale leftover is gone.
        result = self._invoke(runner, project, "--force")
        assert result.exit_code == 0, result.output
        assert not stale.exists()
        assert (claude_dir / "SKILL.md").read_text(encoding="utf-8") == self._stamped()

    # --- flag validation -----------------------------------------------------

    def test_user_scope_rejects_dry_run(self, runner, tmp_path):
        """--scope user + --dry-run is rejected (hardening is project-only)."""
        home = tmp_path / "home"
        with (
            patch.object(
                skill_module, "get_skill_source_content", return_value=self.SOURCE_CONTENT
            ),
            patch.object(skill_module.Path, "home", return_value=home),
        ):
            result = runner.invoke(cli, ["skill", "install", "--scope", "user", "--dry-run"])

        assert result.exit_code == 1
        assert "--scope project" in result.output

    def test_user_scope_rejects_force(self, runner, tmp_path):
        """--scope user + --force is rejected."""
        home = tmp_path / "home"
        with (
            patch.object(
                skill_module, "get_skill_source_content", return_value=self.SOURCE_CONTENT
            ),
            patch.object(skill_module.Path, "home", return_value=home),
        ):
            result = runner.invoke(cli, ["skill", "install", "--scope", "user", "--force"])

        assert result.exit_code == 1
        assert "--scope project" in result.output

    def test_user_scope_rejects_no_clobber(self, runner, tmp_path):
        """--scope user + --no-clobber is rejected."""
        home = tmp_path / "home"
        with (
            patch.object(
                skill_module, "get_skill_source_content", return_value=self.SOURCE_CONTENT
            ),
            patch.object(skill_module.Path, "home", return_value=home),
        ):
            result = runner.invoke(cli, ["skill", "install", "--scope", "user", "--no-clobber"])

        assert result.exit_code == 1
        assert "--scope project" in result.output

    def test_force_and_no_clobber_are_mutually_exclusive(self, runner, tmp_path):
        """--force + --no-clobber together is rejected."""
        project = tmp_path / "project"
        result = self._invoke(runner, project, "--force", "--no-clobber")

        assert result.exit_code == 1
        assert "mutually exclusive" in result.output.lower()

    def test_atomic_write_no_partial_on_fresh_install(self, runner, tmp_path):
        """Successful install leaves no temp files in the target directory."""
        project = tmp_path / "project"
        result = self._invoke(runner, project)

        assert result.exit_code == 0, result.output
        claude_dir = project / ".claude" / "skills" / "notebooklm"
        # No stray ``.SKILL.md.*.tmp`` siblings should remain.
        leftovers = [p.name for p in claude_dir.iterdir() if p.name != "SKILL.md"]
        assert leftovers == [], f"unexpected leftover files: {leftovers}"

    def test_atomic_write_text_uses_shared_replace_helper(self, tmp_path, monkeypatch):
        """Text skill writes share the Windows transient-retry replace helper."""
        calls: list[tuple[Path, Path]] = []

        def fake_replace(temp_path: Path, path: Path) -> None:
            calls.append((temp_path, path))
            temp_path.replace(path)

        monkeypatch.setattr(skill_module, "replace_file_atomically", fake_replace)
        target = tmp_path / "skills" / "SKILL.md"

        skill_module.atomic_write_text(target, "content")

        assert target.read_text(encoding="utf-8") == "content"
        assert len(calls) == 1
        assert calls[0][1] == target


class TestSkillStatus:
    """Tests for skill status command."""

    def test_skill_status_not_installed(self, runner, tmp_path):
        """Test status when skill is not installed."""
        home = tmp_path / "home"

        with patch.object(skill_module.Path, "home", return_value=home):
            result = runner.invoke(cli, ["skill", "status"])

        assert result.exit_code == 0
        assert "not installed" in result.output.lower()
        assert "claude code" in result.output.lower()
        assert "agent skills" in result.output.lower()

    def test_skill_status_installed_version_mismatch(self, runner, tmp_path):
        """Test status when skill is installed with a different version than the CLI."""
        home = tmp_path / "home"
        skill_dest = home / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        skill_dest.parent.mkdir(parents=True)
        skill_dest.write_text("<!-- notebooklm-py v0.1.0 -->\n# Test")

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module, "get_package_version", return_value="9.9.9"),
        ):
            result = runner.invoke(cli, ["skill", "status"])

        assert result.exit_code == 0
        assert "installed" in result.output.lower()
        assert "version mismatch" in result.output.lower()

    def test_skill_status_both_targets_same_version(self, runner, tmp_path):
        """Test status when both targets are installed with the current version."""
        home = tmp_path / "home"
        version = "1.2.3"
        for subdir in [".claude/skills/notebooklm", ".agents/skills/notebooklm"]:
            dest = home / subdir / "SKILL.md"
            dest.parent.mkdir(parents=True)
            dest.write_text(f"<!-- notebooklm-py v{version} -->\n# Test")

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module, "get_package_version", return_value=version),
        ):
            result = runner.invoke(cli, ["skill", "status"])

        assert result.exit_code == 0
        assert "version mismatch" not in result.output.lower()
        assert result.output.count("Installed") >= 2

    def test_skill_status_json(self, runner, tmp_path):
        """``skill status --json`` emits a single structured document."""
        home = tmp_path / "home"
        version = "1.2.3"
        dest = home / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text(f"<!-- notebooklm-py v{version} -->\n# Test")

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module, "get_package_version", return_value=version),
        ):
            result = runner.invoke(cli, ["skill", "status", "--target", "agents", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["cli_version"] == version
        agents = next(t for t in payload["targets"] if t["target"] == "agents")
        assert agents["installed"] is True
        assert agents["skill_version"] == version
        assert agents["version_mismatch"] is False
        assert agents["files"] == 1  # only SKILL.md was written by this fixture

    def test_skill_status_json_files_counts_the_whole_tree(self, runner, tmp_path):
        """``files`` counts every installed file, not just the entry file."""
        home = tmp_path / "home"
        version = "1.2.3"
        skill_dir = home / ".agents" / "skills" / "notebooklm"
        (skill_dir / "references").mkdir(parents=True)
        (skill_dir / "scripts").mkdir()
        (skill_dir / "SKILL.md").write_text(f"<!-- notebooklm-py v{version} -->\n# Test")
        (skill_dir / "references" / "setup.md").write_text("setup body")
        (skill_dir / "scripts" / "nlm").write_text("#!/bin/sh\n")

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module, "get_package_version", return_value=version),
        ):
            result = runner.invoke(cli, ["skill", "status", "--target", "agents", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        agents = next(t for t in payload["targets"] if t["target"] == "agents")
        assert agents["files"] == 3

    def test_skill_status_json_files_zero_when_not_installed(self, runner, tmp_path):
        """An uninstalled target reports ``files: 0``."""
        home = tmp_path / "home"

        with patch.object(skill_module.Path, "home", return_value=home):
            result = runner.invoke(cli, ["skill", "status", "--target", "claude", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        claude = next(t for t in payload["targets"] if t["target"] == "claude")
        assert claude["installed"] is False
        assert claude["files"] == 0


class TestSkillUninstall:
    """Tests for skill uninstall command."""

    def test_skill_uninstall_removes_selected_target_only(self, runner, tmp_path):
        """Test that uninstall removes only the requested target."""
        home = tmp_path / "home"
        skill_dest = home / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        other_dest = home / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        skill_dest.parent.mkdir(parents=True)
        skill_dest.write_text("# Test")
        other_dest.parent.mkdir(parents=True)
        other_dest.write_text("# Test")

        with patch.object(skill_module.Path, "home", return_value=home):
            result = runner.invoke(cli, ["skill", "uninstall", "--target", "agents"])

        assert result.exit_code == 0
        assert not skill_dest.exists()
        assert other_dest.exists()

    def test_skill_uninstall_removes_entire_directory_tree(self, runner, tmp_path):
        """Uninstall removes the whole installed tree (references/, scripts/), not
        just the entry file -- it's ``shutil.rmtree``, not a single ``unlink``."""
        home = tmp_path / "home"
        skill_dir = home / ".claude" / "skills" / "notebooklm"
        (skill_dir / "references").mkdir(parents=True)
        (skill_dir / "scripts").mkdir()
        (skill_dir / "SKILL.md").write_text("# Test", encoding="utf-8")
        (skill_dir / "references" / "setup.md").write_text("setup", encoding="utf-8")
        (skill_dir / "scripts" / "nlm").write_text("#!/bin/sh\n", encoding="utf-8")

        with patch.object(skill_module.Path, "home", return_value=home):
            result = runner.invoke(cli, ["skill", "uninstall", "--target", "claude"])

        assert result.exit_code == 0, result.output
        assert not skill_dir.exists()

    def test_skill_uninstall_all_targets_removes_both(self, runner, tmp_path):
        """Test that uninstall --target all removes both targets and cleans empty dirs."""
        home = tmp_path / "home"
        for subdir in [".claude/skills/notebooklm", ".agents/skills/notebooklm"]:
            dest = home / subdir / "SKILL.md"
            dest.parent.mkdir(parents=True)
            dest.write_text("# Test")

        with patch.object(skill_module.Path, "home", return_value=home):
            result = runner.invoke(cli, ["skill", "uninstall"])

        assert result.exit_code == 0
        assert not (home / ".claude" / "skills" / "notebooklm" / "SKILL.md").exists()
        assert not (home / ".agents" / "skills" / "notebooklm" / "SKILL.md").exists()
        # Empty intermediate directories should be cleaned up
        assert not (home / ".claude" / "skills" / "notebooklm").exists()
        assert not (home / ".agents" / "skills" / "notebooklm").exists()

    def test_skill_uninstall_not_installed(self, runner, tmp_path):
        """Test uninstall when skill doesn't exist."""
        home = tmp_path / "home"

        with patch.object(skill_module.Path, "home", return_value=home):
            result = runner.invoke(cli, ["skill", "uninstall"])

        assert result.exit_code == 0
        assert "not installed" in result.output.lower()


class TestSkillShow:
    """Tests for skill show command."""

    def test_skill_show_displays_source_content(self, runner):
        """Test that show defaults to the packaged skill source."""
        with patch.object(
            skill_module,
            "get_skill_source_content",
            return_value="# NotebookLM Skill\nTest content",
        ):
            result = runner.invoke(cli, ["skill", "show"])

        assert result.exit_code == 0
        assert "NotebookLM Skill" in result.output

    def test_skill_show_source_not_found(self, runner):
        """Test that show exits with code 1 when package data is missing."""
        with patch.object(skill_module, "get_skill_source_content", return_value=None):
            result = runner.invoke(cli, ["skill", "show"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_skill_show_installed_target(self, runner, tmp_path):
        """Test that show can read an installed target."""
        home = tmp_path / "home"
        skill_dest = home / ".claude" / "skills" / "notebooklm" / "SKILL.md"
        skill_dest.parent.mkdir(parents=True)
        skill_dest.write_text("# NotebookLM Skill\nInstalled content")

        with patch.object(skill_module.Path, "home", return_value=home):
            result = runner.invoke(cli, ["skill", "show", "--target", "claude"])

        assert result.exit_code == 0
        assert "Installed content" in result.output

    def test_skill_show_target_not_installed(self, runner, tmp_path):
        """Test show when an installed target doesn't exist."""
        home = tmp_path / "home"

        with patch.object(skill_module.Path, "home", return_value=home):
            result = runner.invoke(cli, ["skill", "show", "--target", "claude"])

        assert result.exit_code == 0
        assert "not installed" in result.output.lower()


class TestSkillSourceFallback:
    """Tests for resolving the canonical repository skill."""

    def test_get_skill_source_content_reads_claude_agent_template(self):
        """Test that skill content is sourced through the shared agent template loader."""
        with patch.object(
            skill_module, "get_agent_source_content", return_value="# Canonical Skill"
        ):
            assert skill_module.get_skill_source_content() == "# Canonical Skill"

    def test_get_skill_source_content_returns_none_when_template_missing(self):
        """Test that None is returned when bundled claude instructions are missing."""
        with patch.object(skill_module, "get_agent_source_content", return_value=None):
            assert skill_module.get_skill_source_content() is None


# NOTE: ``TestSkillVersionExtraction`` / ``TestAddVersionComment`` /
# ``TestRemoveEmptyParents`` (and ``TestSkillInstallReporting``) tested
# functions that now live in the transport-neutral ``_app.skill`` core. They
# were MOVED down to ``tests/unit/app/test_app_skill.py`` (direct calls, no
# Click). ``TestSkillSourceFallback`` stays here because ``get_skill_source_content``
# is CLI-owned (the packaged-source loader is not part of ``_app.skill``).


class TestSkillPackage:
    """Tests for skill package (Claude-uploadable archive for chat/Cowork).

    ``package`` reads the whole packaged skill tree through
    ``get_skill_source_tree`` (``{relative_posix_path: content}``), same as
    ``install`` -- so these tests patch the tree seam with a small dict
    fixture instead of a single source string (mirrors ``TestSkillInstall``).
    """

    SOURCE_TREE = {
        "SKILL.md": "---\nname: notebooklm\ndescription: test skill\n---\n# Source body v1",
        "references/setup.md": "# Setup",
        "scripts/nlm": "#!/usr/bin/env bash\necho hi\n",
    }

    def _stamped(
        self, source: dict[str, str] | None = None, version: str = "1.0.0"
    ) -> dict[str, str]:
        return skill_module.stamp_skill_files(source or self.SOURCE_TREE, version)

    def _invoke(self, runner, *extra_args: str, source: dict[str, str] | None = None):
        with (
            patch.object(
                skill_module,
                "get_skill_source_tree",
                return_value=source or self.SOURCE_TREE,
            ),
            patch.object(skill_module, "get_package_version", return_value="1.0.0"),
        ):
            return runner.invoke(cli, ["skill", "package", *extra_args])

    def _read_archive(self, archive_path: Path) -> dict[str, str]:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(archive_path.read_bytes())) as archive:
            prefix = f"{skill_module.SKILL_ARCHIVE_DIRNAME}/"
            return {
                name.removeprefix(prefix): archive.read(name).decode("utf-8")
                for name in archive.namelist()
            }

    def _read_entry(self, archive_path: Path) -> str:
        return self._read_archive(archive_path)[skill_module.SKILL_ENTRY]

    def test_package_default_output_in_cwd(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = self._invoke(runner)

            assert result.exit_code == 0, result.output
            archive = Path(skill_module.DEFAULT_ARCHIVE_FILENAME)
            assert archive.exists()
            assert self._read_archive(archive) == self._stamped()
            assert "Packaged" in result.output
            assert "Capabilities" in result.output

    def test_package_output_file_path(self, runner, tmp_path):
        target = tmp_path / "custom-name.zip"
        result = self._invoke(runner, "--output", str(target))

        assert result.exit_code == 0, result.output
        assert self._read_archive(target) == self._stamped()

    def test_package_output_directory_uses_default_filename(self, runner, tmp_path):
        result = self._invoke(runner, "--output", str(tmp_path))

        assert result.exit_code == 0, result.output
        assert (tmp_path / skill_module.DEFAULT_ARCHIVE_FILENAME).exists()

    def test_package_output_directory_with_existing_archive_refuses(self, runner, tmp_path):
        (tmp_path / skill_module.DEFAULT_ARCHIVE_FILENAME).write_bytes(b"old")
        result = self._invoke(runner, "--output", str(tmp_path))

        assert result.exit_code == 1
        assert (tmp_path / skill_module.DEFAULT_ARCHIVE_FILENAME).read_bytes() == b"old"

    def test_package_refuses_overwrite_without_force(self, runner, tmp_path):
        target = tmp_path / "skill.zip"
        target.write_bytes(b"old")
        result = self._invoke(runner, "--output", str(target))

        assert result.exit_code == 1
        assert target.read_bytes() == b"old"

    def test_package_force_overwrites(self, runner, tmp_path):
        target = tmp_path / "skill.zip"
        target.write_bytes(b"old")
        result = self._invoke(runner, "--output", str(target), "--force")

        assert result.exit_code == 0, result.output
        assert self._read_archive(target) == self._stamped()

    def test_package_missing_source_errors(self, runner, tmp_path):
        with (
            patch.object(skill_module, "get_skill_source_tree", return_value=None),
            patch.object(skill_module, "get_package_version", return_value="1.0.0"),
        ):
            result = runner.invoke(
                cli, ["skill", "package", "--output", str(tmp_path / "skill.zip")]
            )

        assert result.exit_code == 1
        assert not (tmp_path / "skill.zip").exists()

    def test_package_json_success_shape(self, runner, tmp_path):
        target = tmp_path / "skill.zip"
        result = self._invoke(runner, "--output", str(target), "--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["path"] == str(target)
        assert payload["version"] == "1.0.0"
        assert payload["entries"] == sorted(
            f"{skill_module.SKILL_ARCHIVE_DIRNAME}/{rel_path}" for rel_path in self.SOURCE_TREE
        )
        assert payload["size_bytes"] == len(target.read_bytes())

    def test_package_json_failure_output_exists_envelope(self, runner, tmp_path):
        target = tmp_path / "skill.zip"
        target.write_bytes(b"old")
        result = self._invoke(runner, "--output", str(target), "--json")

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"] is True
        assert payload["code"] == "OUTPUT_EXISTS"
        assert "message" in payload

    def test_package_json_failure_missing_source_envelope(self, runner, tmp_path):
        with (
            patch.object(skill_module, "get_skill_source_tree", return_value=None),
            patch.object(skill_module, "get_package_version", return_value="1.0.0"),
        ):
            result = runner.invoke(
                cli, ["skill", "package", "--output", str(tmp_path / "skill.zip"), "--json"]
            )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"] is True
        assert payload["code"] == "SKILL_SOURCE_MISSING"

    def test_package_is_deterministic_across_paths(self, runner, tmp_path):
        first = tmp_path / "one.zip"
        second = tmp_path / "two.zip"
        assert self._invoke(runner, "--output", str(first)).exit_code == 0
        assert self._invoke(runner, "--output", str(second)).exit_code == 0

        assert first.read_bytes() == second.read_bytes()

    def test_package_long_description_warns_on_stderr_in_json_mode(self, runner, tmp_path):
        long_tree = {
            **self.SOURCE_TREE,
            "SKILL.md": f"---\nname: notebooklm\ndescription: {'x' * 1100}\n---\n# Body",
        }
        target = tmp_path / "skill.zip"
        result = self._invoke(runner, "--output", str(target), "--json", source=long_tree)

        assert result.exit_code == 0, result.output
        json.loads(result.stdout)  # stdout stays pure JSON
        assert "1100 characters" in result.stderr

    def test_package_short_description_does_not_warn(self, runner, tmp_path):
        target = tmp_path / "skill.zip"
        result = self._invoke(runner, "--output", str(target), "--json")

        assert result.exit_code == 0, result.output
        assert result.stderr == ""

    def test_atomic_write_bytes_uses_shared_replace_helper(self, tmp_path, monkeypatch):
        """Binary skill writes share the Windows transient-retry replace helper."""
        calls: list[tuple[Path, Path]] = []

        def fake_replace(temp_path: Path, path: Path) -> None:
            calls.append((temp_path, path))
            temp_path.replace(path)

        monkeypatch.setattr(skill_module, "replace_file_atomically", fake_replace)
        target = tmp_path / "skills" / "archive.zip"

        skill_module.atomic_write_bytes(target, b"content")

        assert target.read_bytes() == b"content"
        assert len(calls) == 1
        assert calls[0][1] == target

    def test_package_write_failure_emits_envelope(self, runner, tmp_path, monkeypatch):
        """An OSError from the archive write surfaces as WRITE_FAILED, not a traceback."""

        def boom(path: Path, data: bytes) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(skill_module, "atomic_write_bytes", boom)
        result = self._invoke(runner, "--output", str(tmp_path / "skill.zip"), "--json")

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["error"] is True
        assert payload["code"] == "WRITE_FAILED"
        assert result.exception is None or isinstance(result.exception, SystemExit)

    def test_package_trailing_slash_directory_intent(self, runner, tmp_path):
        """A nonexistent --output ending in a separator creates the directory and
        writes the default filename inside it (Path would silently drop the slash)."""
        result = self._invoke(runner, "--output", str(tmp_path / "newdir") + "/")

        assert result.exit_code == 0, result.output
        assert (tmp_path / "newdir" / skill_module.DEFAULT_ARCHIVE_FILENAME).exists()
