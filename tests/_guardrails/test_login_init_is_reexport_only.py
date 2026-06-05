"""Assert cli/services/login/__init__.py contains only re-exports.

See phase-3.md → DoD Verification Helpers for the policy specification.
"""

from __future__ import annotations

import ast
from pathlib import Path

INIT_PATH = Path("src/notebooklm/cli/services/login/__init__.py")


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_future_import(node: ast.stmt) -> bool:
    return isinstance(node, ast.ImportFrom) and node.module == "__future__"


def _is_reexport_import(node: ast.stmt) -> bool:
    # Either relative (`from .x import y`) or absolute (`from typing import TYPE_CHECKING`).
    # ImportFrom covers both; we accept the node regardless of `level`.
    return isinstance(node, ast.ImportFrom)


def _is_plain_import(node: ast.stmt) -> bool:
    return isinstance(node, ast.Import)


def _is_dunder_all(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    target = node.targets[0]
    if not (isinstance(target, ast.Name) and target.id == "__all__"):
        return False
    value = node.value
    if not isinstance(value, (ast.List, ast.Tuple)):
        return False
    return all(isinstance(elt, ast.Constant) and isinstance(elt.value, str) for elt in value.elts)


def _is_alias_reexport(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    target = node.targets[0]
    if not isinstance(target, ast.Name) or target.id == "__all__":
        return False  # `__all__` is handled by _is_dunder_all
    return isinstance(node.value, ast.Name)


def _is_type_checking_block(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"):
        return False
    if node.orelse:
        return False  # no else branch
    return all(_node_allowed(child) for child in node.body)


def _node_allowed(node: ast.stmt) -> bool:
    return (
        _is_docstring(node)
        or _is_future_import(node)
        or _is_reexport_import(node)
        or _is_plain_import(node)
        or _is_dunder_all(node)
        or _is_alias_reexport(node)
        or _is_type_checking_block(node)
    )


def test_login_init_reexport_only() -> None:
    if not INIT_PATH.exists():
        # Package not yet split (e.g. pre-T4 baseline run). Skip gracefully —
        # the policy only applies once the package exists.
        import pytest

        pytest.skip(f"{INIT_PATH} does not exist yet (pre-split state)")
    tree = ast.parse(INIT_PATH.read_text(encoding="utf-8"))
    offenders = [
        (node.lineno, type(node).__name__) for node in tree.body if not _node_allowed(node)
    ]
    assert not offenders, (
        f"{INIT_PATH} contains disallowed node(s): {offenders}. "
        f"See phase-3.md → DoD Verification Helpers for the re-export-only policy."
    )
