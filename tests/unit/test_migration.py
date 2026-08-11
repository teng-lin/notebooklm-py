"""Tests for migration from legacy flat layout to profile-based structure."""

import json
import os
import stat
import sys
from unittest.mock import patch

import pytest

from notebooklm.migration import ensure_profiles_dir, migrate_to_profiles
from notebooklm.paths import _reset_config_cache, set_active_profile


@pytest.fixture(autouse=True)
def _reset_profile_state():
    """Reset module-level profile state between tests."""
    set_active_profile(None)
    _reset_config_cache()
    yield
    set_active_profile(None)
    _reset_config_cache()


def _clean_env():
    env = os.environ.copy()
    env.pop("NOTEBOOKLM_HOME", None)
    env.pop("NOTEBOOKLM_PROFILE", None)
    return env


class TestMigrateToProfiles:
    def test_already_migrated(self, tmp_path):
        """Returns False if profiles/ already exists."""
        (tmp_path / "profiles" / "default").mkdir(parents=True)
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert migrate_to_profiles() is False

    def test_fresh_install(self, tmp_path):
        """Creates profiles/ dir on fresh install (no legacy files)."""
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert migrate_to_profiles() is True
            assert (tmp_path / "profiles").exists()

    def test_migrates_legacy_files(self, tmp_path):
        """Moves legacy files into profiles/default/."""
        # Create legacy layout
        (tmp_path / "storage_state.json").write_text('{"cookies":[]}')
        (tmp_path / "context.json").write_text('{"notebook_id":"nb1"}')
        (tmp_path / "browser_profile").mkdir()
        (tmp_path / "browser_profile" / "data").write_text("chrome data")

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert migrate_to_profiles() is True

        default_dir = tmp_path / "profiles" / "default"
        # Files moved to profile dir
        assert (default_dir / "storage_state.json").exists()
        assert json.loads((default_dir / "storage_state.json").read_text()) == {"cookies": []}
        assert (default_dir / "context.json").exists()
        assert (default_dir / "browser_profile" / "data").exists()
        # Migration marker present
        assert (default_dir / ".migration_complete").exists()
        # Originals removed
        assert not (tmp_path / "storage_state.json").exists()
        assert not (tmp_path / "context.json").exists()
        assert not (tmp_path / "browser_profile").exists()

    def test_promotes_legacy_account_metadata_in_band(self, tmp_path):
        """#2103 PR-0: the startup layout migration also promotes a legacy
        ``context.json[account]`` record in-band — a completeness nicety (the
        ``read_account_metadata`` chokepoint retries promotion on every call
        regardless, so this isn't load-bearing for correctness), but new
        production code on the upgrade path that shipped with zero test
        coverage otherwise. Deliberately does NOT call ``read_account_metadata``
        afterward — that would mask a broken migration-side promotion behind
        the reactive chokepoint healing it a moment later."""
        (tmp_path / "storage_state.json").write_text(
            json.dumps({"cookies": [{"name": "SID", "value": "v"}]})
        )
        (tmp_path / "context.json").write_text(
            json.dumps(
                {
                    "account": {"authuser": 3, "email": "legacy@example.com"},
                    "notebook_id": "nb-123",
                }
            )
        )

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            migrate_to_profiles()

        default_dir = tmp_path / "profiles" / "default"
        storage_data = json.loads((default_dir / "storage_state.json").read_text())
        assert storage_data["notebooklm"]["account"] == {
            "authuser": 3,
            "email": "legacy@example.com",
        }
        # Legacy key scrubbed; non-account context state (notebook_id) survives.
        context_data = json.loads((default_dir / "context.json").read_text())
        assert "account" not in context_data
        assert context_data.get("notebook_id") == "nb-123"

    def test_migration_without_legacy_account_metadata_is_unaffected(self, tmp_path):
        """A profile with no legacy account record migrates exactly as before —
        the new promotion hook must be a true no-op when there's nothing to
        promote, not just when it happens to succeed."""
        (tmp_path / "storage_state.json").write_text(json.dumps({"cookies": []}))
        (tmp_path / "context.json").write_text(json.dumps({"notebook_id": "nb-456"}))

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert migrate_to_profiles() is True

        default_dir = tmp_path / "profiles" / "default"
        storage_data = json.loads((default_dir / "storage_state.json").read_text())
        assert "notebooklm" not in storage_data
        context_data = json.loads((default_dir / "context.json").read_text())
        assert context_data == {"notebook_id": "nb-456"}

    def test_updates_config(self, tmp_path):
        """Sets default_profile in config.json."""
        (tmp_path / "storage_state.json").write_text('{"cookies":[]}')

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            migrate_to_profiles()

        config = json.loads((tmp_path / "config.json").read_text())
        assert config["default_profile"] == "default"

    def test_preserves_existing_config(self, tmp_path):
        """Preserves existing config.json values during migration."""
        (tmp_path / "storage_state.json").write_text('{"cookies":[]}')
        (tmp_path / "config.json").write_text(json.dumps({"language": "ja"}))

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            migrate_to_profiles()

        config = json.loads((tmp_path / "config.json").read_text())
        assert config["language"] == "ja"
        assert config["default_profile"] == "default"

    def test_idempotent(self, tmp_path):
        """Running twice is safe — second call is no-op."""
        (tmp_path / "storage_state.json").write_text('{"cookies":[]}')

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert migrate_to_profiles() is True
            assert migrate_to_profiles() is False  # Already migrated

    def test_partial_legacy(self, tmp_path):
        """Works with only some legacy files present."""
        (tmp_path / "storage_state.json").write_text('{"cookies":[]}')
        # No context.json or browser_profile

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            assert migrate_to_profiles() is True

        default_dir = tmp_path / "profiles" / "default"
        assert (default_dir / "storage_state.json").exists()
        assert not (default_dir / "context.json").exists()

    def test_interrupted_migration_retries(self, tmp_path):
        """Interrupted migration (marker + leftover legacy files) is retried."""
        # Simulate crash after marker but before deletion (old buggy order)
        (tmp_path / "storage_state.json").write_text('{"cookies":[]}')
        default_dir = tmp_path / "profiles" / "default"
        default_dir.mkdir(parents=True)
        (default_dir / "storage_state.json").write_text('{"cookies":[]}')
        (default_dir / ".migration_complete").write_text("migrated\n")
        # Legacy file still at root — this simulates a crash

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            # ensure_profiles_dir should detect leftover legacy files and re-migrate
            ensure_profiles_dir()

        # Legacy file should now be cleaned up
        assert not (tmp_path / "storage_state.json").exists()
        assert (default_dir / "storage_state.json").exists()

    def test_marker_written_after_deletion(self, tmp_path):
        """Marker is written only after originals are deleted."""
        (tmp_path / "storage_state.json").write_text('{"cookies":[]}')

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            migrate_to_profiles()

        default_dir = tmp_path / "profiles" / "default"
        # Marker exists
        assert (default_dir / ".migration_complete").exists()
        # Original removed
        assert not (tmp_path / "storage_state.json").exists()

    def test_overwrites_stale_profile_when_legacy_is_newer(self, tmp_path):
        """Fresh login data at legacy root overwrites a stale profile copy.

        Regression for the scenario where a previous migration left a profile
        copy in place, and a subsequent `login` wrote fresh cookies to the
        legacy root path. The migration must prefer the newer source.
        """
        default_dir = tmp_path / "profiles" / "default"
        default_dir.mkdir(parents=True)
        stale_profile = default_dir / "storage_state.json"
        stale_profile.write_text('{"cookies":["stale"]}')
        os.utime(stale_profile, (1000, 1000))

        fresh_legacy = tmp_path / "storage_state.json"
        fresh_legacy.write_text('{"cookies":["fresh"]}')
        os.utime(fresh_legacy, (2000, 2000))

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            migrate_to_profiles()

        # Profile copy now holds the fresh data, legacy file is gone
        assert json.loads(stale_profile.read_text()) == {"cookies": ["fresh"]}
        assert not fresh_legacy.exists()

    def test_keeps_newer_profile_when_legacy_is_older(self, tmp_path):
        """An up-to-date profile copy is preserved over an older legacy file."""
        default_dir = tmp_path / "profiles" / "default"
        default_dir.mkdir(parents=True)
        fresh_profile = default_dir / "storage_state.json"
        fresh_profile.write_text('{"cookies":["fresh"]}')
        os.utime(fresh_profile, (2000, 2000))

        stale_legacy = tmp_path / "storage_state.json"
        stale_legacy.write_text('{"cookies":["stale"]}')
        os.utime(stale_legacy, (1000, 1000))

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            migrate_to_profiles()

        # Profile copy is preserved, legacy file is cleaned up
        assert json.loads(fresh_profile.read_text()) == {"cookies": ["fresh"]}
        assert not stale_legacy.exists()

    def test_stale_temp_file_is_cleaned_and_copy_completes(self, tmp_path):
        """A torn copy from an interrupted run never becomes the migrated file.

        Copies land under a dot-prefixed temp name and are renamed into place
        only once complete, so an interrupted run can leave a partial only at
        the temp name — with an mtime *newer* than the intact legacy source.
        The retry must discard the stale temp (not let it satisfy any
        freshness check) and complete the copy from the intact original.
        """
        src = tmp_path / "storage_state.json"
        src.write_text('{"cookies":["intact"]}')
        os.utime(src, (1000, 1000))

        default_dir = tmp_path / "profiles" / "default"
        default_dir.mkdir(parents=True)
        stale_tmp = default_dir / ".storage_state.json.migrating"
        stale_tmp.write_text('{"cook')  # truncated; mtime is "now" (newer than src)

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            migrate_to_profiles()

        dst = default_dir / "storage_state.json"
        assert json.loads(dst.read_text()) == {"cookies": ["intact"]}
        assert not stale_tmp.exists()
        assert not src.exists()

    def test_stale_temp_dir_is_cleaned_and_copy_completes(self, tmp_path):
        """A partial temp dir from an interrupted copytree is discarded on retry."""
        legacy = tmp_path / "browser_profile"
        legacy.mkdir()
        (legacy / "data").write_text("chrome data")
        (legacy / "prefs").write_text("prefs data")

        default_dir = tmp_path / "profiles" / "default"
        default_dir.mkdir(parents=True)
        stale_tmp = default_dir / ".browser_profile.migrating"
        stale_tmp.mkdir()
        (stale_tmp / "data").write_text("chr")  # partial copy, missing "prefs"

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            migrate_to_profiles()

        dst = default_dir / "browser_profile"
        assert (dst / "data").read_text() == "chrome data"
        assert (dst / "prefs").read_text() == "prefs data"
        assert not stale_tmp.exists()
        assert not legacy.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_storage_state_permissions_tightened(self, tmp_path):
        """A world-readable legacy storage_state.json migrates to a 0600 copy."""
        src = tmp_path / "storage_state.json"
        src.write_text('{"cookies":[]}')
        src.chmod(0o644)
        ctx = tmp_path / "context.json"
        ctx.write_text('{"notebook_id":"nb1"}')
        ctx.chmod(0o644)

        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            migrate_to_profiles()

        default_dir = tmp_path / "profiles" / "default"
        # Secret-bearing file is tightened regardless of legacy mode
        migrated = default_dir / "storage_state.json"
        assert stat.S_IMODE(migrated.stat().st_mode) == 0o600
        # Non-secret files keep the legacy mode
        assert stat.S_IMODE((default_dir / "context.json").stat().st_mode) == 0o644


