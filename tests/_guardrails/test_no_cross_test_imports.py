"""Forbid one test module importing from another ``test_*`` module (issue #1445).

Regression gate for #1431. That issue extracted shared helpers out of ``test_*``
*gate* modules into ``_``-prefixed non-test modules (``_guardrails/_ast_reach_in.py``,
``_guardrails/_cassette_shape_lint.py``) so behavioural tests stop importing from
gate test modules. This gate keeps the property from creeping back: a test module
must not import a name from another pytest ``test_*`` module — a shared
helper/constant belongs in a ``_``-prefixed non-test module that both import.

Detection is by *imported module path*: an ``import`` / ``from ... import`` whose
target module's final dotted component starts with ``test_`` is a pytest test
module (``_``-prefixed helper modules never match, and ``src`` has no ``test_*``
module), so there are no false positives. The ``from . import test_sibling``
module-as-name form is intentionally not matched — it cannot be distinguished
from importing a ``test_``-prefixed *function* without resolving the target.

The allowlist is **shrink-only**: it grandfathers the deliberate lock-step pins
that predate this gate; new cross-test imports fail, and an allowlist entry whose
import has since been extracted must be removed (``test_allowlist_is_shrink_only``).
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

TESTS_ROOT = Path(__file__).resolve().parents[1]  # the ``tests/`` directory

# (importing file relative to ``tests/``, imported ``test_*`` module's final component).
# Shrink-only — do NOT add entries. Each is a deliberate pin to a static gate's
# source-of-truth constant; the clean fix is to move that constant into a
# ``_``-prefixed non-test module both import, after which the entry is removed.
_ALLOWED_CROSS_TEST_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    {
        # Behavioural Tier-1 floor pins ``LOOKUP_NAMESPACES`` to the static contract gate
        # so the two halves stay in lock-step (test_public_api_behavior.py).
        ("unit/test_public_api_behavior.py", "test_public_api_contract"),
        # v0.8.0 release gate shares PROJECT_ROOT / SRC_ROOT / V080_BREAKING_CHANGES
        # with the deprecation-coverage gate.
        ("_guardrails/test_v080_release_gate.py", "test_v080_deprecation_coverage"),
    }
)


def _imported_test_modules(tree: ast.AST) -> set[str]:
    """Return the final components of any imported ``test_*`` modules in ``tree``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            tail = node.module.rsplit(".", 1)[-1]
            if tail.startswith("test_"):
                found.add(tail)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                tail = alias.name.rsplit(".", 1)[-1]
                if tail.startswith("test_"):
                    found.add(tail)
    return found


@functools.cache
def _scan() -> frozenset[tuple[str, str]]:
    """Every (file-relative-to-tests, imported-test-module) pair in the suite.

    Cached (and returned immutable) because both gate tests call it; the test
    tree does not change within a session.
    """
    found: set[tuple[str, str]] = set()
    for path in TESTS_ROOT.rglob("*.py"):
        rel = path.relative_to(TESTS_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for module in _imported_test_modules(tree):
            found.add((rel, module))
    return frozenset(found)


def test_no_new_cross_test_module_imports() -> None:
    new = _scan() - _ALLOWED_CROSS_TEST_IMPORTS
    assert not new, (
        "test module(s) import from another test_* module — move the shared symbol "
        "into a _-prefixed non-test module that both import (issue #1431/#1445):\n"
        + "\n".join(f"  {f}  ->  {m}" for f, m in sorted(new))
    )


def test_allowlist_is_shrink_only() -> None:
    stale = _ALLOWED_CROSS_TEST_IMPORTS - _scan()
    assert not stale, (
        "allowlisted cross-test import(s) no longer exist — remove them "
        "(the allowlist is shrink-only):\n" + "\n".join(f"  {f}  ->  {m}" for f, m in sorted(stale))
    )


def test_detector_flags_each_import_form() -> None:
    sample = (
        "from test_foo import BAR\n"  # bare absolute from-import
        "import pkg.test_baz\n"  # dotted plain import
        "from a.b.test_qux import Z\n"  # dotted from-import
        "import test_bare\n"  # bare plain import
        "from .test_rel import Q\n"  # relative from-import (the allowlisted form)
    )
    assert _imported_test_modules(ast.parse(sample)) == {
        "test_foo",
        "test_baz",
        "test_qux",
        "test_bare",
        "test_rel",
    }


def test_detector_ignores_non_test_imports() -> None:
    sample = "from _guardrails._ast_reach_in import V\nimport ast\nfrom pkg import testing\n"
    assert _imported_test_modules(ast.parse(sample)) == set()
