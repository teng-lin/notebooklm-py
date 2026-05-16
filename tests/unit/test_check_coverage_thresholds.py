"""Tests for ``scripts/check_coverage_thresholds.py``.

Covers both the original drift check and the per-file floor extension.
The script is imported via spec-loading rather than ``from
scripts.check_coverage_thresholds import main`` because ``scripts/`` is not
a package in this repo.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_coverage_thresholds.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_coverage_thresholds", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_module()


# ---------------------------------------------------------------------------
# Original Phase-1 drift check (regression guard for the existing behavior)
# ---------------------------------------------------------------------------


def _write_pyproject(tmp_path: Path, fail_under: int = 90, per_file: dict | None = None) -> Path:
    body = f"""\
[tool.coverage.report]
fail_under = {fail_under}
"""
    if per_file:
        body += "\n[tool.notebooklm.per_file_coverage_floors]\n"
        for k, v in per_file.items():
            body += f'"{k}" = {v}\n'
    p = tmp_path / "pyproject.toml"
    p.write_text(body, encoding="utf-8")
    return p


def _write_workflow(tmp_path: Path, threshold: int = 90) -> Path:
    p = tmp_path / "test.yml"
    p.write_text(
        f"jobs:\n  test:\n    steps:\n    - run: pytest --cov-fail-under={threshold}\n",
        encoding="utf-8",
    )
    return p


def test_original_drift_match(tmp_path, capsys, script):
    pp = _write_pyproject(tmp_path, fail_under=90)
    yml = _write_workflow(tmp_path, threshold=90)
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out and "90%" in out


def test_original_drift_mismatch_fails(tmp_path, capsys, script):
    pp = _write_pyproject(tmp_path, fail_under=90)
    yml = _write_workflow(tmp_path, threshold=70)
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "DRIFT" in err


# ---------------------------------------------------------------------------
# per-file floor enforcement
# ---------------------------------------------------------------------------


def _write_coverage_json(tmp_path: Path, files: dict[str, float]) -> Path:
    """Build a minimal coverage.json with the requested per-file percentages."""
    payload = {
        "files": {path: {"summary": {"percent_covered": pct}} for path, pct in files.items()}
    }
    p = tmp_path / "coverage.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_per_file_floors_all_met(tmp_path, capsys, script):
    pp = _write_pyproject(
        tmp_path,
        fail_under=90,
        per_file={"src/notebooklm/cli/doctor.py": 60, "src/notebooklm/cli/profile.py": 70},
    )
    yml = _write_workflow(tmp_path, threshold=90)
    cov = _write_coverage_json(
        tmp_path,
        {
            "src/notebooklm/cli/doctor.py": 65.0,
            "src/notebooklm/cli/profile.py": 75.0,
        },
    )
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml), "--coverage-json", str(cov)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 per-file floor(s) all met" in out


def test_per_file_floor_breach_fails_with_filename(tmp_path, capsys, script):
    pp = _write_pyproject(
        tmp_path,
        fail_under=90,
        per_file={"src/notebooklm/cli/doctor.py": 80},
    )
    yml = _write_workflow(tmp_path, threshold=90)
    cov = _write_coverage_json(tmp_path, {"src/notebooklm/cli/doctor.py": 65.0})
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml), "--coverage-json", str(cov)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "PER-FILE COVERAGE FLOOR BREACH" in err
    assert "src/notebooklm/cli/doctor.py" in err
    assert "65.00" in err
    assert "80%" in err


def test_per_file_missing_from_coverage_json_fails(tmp_path, capsys, script):
    """A guarded file with no measurement is itself a CI failure."""
    pp = _write_pyproject(
        tmp_path,
        fail_under=90,
        per_file={"src/notebooklm/cli/doctor.py": 60, "src/notebooklm/renamed.py": 60},
    )
    yml = _write_workflow(tmp_path, threshold=90)
    cov = _write_coverage_json(tmp_path, {"src/notebooklm/cli/doctor.py": 65.0})
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml), "--coverage-json", str(cov)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "MISSING" in err
    assert "src/notebooklm/renamed.py" in err


def test_per_file_no_floors_table_passes(tmp_path, capsys, script):
    pp = _write_pyproject(tmp_path, fail_under=90)  # no per_file table
    yml = _write_workflow(tmp_path, threshold=90)
    cov = _write_coverage_json(tmp_path, {"src/notebooklm/cli/doctor.py": 0.0})
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml), "--coverage-json", str(cov)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no [tool.notebooklm.per_file_coverage_floors]" in out


def test_coverage_json_missing_returns_2(tmp_path, capsys, script):
    pp = _write_pyproject(tmp_path, fail_under=90, per_file={"foo.py": 0})
    yml = _write_workflow(tmp_path, threshold=90)
    rc = script.main(
        [
            "--pyproject",
            str(pp),
            "--workflow",
            str(yml),
            "--coverage-json",
            str(tmp_path / "no-such.json"),
        ]
    )
    assert rc == 2


def test_per_file_floors_non_table_returns_2(tmp_path, capsys, script):
    """A non-table ``per_file_coverage_floors`` (string, list, etc.) is a
    misconfiguration that must not silently bypass enforcement."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        "[tool.coverage.report]\nfail_under = 90\n\n"
        '[tool.notebooklm]\nper_file_coverage_floors = "oops"\n',
        encoding="utf-8",
    )
    yml = _write_workflow(tmp_path, threshold=90)
    cov = _write_coverage_json(tmp_path, {"src/notebooklm/cli/doctor.py": 50.0})
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml), "--coverage-json", str(cov)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "must be a TOML table" in err


