"""Unit tests for the ``notebooklm doctor`` diagnostics command."""

import json
import sys
from pathlib import Path
from typing import Any
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
    monkeypatch.delenv("NOTEBOOKLM_BASE_URL", raising=False)
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


def _storage(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cookies": [{"domain": ".google.com", "path": "/", "secure": True, **c} for c in cookies]
    }


def _auth_summary(check: dict[str, str]) -> dict[str, str]:
    return {key: check[key] for key in ("status", "detail")}


def _invoke_json(runner, args: list[str], *, exit_code: int = 0) -> dict:
    result = runner.invoke(cli, [*args, "doctor", "--json"])
    assert result.exit_code == exit_code, result.output
    return json.loads(result.output)


def test_doctor_reports_clean_profile_layout(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home
    profile_dir = _make_profile(home)
    _write_json(
        profile_dir / "storage_state.json",
        _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}]),
    )
    _write_json(home / "config.json", {"default_profile": "default"})

    data = _invoke_json(runner, [])

    assert data["profile"] == "default"
    assert data["profile_source"] == "config.json"
    assert data["checks"]["migration"] == {"status": "pass", "detail": "complete"}
    assert _auth_summary(data["checks"]["auth"]) == {
        "status": "pass",
        "detail": "local auth cookies usable (2 cookies)",
    }
    assert data["checks"]["config"] == {
        "status": "pass",
        "detail": "valid (default_profile: default)",
    }
    # Windows passes with an ACL-explaining detail rather than warning about
    # POSIX bits ``paths._ensure_dir`` deliberately never sets (#2046).
    assert data["checks"]["profile_dir"]["status"] == "pass"
    assert str(profile_dir) in data["checks"]["profile_dir"]["detail"]
    assert "permissions:" not in data["checks"]["profile_dir"]["detail"]
    if sys.platform == "win32":
        assert "ACL" in data["checks"]["profile_dir"]["detail"]
    else:
        assert data["checks"]["profile_dir"]["detail"] == str(profile_dir)


def test_doctor_json_renders_the_windows_profile_dir_detail(
    runner, isolated_notebooklm_home, monkeypatch
):
    """The Windows ``--json`` envelope, exercised on every runner (#2046).

    The sibling assertions above can only describe Windows behaviour when the
    tests happen to run on Windows — which is precisely why the original defect
    reached a release. Faking ``sys.platform`` (read at call time by
    ``_app.doctor._is_windows``) puts the Windows row in front of the Linux and
    macOS jobs too.
    """
    home = isolated_notebooklm_home
    profile_dir = home / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    if sys.platform != "win32":
        # The wide mode a Windows-created directory reports, which the old
        # unconditional check turned into a permanent, unfixable warn.
        profile_dir.chmod(0o777)
    _write_json(profile_dir / "storage_state.json", _storage([]))
    monkeypatch.setattr(sys, "platform", "win32")

    data = _invoke_json(runner, [], exit_code=1)

    assert data["checks"]["profile_dir"]["status"] == "pass"
    detail = data["checks"]["profile_dir"]["detail"]
    assert str(profile_dir) in detail
    assert "ACL" in detail
    assert "permissions:" not in detail
    assert "0o700" not in detail


def test_doctor_explicit_storage_drives_path_info_and_auth(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home
    profile_dir = _make_profile(home, "work")
    _write_json(profile_dir / "storage_state.json", _storage([]))
    storage = home / "custom.json"
    _write_json(
        storage,
        _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}]),
    )

    data = _invoke_json(runner, ["--profile", "work", "--storage", str(storage)])

    assert data["profile"] == "work"
    assert data["profile_source"] == "CLI flag (--storage, profile ignored)"
    assert _auth_summary(data["checks"]["auth"]) == {
        "status": "pass",
        "detail": "local auth cookies usable (2 cookies)",
    }


def test_doctor_reports_legacy_layout_without_startup_migration(runner, isolated_notebooklm_home):
    home = isolated_notebooklm_home
    _write_json(
        home / "storage_state.json",
        _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}]),
    )

    data = _invoke_json(runner, ["--storage", str(home / "storage_state.json")], exit_code=1)

    assert data["checks"]["migration"] == {
        "status": "fail",
        "detail": "legacy layout detected",
    }
    assert data["checks"]["profile_dir"]["status"] == "fail"
    assert _auth_summary(data["checks"]["auth"]) == {
        "status": "pass",
        "detail": "local auth cookies usable (2 cookies)",
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
    assert _auth_summary(data["checks"]["auth"]) == {
        "status": "fail",
        "detail": "not authenticated",
    }


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

    assert data["checks"]["auth"]["status"] == "fail"
    assert data["checks"]["auth"]["detail"].startswith(
        "invalid storage file: Storage state must contain a 'cookies' list."
    )


def test_doctor_reports_invalid_storage_cookie_shape(runner, isolated_notebooklm_home):
    profile_dir = _make_profile(isolated_notebooklm_home)
    _write_json(profile_dir / "storage_state.json", {"cookies": {"name": "SID"}})

    data = _invoke_json(runner, [], exit_code=1)

    assert data["checks"]["auth"]["status"] == "fail"
    assert data["checks"]["auth"]["detail"].startswith(
        "invalid storage file: Storage state must contain a 'cookies' list."
    )


def test_doctor_reports_cookies_missing_sid(runner, isolated_notebooklm_home):
    profile_dir = _make_profile(isolated_notebooklm_home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "HSID", "value": "x"}]))

    data = _invoke_json(runner, [], exit_code=1)

    assert _auth_summary(data["checks"]["auth"]) == {
        "status": "fail",
        "detail": "SID cookie missing",
    }


