"""Regression tests for the independent GitHub Actions test matrix."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.repo_lint

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "test.yml"


def test_test_matrix_is_independent_and_preserves_ci_contract():
    """The test matrix must remain an independent 3-by-5 required check."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert {"quality", "test"} <= set(jobs)
    assert jobs["quality"]["name"] == "Code Quality"
    assert jobs["test"]["name"] == "Test (${{ matrix.os }}, Python ${{ matrix.python-version }})"
    assert "needs" not in jobs["test"]
    assert jobs["test"]["strategy"]["fail-fast"] is False

    matrix = jobs["test"]["strategy"]["matrix"]
    assert matrix == {
        "os": ["ubuntu-latest", "macos-latest", "windows-latest"],
        "python-version": ["3.10", "3.11", "3.12", "3.13", "3.14"],
    }
