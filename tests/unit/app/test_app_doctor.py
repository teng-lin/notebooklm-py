"""Tests for ``notebooklm._app.doctor`` (transport-neutral doctor core).

This is net-new **direct** coverage of the previously app-untested doctor
core: it drives :func:`run_checks` against a typed :class:`DoctorReport` and
asserts on ``report.checks[...]`` ``{"status", "detail"}`` strings, the
``profile`` / ``profile_source`` projection, ``fixes_applied``, and
``has_failures`` — no Click / ``CliRunner``.

The four checks need five injected path helpers (a :class:`DoctorPaths`
bundle). Rather than hand-roll fakes, the helpers are the real
``notebooklm.paths`` resolvers pointed at a per-test ``NOTEBOOKLM_HOME`` tmp
dir — exactly what ``cli/doctor_cmd.py`` forwards — so these tests exercise the
same resolution + check logic the CLI does, one layer below the CliRunner. The
``--json`` envelope shape + exit-code mapping stay in
``tests/unit/cli/test_doctor.py`` (the thin CLI shell).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from notebooklm import paths
from notebooklm._app.doctor import DoctorPaths, DoctorReport, _apply_fixes, run_checks


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate the doctor core from the real profile home + cached profile state."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    paths.set_active_profile(None)
    paths._reset_config_cache()
    yield tmp_path
    paths.set_active_profile(None)
    paths._reset_config_cache()


def _doctor_paths(
    *,
    headless_reauth_check: Callable[[], dict[str, str]] | None = None,
) -> DoctorPaths:
    """Bundle the real path helpers, mirroring ``cli/doctor_cmd._doctor_paths``."""
    return DoctorPaths(
        get_path_info=paths.get_path_info,
        get_home_dir=paths.get_home_dir,
        get_profile_dir=paths.get_profile_dir,
        get_storage_path=paths.get_storage_path,
        get_config_path=paths.get_config_path,
        headless_reauth_check=headless_reauth_check or _headless_reauth_check,
    )


def _headless_reauth_check() -> dict[str, str]:
    """Mirror ``cli/doctor_cmd._headless_reauth_check`` for the neutral core.

    Uses the real readiness probe pointed at the per-test browser-profile dir,
    so these app-level tests exercise the same mapping the CLI forwards.
    """
    from notebooklm._browser.headless_reauth import headless_reauth_readiness

    readiness = headless_reauth_readiness(browser_profile=paths.get_browser_profile_dir())
    return {
        "status": "pass" if readiness.available else "warn",
        "detail": readiness.detail,
    }


def _run(
    *,
    fix: bool = False,
    platform: str | None = None,
    headless_reauth_check: Callable[[], dict[str, str]] | None = None,
) -> DoctorReport:
    return run_checks(
        fix=fix,
        paths=_doctor_paths(headless_reauth_check=headless_reauth_check),
        platform=platform,
    )


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


def _mkdir_kwargs_for_missing_profile_dir(platform: str) -> dict[str, Any]:
    """Return the ``mkdir`` kwargs ``_apply_fixes`` uses to create a missing dir.

    Driven against a mock ``profile_dir`` rather than a real one: on a POSIX
    runner the resulting directory looks identical whether or not ``mode=`` was
    passed, and a global ``Path.mkdir`` spy cannot tell the doctor's own call
    apart from the one ``paths._ensure_dir`` makes for the same path.
    """
    profile_dir = MagicMock()
    profile_dir.__str__.return_value = "/fake/profiles/default"  # type: ignore[attr-defined]
    # The create branch is guarded on the path being absent (a plain file there
    # is a different failure that --fix must not try to mkdir over).
    profile_dir.exists.return_value = False
    checks = {
        "migration": {"status": "pass", "detail": "clean (no legacy files)"},
        "profile_dir": {"status": "fail", "detail": "not found"},
    }

    _apply_fixes(
        checks,
        Path("/fake"),
        profile_dir,
        lambda: False,
        platform=platform,
    )

    profile_dir.mkdir.assert_called_once()
    return profile_dir.mkdir.call_args.kwargs


# ---------------------------------------------------------------------------
# clean / healthy layout
# ---------------------------------------------------------------------------


def test_reports_clean_profile_layout(home: Path) -> None:
    profile_dir = _make_profile(home)
    _write_json(
        profile_dir / "storage_state.json",
        _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}]),
    )
    _write_json(home / "config.json", {"default_profile": "default"})

    report = _run()

    assert report.profile == "default"
    assert report.profile_source == "config.json"
    assert report.checks["migration"] == {"status": "pass", "detail": "complete"}
    assert report.checks["auth"] == {
        "status": "pass",
        "detail": "local auth cookies present (2 cookies)",
    }
    assert report.checks["config"] == {
        "status": "pass",
        "detail": "valid (default_profile: default)",
    }
    assert not report.has_failures
    assert report.checks["profile_dir"]["status"] == "pass"
    if sys.platform != "win32":
        assert report.checks["profile_dir"]["detail"] == str(profile_dir)


# ---------------------------------------------------------------------------
# migration / profile-dir failures
# ---------------------------------------------------------------------------


def test_reports_legacy_layout_without_migration(home: Path) -> None:
    _write_json(
        home / "storage_state.json",
        _storage([{"name": "SID", "value": "x"}, {"name": "__Secure-1PSIDTS", "value": "y"}]),
    )

    report = _run()

    assert report.checks["migration"] == {"status": "fail", "detail": "legacy layout detected"}
    assert report.checks["profile_dir"]["status"] == "fail"
    # Legacy storage_state.json still carries the Tier-1 cookie set -> auth passes.
    assert report.checks["auth"] == {
        "status": "pass",
        "detail": "local auth cookies present (2 cookies)",
    }
    assert report.has_failures


def test_reports_missing_profile_dir(home: Path) -> None:
    report = _run()

    assert report.checks["migration"] == {"status": "pass", "detail": "clean (no legacy files)"}
    assert report.checks["profile_dir"] == {
        "status": "fail",
        "detail": f"{home / 'profiles' / 'default'} not found",
    }
    assert report.checks["auth"] == {"status": "fail", "detail": "not authenticated"}
    assert report.has_failures


# ---------------------------------------------------------------------------
# profile-dir permissions: the Windows carve-out (#2046)
#
# ``platform`` is injected rather than skipped so the Windows branch is covered
# on the Linux CI runners too — the whole defect was that nobody ran the
# Windows path locally.
# ---------------------------------------------------------------------------


def test_windows_profile_dir_passes_with_non_posix_mode(home: Path) -> None:
    """Windows must not be warned about POSIX bits ``paths.py`` never sets.

    The reporter in #2025 saw a permanent ``warn`` reading
    ``(permissions: 0o777, expected: 0o700)`` — describing exactly the
    ACL-inherited state ``paths._ensure_dir`` creates on purpose on Windows.
    """
    profile_dir = home / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    if sys.platform != "win32":
        # The wide mode a Windows-created dir reports; on POSIX it would warn.
        profile_dir.chmod(0o777)

    report = _run(platform="win32")

    assert report.checks["profile_dir"]["status"] == "pass"
    detail = report.checks["profile_dir"]["detail"]
    assert str(profile_dir) in detail
    # Reported as "evaluated, not applicable" — never silently hidden.
    assert "ACL" in detail
    assert "0o700" not in detail
    assert "permissions:" not in detail


def test_windows_profile_dir_still_fails_when_absent(home: Path) -> None:
    """The carve-out is permissions-only: a missing dir is still a hard fail."""
    report = _run(platform="win32")

    assert report.checks["profile_dir"] == {
        "status": "fail",
        "detail": f"{home / 'profiles' / 'default'} not found",
    }
    assert report.has_failures


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod cannot set POSIX mode bits on Windows (setup, not SUT)"
)
def test_posix_profile_dir_still_warns_on_wide_mode(home: Path) -> None:
    """POSIX behaviour is unchanged — the wide-mode warn must survive."""
    profile_dir = home / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    profile_dir.chmod(0o777)

    report = _run(platform="linux")

    assert report.checks["profile_dir"] == {
        "status": "warn",
        "detail": f"{profile_dir} (permissions: 0o777, expected: 0o700)",
    }


def test_windows_fix_creates_dir_without_claiming_a_permission_change(home: Path) -> None:
    """``--fix`` must not promise a mode it cannot apply on Windows.

    Pre-fix the directory is missing, so ``--fix`` creates it (no ``mode=``
    on Windows, mirroring ``paths._ensure_dir``) and reports only the
    creation — never a "Fixed permissions on …" line.
    """
    profile_dir = home / "profiles" / "default"

    report = _run(fix=True, platform="win32")

    assert profile_dir.is_dir()
    assert report.fixes_applied == [f"Created profile directory: {profile_dir}"]
    assert report.checks["profile_dir"]["status"] == "pass"
    assert "ACL" in report.checks["profile_dir"]["detail"]


def test_windows_fix_creates_the_dir_without_a_posix_mode() -> None:
    """Pin the actual ``mkdir`` call, not just its outcome.

    Asserting only "the directory now exists" cannot tell a Windows-correct
    ``mkdir()`` from the old ``mkdir(mode=0o700)`` — on a POSIX runner both
    produce a directory. Passing ``mode=`` on Windows is the defect: pre-3.13
    silently ignores it, 3.13+ turns it into an over-restrictive ACL. This is
    also the assertion that catches ``platform`` being dropped on the way into
    ``_apply_fixes``.
    """
    kwargs = _mkdir_kwargs_for_missing_profile_dir("win32")

    assert "mode" not in kwargs
    assert kwargs == {"parents": True, "exist_ok": True}


def test_posix_fix_creates_the_dir_with_mode_0o700() -> None:
    """The mirror assertion: POSIX must still get the restrictive mode."""
    assert _mkdir_kwargs_for_missing_profile_dir("linux") == {
        "parents": True,
        "exist_ok": True,
        "mode": 0o700,
    }


def test_windows_fix_never_reports_a_permission_repair(home: Path) -> None:
    """A wide-mode dir on Windows is already ``pass``, so nothing to "fix"."""
    profile_dir = home / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    if sys.platform != "win32":
        profile_dir.chmod(0o777)

    report = _run(fix=True, platform="win32")

    assert not any("permission" in fix.lower() for fix in report.fixes_applied)


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod cannot set POSIX mode bits on Windows (setup, not SUT)"
)
def test_posix_fix_still_repairs_wide_mode(home: Path) -> None:
    """POSIX ``--fix`` behaviour is unchanged by the Windows carve-out."""
    profile_dir = home / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    profile_dir.chmod(0o777)

    report = _run(fix=True, platform="linux")

    assert report.fixes_applied == [f"Fixed permissions on {profile_dir}"]
    assert report.checks["profile_dir"] == {"status": "pass", "detail": str(profile_dir)}
    assert profile_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(
    sys.platform == "win32", reason="chmod cannot set POSIX mode bits on Windows (setup, not SUT)"
)
def test_fix_no_longer_greenlights_a_wide_mode_dir_after_migration(home: Path) -> None:
    """Migration + a pre-existing wide-mode profile dir now repairs BOTH.

    Deliberate POSIX behaviour change. ``_apply_fixes`` used to hard-code
    ``profile_dir`` to ``pass`` after a successful migration, which reported a
    green permissions row for a directory that was actually ``0o777`` — a false
    green on the very check ``doctor`` exists to run. Re-running the check
    instead means the permissions branch sees the warn and repairs it, so the
    run reports two fixes and ends in a genuinely correct state.
    """
    profile_dir = home / "profiles" / "default"
    profile_dir.mkdir(parents=True)
    profile_dir.chmod(0o777)
    # A legacy file alongside profiles/ puts migration into its "warn" state.
    _write_json(home / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))

    report = _run(fix=True, platform="linux")

    assert f"Fixed permissions on {profile_dir}" in report.fixes_applied
    assert report.checks["profile_dir"] == {"status": "pass", "detail": str(profile_dir)}
    assert profile_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_a_file_at_the_profile_path_fails_on_every_platform(home: Path, platform: str) -> None:
    """Existence is not enough — a plain file there makes the profile unusable.

    The Windows carve-out short-circuits before the mode check, so without an
    explicit directory test it would have reported ``pass`` for a path nothing
    can ever write ``storage_state.json`` beneath.
    """
    profile_path = home / "profiles" / "default"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("not a directory", encoding="utf-8")

    report = _run(platform=platform)

    assert report.checks["profile_dir"] == {
        "status": "fail",
        "detail": f"{profile_path} exists but is not a directory",
    }
    assert report.has_failures


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_fix_leaves_a_file_at_the_profile_path_alone(home: Path, platform: str) -> None:
    """``--fix`` must not ``mkdir`` over a file, nor claim it repaired one.

    ``mkdir(exist_ok=True)`` only tolerates an existing *directory*, so an
    unguarded repair would raise ``FileExistsError`` out of ``doctor --fix``.
    Clearing the path means deleting a user file, which doctor will not do.
    """
    profile_path = home / "profiles" / "default"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text("not a directory", encoding="utf-8")

    report = _run(fix=True, platform=platform)

    assert profile_path.is_file()
    assert profile_path.read_text(encoding="utf-8") == "not a directory"
    # The whole list, not a substring probe: a stray "Fixed permissions on …"
    # would be just as wrong as a stray "Created profile directory: …".
    assert report.fixes_applied == []
    assert report.checks["profile_dir"]["status"] == "fail"


def test_platform_defaults_to_sys_platform(home: Path) -> None:
    """Omitting ``platform`` must behave exactly like passing the host's own."""
    _make_profile(home)

    assert _run().checks["profile_dir"] == _run(platform=sys.platform).checks["profile_dir"]