def test_doctor_warns_when_psidts_missing(runner, isolated_notebooklm_home):
    """SID but no __Secure-1PSIDTS warns (not fails) and keeps exit 0 (issue #1753)."""
    home = isolated_notebooklm_home
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))
    _write_json(home / "config.json", {"default_profile": "default"})

    # A lone SID is a warn, not a failure, so doctor still exits 0.
    data = _invoke_json(runner, [], exit_code=0)

    assert data["checks"]["auth"]["status"] == "warn"
    assert "__Secure-1PSIDTS missing" in data["checks"]["auth"]["detail"]


def test_doctor_text_mode_does_not_greenlight_auth_warn(runner, isolated_notebooklm_home):
    """Text mode must not print 'All checks passed.' when auth only warns (#1753).

    A warn keeps the exit code at 0, but the green all-passed footer would
    greenlight the exact unusable (SID-without-__Secure-1PSIDTS) state doctor is
    meant to surface — so an auth-specific advisory is rendered instead.
    """
    home = isolated_notebooklm_home
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))
    _write_json(home / "config.json", {"default_profile": "default"})

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "All checks passed" not in result.output
    assert "raised a warning" in result.output


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
    assert data["checks"]["profile_dir"]["status"] == "pass"
    assert str(profile_dir) in data["checks"]["profile_dir"]["detail"]
    if sys.platform != "win32":
        assert profile_dir.stat().st_mode & 0o777 == 0o700
        assert data["checks"]["profile_dir"]["detail"] == str(profile_dir)
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
        ["--storage", str(home / "storage_state.json"), "doctor", "--fix", "--json"],
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
    for name, check in data["checks"].items():
        expected = (
            {"status", "detail", "source", "scope", "guidance"}
            if name == "auth"
            else {"status", "detail"}
        )
        assert set(check) == expected
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


@pytest.mark.parametrize("error_type", [ValueError, OSError, RuntimeError])
def test_doctor_headless_reauth_degrades_to_warn_on_profile_resolution_error(
    runner, isolated_notebooklm_home, monkeypatch, error_type
):
    """A read-only diagnostic must not crash if the profile dir cannot resolve.

    Path resolution can raise ``ValueError`` / ``OSError`` and readiness probes
    can raise ``RuntimeError``; the headless-reauth check degrades each to a
    ``warn`` row instead of bubbling up. ``monkeypatch.setattr`` on the public
    helper avoids growing the string-patch ratchet for this file.
    """
    from notebooklm.cli import doctor_cmd

    _make_profile(isolated_notebooklm_home)
    _write_json(
        isolated_notebooklm_home / "profiles" / "default" / "storage_state.json",
        _storage([{"name": "SID", "value": "x"}]),
    )

    def _boom(*_a, **_k):
        raise error_type("malformed profile name")

    monkeypatch.setattr(doctor_cmd, "get_browser_profile_dir", _boom)

    data = _invoke_json(runner, [])

    assert data["checks"]["headless_reauth"]["status"] == "warn"
    assert "could not resolve the browser profile" in data["checks"]["headless_reauth"]["detail"]
    # The error type is surfaced, never a raw path / value.
    assert error_type.__name__ in data["checks"]["headless_reauth"]["detail"]


def test_doctor_headless_reauth_uses_live_storage_and_profile(runner, isolated_notebooklm_home):
    """The L3 readiness row resolves the root command's auth identity."""
    browser_profile = isolated_notebooklm_home / "custom-browser"
    (browser_profile / "Default").mkdir(parents=True)
    storage = isolated_notebooklm_home / "custom.json"

    with patch.object(
        doctor_cmd_module,
        "get_browser_profile_dir",
        return_value=browser_profile,
    ) as resolve_browser_profile:
        data = _invoke_json(
            runner,
            ["--profile", "work", "--storage", str(storage)],
            exit_code=1,
        )

    resolve_browser_profile.assert_called_once_with(
        profile="work",
        storage_path=storage.resolve(),
    )
    assert data["checks"]["headless_reauth"]["status"] in {"pass", "warn"}


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
    _write_json(
        profile_dir / "storage_state.json",
        _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}]),
    )
    _write_json(home / "config.json", {"default_profile": "default"})

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "✗ fail" not in result.output
    assert "All checks passed" not in result.output
    assert "Online authentication was not tested" in " ".join(result.output.split())
    assert "auth check --test --passive" in " ".join(result.output.split())


