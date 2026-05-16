"""CLI integration tests for ``notebooklm profile``.

The ``profile`` command group (``list`` / ``create`` / ``switch`` /
``rename``) is a local-only profile manager — every subcommand operates on
``$NOTEBOOKLM_HOME/profiles/`` and ``$NOTEBOOKLM_HOME/config.json`` and
makes **no** HTTP requests. The matched cassette ``cli_profile.yaml`` is
intentionally empty (``interactions: []``).

Why register an empty cassette at all?
    With ``record_mode="none"`` (the default for CI replay), VCR raises on
    any unmatched request. Pinning each ``profile`` subcommand to an empty
    cassette turns "future refactor accidentally adds a network call" into
    a loud test failure (``CannotOverwriteExistingCassetteException``).
    This closes audit I10 (CLI VCR coverage gap) without recording fake
    traffic.

Coverage scope (per task MUST-NOT: no destructive operations):
    * ``profile list``           — happy path + empty path (no profiles)
    * ``profile create <name>``  — happy path + duplicate-name failure
    * ``profile switch <name>``  — happy path + missing-target failure
    * ``profile rename a b``     — happy path

Every test runs inside an isolated ``NOTEBOOKLM_HOME`` (``tmp_path``) and
resets ``notebooklm.paths`` module caches so the sandbox env var actually
takes effect.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from notebooklm import paths
from notebooklm.notebooklm_cli import cli

from .conftest import notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``NOTEBOOKLM_HOME`` into ``tmp_path`` and clear module caches."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    paths.set_active_profile(None)
    paths._reset_config_cache()
    yield tmp_path
    paths.set_active_profile(None)
    paths._reset_config_cache()


def _make_profile(home: Path, name: str) -> Path:
    """Create a profile directory under ``home/profiles/<name>``."""
    profile_dir = home / "profiles" / name
    profile_dir.mkdir(parents=True)
    return profile_dir


class TestProfileListCommand:
    """``notebooklm profile list`` — pure filesystem read, no HTTP."""

    @pytest.mark.parametrize("json_flag", [False, True])
    @notebooklm_vcr.use_cassette("cli_profile.yaml")
    def test_list_with_profiles(self, runner, isolated_home: Path, json_flag: bool) -> None:
        """List enumerates existing profile directories.

        Asserts:
          * exit code 0
          * No HTTP traffic emitted.
          * ``--json`` output exposes the populated ``profiles`` list and
            the resolved ``active`` profile name.
        """
        _make_profile(isolated_home, "default")
        _make_profile(isolated_home, "work")

        args = ["profile", "list"]
        if json_flag:
            args.append("--json")

        result = runner.invoke(cli, args)

        assert result.exit_code == 0, result.output
        if json_flag:
            data = json.loads(result.output)
            assert {p["name"] for p in data["profiles"]} == {"default", "work"}
            assert data["active"] == "default"
        else:
            # Rich table contains both profile names verbatim.
            assert "default" in result.output
            assert "work" in result.output

    @notebooklm_vcr.use_cassette("cli_profile.yaml")
    def test_list_empty(self, runner, isolated_home: Path) -> None:
        """With no profile directories the CLI prints a friendly hint."""
        result = runner.invoke(cli, ["profile", "list"])

        assert result.exit_code == 0, result.output
        assert "No profiles found" in result.output


class TestProfileCreateCommand:
    """``notebooklm profile create`` — mkdir + perms, no HTTP."""

    @notebooklm_vcr.use_cassette("cli_profile.yaml")
    def test_create_new_profile(self, runner, isolated_home: Path) -> None:
        """Create writes a new profile directory under ``profiles/``."""
        result = runner.invoke(cli, ["profile", "create", "work"])

        assert result.exit_code == 0, result.output
        assert (isolated_home / "profiles" / "work").is_dir()
        assert "Profile 'work' created" in result.output

    @notebooklm_vcr.use_cassette("cli_profile.yaml")
    def test_create_duplicate_fails(self, runner, isolated_home: Path) -> None:
        """Creating an existing profile is rejected with a clean error."""
        _make_profile(isolated_home, "work")

        result = runner.invoke(cli, ["profile", "create", "work"])

        assert result.exit_code != 0
        assert "already exists" in result.output


class TestProfileSwitchCommand:
    """``notebooklm profile switch`` — config.json write, no HTTP."""

    @notebooklm_vcr.use_cassette("cli_profile.yaml")
    def test_switch_to_existing_profile(self, runner, isolated_home: Path) -> None:
        """Switch updates ``default_profile`` in ``config.json``."""
        _make_profile(isolated_home, "default")
        _make_profile(isolated_home, "work")

        result = runner.invoke(cli, ["profile", "switch", "work"])

        assert result.exit_code == 0, result.output
        config = json.loads((isolated_home / "config.json").read_text(encoding="utf-8"))
        assert config["default_profile"] == "work"

    @notebooklm_vcr.use_cassette("cli_profile.yaml")
    def test_switch_missing_profile_fails(self, runner, isolated_home: Path) -> None:
        """Switching to a profile that doesn't exist is rejected."""
        _make_profile(isolated_home, "default")

        result = runner.invoke(cli, ["profile", "switch", "no-such-profile"])

        assert result.exit_code != 0
        assert "not found" in result.output


class TestProfileRenameCommand:
    """``notebooklm profile rename`` — directory rename, no HTTP."""

    @notebooklm_vcr.use_cassette("cli_profile.yaml")
    def test_rename_profile(self, runner, isolated_home: Path) -> None:
        """Rename moves the profile directory and reports success."""
        _make_profile(isolated_home, "work")

        result = runner.invoke(cli, ["profile", "rename", "work", "work-archive"])

        assert result.exit_code == 0, result.output
        assert not (isolated_home / "profiles" / "work").exists()
        assert (isolated_home / "profiles" / "work-archive").is_dir()