class TestEnsureProfilesDir:
    def test_creates_on_first_run(self, tmp_path):
        """Creates profiles directory on first invocation."""
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            ensure_profiles_dir()
            assert (tmp_path / "profiles").exists()

    def test_noop_when_exists(self, tmp_path):
        """Does nothing if profiles/ already exists."""
        (tmp_path / "profiles").mkdir()
        with patch.dict(os.environ, {"NOTEBOOKLM_HOME": str(tmp_path)}, clear=True):
            ensure_profiles_dir()  # Should not raise
            assert (tmp_path / "profiles").exists()


class TestExplicitAuthBypassesProfileSetup:
    """Verify that --storage and NOTEBOOKLM_AUTH_JSON don't require writable home."""

    def test_storage_flag_skips_profile_setup(self, tmp_path):
        """CLI with --storage should not call ensure_profiles_dir."""
        from click.testing import CliRunner

        from notebooklm.notebooklm_cli import cli

        # Make home read-only to prove profiles dir creation is skipped
        ro_home = tmp_path / "ro_home"
        ro_home.mkdir(mode=0o500)

        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--storage", str(storage_file), "status", "--paths"],
            env={"NOTEBOOKLM_HOME": str(ro_home)},
        )
        # Should not crash with PermissionError from profiles dir creation
        assert result.exit_code != 1 or "PermissionError" not in str(result.exception or "")

    def test_auth_json_env_skips_profile_setup(self, tmp_path):
        """CLI with NOTEBOOKLM_AUTH_JSON should not call ensure_profiles_dir."""
        from click.testing import CliRunner

        from notebooklm.notebooklm_cli import cli

        ro_home = tmp_path / "ro_home"
        ro_home.mkdir(mode=0o500)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["status", "--paths"],
            env={
                "NOTEBOOKLM_HOME": str(ro_home),
                "NOTEBOOKLM_AUTH_JSON": '{"cookies": [{"name": "SID", "value": "x", "domain": ".google.com"}, {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"}]}',
            },
        )
        assert result.exit_code != 1 or "PermissionError" not in str(result.exception or "")
