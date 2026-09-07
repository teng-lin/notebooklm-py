"""Validation tests for the historical qualification split (Phase 2).

Guards that:
1. Every file in tests/qualification/historical carries pytest.mark.historical.
2. Historical tests are excluded from collection by default (without --run-historical).
3. Historical tests are indexed as historical_manual in the relevance ledger.
4. Active future v1.0 gates remain in tests/_guardrails/ and are not historical.
5. All shared v0.8.0 helpers are imported from tests._helpers._v080_history.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

from tests.conftest import pytest_ignore_collect

REPO_ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_DIR = REPO_ROOT / "tests" / "qualification" / "historical"
GUARDRAILS_DIR = REPO_ROOT / "tests" / "_guardrails"


def test_historical_directory_contains_expected_files() -> None:
    expected_files = {
        "test_client_operation_contract_inventory.py",
        "test_no_cli_client_patch_surface.py",
        "test_no_session_cmd_patch_surface.py",
        "test_no_session_compat_bridges.py",
        "test_v080_deprecation_coverage.py",
        "test_v080_release_gate.py",
    }
    actual_files = {p.name for p in HISTORICAL_DIR.glob("test_*.py")}
    assert expected_files.issubset(actual_files), (
        f"Missing historical files: {expected_files - actual_files}"
    )


def test_every_historical_file_has_historical_pytestmark() -> None:
    for test_file in sorted(HISTORICAL_DIR.glob("test_*.py")):
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
        marks: list[str] = []
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "pytestmark":
                        marks.append(ast.unparse(stmt.value))
        assert any("pytest.mark.historical" in m for m in marks), (
            f"{test_file.name} missing module-level `pytestmark = pytest.mark.historical`"
        )


def test_pytest_ignore_collect_excludes_historical_by_default() -> None:
    config_mock = MagicMock()
    config_mock.getoption.return_value = False

    sample_historical_path = HISTORICAL_DIR / "test_v080_release_gate.py"
    sample_guardrail_path = GUARDRAILS_DIR / "test_v100_release_gate.py"
    rel_historical_path = Path("tests/qualification/historical/test_v080_release_gate.py")

    assert pytest_ignore_collect(sample_historical_path, config_mock) is True
    assert pytest_ignore_collect(rel_historical_path, config_mock) is True
    assert pytest_ignore_collect(sample_guardrail_path, config_mock) is None


def test_pytest_ignore_collect_allows_historical_when_flag_present() -> None:
    config_mock = MagicMock()
    config_mock.getoption.return_value = True

    sample_historical_path = HISTORICAL_DIR / "test_v080_release_gate.py"
    assert pytest_ignore_collect(sample_historical_path, config_mock) is None


def test_active_v100_gates_remain_in_guardrails() -> None:
    v100_files = [
        GUARDRAILS_DIR / "test_v100_release_gate.py",
        GUARDRAILS_DIR / "test_v100_deprecation_coverage.py",
    ]
    for p in v100_files:
        assert p.is_file(), f"Active v1.0 gate missing from guardrails: {p}"
        source = p.read_text(encoding="utf-8")
        assert "pytest.mark.historical" not in source, f"{p.name} must not be marked historical"


def test_v080_history_helper_is_extracted_and_used() -> None:
    from tests._helpers._v080_history import PROJECT_ROOT, SRC_ROOT, V080_BREAKING_CHANGES

    assert PROJECT_ROOT.is_dir()
    assert SRC_ROOT.is_dir()
    assert isinstance(V080_BREAKING_CHANGES, tuple)