def test_doctor_all_local_passes_do_not_claim_an_online_session(
    runner, isolated_notebooklm_home, monkeypatch
):
    profile_dir = _make_profile(isolated_notebooklm_home)
    _write_json(
        profile_dir / "storage_state.json",
        _storage([{"name": "SID", "value": "opaque"}, {"name": "__Secure-1PSIDTS", "value": "y"}]),
    )
    monkeypatch.setattr(
        doctor_cmd_module,
        "_headless_reauth_check",
        lambda: {"status": "pass", "detail": "ready"},
    )

    result = runner.invoke(cli, ["doctor"])
    output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "No local failures detected. Online authentication was not tested." in output
    assert "All checks passed" not in output
    assert "Authentication source:" in output


@pytest.mark.parametrize("inline_usable", [False, True])
def test_doctor_inspects_inline_auth_instead_of_disk(
    runner, isolated_notebooklm_home, monkeypatch, inline_usable
):
    profile_dir = _make_profile(isolated_notebooklm_home)
    valid = _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}])
    ambient = _storage([{"name": "NID", "value": "ambient"}])
    _write_json(profile_dir / "storage_state.json", ambient if inline_usable else valid)
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(valid if inline_usable else ambient))

    data = _invoke_json(runner, [], exit_code=0 if inline_usable else 1)

    auth = data["checks"]["auth"]
    assert auth["status"] == ("pass" if inline_usable else "fail")
    assert auth["source"] == "NOTEBOOKLM_AUTH_JSON"
    assert auth["scope"] == "local only; online authentication not tested"


@pytest.mark.parametrize("disk_usable", [False, True])
def test_doctor_explicit_storage_overrides_inline_auth(
    runner, isolated_notebooklm_home, monkeypatch, disk_usable
):
    _make_profile(isolated_notebooklm_home)
    valid = _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}])
    ambient = _storage([{"name": "NID", "value": "ambient"}])
    storage = isolated_notebooklm_home / "selected.json"
    _write_json(storage, valid if disk_usable else ambient)
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(ambient if disk_usable else valid))

    data = _invoke_json(runner, ["--storage", str(storage)], exit_code=0 if disk_usable else 1)

    assert data["checks"]["auth"]["status"] == ("pass" if disk_usable else "fail")
    assert data["checks"]["auth"]["source"] == f"file ({storage})"


@pytest.mark.parametrize("invalid_json", ["", "  ", "not json", "[]"])
def test_doctor_invalid_inline_auth_never_falls_back_or_recommends_disk_login(
    runner, isolated_notebooklm_home, monkeypatch, invalid_json
):
    profile_dir = _make_profile(isolated_notebooklm_home)
    _write_json(
        profile_dir / "storage_state.json",
        _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}]),
    )
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", invalid_json)

    data = _invoke_json(runner, [], exit_code=1)
    text = runner.invoke(cli, ["doctor"])

    assert data["checks"]["auth"]["status"] == "fail"
    assert data["checks"]["auth"]["source"] == "NOTEBOOKLM_AUTH_JSON"
    assert data["checks"]["auth"]["guidance"] == "replace_inline_auth"
    assert "Replace NOTEBOOKLM_AUTH_JSON" in " ".join(text.output.split())
    assert "login" not in text.output
    assert "--passive" not in text.output
    assert "To test this auth source online" not in text.output


@pytest.mark.parametrize(
    ("inline", "payload"),
    [
        pytest.param(False, None, id="missing-file"),
        pytest.param(False, "not json", id="invalid-file"),
        pytest.param(False, json.dumps(_storage([])), id="file-missing-SID"),
        pytest.param(True, "", id="empty-inline"),
        pytest.param(True, "not json", id="invalid-inline"),
        pytest.param(True, json.dumps(_storage([])), id="inline-missing-SID"),
    ],
)
def test_doctor_failed_auth_keeps_local_remediation_without_advertising_passive_probe(
    runner, isolated_notebooklm_home, monkeypatch, inline, payload
):
    profile_dir = _make_profile(isolated_notebooklm_home, "work")
    if inline:
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", payload)
    elif payload is not None:
        (profile_dir / "storage_state.json").write_text(payload, encoding="utf-8")
    monkeypatch.setattr(
        doctor_cmd_module,
        "_headless_reauth_check",
        lambda: {"status": "pass", "detail": "ready"},
    )

    data = _invoke_json(runner, ["--profile", "work"], exit_code=1)
    result = runner.invoke(cli, ["--profile", "work", "doctor"])
    output = " ".join(result.output.split())

    assert result.exit_code == 1, result.output
    assert data["checks"]["auth"]["status"] == "fail"
    assert "--passive" not in output
    assert "To test this auth source online" not in output
    if inline:
        assert "Replace NOTEBOOKLM_AUTH_JSON" in output
        assert "notebooklm --profile work login" not in output
    else:
        assert "notebooklm --profile work login" in output


