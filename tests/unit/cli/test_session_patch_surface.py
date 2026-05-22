"""Patch-surface preservation for the ``cli/services/login`` re-export block.

The session command module (``notebooklm.cli.session_cmd`` post-T0, or
``notebooklm.cli.session`` under option-z) re-exports a handful of
internal helpers from ``cli/services/login`` so legacy test code can
monkey-patch them through the session module's namespace
(``notebooklm.cli.session_cmd._refresh_from_browser_cookies = …`` and
friends).

P3.T4 splits ``cli/services/login.py`` into a package. The split MUST
preserve every previously re-exported name so the 37+ patch sites that
target the session module's namespace keep working byte-for-byte.

This test uses a fixed golden baseline (``tests/_fixtures/
session_reexport_baseline.txt``, captured before the split) as the
single source of truth. For each name in the baseline the test asserts:

1. **Importable**: ``getattr(session_module, name)`` returns a non-None
   object.
2. **Correct type**: callable names are still ``callable()``; constant
   names retain their stable type identity (str / dict / etc.).
3. **Patchable**: ``monkeypatch.setattr(f"{session_module}.{name}", …)``
   succeeds and is observable via ``getattr`` immediately after.

The test does NOT auto-rediscover names from the new re-export block —
that would defeat preservation. The baseline fixture is the source of
truth; if a name is intentionally removed in T4, the executor must
also remove it from the fixture AND justify the removal in the PR
description.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest

_BASELINE_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "_fixtures" / "session_reexport_baseline.txt"
)


def _active_session_module_name() -> str:
    """Return the active session command module name.

    Under T0 (post-rename) the module is ``notebooklm.cli.session_cmd``.
    Under option-z (T0 dropped) it stays at ``notebooklm.cli.session``.
    Mode is detected via ``importlib.util.find_spec`` — no import needed.
    """
    if importlib.util.find_spec("notebooklm.cli.session_cmd") is not None:
        return "notebooklm.cli.session_cmd"
    if importlib.util.find_spec("notebooklm.cli.session") is not None:
        return "notebooklm.cli.session"
    raise RuntimeError(
        "Neither notebooklm.cli.session_cmd nor notebooklm.cli.session is importable."
    )


def _load_baseline_names() -> list[str]:
    if not _BASELINE_PATH.exists():
        raise RuntimeError(
            f"Golden baseline fixture missing: {_BASELINE_PATH}. "
            "Re-capture it per the phase-3.md P3.T4 spec before running this test."
        )
    return [
        line.strip()
        for line in _BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# Expected-type map for non-callable names. Functions / classes are checked
# via ``callable()``; everything in this map is checked via ``isinstance()``.
# Keep in sync with the baseline fixture: a name listed here MUST also be
# in the baseline file. If T4 adds a constant to the re-export surface,
# add it both here AND to the baseline.
_EXPECTED_CONSTANT_TYPES: dict[str, type] = {
    # Currently empty — the rev-3-era source-plan referenced
    # ``_INCLUDE_DOMAINS_ALL`` and ``_ROOKIEPY_BROWSER_ALIASES`` as
    # constants in the re-export block, but at T4 dispatch HEAD the
    # session_cmd.py re-export block contains functions only (13 names,
    # all callable). If a future change adds constants back to the
    # re-export surface, list them here with their stable type.
}


@pytest.fixture
def session_module():
    """Return the active session command module (imported fresh)."""
    name = _active_session_module_name()
    return importlib.import_module(name)


def test_baseline_fixture_is_nonempty() -> None:
    names = _load_baseline_names()
    assert names, (
        f"{_BASELINE_PATH} is empty. The fixture must list every name re-exported "
        "from the active session module's ``from .services.login import (...)`` block."
    )


@pytest.mark.parametrize("name", _load_baseline_names())
def test_name_is_importable_and_correctly_typed(name: str, session_module) -> None:
    obj = getattr(session_module, name, None)
    assert obj is not None, (
        f"{session_module.__name__}.{name} is missing — the T4 split or session re-export "
        "block dropped this name. Restore the re-export (preferred) or justify the removal "
        "in the PR description AND remove the name from the golden baseline."
    )

    expected_type = _EXPECTED_CONSTANT_TYPES.get(name)
    if expected_type is None:
        # Default: callable (function or class).
        assert callable(obj), (
            f"{session_module.__name__}.{name} is no longer callable (type={type(obj).__name__}). "
            "If this is intentional, add an entry to _EXPECTED_CONSTANT_TYPES."
        )
    else:
        assert isinstance(obj, expected_type), (
            f"{session_module.__name__}.{name} is type {type(obj).__name__}, "
            f"expected {expected_type.__name__}."
        )


@pytest.mark.parametrize("name", _load_baseline_names())
def test_name_is_monkeypatchable(
    name: str, session_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = f"{session_module.__name__}.{name}"
    sentinel = object()
    monkeypatch.setattr(target, sentinel)
    assert getattr(session_module, name) is sentinel, (
        f"monkeypatch.setattr({target!r}, …) did not take effect. The name is no longer "
        "a direct attribute of the session module — it may have been replaced by a "
        "descriptor, a property, or a module-level __getattr__ that ignores writes."
    )