def test_warn_only_layout_has_no_failures(home: Path) -> None:
    """Legacy files alongside a migrated profiles/ dir is a warn, not a fail."""
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))
    _write_json(home / "context.json", {"current_notebook": "nb_123"})

    report = _run()

    assert report.checks["migration"]["status"] == "warn"
    assert not report.has_failures


# ---------------------------------------------------------------------------
# auth / storage-shape failures
# ---------------------------------------------------------------------------


def test_reports_invalid_storage_json(home: Path) -> None:
    profile_dir = _make_profile(home)
    (profile_dir / "storage_state.json").write_text("{not json", encoding="utf-8")

    report = _run()

    assert report.checks["auth"]["status"] == "fail"
    assert report.checks["auth"]["detail"].startswith("invalid storage file:")


def test_reports_invalid_storage_root_shape(home: Path) -> None:
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", [])

    report = _run()

    assert report.checks["auth"] == {
        "status": "fail",
        "detail": "invalid storage file: storage root is not an object",
    }


def test_reports_invalid_storage_cookie_shape(home: Path) -> None:
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", {"cookies": {"name": "SID"}})

    report = _run()

    assert report.checks["auth"] == {
        "status": "fail",
        "detail": "invalid storage file: cookies is not a list",
    }


def test_reports_cookies_missing_sid(home: Path) -> None:
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "HSID", "value": "x"}]))

    report = _run()

    assert report.checks["auth"] == {"status": "fail", "detail": "SID cookie missing"}