@pytest.mark.parametrize("fix", [False, True])
def test_doctor_inline_auth_passes_without_creating_profile(
    runner, isolated_notebooklm_home, monkeypatch, fix
):
    monkeypatch.setenv(
        "NOTEBOOKLM_AUTH_JSON",
        json.dumps(
            _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}])
        ),
    )
    args = ["doctor", "--json"]
    if fix:
        args.append("--fix")

    result = runner.invoke(cli, args)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["checks"]["auth"]["status"] == "pass"
    assert data["checks"]["profile_dir"]["detail"] == "not required for inline authentication"
    assert "not applicable" in data["checks"]["headless_reauth"]["detail"]
    assert not (isolated_notebooklm_home / "profiles").exists()


def test_doctor_inline_credentials_are_absent_from_repr_and_output(
    runner, isolated_notebooklm_home, monkeypatch
):
    sentinel = "synthetic-private-cookie-value"
    monkeypatch.setenv(
        "NOTEBOOKLM_AUTH_JSON",
        json.dumps(
            _storage(
                [
                    {"name": "SID", "value": sentinel},
                    {"name": "__Secure-1PSIDTS", "value": "y"},
                ]
            )
        ),
    )

    bundle = doctor_cmd_module._doctor_paths()
    assert sentinel not in repr(bundle)
    assert bundle.read_auth_state is not None
    assert sentinel not in repr(bundle.read_auth_state)
    for output_args in (["doctor"], ["doctor", "--json"]):
        result = runner.invoke(cli, output_args)
        assert result.exit_code == 0, result.output
        assert sentinel not in result.output


@pytest.mark.parametrize("selector", ["flag", "env"])
def test_doctor_checks_selected_profile_and_preserves_it_in_guidance(
    runner, isolated_notebooklm_home, monkeypatch, selector
):
    _write_json(_make_profile(isolated_notebooklm_home) / "storage_state.json", _storage([]))
    selected = _make_profile(isolated_notebooklm_home, "work") / "storage_state.json"
    _write_json(selected, _storage([{"name": "SID", "value": "x"}]))
    if selector == "env":
        monkeypatch.setenv("NOTEBOOKLM_PROFILE", "work")
        args = []
    else:
        args = ["--profile", "work"]

    data = _invoke_json(runner, args)
    result = runner.invoke(cli, [*args, "doctor"])
    output = " ".join(result.output.split())

    assert data["checks"]["auth"]["source"] == f"file ({selected})"
    assert data["checks"]["auth"]["status"] == "warn"
    assert "notebooklm --profile work login" in output
    assert "notebooklm --profile work auth check --test" in output
    assert "--passive" not in output


def test_doctor_preserves_storage_override_in_online_and_login_guidance(
    runner, isolated_notebooklm_home
):
    _make_profile(isolated_notebooklm_home)
    storage = isolated_notebooklm_home / "auth with spaces.json"
    _write_json(storage, _storage([{"name": "SID", "value": "x"}]))

    result = runner.invoke(cli, ["--storage", str(storage), "doctor"])
    output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    quoted_storage = f"'{storage}'"
    selector = f"notebooklm --storage {quoted_storage}"
    assert f"{selector} login" in output
    assert f"{selector} auth check --test" in output
    assert "--passive" not in output


def test_doctor_source_command_quotes_windows_storage_paths_for_powershell():
    report = doctor_cmd_module.DoctorReport(profile="default", profile_source="default", checks={})
    auth = doctor_cmd_module.AuthSource(
        storage_override=Path(r"C:\Users\A User\storage state.json"),
        profile="ignored",
        has_env_auth=False,
    )

    command = doctor_cmd_module._source_command(
        report, auth, "auth", "check", "--test", "--passive", platform="win32"
    )

    assert command == (
        r"notebooklm --storage 'C:\Users\A User\storage state.json' auth check --test --passive"
    )
    assert "--profile" not in command


