"""Guardrail: the MCP and REST source adapters share ONE source-policy definition.

The batch/wait caps and the fatal-vs-isolate classifier live in the transport-neutral
``_app`` core (``_app.source_batch`` / ``_app.source_wait``). This gate forbids either
adapter from re-declaring the cap constants locally (which is how they drifted before —
the MCP copy swallowed fatal errors and skipped the caps) and pins that both consult
the same shared ``batch_item_is_fatal``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "notebooklm"

_POLICY_NAMES = frozenset(
    {
        "MAX_BATCH_URLS",
        "MAX_WAIT_TIMEOUT",
        "MAX_WAIT_SOURCE_IDS",
    }
)

# The adapter modules that must IMPORT the policy, never re-declare it.
_ADAPTERS = [
    _SRC / "server" / "routes" / "sources.py",
    _SRC / "mcp" / "tools" / "sources.py",
    _SRC / "mcp" / "tools" / "_waitagg.py",
]

# The _app modules that are the sole definers.
_DEFINERS = [
    _SRC / "_app" / "source_batch.py",
    _SRC / "_app" / "source_wait.py",
]


def _module_level_assigned_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:  # module level only — imports/aliases are ImportFrom, not Assign
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imported_names(path: Path) -> set[str]:
    """Names an ``ImportFrom`` binds into the module namespace (respecting ``as``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _local_def_names(path: Path) -> set[str]:
    """Function/assignment names DEFINED locally in the module (a fork, not an import)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    return names | _module_level_assigned_names(path)


@pytest.mark.parametrize("adapter", _ADAPTERS, ids=lambda p: str(p.relative_to(_SRC)))
def test_adapters_do_not_redeclare_the_cap_policy(adapter: Path) -> None:
    """An adapter re-`MAX_* = ...` assignment (drift risk) fails here; imports are fine."""
    redeclared = _module_level_assigned_names(adapter) & _POLICY_NAMES
    assert not redeclared, (
        f"{adapter.relative_to(_SRC)} re-declares source-policy caps {sorted(redeclared)}; "
        "import them from _app.source_batch / _app.source_wait instead so MCP and REST "
        "can't drift."
    )


def test_app_defines_every_cap_exactly_once() -> None:
    """Each cap is a module-level assignment in exactly one _app definer module."""
    definers: dict[str, list[str]] = {name: [] for name in _POLICY_NAMES}
    for path in _DEFINERS:
        for name in _module_level_assigned_names(path) & _POLICY_NAMES:
            definers[name].append(path.name)
    for name, where in definers.items():
        assert len(where) == 1, f"{name} must be defined once in _app; found in {where}"


# The batch adapters that must IMPORT the fatal classifier, never fork it.
_BATCH_ADAPTERS = [
    _SRC / "server" / "routes" / "sources.py",
    _SRC / "mcp" / "tools" / "sources.py",
]


@pytest.mark.parametrize("adapter", _BATCH_ADAPTERS, ids=lambda p: str(p.relative_to(_SRC)))
def test_batch_adapters_import_the_fatal_classifier_and_dont_fork_it(adapter: Path) -> None:
    """Dependency-free (AST) no-fork guard: each batch adapter must IMPORT
    ``batch_item_is_fatal`` from ``_app.source_batch`` and must NOT define its own.

    This runs even when the ``fastmcp`` / ``fastapi`` extras are absent (the common
    ``--extra dev`` contributor install), unlike the object-identity check below —
    so a future MCP-local re-implementation of the classifier can't slip in behind a
    skipped guard.
    """
    assert "batch_item_is_fatal" in _imported_names(adapter), (
        f"{adapter.relative_to(_SRC)} must import batch_item_is_fatal from "
        "_app.source_batch (the single fatal-vs-isolate definition)."
    )
    assert "batch_item_is_fatal" not in _local_def_names(adapter), (
        f"{adapter.relative_to(_SRC)} defines its own batch_item_is_fatal — that is "
        "the fork this consolidation exists to prevent. Import it from _app.source_batch."
    )


def test_both_adapters_share_the_same_fatal_classifier() -> None:
    """Both adapters bind the exact same ``batch_item_is_fatal`` object (no fork).

    Stronger than the AST guard above (proves object identity, not just an import by
    name), but requires the transport extras — hence the skip. The AST guard is the
    always-on backstop for extra-less installs.
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("fastmcp")
    from notebooklm._app.source_batch import batch_item_is_fatal as canonical
    from notebooklm.mcp.tools import sources as mcp_sources
    from notebooklm.server.routes import sources as rest_sources

    assert mcp_sources.batch_item_is_fatal is canonical
    assert rest_sources.batch_item_is_fatal is canonical