def test_warns_when_sid_present_but_psidts_missing(home: Path) -> None:
    """SID without __Secure-1PSIDTS is a warn, not a pass or a fail (issue #1753).

    The session looks authenticated (SID present) but is missing the Tier-1
    freshness partner every real RPC enforces. doctor must stop greenlighting
    it, yet not hard-fail a session that may still re-mint the cookie at
    runtime — so it lands as a warn that does not flip ``has_failures``.
    """
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))

    report = _run()

    assert report.checks["auth"]["status"] == "warn"
    assert "__Secure-1PSIDTS missing" in report.checks["auth"]["detail"]
    assert not report.has_failures


# ---------------------------------------------------------------------------
# config failures / warnings
# ---------------------------------------------------------------------------


def test_warns_when_config_default_profile_is_missing(home: Path) -> None:
    _make_profile(home)
    _write_json(home / "profiles" / "default" / "storage_state.json", _storage([]))
    _write_json(home / "config.json", {"default_profile": "missing"})

    report = _run()

    assert report.profile == "missing"
    assert report.profile_source == "config.json"
    assert report.checks["profile_dir"]["status"] == "fail"
    assert report.checks["config"] == {
        "status": "warn",
        "detail": "default_profile 'missing' does not exist",
    }


def test_reports_invalid_config_root_shape(home: Path) -> None:
    _make_profile(home)
    _write_json(home / "config.json", [])

    report = _run()

    assert report.checks["config"] == {
        "status": "fail",
        "detail": "invalid: config root is not an object",
    }