def test_doctor_source_command_quotes_windows_ampersand_without_spaces():
    report = doctor_cmd_module.DoctorReport(profile="default", profile_source="default", checks={})
    auth = doctor_cmd_module.AuthSource(
        storage_override=Path(r"C:\Users\R&D\auth.json"), profile=None, has_env_auth=False
    )

    command = doctor_cmd_module._source_command(report, auth, "login", platform="win32")

    assert command == r"notebooklm --storage 'C:\Users\R&D\auth.json' login"


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        (r"C:\Users\%TEMP%\auth.json", r"'C:\Users\%TEMP%\auth.json'"),
        (r"C:\Users\R&D\auth.json", r"'C:\Users\R&D\auth.json'"),
        (r"C:\Users\A User\auth.json", r"'C:\Users\A User\auth.json'"),
        (r"C:\Users\O'Brien\auth`$name.json", r"'C:\Users\O''Brien\auth`$name.json'"),
        (r"C:\Users\O’Neil\auth.json", r"'C:\Users\O’’Neil\auth.json'"),
        ("name‘a’b‚c‛d", "'name‘‘a’’b‚‚c‛‛d'"),
        ("C:\\R&D\\", r"'C:\R&D\'"),
        ("%TEMP%", "'%TEMP%'"),
        ("", "''"),
        ("work-prod", "work-prod"),
    ],
)
def test_powershell_command_arg_preserves_literal_values(value, quoted):
    assert doctor_cmd_module._powershell_command_arg(value) == quoted


def test_doctor_source_command_quotes_windows_variable_like_profile():
    report = doctor_cmd_module.DoctorReport(profile="default", profile_source="default", checks={})
    auth = doctor_cmd_module.AuthSource(storage_override=None, profile="%TEMP%", has_env_auth=False)

    command = doctor_cmd_module._source_command(report, auth, "login", platform="win32")

    assert command == "notebooklm --profile '%TEMP%' login"


@pytest.fixture(
    params=[
        (
            "test_authentication",
            [
                ["auth", "check", "--test", "--passive"],
                ["login"],
                ["doctor", "--fix"],
                ["doctor", "--fix"],
            ],
        ),
        (
            "recover_file_authentication",
            [
                ["auth", "check", "--test"],
                ["login"],
                ["doctor", "--fix"],
                ["doctor", "--fix"],
            ],
        ),
        (
            "refresh_authentication",
            [
                ["login"],
                ["doctor", "--fix"],
                ["doctor", "--fix"],
            ],
        ),
    ],
    ids=["passive", "recovery", "login"],
)
def doctor_command_hint_case(request):
    guidance, commands = request.param
    report = doctor_cmd_module.DoctorReport(
        profile="default",
        profile_source="default",
        checks={
            "migration": {"status": "fail", "detail": "legacy layout"},
            "profile_dir": {"status": "fail", "detail": "missing profile directory"},
            "auth": {
                "status": "fail" if guidance == "refresh_authentication" else "warn",
                "detail": "synthetic auth diagnostic",
                "guidance": guidance,
            },
        },
    )
    return report, commands


def test_doctor_windows_prints_copyable_commands_on_unwrapped_lines(
    capsys, doctor_command_hint_case
):
    report, commands = doctor_command_hint_case
    storage = Path(r"C:\Users\R&D\%TEMP%\O'Brien\O’Neil\name‘a’b‚c‛d\auth space`$name.json")
    auth = doctor_cmd_module.AuthSource(storage_override=storage, profile=None, has_env_auth=False)

    doctor_cmd_module._display_results(report, auth=auth, platform="win32")
    output = capsys.readouterr().out
    command_lines = [line for line in output.splitlines() if line.startswith("notebooklm ")]

    prefix = (
        r"notebooklm --storage 'C:\Users\R&D\%TEMP%\O''Brien\O’’Neil\name‘‘a’’b‚‚c‛‛d"
        r"\auth space`$name.json'"
    )
    assert command_lines == [f"{prefix} {' '.join(args)}" for args in commands]
    assert all(len(line) > 80 for line in command_lines)
    assert "in PowerShell" in " ".join(output.split())


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell argv probe is a Windows check")
def test_windows_printed_powershell_commands_roundtrip_synthetic_argv(
    capsys, doctor_command_hint_case
):
    import base64
    import shutil
    import subprocess

    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell is not None, "The supported Windows test runner must provide PowerShell"
    report, commands = doctor_command_hint_case
    storage = Path(r"C:\Users\R&D\%TEMP%\O'Brien\O’Neil\name‘a’b‚c‛d\auth space`$name.json")
    auth = doctor_cmd_module.AuthSource(storage_override=storage, profile=None, has_env_auth=False)
    doctor_cmd_module._display_results(report, auth=auth, platform="win32")
    command_lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("notebooklm ")
    ]
    assert len(command_lines) == len(commands)
    script = (
        "function notebooklm {\n"
        "  $json = ConvertTo-Json -InputObject @($args) -Compress\n"
        "  [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))\n"
        "}\n" + "\n".join(command_lines)
    )

    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    actual_args = [
        json.loads(base64.b64decode(line).decode("utf-8")) for line in result.stdout.splitlines()
    ]
    assert actual_args == [["--storage", str(storage), *args] for args in commands]


