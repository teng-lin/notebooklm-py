"""Tests for skill CLI commands."""

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli

# Get the actual skill module (not the click group that shadows it)
skill_module = importlib.import_module("notebooklm.cli.skill_cmd")


@pytest.fixture
def runner():
    return CliRunner()


class TestSkillInstall:
    """Tests for skill install command."""

    def test_skill_install_creates_all_default_targets(self, runner, tmp_path):
        """Test that install writes both supported user targets by default."""
        home = tmp_path / "home"
        mock_source_content = "---\nname: notebooklm\n---\n# Test"

        with (
            patch.object(
                skill_module, "get_skill_source_content", return_value=mock_source_content
            ),
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
        mock_source_content = "---\nname: notebooklm\n---\n# Test"

        with (
            patch.object(
                skill_module, "get_skill_source_content", return_value=mock_source_content
            ),
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
        mock_source_content = "---\nname: notebooklm\n---\n# Test"

        with (
            patch.object(
                skill_module, "get_skill_source_content", return_value=mock_source_content
            ),
            patch.object(skill_module.Path, "cwd", return_value=project),
        ):
            result = runner.invoke(cli, ["skill", "install", "--scope", "project"])

        assert result.exit_code == 0
        assert (project / ".claude" / "skills" / "notebooklm" / "SKILL.md").exists()
        assert (project / ".agents" / "skills" / "notebooklm" / "SKILL.md").exists()

    def test_skill_install_source_not_found(self, runner, tmp_path):
        """Test error when source file doesn't exist."""
        with patch.object(skill_module, "get_skill_source_content", return_value=None):
            result = runner.invoke(cli, ["skill", "install"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_skill_install_partial_failure_reports_both(self, runner, tmp_path):
        """Test that a per-target write failure is reported but other targets still install."""
        home = tmp_path / "home"
        mock_source_content = "---\nname: notebooklm\n---\n# Test"

        # Make the claude target path a file so mkdir(parents=True) raises NotADirectoryError
        claude_dir = home / ".claude" / "skills" / "notebooklm"
        claude_dir.parent.mkdir(parents=True)
        claude_dir.write_text("blocker")

        with (
            patch.object(
                skill_module, "get_skill_source_content", return_value=mock_source_content
            ),
            patch.object(skill_module.Path, "home", return_value=home),
        ):
            result = runner.invoke(cli, ["skill", "install"])

        assert result.exit_code == 1
        assert "failed" in result.output.lower()
        # agents target should still have succeeded
        assert (home / ".agents" / "skills" / "notebooklm" / "SKILL.md").exists()


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
                skill_module, "get_skill_source_content", return_value=self.SOURCE_CONTENT
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

    def test_atomic_write_text_removes_its_temp_file_when_the_replace_fails(self, tmp_path):
        """A failed replace must not strand a ``.SKILL.md.*.tmp`` sibling.

        Driven by a real collision -- the destination is an existing directory,
        which ``os.replace`` refuses on every platform -- so the cleanup path
        runs against the real replace helper rather than a stub.
        """
        blocked = tmp_path / "SKILL.md"
        blocked.mkdir()

        with pytest.raises(OSError):
            skill_module.atomic_write_text(blocked, "content")

        # The temp file is a sibling of the destination, so it would show up here.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["SKILL.md"]
        assert blocked.is_dir()

    def test_atomic_write_text_reraises_the_write_error_when_cleanup_also_fails(
        self, tmp_path, monkeypatch
    ):
        """Temp-file cleanup is best effort and must never mask the real failure.

        The replace helper swaps the temp file for a directory before failing,
        so the follow-up ``unlink`` raises too; the caller must still see the
        original write error, not the cleanup one.
        """

        def fake_replace(temp_path: Path, path: Path) -> None:
            temp_path.unlink()
            temp_path.mkdir()  # unlink() on a directory fails on every platform
            raise OSError("replace exploded")

        monkeypatch.setattr(skill_module, "replace_file_atomically", fake_replace)

        with pytest.raises(OSError, match="replace exploded"):
            skill_module.atomic_write_text(tmp_path / "SKILL.md", "content")


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

    def test_skill_status_not_installed_json_has_unknown_content_integrity(self, runner, tmp_path):
        """Absent targets expose null content integrity in JSON."""
        home = tmp_path / "home"

        with patch.object(skill_module.Path, "home", return_value=home):
            result = runner.invoke(cli, ["skill", "status", "--target", "agents", "--json"])

        assert result.exit_code == 0, result.output
        agents = json.loads(result.output)["targets"][0]
        assert agents["installed"] is False
        assert agents["content_mismatch"] is None

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
        assert "content drift" not in result.output.lower()

    def test_skill_status_both_targets_same_version(self, runner, tmp_path):
        """Test status when both targets are installed with the current version."""
        home = tmp_path / "home"
        version = "1.2.3"
        source = "---\nname: notebooklm\n---\n# Test"
        canonical = skill_module.add_version_comment(source, version)
        for subdir in [".claude/skills/notebooklm", ".agents/skills/notebooklm"]:
            dest = home / subdir / "SKILL.md"
            dest.parent.mkdir(parents=True)
            dest.write_text(canonical, encoding="utf-8")

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module, "get_package_version", return_value=version),
            patch.object(skill_module, "get_skill_source_content", return_value=source),
        ):
            result = runner.invoke(cli, ["skill", "status"])

        assert result.exit_code == 0
        assert "version mismatch" not in result.output.lower()
        assert "content drift" not in result.output.lower()
        assert result.output.count("Installed") >= 2

    def test_skill_status_json(self, runner, tmp_path):
        """``skill status --json`` emits a single structured document."""
        home = tmp_path / "home"
        version = "1.2.3"
        source = "---\nname: notebooklm\n---\n# Test"
        dest = home / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text(skill_module.add_version_comment(source, version), encoding="utf-8")

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module, "get_package_version", return_value=version),
            patch.object(skill_module, "get_skill_source_content", return_value=source),
        ):
            result = runner.invoke(cli, ["skill", "status", "--target", "agents", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["cli_version"] == version
        agents = next(t for t in payload["targets"] if t["target"] == "agents")
        assert agents["installed"] is True
        assert agents["skill_version"] == version
        assert agents["version_mismatch"] is False
        assert agents["content_mismatch"] is False

    def test_skill_status_reports_same_version_content_drift(self, runner, tmp_path):
        """Same-version edits are distinct from package-version mismatches."""
        home = tmp_path / "home"
        version = "1.2.3"
        source = "---\nname: notebooklm\n---\n# Canonical"
        dest = home / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text(
            skill_module.add_version_comment(
                "---\nname: notebooklm\n---\n# Locally edited", version
            ),
            encoding="utf-8",
        )

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module, "get_package_version", return_value=version),
            patch.object(skill_module, "get_skill_source_content", return_value=source),
        ):
            human = runner.invoke(cli, ["skill", "status", "--target", "agents"])
            structured = runner.invoke(cli, ["skill", "status", "--target", "agents", "--json"])

        assert human.exit_code == 0, human.output
        assert "content drift" in human.output.lower()
        assert "version mismatch" not in human.output.lower()
        assert json.loads(structured.output)["targets"][0]["content_mismatch"] is True

    def test_skill_status_invalid_utf8_is_content_drift(self, runner, tmp_path):
        """Invalid UTF-8 reports drift without crashing JSON status."""
        home = tmp_path / "home"
        version = "1.2.3"
        source = "---\nname: notebooklm\n---\n# Canonical"
        dest = home / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"<!-- notebooklm-py v1.2.3 -->\n\xff")

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module, "get_package_version", return_value=version),
            patch.object(skill_module, "get_skill_source_content", return_value=source),
        ):
            result = runner.invoke(cli, ["skill", "status", "--target", "agents", "--json"])

        assert result.exit_code == 0, result.output
        agents = json.loads(result.output)["targets"][0]
        assert agents["skill_version"] is None
        assert agents["content_mismatch"] is True

    def test_skill_status_reports_unavailable_content_comparison(self, runner, tmp_path):
        """Missing canonical content renders an unavailable comparison."""
        home = tmp_path / "home"
        version = "1.2.3"
        dest = home / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text(f"<!-- notebooklm-py v{version} -->\n# Test", encoding="utf-8")

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(skill_module, "get_package_version", return_value=version),
            patch.object(skill_module, "get_skill_source_content", return_value=None),
        ):
            human = runner.invoke(cli, ["skill", "status", "--target", "agents"])
            structured = runner.invoke(cli, ["skill", "status", "--target", "agents", "--json"])

        assert human.exit_code == 0, human.output
        assert "content comparison unavailable" in human.output.lower()
        assert json.loads(structured.output)["targets"][0]["content_mismatch"] is None

    def test_skill_status_packaged_source_io_failure_is_comparison_unavailable(
        self, runner, tmp_path
    ):
        """Packaged-source I/O failures render null rather than escaping."""
        home = tmp_path / "home"
        dest = home / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text("# Installed", encoding="utf-8")

        with (
            patch.object(skill_module.Path, "home", return_value=home),
            patch.object(
                skill_module,
                "get_skill_source_content",
                side_effect=PermissionError("denied"),
            ),
        ):
            result = runner.invoke(cli, ["skill", "status", "--target", "agents", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["targets"][0]["content_mismatch"] is None

    def test_skill_status_path_probe_failure_is_uninstalled_and_unavailable(self, runner, tmp_path):
        """An inaccessible target path does not crash structured status."""
        inaccessible_path = MagicMock(spec=Path)
        inaccessible_path.exists.side_effect = PermissionError("denied")

        with (
            patch.object(skill_module, "get_skill_path", return_value=inaccessible_path),
            patch.object(
                skill_module,
                "get_skill_source_content",
                return_value="---\nname: notebooklm\n---\n# Canonical",
            ),
        ):
            result = runner.invoke(cli, ["skill", "status", "--target", "agents", "--json"])

        assert result.exit_code == 0, result.output
        agents = json.loads(result.output)["targets"][0]
        assert agents["installed"] is False
        assert agents["skill_version"] is None
        assert agents["content_mismatch"] is None

    def test_skill_status_project_drift_shows_scoped_force_repair(self, runner, tmp_path):
        """Project drift suggests the target-specific forced project repair."""
        project = tmp_path / "project"
        version = "1.2.3"
        source = "---\nname: notebooklm\n---\n# Canonical"
        dest = project / ".agents" / "skills" / "notebooklm" / "SKILL.md"
        dest.parent.mkdir(parents=True)
        dest.write_text(
            skill_module.add_version_comment(
                "---\nname: notebooklm\n---\n# Locally edited", version
            ),
            encoding="utf-8",
        )

        with (
            patch.object(skill_module.Path, "cwd", return_value=project),
            patch.object(skill_module, "get_package_version", return_value=version),
            patch.object(skill_module, "get_skill_source_content", return_value=source),
        ):
            result = runner.invoke(
                cli,
                [
                    "skill",
                    "status",
                    "--scope",
                    "project",
                    "--target",
                    "agents",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "notebooklm skill install --scope project --target agents --force" in result.output
        assert "overwrites the differing project skill" in result.output


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
    """Tests for skill package (Claude-uploadable archive for chat/Cowork)."""

    SOURCE_CONTENT = "---\nname: notebooklm\ndescription: test skill\n---\n# Source body v1"

    def _stamped(self, source: str | None = None, version: str = "1.0.0") -> str:
        return skill_module.add_version_comment(source or self.SOURCE_CONTENT, version)

    def _invoke(self, runner, *extra_args: str, source: str | None = None):
        with (
            patch.object(
                skill_module,
                "get_skill_source_content",
                return_value=source or self.SOURCE_CONTENT,
            ),
            patch.object(skill_module, "get_package_version", return_value="1.0.0"),
        ):
            return runner.invoke(cli, ["skill", "package", *extra_args])

    def _read_entry(self, archive_path: Path) -> str:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(archive_path.read_bytes())) as archive:
            assert archive.namelist() == [skill_module.SKILL_ARCHIVE_ENTRY]
            return archive.read(skill_module.SKILL_ARCHIVE_ENTRY).decode("utf-8")

    def test_package_default_output_in_cwd(self, runner, tmp_path):
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = self._invoke(runner)

            assert result.exit_code == 0, result.output
            archive = Path(skill_module.DEFAULT_ARCHIVE_FILENAME)
            assert archive.exists()
            assert self._read_entry(archive) == self._stamped()
            assert "Packaged" in result.output
            assert "Capabilities" in result.output

    def test_package_output_file_path(self, runner, tmp_path):
        target = tmp_path / "custom-name.zip"
        result = self._invoke(runner, "--output", str(target))

        assert result.exit_code == 0, result.output
        assert self._read_entry(target) == self._stamped()

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
        assert self._read_entry(target) == self._stamped()

    def test_package_missing_source_errors(self, runner, tmp_path):
        with (
            patch.object(skill_module, "get_skill_source_content", return_value=None),
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
        assert payload["entries"] == [skill_module.SKILL_ARCHIVE_ENTRY]
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
            patch.object(skill_module, "get_skill_source_content", return_value=None),
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
        long_source = f"---\nname: notebooklm\ndescription: {'x' * 1100}\n---\n# Body"
        target = tmp_path / "skill.zip"
        result = self._invoke(runner, "--output", str(target), "--json", source=long_source)

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

    def test_atomic_write_bytes_removes_its_temp_file_when_the_replace_fails(self, tmp_path):
        """A failed archive replace must not strand a ``.skill.zip.*.tmp`` sibling.

        Same real collision as the text twin: the destination is an existing
        directory, which ``os.replace`` refuses on every platform.
        """
        blocked = tmp_path / "skill.zip"
        blocked.mkdir()

        with pytest.raises(OSError):
            skill_module.atomic_write_bytes(blocked, b"content")

        assert sorted(p.name for p in tmp_path.iterdir()) == ["skill.zip"]
        assert blocked.is_dir()

    def test_atomic_write_bytes_reraises_the_write_error_when_cleanup_also_fails(
        self, tmp_path, monkeypatch
    ):
        """Temp-file cleanup is best effort and must never mask the real failure.

        The replace helper swaps the temp file for a directory before failing,
        so the follow-up ``unlink`` raises too; the caller must still see the
        original write error, which is what ``package`` turns into WRITE_FAILED.
        """

        def fake_replace(temp_path: Path, path: Path) -> None:
            temp_path.unlink()
            temp_path.mkdir()  # unlink() on a directory fails on every platform
            raise OSError("replace exploded")

        monkeypatch.setattr(skill_module, "replace_file_atomically", fake_replace)

        with pytest.raises(OSError, match="replace exploded"):
            skill_module.atomic_write_bytes(tmp_path / "skill.zip", b"content")

    @pytest.mark.parametrize(
        ("source", "reason"),
        [
            # Each input embeds an OVER-LIMIT description so the test can tell
            # "the check was skipped" apart from "the check ran and passed".
            pytest.param(
                f"description: {'x' * 1100}\n# Body only, no frontmatter at all",
                "absent",
                id="no-frontmatter",
            ),
            pytest.param(
                f"---\nname: notebooklm\ndescription: {'x' * 1100}",
                "unclosed",
                id="unterminated-frontmatter",
            ),
        ],
    )
    def test_package_skips_the_description_check_when_frontmatter_is_unparseable(
        self, runner, tmp_path, source, reason
    ):
        """No parseable frontmatter means no description to length-check.

        The over-limit warning is skipped rather than guessed at, and packaging
        still succeeds with the stamped body verbatim.
        """
        target = tmp_path / "skill.zip"
        result = self._invoke(runner, "--output", str(target), "--json", source=source)

        assert result.exit_code == 0, result.output
        assert result.stderr == "", f"unexpected warning for {reason} frontmatter"
        assert self._read_entry(target) == self._stamped(source)

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