def test_per_file_floor_non_numeric_returns_2(tmp_path, capsys, script):
    """A non-numeric floor value (string, etc.) yields exit 2 with context, not a crash."""
    pp = _write_pyproject(
        tmp_path,
        fail_under=90,
        per_file={"src/notebooklm/cli/doctor.py": '"sixty"'},  # raw TOML — string value
    )
    yml = _write_workflow(tmp_path, threshold=90)
    cov = _write_coverage_json(tmp_path, {"src/notebooklm/cli/doctor.py": 70.0})
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml), "--coverage-json", str(cov)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "could not be compared" in err
    assert "src/notebooklm/cli/doctor.py" in err


def test_coverage_json_files_wrong_shape_returns_2(tmp_path, capsys, script):
    """``coverage.json`` with ``files`` as something other than an object → exit 2."""
    pp = _write_pyproject(tmp_path, fail_under=90, per_file={"foo.py": 0})
    yml = _write_workflow(tmp_path, threshold=90)
    bad = tmp_path / "coverage.json"
    bad.write_text(json.dumps({"files": []}), encoding="utf-8")  # list, not dict
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml), "--coverage-json", str(bad)])
    assert rc == 2
    assert "'files' must be an object map" in capsys.readouterr().err


def test_coverage_json_malformed_returns_2(tmp_path, capsys, script):
    pp = _write_pyproject(tmp_path, fail_under=90, per_file={"foo.py": 0})
    yml = _write_workflow(tmp_path, threshold=90)
    bad = tmp_path / "coverage.json"
    bad.write_text("{not-json", encoding="utf-8")
    rc = script.main(["--pyproject", str(pp), "--workflow", str(yml), "--coverage-json", str(bad)])
    assert rc == 2
    assert "malformed" in capsys.readouterr().err


def test_per_file_real_baselines_pass_against_repo_pyproject(tmp_path, capsys, script):
    """Round-trip: the floors checked into pyproject.toml pass against a fresh
    coverage.json that records the floor exact value (sanity check that the
    parser+comparator agree on the data shape that ``coverage json`` emits).
    """
    real_pp = REPO_ROOT / "pyproject.toml"
    yml = _write_workflow(tmp_path, threshold=90)
    cov = _write_coverage_json(
        tmp_path,
        {
            # Match the floors checked in — every path at exactly its floor
            # should pass (>= comparison).
            "src/notebooklm/__main__.py": 0.0,
            "src/notebooklm/cli/_firefox_containers.py": 75.0,
            "src/notebooklm/cli/doctor.py": 63.0,
            "src/notebooklm/cli/profile.py": 74.0,
            "src/notebooklm/cli/session.py": 83.0,
        },
    )
    rc = script.main(
        ["--pyproject", str(real_pp), "--workflow", str(yml), "--coverage-json", str(cov)]
    )
    assert rc == 0


def teardown_module():
    """Drop the spec-loaded module so other tests aren't affected."""
    sys.modules.pop("check_coverage_thresholds", None)