@pytest.mark.parametrize("guidance", ["refresh_authentication", "recover_file_authentication"])
def test_doctor_windows_hints_explicitly_name_powershell(capsys, guidance):
    report = doctor_cmd_module.DoctorReport(
        profile="default",
        profile_source="default",
        checks={
            "migration": {"status": "fail", "detail": "legacy layout"},
            "profile_dir": {"status": "fail", "detail": "missing profile directory"},
            "auth": {
                "status": "fail",
                "detail": "SID missing",
                "guidance": guidance,
            },
        },
    )
    auth = doctor_cmd_module.AuthSource(
        storage_override=Path(r"C:\Users\%TEMP%\auth.json"), profile=None, has_env_auth=False
    )

    doctor_cmd_module._display_results(report, auth=auth, platform="win32")
    output = " ".join(capsys.readouterr().out.split())

    if guidance == "refresh_authentication":
        assert "Re-run in PowerShell" in output
        assert "auth check --test" not in output
    else:
        assert "re-run in PowerShell" in output
        assert "auth check --test" in output
        assert "--passive" not in output
    assert "Run in PowerShell" in output
    assert "run in PowerShell" in output
    assert "doctor --fix" in output
    assert "login" in output


def test_doctor_renders_bracketed_check_details_literally(capsys):
    report = doctor_cmd_module.DoctorReport(
        profile="default",
        profile_source="default",
        checks={"profile_dir": {"status": "pass", "detail": "profile/[backup]/data"}},
    )
    auth = doctor_cmd_module.AuthSource(storage_override=None, profile=None, has_env_auth=False)

    doctor_cmd_module._display_results(report, auth=auth)

    assert "profile/[backup]/data" in capsys.readouterr().out


def test_doctor_renders_bracketed_profile_source_and_fix_messages_literally(capsys):
    fixes = [
        "Created profile directory: /tmp/[backup]/work",
        "Fixed permissions on /tmp/[backup]/work",
    ]
    report = doctor_cmd_module.DoctorReport(
        profile="work[backup]",
        profile_source="config[default]",
        checks={},
        fixes_applied=fixes,
    )
    auth = doctor_cmd_module.AuthSource(storage_override=None, profile=None, has_env_auth=False)

    doctor_cmd_module._display_results(report, auth=auth)
    output = capsys.readouterr().out

    profile_row = next(line for line in output.splitlines() if "Profile" in line)
    assert "work[backup]" in profile_row
    assert "source: config[default]" in profile_row
    for fix in fixes:
        assert fix in output


def test_doctor_preserves_emoji_like_posix_paths_in_details_sources_commands_and_fixes(capsys):
    directory = "/tmp/:key:/[backup]"
    storage = Path(f"{directory}/auth state.json")
    fixes = [f"Created profile directory: {directory}", f"Fixed permissions on {directory}"]
    report = doctor_cmd_module.DoctorReport(
        profile="work:key:",
        profile_source="config:key:",
        checks={"auth": {"status": "pass", "detail": directory, "source": f"file ({storage})"}},
        fixes_applied=fixes,
    )
    auth = doctor_cmd_module.AuthSource(storage_override=storage, profile=None, has_env_auth=False)

    doctor_cmd_module._display_results(report, auth=auth, platform="linux")
    output = capsys.readouterr().out

    profile_row = next(line for line in output.splitlines() if "Profile" in line)
    auth_row = next(line for line in output.splitlines() if "Auth" in line)
    assert "work:key:" in profile_row
    assert "source: config:key:" in profile_row
    assert directory in auth_row
    assert f"Authentication source: file ({storage})" in " ".join(output.split())
    assert f"notebooklm --storage '{storage}' auth check --test --passive" in output.splitlines()
    for fix in fixes:
        assert fix in output


