"""Tests for cli.profile helpers."""

import importlib
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.cli.profile_cmd import _PROFILE_NAME_RE, email_to_profile_name
from notebooklm.notebooklm_cli import cli
from notebooklm.paths import _reset_config_cache, set_active_profile

profile_module = importlib.import_module("notebooklm.cli.profile_cmd")


@pytest.fixture(autouse=True)
def reset_profile_state():
    set_active_profile(None)
    _reset_config_cache()
    yield
    set_active_profile(None)
    _reset_config_cache()


@pytest.fixture
def runner():
    return CliRunner()


def notebooklm_env(home: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["NOTEBOOKLM_HOME"] = str(home)
    env.pop("NOTEBOOKLM_PROFILE", None)
    env.update(extra)
    return env


def make_profile(home: Path, name: str) -> Path:
    profile_dir = home / "profiles" / name
    profile_dir.mkdir(parents=True)
    return profile_dir


def read_config(home: Path) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


def profile_names(home: Path) -> list[str]:
    profiles_dir = home / "profiles"
    if not profiles_dir.exists():
        return []
    return sorted(path.name for path in profiles_dir.iterdir() if path.is_dir())


class TestEmailToProfileName:
    @pytest.mark.parametrize(
        ("email", "expected"),
        [
            ("alice@example.com", "alice"),
            ("alice.smith@example.com", "alice-smith"),
            ("bob+work@gmail.com", "bob-work"),
            ("teng.lin.9414@gmail.com", "teng-lin-9414"),
            ("under_score@gmail.com", "under_score"),
            ("dash-already@gmail.com", "dash-already"),
        ],
    )
    def test_sanitization(self, email, expected):
        assert email_to_profile_name(email) == expected

    def test_falls_back_when_local_part_starts_with_punctuation(self):
        # All-punctuation local-part collapses to empty → fallback fires.
        assert email_to_profile_name("...@example.com") == "account"

    def test_uses_provided_fallback(self):
        assert email_to_profile_name("...@example.com", fallback="custom") == "custom"

    def test_no_at_sign_treats_input_as_local_part(self):
        assert email_to_profile_name("plain") == "plain"

    def test_result_always_passes_profile_name_validation(self):
        # Hard property: every output must satisfy the regex used by the
        # `profile create` command, otherwise downstream usage would fail.
        for email in [
            "alice@example.com",
            "a.b.c+d@test.org",
            "...@x.com",  # falls back
            "x" * 64 + "@long.com",
        ]:
            name = email_to_profile_name(email)
            assert _PROFILE_NAME_RE.match(name), name


class TestProfileListAccountMetadata:
    def test_json_includes_account_metadata(self, tmp_path):
        profile_dir = tmp_path / "profiles" / "bob"
        profile_dir.mkdir(parents=True)
        storage_path = profile_dir / "storage_state.json"
        storage_path.write_text("{}")
        (profile_dir / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@gmail.com"}}),
            encoding="utf-8",
        )

        def fake_get_storage_path(profile=None):
            assert profile == "bob"
            return storage_path

        runner = CliRunner()
        with (
            patch.object(profile_module, "list_profiles", return_value=["bob"]),
            patch.object(profile_module, "resolve_profile", return_value="bob"),
            patch.object(profile_module, "get_storage_path", side_effect=fake_get_storage_path),
        ):
            result = runner.invoke(cli, ["profile", "list", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["profiles"] == [
            {
                "name": "bob",
                "active": True,
                "authenticated": True,
                "account": "bob@gmail.com",
            }
        ]

    def test_json_wraps_unexpected_filesystem_error(self, runner, tmp_path):
        with patch.object(profile_module, "list_profiles", side_effect=OSError("denied")):
            result = runner.invoke(
                cli,
                ["profile", "list", "--json"],
                env=notebooklm_env(tmp_path),
                catch_exceptions=True,
            )

        assert result.exit_code == 2
        payload = json.loads(result.output)
        assert payload == {
            "error": True,
            "code": "UNEXPECTED_ERROR",
            "message": "Unexpected error: denied",
        }
        assert result.stderr == ""

    def test_text_reports_filesystem_error(self, runner, tmp_path):
        # The text-mode list path used to be unwrapped (the --json path above is
        # already routed through handle_errors), so a filesystem failure escaped
        # as a raw traceback. It must now surface a friendly error + exit 1.
        with patch.object(profile_module, "list_profiles", side_effect=OSError("denied")):
            result = runner.invoke(
                cli,
                ["profile", "list"],
                env=notebooklm_env(tmp_path),
                catch_exceptions=True,
            )

        assert result.exit_code == 1, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Failed to list profiles: denied" in result.output


class TestProfileCreateCommand:
    @pytest.mark.parametrize("name", [".", "-work", "_work", "work/team", "../work"])
    def test_rejects_invalid_profile_names(self, runner, tmp_path, name):
        result = runner.invoke(cli, ["profile", "create", "--", name], env=notebooklm_env(tmp_path))

        assert result.exit_code == 1
        assert "Invalid profile name" in result.output
        assert profile_names(tmp_path) == []

    def test_create_existing_profile_fails(self, runner, tmp_path):
        make_profile(tmp_path, "work")

        result = runner.invoke(cli, ["profile", "create", "work"], env=notebooklm_env(tmp_path))

        assert result.exit_code == 1
        assert "Profile 'work' already exists." in result.output

    def test_create_reports_mkdir_failure(self, runner, tmp_path):
        # A filesystem failure while materializing the profile directory must
        # yield a friendly error + exit 1, not a raw traceback / exit 2.
        def fake_get_profile_dir(name, create=False):
            if create:
                raise OSError("read-only filesystem")
            return tmp_path / "profiles" / name

        with patch.object(profile_module, "get_profile_dir", side_effect=fake_get_profile_dir):
            result = runner.invoke(
                cli,
                ["profile", "create", "work"],
                env=notebooklm_env(tmp_path),
                catch_exceptions=True,
            )

        assert result.exit_code == 1, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Failed to create profile 'work': read-only filesystem" in result.output


class TestProfileSwitchCommand:
    def test_switch_missing_profile_shows_available_profiles(self, runner, tmp_path):
        make_profile(tmp_path, "personal")
        make_profile(tmp_path, "work")

        result = runner.invoke(
            cli,
            ["profile", "switch", "missing"],
            env=notebooklm_env(tmp_path),
        )

        assert result.exit_code == 1
        assert "Profile 'missing' not found." in result.output
        assert "Available: personal, work" in result.output

    def test_switch_writes_default_profile_with_secure_permissions(self, runner, tmp_path):
        make_profile(tmp_path, "work")
        (tmp_path / "config.json").write_text(json.dumps({"language": "ja"}), encoding="utf-8")

        result = runner.invoke(cli, ["profile", "switch", "work"], env=notebooklm_env(tmp_path))

        assert result.exit_code == 0, result.output
        assert read_config(tmp_path) == {"language": "ja", "default_profile": "work"}
        if sys.platform != "win32":
            assert ((tmp_path / "config.json").stat().st_mode & 0o777) == 0o600
            assert (tmp_path.stat().st_mode & 0o777) == 0o700

    def test_switch_secures_existing_config_directory(self, runner, tmp_path):
        make_profile(tmp_path, "work")
        if sys.platform != "win32":
            tmp_path.chmod(0o777)

        result = runner.invoke(cli, ["profile", "switch", "work"], env=notebooklm_env(tmp_path))

        assert result.exit_code == 0, result.output
        if sys.platform != "win32":
            assert (tmp_path.stat().st_mode & 0o777) == 0o700

    def test_atomic_write_config_roundtrips_payload(self, tmp_path):
        """The lock-protected wrapper writes whatever the mutator returns and
        the next reader sees the exact same dict. Paths are no longer auto-
        stringified — callers must pass JSON-serializable types directly.
        """
        config_path = tmp_path / "config.json"

        profile_module._atomic_write_config(config_path, lambda d: {**d, "default_profile": "work"})

        assert read_config(tmp_path) == {"default_profile": "work"}

    def test_switch_recovers_from_corrupt_config(self, runner, tmp_path):
        make_profile(tmp_path, "work")
        (tmp_path / "config.json").write_text("{not json", encoding="utf-8")

        result = runner.invoke(cli, ["profile", "switch", "work"], env=notebooklm_env(tmp_path))

        assert result.exit_code == 0, result.output
        assert read_config(tmp_path) == {"default_profile": "work"}

    def test_switch_reports_config_write_failure(self, runner, tmp_path):
        make_profile(tmp_path, "work")
        config_path = tmp_path / "config.json"

        with patch.object(
            profile_module, "atomic_update_json", side_effect=OSError("permission denied")
        ):
            result = runner.invoke(
                cli,
                ["profile", "switch", "work"],
                env=notebooklm_env(tmp_path),
            )

        assert result.exit_code == 1
        assert "Failed to update config.json: permission denied" in result.output
        assert not config_path.exists()


class TestProfileDeleteCommand:
    def test_delete_blocks_configured_default_profile(self, runner, tmp_path):
        make_profile(tmp_path, "work")
        (tmp_path / "config.json").write_text(
            json.dumps({"default_profile": "work"}),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli,
            ["profile", "delete", "work", "--confirm"],
            env=notebooklm_env(tmp_path),
        )

        assert result.exit_code == 1
        assert "Cannot delete active/default profile 'work'." in result.output
        assert (tmp_path / "profiles" / "work").exists()

    def test_delete_blocks_active_profile_from_cli_flag(self, runner, tmp_path):
        make_profile(tmp_path, "work")

        result = runner.invoke(
            cli,
            ["--profile", "work", "profile", "delete", "work", "--confirm"],
            env=notebooklm_env(tmp_path),
        )

        assert result.exit_code == 1
        assert "Cannot delete active/default profile 'work'." in result.output
        assert (tmp_path / "profiles" / "work").exists()

    def test_delete_cancel_leaves_profile(self, runner, tmp_path):
        make_profile(tmp_path, "old")

        result = runner.invoke(
            cli,
            ["profile", "delete", "old"],
            input="n\n",
            env=notebooklm_env(tmp_path),
        )

        assert result.exit_code == 0, result.output
        assert "Cancelled." in result.output
        assert (tmp_path / "profiles" / "old").exists()

    def test_delete_confirm_removes_profile(self, runner, tmp_path):
        make_profile(tmp_path, "old")

        result = runner.invoke(
            cli,
            ["profile", "delete", "old", "--confirm"],
            env=notebooklm_env(tmp_path),
        )

        assert result.exit_code == 0, result.output
        assert "Profile 'old' deleted." in result.output
        assert not (tmp_path / "profiles" / "old").exists()

    def test_delete_reports_rmtree_failure(self, runner, tmp_path, monkeypatch):
        # A locked/partially-deleted profile directory (common on Windows when
        # the browser profile is held by AV/the browser) must yield a friendly
        # error + exit 1, not a raw traceback or the exit-2 bug-report path.
        make_profile(tmp_path, "old")

        # Patch the consumer-side binding (profile_module.shutil) rather than the
        # global shutil module: wrap the real module and override only rmtree, so
        # the simulated failure stays isolated to this command and never mutates
        # the process-wide shutil (ADR-0007 object-target form on the consumer).
        fake_shutil = MagicMock(wraps=shutil)
        fake_shutil.rmtree.side_effect = OSError("locked")
        monkeypatch.setattr(profile_module, "shutil", fake_shutil)
        result = runner.invoke(
            cli,
            ["profile", "delete", "old", "--confirm"],
            env=notebooklm_env(tmp_path),
            catch_exceptions=True,
        )

        assert result.exit_code == 1, result.output
        # Friendly Click error path — never an uncaught traceback.
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Failed to delete profile 'old': locked" in result.output


class TestProfileRenameCommand:
    def test_rename_profile_success(self, runner, tmp_path):
        old_dir = make_profile(tmp_path, "work")
        (old_dir / "context.json").write_text("{}", encoding="utf-8")

        result = runner.invoke(
            cli,
            ["profile", "rename", "work", "client"],
            env=notebooklm_env(tmp_path),
        )

        assert result.exit_code == 0, result.output
        assert "Profile renamed: work" in result.output
        assert not (tmp_path / "profiles" / "work").exists()
        assert (tmp_path / "profiles" / "client" / "context.json").exists()

    def test_rename_reports_move_failure(self, runner, tmp_path, monkeypatch):
        # A failure moving the profile directory (e.g. a locked browser-profile
        # file held by AV/the browser on Windows) must yield a friendly error +
        # exit 1, not a raw traceback / exit 2.
        make_profile(tmp_path, "work")

        # Patch the consumer-side binding (profile_module.os) rather than the
        # global os module: wrap the real module and override only rename, so
        # the simulated failure stays isolated to this command and never mutates
        # the process-wide os (ADR-0007 object-target form on the consumer).
        fake_os = MagicMock(wraps=os)
        fake_os.rename.side_effect = OSError("locked")
        monkeypatch.setattr(profile_module, "os", fake_os)
        result = runner.invoke(
            cli,
            ["profile", "rename", "work", "client"],
            env=notebooklm_env(tmp_path),
            catch_exceptions=True,
        )

        assert result.exit_code == 1, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Failed to rename profile 'work': locked" in result.output

    def test_rename_updates_configured_default_profile(self, runner, tmp_path):
        make_profile(tmp_path, "work")
        (tmp_path / "config.json").write_text(
            json.dumps({"default_profile": "work", "language": "en"}),
            encoding="utf-8",
        )

        result = runner.invoke(
            cli,
            ["profile", "rename", "work", "client"],
            env=notebooklm_env(tmp_path),
        )

        assert result.exit_code == 0, result.output
        assert read_config(tmp_path) == {"default_profile": "client", "language": "en"}
        if sys.platform != "win32":
            assert ((tmp_path / "config.json").stat().st_mode & 0o777) == 0o600

    def test_rename_silently_recovers_corrupt_config_after_profile_move(self, runner, tmp_path):
        """Corrupt ``config.json`` is recovered under the lock, not failed loudly.

        Recovery happens inside the locked mutator via
        ``recover_from_corrupt=True`` (see PR #465). Because the corrupt
        config has no ``default_profile`` field, the implicit fallback
        ("default") does not match "work", so no retarget is needed — the
        config ends up as the empty dict that recovery produced.
        """
        make_profile(tmp_path, "work")
        (tmp_path / "config.json").write_text("{not json", encoding="utf-8")

        result = runner.invoke(
            cli,
            ["profile", "rename", "work", "client"],
            env=notebooklm_env(tmp_path),
        )

        assert result.exit_code == 0, result.output
        # No warning — corruption is silently recovered under the lock.
        assert "Warning: profile renamed but config.json update failed" not in result.output
        assert "Profile renamed: work" in result.output
        assert not (tmp_path / "profiles" / "work").exists()
        assert (tmp_path / "profiles" / "client").exists()
        # Recovery wrote a valid empty dict back to config.json.
        assert read_config(tmp_path) == {}

    def test_rename_recovers_corrupt_config_and_retargets_default(self, runner, tmp_path):
        """Corrupt config + a ``profile switch`` racing in writes both win.

        We can't realistically race a second process here, but we can show
        that recovery is performed under the lock by writing a corrupt file
        whose recovered form would still need the retarget. To do that we
        simulate the scenario where the corrupt payload happens to be the
        target of the rename: recovery yields ``{}``, the mutator sees no
        ``default_profile``, defaults to "default", and so does nothing —
        which matches the prior test. The retarget itself is covered by
        :meth:`test_rename_updates_configured_default_profile` above.
        """
        # Effectively a smoke test that the same recovery path applies when
        # ``default_profile`` was missing entirely (no retarget triggered).
        make_profile(tmp_path, "work")
        (tmp_path / "config.json").write_text("not json at all", encoding="utf-8")

        result = runner.invoke(
            cli,
            ["profile", "rename", "work", "client"],
            env=notebooklm_env(tmp_path),
        )

        assert result.exit_code == 0, result.output
        assert (tmp_path / "profiles" / "client").exists()
        assert read_config(tmp_path) == {}


class TestProfileJsonOutput:
    """``--json`` emits a single parseable document for every profile mutation."""

    def test_create_json(self, runner, tmp_path):
        result = runner.invoke(
            cli, ["profile", "create", "work", "--json"], env=notebooklm_env(tmp_path)
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"profile": "work", "status": "created"}
        assert (tmp_path / "profiles" / "work").exists()

    def test_switch_json(self, runner, tmp_path):
        make_profile(tmp_path, "work")
        result = runner.invoke(
            cli, ["profile", "switch", "work", "--json"], env=notebooklm_env(tmp_path)
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload == {"profile": "work", "status": "switched"}
        assert read_config(tmp_path)["default_profile"] == "work"

    def test_delete_json_skips_confirm(self, runner, tmp_path):
        make_profile(tmp_path, "default")
        make_profile(tmp_path, "work")
        # No --yes: --json must imply non-interactive and delete without prompting.
        result = runner.invoke(
            cli, ["profile", "delete", "work", "--json"], env=notebooklm_env(tmp_path)
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"profile": "work", "status": "deleted"}
        assert not (tmp_path / "profiles" / "work").exists()

    def test_rename_json(self, runner, tmp_path):
        make_profile(tmp_path, "work")
        result = runner.invoke(
            cli, ["profile", "rename", "work", "client", "--json"], env=notebooklm_env(tmp_path)
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {
            "old_name": "work",
            "new_name": "client",
            "default_updated": False,
            "status": "renamed",
            "config_warning": None,
        }
        assert (tmp_path / "profiles" / "client").exists()

    def test_create_duplicate_json_error_envelope(self, runner, tmp_path):
        """A validation failure under ``--json`` still emits one JSON document on
        stdout (the grouped-CLI ClickException -> envelope path), not human text."""
        make_profile(tmp_path, "work")
        result = runner.invoke(
            cli, ["profile", "create", "work", "--json"], env=notebooklm_env(tmp_path)
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"] is True
        assert payload["code"] == "VALIDATION_ERROR"

    def test_switch_missing_json_error_envelope(self, runner, tmp_path):
        result = runner.invoke(
            cli, ["profile", "switch", "missing", "--json"], env=notebooklm_env(tmp_path)
        )
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["error"] is True
        assert payload["code"] == "VALIDATION_ERROR"
