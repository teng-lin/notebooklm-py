"""Pin notebooklm.__version__ matches pyproject.toml.

Defends against the version-gate skip hazard: if pyproject.toml is bumped
in a separate PR from the deprecation removal, gate tests skip silently.
This test makes the version-bump -> removal-active relationship a runtime
invariant, not a release-checklist item.
"""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python <3.11
    import tomli as tomllib

import pytest

import notebooklm

pytestmark = pytest.mark.repo_lint


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    parsed = tomllib.loads(pyproject.read_text("utf-8"))
    assert notebooklm.__version__ == parsed["project"]["version"]