@pytest.mark.parametrize("inline", [False, True], ids=["file", "inline"])
@pytest.mark.parametrize(
    "psidts_fields",
    [{"expires": 1}, {"domain": "notebooklm.google.com"}],
    ids=["expired", "other-app-alias"],
)
def test_doctor_name_only_complete_warning_uses_passive_before_auth_replacement(
    runner, isolated_notebooklm_home, monkeypatch, inline, psidts_fields
):
    cookies = [
        {"name": "SID", "value": "x"},
        {"name": "__Secure-1PSIDTS", "value": "y", **psidts_fields},
    ]
    state = _storage(cookies)
    if inline:
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(state))
    else:
        profile_dir = _make_profile(isolated_notebooklm_home)
        _write_json(profile_dir / "storage_state.json", state)
    monkeypatch.setattr(
        doctor_cmd_module,
        "_headless_reauth_check",
        lambda: {"status": "pass", "detail": "ready"},
    )

    data = _invoke_json(runner, [])
    result = runner.invoke(cli, ["doctor"])
    output = " ".join(result.output.split())

    assert data["checks"]["auth"]["status"] == "warn"
    assert data["checks"]["auth"]["guidance"] == "test_authentication"
    assert result.exit_code == 0, result.output
    passive_index = output.index("auth check --test --passive")
    replacement_index = output.index("If the passive check fails")
    assert passive_index < replacement_index
    assert output.count("auth check --test --passive") == 1
    assert "Online authentication was not tested." in output
    if inline:
        assert "replace NOTEBOOKLM_AUTH_JSON" in output[replacement_index:]
        assert "Replace NOTEBOOKLM_AUTH_JSON" not in output
    else:
        assert "notebooklm --profile default login" in output[replacement_index:]
        assert "Re-run" not in output


