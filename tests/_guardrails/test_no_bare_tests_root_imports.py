"""Forbid bare (non-``tests.``-prefixed) imports of tests-root modules (#1482).

The fragile class: a bare ``from conftest import X`` / ``from vcr_config import X``
/ ``from cassette_patterns import X`` (or ``import vcr_config``) resolves only
because some test file ran ``sys.path.insert(0, <tests dir>)``, polluting the
session ``sys.path``. It is **masked in full-suite runs** by collection ordering
but **breaks isolated single-file runs** and is an **xdist** flakiness risk (a
worker that doesn't collect a path-inserting sibling fails collection).
``conftest`` is additionally *ambiguous* — many ``conftest.py`` exist — so a bare
import can silently grab the wrong one depending on ``sys.path`` order.

The fix is to import via the fully qualified ``tests.`` package path
(``tests/__init__.py`` plus ``pythonpath = ["."]`` in ``pyproject.toml``) or a
relative import.

**Scope (deliberately narrow):** the loose tests-root ``.py`` modules
(``conftest``, ``vcr_config``, ``cassette_patterns``, ``cassette_sanitizer``) and
the top-level group dirs (``integration`` / ``unit`` / ``e2e``, e.g. a bare
``from integration.conftest import …``). The shared helper packages
(``_fixtures``, ``_helpers``, ``_guardrails``) are intentionally NOT covered by
this narrow rule; tightening bare helper imports to ``tests.*`` is a separate,
optional sweep.

Detection is by import *target* (AST), covering both module-level and
function-local imports, so an aliased or nested form cannot slip past.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

TESTS_ROOT = Path(__file__).resolve().parents[1]  # the ``tests/`` directory

#: Bare top-level import names that are fragile (resolve only via sys.path
#: pollution). A bare import whose first dotted component is one of these must
#: instead use the ``tests.``-qualified path or a relative import.
FORBIDDEN_BARE_TOP: frozenset[str] = frozenset(
    {
        "conftest",
        "vcr_config",
        "cassette_patterns",
        "cassette_sanitizer",
        "integration",
        "unit",
        "e2e",
    }
)


def _bare_violations(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, imported_module)`` for every bare tests-root import."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return []  # a syntactically-broken file is pytest collection's problem, not ours
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative import (``from . import``) — always fine
                continue
            top = (node.module or "").split(".")[0]
            if top in FORBIDDEN_BARE_TOP:
                out.append((node.lineno, node.module or ""))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_BARE_TOP:
                    out.append((node.lineno, alias.name))
    return out


def test_no_bare_tests_root_imports() -> None:
    """No test file may import a tests-root module by a bare top-level name."""
    offenders: dict[str, list[tuple[int, str]]] = {}
    for p in sorted(TESTS_ROOT.rglob("*.py")):
        violations = _bare_violations(p)
        if violations:
            offenders[str(p.relative_to(TESTS_ROOT.parent))] = violations

    assert not offenders, (
        "Bare imports of tests-root modules found — use the `tests.`-qualified "
        "path (e.g. `from tests.vcr_config import …`, `from tests.integration."
        "conftest import …`) or a relative import. Bare imports resolve only via "
        "`sys.path` pollution and break isolated / xdist collection (#1482):\n"
        + "\n".join(f"  {f}: {v}" for f, v in sorted(offenders.items()))
    )


def test_detector_flags_bare_but_allows_qualified_and_relative(tmp_path) -> None:
    """Self-test: only the bare form is flagged; `tests.*`, relative, and the
    intentionally-exempt helper packages pass."""
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "from conftest import a\n"  # bare -> flagged (line 1)
        "import vcr_config\n"  # bare -> flagged (line 2)
        "from integration.conftest import b\n"  # bare group dir -> flagged (line 3)
        "from tests.vcr_config import c\n"  # qualified -> OK
        "from .conftest import d\n"  # relative -> OK
        "from _fixtures.fake_core import e\n",  # exempt helper package -> OK
        encoding="utf-8",
    )
    assert _bare_violations(probe) == [
        (1, "conftest"),
        (2, "vcr_config"),
        (3, "integration.conftest"),
    ]