def test_config_absent_passes_with_defaults(home: Path) -> None:
    _make_profile(home)
    _write_json(home / "profiles" / "default" / "storage_state.json", _storage([]))

    report = _run()

    assert report.checks["config"] == {"status": "pass", "detail": "not present (using defaults)"}


# ---------------------------------------------------------------------------
# --fix paths
# ---------------------------------------------------------------------------


def test_fix_creates_missing_profile_dir(home: Path) -> None:
    report = _run(fix=True)

    profile_dir = home / "profiles" / "default"
    assert profile_dir.is_dir()
    assert report.checks["profile_dir"]["status"] == "pass"
    if sys.platform != "win32":
        assert profile_dir.stat().st_mode & 0o777 == 0o700
        assert report.checks["profile_dir"]["detail"] == str(profile_dir)
    # No auth was set up, so the auth check still fails after the fix.
    assert report.checks["auth"]["status"] == "fail"
    assert report.fixes_applied == [f"Created profile directory: {profile_dir}"]
    # A lingering auth failure means the install is still broken.
    assert report.has_failures


def test_fix_migrates_legacy_layout(home: Path) -> None:
    storage_payload = _storage([{"name": "SID", "value": "x"}])
    context_payload = {"current_notebook": "nb_123"}
    _write_json(home / "storage_state.json", storage_payload)
    _write_json(home / "context.json", context_payload)

    report = _run(fix=True)

    profile_dir = home / "profiles" / "default"
    assert not (home / "storage_state.json").exists()
    assert json.loads((profile_dir / "storage_state.json").read_text(encoding="utf-8")) == (
        storage_payload
    )
    assert json.loads((profile_dir / "context.json").read_text(encoding="utf-8")) == (
        context_payload
    )
    assert report.checks["migration"] == {
        "status": "pass",
        "detail": "complete (just migrated)",
    }
    assert report.fixes_applied == ["Migrated legacy layout to profiles/default/"]
    # The migrated layout carries a SID and a profile dir, so nothing fails.
    assert not report.has_failures