@pytest.mark.parametrize("inline", [False, True], ids=["file", "inline"])
@pytest.mark.parametrize("fix", [False, True])
@pytest.mark.parametrize(
    "psidts_fields",
    [None, {"value": ""}, {"path": []}, {"domain": ".example.com"}],
    ids=["missing", "empty", "malformed", "disallowed-domain"],
)
def test_doctor_incomplete_export_gives_source_specific_guidance_without_side_effects(
    runner, isolated_notebooklm_home, monkeypatch, inline, fix, psidts_fields
):
    import subprocess

    import httpx

    cookies = [{"name": "SID", "value": "x"}]
    if psidts_fields is not None:
        cookies.append({"name": "__Secure-1PSIDTS", "value": "y", **psidts_fields})
    state = _storage(cookies)
    profile_dir = _make_profile(isolated_notebooklm_home, "work")
    storage = profile_dir / "storage_state.json"
    _write_json(storage, state)
    original = storage.read_bytes()
    inline_json = json.dumps(state)
    if inline:
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", inline_json)
    monkeypatch.setattr(
        doctor_cmd_module,
        "_headless_reauth_check",
        lambda: {"status": "pass", "detail": "ready"},
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("doctor must not run the recovery commands it suggests")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    args = ["--profile", "work", "doctor"]
    if fix:
        args.append("--fix")

    json_result = runner.invoke(cli, [*args, "--json"])
    text_result = runner.invoke(cli, args)
    output = " ".join(text_result.output.split())

    assert json_result.exit_code == text_result.exit_code == 0
    auth = json.loads(json_result.output)["checks"]["auth"]
    assert auth["status"] == "warn"
    assert auth["scope"] == "local only; online authentication not tested"
    assert "--passive" not in output
    assert storage.read_bytes() == original
    if inline:
        from notebooklm.cli.services.auth_source import read_env_auth_json

        assert auth["source"] == "NOTEBOOKLM_AUTH_JSON"
        assert auth["guidance"] == "replace_incomplete_inline_auth"
        assert "incomplete export" in output
        assert "no writable backing file for recovery" in output
        assert "complete export" in output
        assert "unset it to use stored profile authentication and recovery" in output
        assert "auth check --test" not in output
        assert read_env_auth_json() == inline_json
    else:
        assert auth["source"] == f"file ({storage})"
        assert auth["guidance"] == "recover_file_authentication"
        assert "notebooklm --profile work auth check --test" in output
        assert "may refresh, rotate, or update stored cookies" in output
        assert "If recovery fails" in output
        assert "notebooklm --profile work login" in output


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_doctor_source_command_keeps_posix_storage_quoting(platform):
    report = doctor_cmd_module.DoctorReport(profile="default", profile_source="default", checks={})
    auth = doctor_cmd_module.AuthSource(
        storage_override=Path("/tmp/auth with spaces.json"), profile=None, has_env_auth=False
    )

    command = doctor_cmd_module._source_command(report, auth, "login", platform=platform)

    assert command == f"notebooklm --storage '{auth.storage_override}' login"


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_doctor_source_command_preserves_profile_selector(platform):
    report = doctor_cmd_module.DoctorReport(profile="default", profile_source="default", checks={})
    auth = doctor_cmd_module.AuthSource(storage_override=None, profile="work", has_env_auth=False)

    command = doctor_cmd_module._source_command(report, auth, "login", platform=platform)

    assert command == "notebooklm --profile work login"


@pytest.mark.parametrize(
    ("auth_warn", "headless_warn", "warned_labels"),
    [
        (False, True, "Headless Reauth"),
        (True, False, "Auth"),
        (True, True, "Auth, Headless Reauth"),
    ],
)
def test_doctor_warning_footer_names_the_warned_rows(
    runner, isolated_notebooklm_home, monkeypatch, auth_warn, headless_warn, warned_labels
):
    profile_dir = _make_profile(isolated_notebooklm_home)
    cookies = [{"name": "SID", "value": "x"}]
    if not auth_warn:
        cookies.append({"name": "__Secure-1PSIDTS", "value": "y"})
    _write_json(profile_dir / "storage_state.json", _storage(cookies))
    monkeypatch.setattr(
        doctor_cmd_module,
        "_headless_reauth_check",
        lambda: {
            "status": "warn" if headless_warn else "pass",
            "detail": "unavailable" if headless_warn else "ready",
        },
    )

    result = runner.invoke(cli, ["doctor"])
    output = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert f"Local checks raised a warning ({warned_labels})." in output
    assert "Online authentication was not tested." in output
    assert "All checks passed" not in output


def test_doctor_routes_auth_cookies_to_configured_base_url(
    runner, isolated_notebooklm_home, monkeypatch
):
    profile_dir = _make_profile(isolated_notebooklm_home)
    _write_json(
        profile_dir / "storage_state.json",
        _storage(
            [
                {"name": "SID", "value": "x", "domain": "notebooklm.google.com"},
                {"name": "__Secure-1PSIDTS", "value": "y", "domain": "notebooklm.google.com"},
            ]
        ),
    )
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.google.com")

    data = _invoke_json(runner, [])

    assert data["checks"]["auth"]["status"] == "pass"


@pytest.mark.parametrize("inline", [False, True], ids=["file", "inline"])
@pytest.mark.parametrize("fix", [False, True])
def test_doctor_invalid_base_url_is_configuration_failure_without_credential_repairs(
    runner, isolated_notebooklm_home, monkeypatch, inline, fix
):
    import httpx

    profile_dir = _make_profile(isolated_notebooklm_home)
    storage = profile_dir / "storage_state.json"
    state = _storage(
        [{"name": "SID", "value": "synthetic-secret"}, {"name": "__Secure-1PSIDTS", "value": "y"}]
    )
    _write_json(storage, state)
    original = storage.read_bytes()
    inline_json = json.dumps(state)
    if inline:
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", inline_json)
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://invalid.example/")

    def forbidden(*args, **kwargs):
        raise AssertionError("doctor must not contact Google")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    args = ["doctor"]
    if fix:
        args.append("--fix")

    json_result = runner.invoke(cli, [*args, "--json"])
    text_result = runner.invoke(cli, args)

    assert json_result.exit_code == text_result.exit_code == 1
    auth = json.loads(json_result.output)["checks"]["auth"]
    assert auth["status"] == "fail"
    assert auth["detail"].startswith("invalid NotebookLM URL configuration:")
    assert auth["guidance"] == "configure_notebooklm_url"
    assert auth["scope"] == "local only; online authentication not tested"
    assert auth["source"] == ("NOTEBOOKLM_AUTH_JSON" if inline else f"file ({storage})")
    assert "Fix or unset NOTEBOOKLM_BASE_URL" in " ".join(text_result.output.split())
    assert "invalid storage file" not in text_result.output
    assert "invalid inline authentication" not in text_result.output
    # Optional browser-profile setup diagnostics can independently mention
    # login. The auth/config error must not recommend replacing credentials.
    assert "Re-run" not in text_result.output
    assert "Replace NOTEBOOKLM_AUTH_JSON" not in text_result.output
    assert "synthetic-secret" not in json_result.output + text_result.output
    assert storage.read_bytes() == original
    if inline:
        from notebooklm.cli.services.auth_source import read_env_auth_json

        assert read_env_auth_json() == inline_json


@pytest.mark.parametrize("fix", [False, True])
def test_doctor_never_fetches_refreshes_or_rotates_auth(
    runner, isolated_notebooklm_home, monkeypatch, fix
):
    import subprocess

    import httpx

    profile_dir = _make_profile(isolated_notebooklm_home)
    storage = profile_dir / "storage_state.json"
    _write_json(storage, _storage([{"name": "SID", "value": "opaque"}]))
    original = storage.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("doctor must not fetch, refresh, rotate or launch a browser")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    args = ["doctor", "--json"]
    if fix:
        args.append("--fix")

    result = runner.invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["checks"]["auth"]["status"] == "warn"
    assert storage.read_bytes() == original


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

    data = _invoke_json(
        runner,
        ["--storage", str(profile_dir / "storage_state.json")],
        exit_code=0,
    )

    assert data["checks"]["migration"]["status"] == "warn"
    assert not any(c["status"] == "fail" for c in data["checks"].values())
