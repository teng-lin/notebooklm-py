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
from collections.abc import Iterator
from pathlib import Path

import pytest

from notebooklm import paths
from notebooklm._app.doctor import DoctorPaths, DoctorReport, run_checks


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


def _doctor_paths() -> DoctorPaths:
    """Bundle the real path helpers, mirroring ``cli/doctor_cmd._doctor_paths``."""
    return DoctorPaths(
        get_path_info=paths.get_path_info,
        get_home_dir=paths.get_home_dir,
        get_profile_dir=paths.get_profile_dir,
        get_storage_path=paths.get_storage_path,
        get_config_path=paths.get_config_path,
    )


def _run(*, fix: bool = False) -> DoctorReport:
    return run_checks(fix=fix, paths=_doctor_paths())


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


# ---------------------------------------------------------------------------
# clean / healthy layout
# ---------------------------------------------------------------------------


def test_reports_clean_profile_layout(home: Path) -> None:
    profile_dir = _make_profile(home)
    _write_json(profile_dir / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))
    _write_json(home / "config.json", {"default_profile": "default"})

    report = _run()

    assert report.profile == "default"
    assert report.profile_source == "config.json"
    assert report.checks["migration"] == {"status": "pass", "detail": "complete"}
    assert report.checks["auth"] == {
        "status": "pass",
        "detail": "local SID cookie present (1 cookies)",
    }
    assert report.checks["config"] == {
        "status": "pass",
        "detail": "valid (default_profile: default)",
    }
    assert not report.has_failures
    if sys.platform == "win32":
        assert report.checks["profile_dir"]["status"] == "warn"
    else:
        assert report.checks["profile_dir"] == {"status": "pass", "detail": str(profile_dir)}


# ---------------------------------------------------------------------------
# migration / profile-dir failures
# ---------------------------------------------------------------------------


def test_reports_legacy_layout_without_migration(home: Path) -> None:
    _write_json(home / "storage_state.json", _storage([{"name": "SID", "value": "x"}]))

    report = _run()

    assert report.checks["migration"] == {"status": "fail", "detail": "legacy layout detected"}
    assert report.checks["profile_dir"]["status"] == "fail"
    # Legacy storage_state.json still carries a SID -> auth passes.
    assert report.checks["auth"] == {
        "status": "pass",
        "detail": "local SID cookie present (1 cookies)",
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
    if sys.platform != "win32":
        assert profile_dir.stat().st_mode & 0o777 == 0o700
    assert report.checks["profile_dir"] == {"status": "pass", "detail": str(profile_dir)}
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


def test_report_check_set_and_shape(home: Path) -> None:
    _make_profile(home)

    report = _run()

    assert set(report.checks) == {"migration", "profile_dir", "auth", "config"}
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
