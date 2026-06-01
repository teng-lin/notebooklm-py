"""Meta-lint: no runtime ``notebooklm._core`` imports in load-bearing modules.

Consolidates five per-file AST guards that previously lived inline in:

- ``tests/unit/test_authed_post_pipeline.py::test_authed_post_pipeline_has_no_runtime_core_imports``
- ``tests/unit/test_rpc_executor.py::test_rpc_executor_has_no_runtime_core_imports``
- ``tests/unit/test_auth_session.py::test_auth_session_has_no_runtime_client_or_core_imports``
- ``tests/unit/test_cookie_persistence.py::test_cookie_persistence_does_not_import_client_core_at_runtime``
- ``tests/unit/test_polling_registry.py::test_polling_registry_does_not_import_client_core_at_runtime``

Each banned the same shape: runtime ``import notebooklm._core`` (or
``from notebooklm import _core``) **outside** ``TYPE_CHECKING``.
``TYPE_CHECKING``-block imports are fine — they don't execute.

After PR 10 demolished ``src/notebooklm/_core.py`` the guard is
preventive: it stops a future caller from resurrecting the shim.
Single-file enforcement of a cross-cutting policy doesn't scale, so the
policy moves here and the per-file copies are deleted.

The string ``"notebooklm._core"`` still survives as ``CORE_LOGGER_NAME``
at ``src/notebooklm/_runtime/config.py`` — that's a logger name, not an
import, and is intentional. Loggers don't trigger module load.

``_auth/session.py`` additionally bans ``notebooklm.client`` (the
original per-file test caught both axes).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# (relative source path, extra fully-qualified module names also banned).
# ``notebooklm._core`` and its variants are always banned.
GUARDED_MODULES: list[tuple[str, frozenset[str]]] = [
    ("src/notebooklm/_request_types.py", frozenset()),
    ("src/notebooklm/_streaming_post.py", frozenset()),
    ("src/notebooklm/_transport_errors.py", frozenset()),
    ("src/notebooklm/_rpc_executor.py", frozenset()),
    ("src/notebooklm/_auth/session.py", frozenset({"notebooklm.client", "client"})),
    ("src/notebooklm/_cookie_persistence.py", frozenset()),
    ("src/notebooklm/_polling_registry.py", frozenset()),
]


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _inside_type_checking(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.If) and ast.unparse(node.test) in {
            "TYPE_CHECKING",
            "typing.TYPE_CHECKING",
        }:
            return True
    return False


def _is_core_module_name(name: str) -> bool:
    return name.endswith("._core")


def _scan(path: Path, extra_banned: frozenset[str]) -> list[tuple[int, str]]:
    """Return ``[(lineno, descr), …]`` for runtime forbidden imports in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents = _build_parent_map(tree)
    extra_short = {n.rsplit(".", 1)[-1] for n in extra_banned}

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if _inside_type_checking(node, parents):
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_core_module_name(alias.name) or alias.name in extra_banned:
                    violations.append((node.lineno, f"import {alias.name}"))
            continue

        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {alias.name for alias in node.names}
            level = node.level
            prefix = "." * level

            core_hit = (
                _is_core_module_name(module)
                or (module == "notebooklm" and "_core" in names)
                or (level > 0 and module == "_core")
                or (level > 0 and not module and "_core" in names)
            )
            if core_hit:
                joined = ", ".join(sorted(names))
                violations.append((node.lineno, f"from {prefix}{module} import {joined}"))

            extra_hit_module = module in extra_banned
            extra_hit_namelist = (module == "notebooklm" or (level > 0 and not module)) and (
                names & extra_short
            )
            if extra_hit_module:
                joined = ", ".join(sorted(names))
                violations.append((node.lineno, f"from {prefix}{module} import {joined}"))
            elif extra_hit_namelist:
                joined = ", ".join(sorted(names & extra_short))
                violations.append((node.lineno, f"from {prefix}{module} import {joined}"))

    return violations


@pytest.mark.parametrize(
    ("rel_path", "extra_banned"),
    GUARDED_MODULES,
    ids=[Path(p).stem for p, _ in GUARDED_MODULES],
)
def test_no_runtime_core_imports(rel_path: str, extra_banned: frozenset[str]) -> None:
    """Module must not import ``notebooklm._core`` (or other banned names) at runtime.

    ``TYPE_CHECKING``-block imports are allowed.
    """
    path = REPO_ROOT / rel_path
    violations = _scan(path, extra_banned)
    assert violations == [], f"{rel_path} has forbidden runtime imports: {violations}"