# ---------------------------------------------------------------------------
# report shape contract (the typed surface the CLI projects from)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# headless re-auth readiness check
# ---------------------------------------------------------------------------


def _make_browser_profile(home: Path, name: str = "default") -> Path:
    """Create a populated persistent browser-profile dir under the profile."""
    bp = home / "profiles" / name / "browser_profile"
    bp.mkdir(parents=True)
    (bp / "Default").mkdir()
    return bp


def test_headless_reauth_pass_when_profile_and_playwright(home: Path) -> None:
    """A populated browser profile + playwright present → pass row."""
    profile_dir = _make_profile(home)
    _make_browser_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))
    _write_json(home / "config.json", {"default_profile": "default"})
    report = _run(
        headless_reauth_check=lambda: {
            "status": "pass",
            "detail": "ready (persistent profile + playwright present)",
        }
    )

    assert report.checks["headless_reauth"]["status"] == "pass"
    assert "ready" in report.checks["headless_reauth"]["detail"]
    # An otherwise-healthy install with an available L3 fallback has no failures.
    assert not report.has_failures


def test_headless_reauth_warns_without_browser_profile(home: Path) -> None:
    """No persistent browser profile → warn (optional fallback unavailable)."""
    _make_profile(home)
    report = _run(
        headless_reauth_check=lambda: {
            "status": "warn",
            "detail": "unavailable: no reusable browser profile",
        }
    )

    assert report.checks["headless_reauth"]["status"] == "warn"
    assert "no reusable browser profile" in report.checks["headless_reauth"]["detail"]
    # A warn never flips the install to failing.
    assert report.checks["headless_reauth"]["status"] != "fail"


def test_headless_reauth_warn_does_not_force_failure(home: Path) -> None:
    """An unavailable L3 fallback alone must not make ``has_failures`` true."""
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))
    _write_json(home / "config.json", {"default_profile": "default"})
    report = _run(
        headless_reauth_check=lambda: {
            "status": "warn",
            "detail": "unavailable: playwright not installed",
        }
    )

    assert report.checks["headless_reauth"]["status"] == "warn"
    assert not report.has_failures


def test_report_check_set_and_shape(home: Path) -> None:
    _make_profile(home)

    report = _run()

    assert set(report.checks) == {
        "migration",
        "profile_dir",
        "auth",
        "config",
        "headless_reauth",
    }
    for check in report.checks.values():
        assert set(check) == {"status", "detail"}
        assert check["status"] in {"pass", "warn", "fail"}
        assert isinstance(check["detail"], str)
    assert report.fixes_applied == []


def test_has_failures_false_when_only_passes_and_warns(home: Path) -> None:
    """``has_failures`` keys off ``fail`` rows only — a warn must not trip it."""
    report = DoctorReport(
        profile="default",
        profile_source="default",
        checks={
            "migration": {"status": "pass", "detail": "complete"},
            "config": {"status": "warn", "detail": "default_profile 'x' does not exist"},
        },
    )
    assert report.has_failures is False


def test_has_failures_true_when_any_check_fails() -> None:
    report = DoctorReport(
        profile="default",
        profile_source="default",
        checks={
            "migration": {"status": "pass", "detail": "complete"},
            "auth": {"status": "fail", "detail": "not authenticated"},
        },
    )
    assert report.has_failures is True
