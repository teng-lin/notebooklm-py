"""Unit tests for the ``notebooklm doctor`` diagnostics command."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import notebooklm.cli.doctor_cmd as doctor_cmd_module
from notebooklm import paths
from notebooklm.notebooklm_cli import cli


@pytest.fixture(autouse=True)
def isolated_notebooklm_home(tmp_path, monkeypatch):
    """Keep doctor tests away from the real profile home and cached profile state."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    paths.set_active_profile(None)
    paths._reset_config_cache()
    yield tmp_path
    paths.set_active_profile(None)
    paths._reset_config_cache()


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_profile(home: Path, name: str = "default") -> Path:
    profile_dir = home / "profiles" / name
    profile_dir.mkdir(parents=True)
    if sys.platform != "win32":
        profile_dir.chmod(0o700)
    return profile_dir


def _storage(cookies: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    return {"cookies": cookies}


def _invoke_json(runner, args: list[str], *, exit_code: int = 0) -> dict:
    result = runner.invoke(cli, [*args, "doctor", "--json"])
    assert result.exit_code == exit_code, result.output
    return json.loads(result.output)


def test_doctor_reports_clean_profile_layout(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))
    _write_json(home / "config.json", {"default_profile": "default"})

    data = _invoke_json(runner, [])

    assert data["profile"] == "default"
    assert data["profile_source"] == "config.json"
    assert data["checks"]["migration"] == {"status": "pass", "detail": "complete"}
    assert data["checks"]["auth"] == {
        "status": "pass",
        "detail": "local SID cookie present (1 cookies)",
    }
    assert data["checks"]["config"] == {
        "status": "pass",
        "detail": "valid (default_profile: default)",
    }
    if sys.platform == "win32":
        assert data["checks"]["profile_dir"]["status"] == "warn"
        assert str(profile_dir) in data["checks"]["profile_dir"]["detail"]
        assert "permissions:" in data["checks"]["profile_dir"]["detail"]
    else:
        assert data["checks"]["profile_dir"] == {"status": "pass", "detail": str(profile_dir)}


def test_doctor_reports_legacy_layout_without_startup_migration(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home
    _write_json(home / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))

    data = _invoke_json(runner, ["--storage", str(home / "unused.json")], exit_code=1)

    assert data["checks"]["migration"] == {
        "status": "fail",
        "detail": "legacy layout detected",
    }
    assert data["checks"]["profile_dir"]["status"] == "fail"
    assert data["checks"]["auth"] == {
        "status": "pass",
        "detail": "local SID cookie present (1 cookies)",
    }


def test_doctor_reports_missing_profile_dir(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home

    data = _invoke_json(runner, ["--storage", str(home / "unused.json")], exit_code=1)

    assert data["checks"]["migration"] == {
        "status": "pass",
        "detail": "clean (no legacy files)",
    }
    assert data["checks"]["profile_dir"] == {
        "status": "fail",
        "detail": f"{home / 'profiles' / 'default'} not found",
    }
    assert data["checks"]["auth"] == {"status": "fail", "detail": "not authenticated"}


def test_doctor_reports_invalid_storage_json(runner, isolated_notebooklm_home):
    profile_dir = _make_profile(isolated_notebooklm_home)
    profile_dir.joinpath("storage_state.json").write_text("{not json", encoding="utf-8")

    data = _invoke_json(runner, [], exit_code=1)

    assert data["checks"]["auth"]["status"] == "fail"
    assert data["checks"]["auth"]["detail"].startswith("invalid storage file:")


def test_doctor_reports_invalid_storage_root_shape(runner, isolated_notebooklm_home):
    profile_dir = _make_profile(isolated_notebooklm_home)
    _write_json(profile_dir / "storage_state.json", [])

    data = _invoke_json(runner, [], exit_code=1)

    assert data["checks"]["auth"] == {
        "status": "fail",
        "detail": "invalid storage file: storage root is not an object",
    }


def test_doctor_reports_invalid_storage_cookie_shape(runner, isolated_notebooklm_home):
    profile_dir = _make_profile(isolated_notebooklm_home)
    _write_json(profile_dir / "storage_state.json", {"cookies": {"name": "SID"}})

    data = _invoke_json(runner, [], exit_code=1)

    assert data["checks"]["auth"] == {
        "status": "fail",
        "detail": "invalid storage file: cookies is not a list",
    }


def test_doctor_reports_cookies_missing_sid(runner, isolated_notebooklm_home):
    profile_dir = _make_profile(isolated_notebooklm_home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "HSID", "value": "x"}]))

    data = _invoke_json(runner, [], exit_code=1)

    assert data["checks"]["auth"] == {"status": "fail", "detail": "SID cookie missing"}


