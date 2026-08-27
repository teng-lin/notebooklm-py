"""Inventory large module-level container literals in guardrail tests."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUARDRAIL_ROOT = PROJECT_ROOT / "tests" / "_guardrails"
LARGE_LITERAL_THRESHOLD = 20
_CONTAINER_WRAPPERS = frozenset({"dict", "frozenset", "list", "set", "tuple"})


def _target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return sorted(target.id for target in targets if isinstance(target, ast.Name))


def _is_container_literal(value: ast.AST | None) -> bool:
    if isinstance(value, ast.Dict | ast.List | ast.Set | ast.Tuple):
        return True
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _CONTAINER_WRAPPERS
    )


def inventory_large_inline_literals(
    guardrail_root: Path = GUARDRAIL_ROOT,
    *,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, dict[str, int]]:
    """Return large module-level containers as path/name -> literal-leaf count."""
    inventory: dict[str, dict[str, int]] = {}
    for path in sorted(guardrail_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assignments: dict[str, int] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign | ast.AnnAssign) or not _is_container_literal(
                node.value
            ):
                continue
            leaf_count = sum(isinstance(inner, ast.Constant) for inner in ast.walk(node.value))
            if leaf_count < LARGE_LITERAL_THRESHOLD:
                continue
            for name in _target_names(node):
                assignments[name] = leaf_count
        if assignments:
            inventory[path.relative_to(project_root).as_posix()] = assignments
    return inventory


def guardrail_literal_growth(previous: object, current: object) -> list[str]:
    """Describe new or enlarged large literals in the guardrail inventory."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("guardrail literal inventory must be a JSON object")

    growth: list[str] = []
    for path, assignments in sorted(current.items()):
        if not isinstance(path, str) or not isinstance(assignments, dict):
            raise ValueError("guardrail literal inventory must map paths to objects")
        old_assignments = previous.get(path, {})
        if not isinstance(old_assignments, dict):
            raise ValueError("guardrail literal inventory must map paths to objects")
        for name, size in sorted(assignments.items()):
            if not isinstance(name, str) or not isinstance(size, int):
                raise ValueError("guardrail literal sizes must be integers")
            old_size = old_assignments.get(name)
            if old_size is None:
                growth.append(f"{path}:{name}: new {size}-leaf literal")
            elif not isinstance(old_size, int):
                raise ValueError("guardrail literal sizes must be integers")
            elif size > old_size:
                growth.append(f"{path}:{name}: {old_size} -> {size} literal leaves")
    return growth


__all__ = [
    "LARGE_LITERAL_THRESHOLD",
    "guardrail_literal_growth",
    "inventory_large_inline_literals",
]
