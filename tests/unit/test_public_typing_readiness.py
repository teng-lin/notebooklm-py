"""Guardrails for the public typing-readiness contract."""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover -- only hit on Python 3.10
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"
PY_TYPED = REPO_ROOT / "src" / "notebooklm" / "py.typed"

PUBLIC_TYPING_MODULES = {
    "notebooklm",
    "notebooklm._types.*",
    "notebooklm.artifacts",
    "notebooklm.auth",
    "notebooklm.client",
    "notebooklm.config",
    "notebooklm.exceptions",
    "notebooklm.io",
    "notebooklm.log",
    "notebooklm.migration",
    "notebooklm.paths",
    "notebooklm.research",
    "notebooklm.types",
    "notebooklm.urls",
    "notebooklm.utils",
}


def _mypy_overrides() -> list[dict[str, object]]:
    data = tomllib.loads(PYPROJECT_TOML.read_text(encoding="utf-8"))
    return data["tool"]["mypy"]["overrides"]


def test_public_typing_modules_have_strict_mypy_override() -> None:
    """Public exports and moved public type implementations stay in stricter mypy coverage."""
    for override in _mypy_overrides():
        modules = override.get("module")
        if isinstance(modules, list) and PUBLIC_TYPING_MODULES.issubset(set(modules)):
            assert override.get("disallow_untyped_defs") is True
            assert override.get("disallow_any_generics") is True
            assert override.get("warn_return_any") is True
            assert override.get("strict_optional") is True
            return

    raise AssertionError("Public typing modules are missing their strict mypy override.")


def test_package_declares_pep_561_marker() -> None:
    """The package is ready to advertise inline type information to downstream checkers."""
    assert PY_TYPED.is_file()