def test_doctor_warns_when_config_default_profile_is_missing(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home
    _make_profile(home)
    _write_json(home / "profiles" / "default" / "storage_state.json", _storage([]))
    _write_json(home / "config.json", {"default_profile": "missing"})

    data = _invoke_json(runner, [], exit_code=1)

    assert data["profile"] == "missing"
    assert data["profile_source"] == "config.json"
    assert data["checks"]["profile_dir"]["status"] == "fail"
    assert data["checks"]["config"] == {
        "status": "warn",
        "detail": "default_profile 'missing' does not exist",
    }


def test_doctor_reports_invalid_config_root_shape(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home
    _make_profile(home)
    _write_json(home / "config.json", [])

    data = _invoke_json(runner, [], exit_code=1)

    assert data["checks"]["config"] == {
        "status": "fail",
        "detail": "invalid: config root is not an object",
    }


def test_doctor_fix_creates_missing_profile_dir(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home

    result = runner.invoke(cli, ["doctor", "--fix", "--json"])

    # --fix repairs the profile dir, but no auth was set up so the auth check
    # is still failing — doctor exits 1 on the lingering failure.
    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    profile_dir = home / "profiles" / "default"
    assert profile_dir.is_dir()
    if sys.platform != "win32":
        assert profile_dir.stat().st_mode & 0o777 == 0o700
    assert data["checks"]["profile_dir"] == {"status": "pass", "detail": str(profile_dir)}
    assert data["checks"]["auth"]["status"] == "fail"
    assert data["fixes_applied"] == [f"Created profile directory: {profile_dir}"]


def test_doctor_fix_migrates_legacy_layout(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home
    storage_payload = _storage([{"name": "SID", "value": "x"}])
    context_payload = {"current_notebook": "nb_123"}
    _write_json(home / "storage_state.json", storage_payload)
    _write_json(home / "context.json", context_payload)

    result = runner.invoke(
        cli,
        ["--storage", str(home / "unused.json"), "doctor", "--fix", "--json"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    profile_dir = home / "profiles" / "default"
    assert not (home / "storage_state.json").exists()
    assert (profile_dir / "storage_state.json").exists()
    assert (profile_dir / "context.json").exists()
    assert json.loads((profile_dir / "storage_state.json").read_text(encoding="utf-8")) == (
        storage_payload
    )
    assert json.loads((profile_dir / "context.json").read_text(encoding="utf-8")) == (
        context_payload
    )
    assert data["checks"]["migration"] == {
        "status": "pass",
        "detail": "complete (just migrated)",
    }
    assert data["fixes_applied"] == ["Migrated legacy layout to profiles/default/"]


def test_doctor_json_output_shape(runner, isolated_notebooklm_home):
    _make_profile(isolated_notebooklm_home)

    # No storage_state.json was written, so the auth check fails and doctor
    # exits 1; the JSON shape contract still holds on the failure path.
    data = _invoke_json(runner, [], exit_code=1)

    assert set(data) == {"profile", "profile_source", "checks"}
    assert set(data["checks"]) == {
        "migration",
        "profile_dir",
        "auth",
        "config",
        "headless_reauth",
    }
    for check in data["checks"].values():
        assert set(check) == {"status", "detail"}
        assert check["status"] in {"pass", "warn", "fail"}
        assert isinstance(check["detail"], str)


def test_doctor_json_wraps_unexpected_filesystem_error(runner, isolated_notebooklm_home):
    with patch.object(doctor_cmd_module, "get_storage_path", side_effect=OSError("denied")):
        result = runner.invoke(cli, ["doctor", "--json"], catch_exceptions=True)

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload == {
        "error": True,
        "code": "UNEXPECTED_ERROR",
        "message": "Unexpected error: denied",
    }
    assert result.stderr == ""


def test_doctor_headless_reauth_degrades_to_warn_on_profile_resolution_error(
    runner, isolated_notebooklm_home, monkeypatch
):
    """A read-only diagnostic must not crash if the profile dir cannot resolve.

    ``get_browser_profile_dir`` can raise ``ValueError`` (malformed profile
    config) / ``OSError`` (permissions); the headless-reauth check degrades to
    a ``warn`` row instead of bubbling up. ``monkeypatch.setattr`` on the public
    helper avoids growing the string-patch ratchet for this file.
    """
    from notebooklm.cli import doctor_cmd

    _make_profile(isolated_notebooklm_home)
    _write_json(
        isolated_notebooklm_home / "profiles" / "default" / "storage_state.json",
        _storage([{"name": "SID", "value": "x"}]),
    )

    def _boom(*_a, **_k):
        raise ValueError("malformed profile name")

    monkeypatch.setattr(doctor_cmd, "get_browser_profile_dir", _boom)

    data = _invoke_json(runner, [])

    assert data["checks"]["headless_reauth"]["status"] == "warn"
    assert "could not resolve the browser profile" in data["checks"]["headless_reauth"]["detail"]
    # The error type is surfaced, never a raw path / value.
    assert "ValueError" in data["checks"]["headless_reauth"]["detail"]


def test_doctor_text_mode_exits_nonzero_on_failure(runner, isolated_notebooklm_home):
    """Regression for #1160: text-mode doctor must exit 1 when a check fails.

    Previously the command always exited 0, so a broken install read as green
    in CI / ``set -e`` scripts. With no profile dir and no auth, both the
    ``profile_dir`` and ``auth`` checks fail.
    """
    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 1, result.output
    # The rendered table still shows the failing rows.
    assert "fail" in result.output


def test_doctor_text_mode_exits_zero_when_all_pass(runner, isolated_notebooklm_home):
    """A fully healthy profile keeps doctor's text mode at exit 0.

    On Windows the profile_dir permissions check warns rather than passes, but a
    warning is not a failure, so the command still exits 0.
    """
    home = isolated_notebooklm_home
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))
    _write_json(home / "config.json", {"default_profile": "default"})

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "fail" not in result.output


def test_doctor_warn_only_keeps_exit_zero(runner, isolated_notebooklm_home):
    """A lingering warning (no failures) must not flip the exit code to 1.

    Legacy files alongside a migrated ``profiles/`` directory is a ``warn``
    (partial migration), not a ``fail`` — doctor should still exit 0.
    """
    home = isolated_notebooklm_home
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))
    # Leftover legacy file alongside the profiles/ dir -> migration warn. The
    # ``--storage`` override suppresses the startup migration that would
    # otherwise sweep this file into the profile before the checks run.
    _write_json(home / "context.json", {"current_notebook": "nb_123"})

    data = _invoke_json(runner, ["--storage", str(home / "unused.json")], exit_code=0)

    assert data["checks"]["migration"]["status"] == "warn"
    assert not any(c["status"] == "fail" for c in data["checks"].values())
