"""Derive the frozen lock-unavailable policy ownership snapshot."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUTH_ROOT = PROJECT_ROOT / "src" / "notebooklm" / "_auth"
POLICY_NAMES: tuple[str, ...] = (
    "raise_on_lock_unavailable",
    "report_on_lock_unavailable",
    "skip_on_lock_unavailable",
)


def _owned_functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module = path.stem
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found[f"{module}.{node.name}"] = node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    found[f"{module}.{node.name}.{member.name}"] = member
    return found


def _calls_bare_name(node: ast.AST, target: str) -> bool:
    return any(
        isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name) and inner.func.id == target
        for inner in ast.walk(node)
    )


def derive_storage_transaction_policy() -> dict[str, list[str]]:
    """Map each policy function to its direct callers in the storage owners."""
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for name in ("profile_store.py", "storage.py"):
        functions.update(_owned_functions(AUTH_ROOT / name))
    return {
        policy: sorted(owner for owner, node in functions.items() if _calls_bare_name(node, policy))
        for policy in POLICY_NAMES
    }


def storage_transaction_policy_growth(previous: object, current: object) -> list[str]:
    """Describe newly added policies/callers that need explicit acknowledgement."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("storage transaction policy baseline must be a JSON object")

    growth: list[str] = []
    for policy, callers in sorted(current.items()):
        if (
            not isinstance(policy, str)
            or not isinstance(callers, list)
            or not all(isinstance(caller, str) for caller in callers)
        ):
            raise ValueError("storage policy baseline must map names to string lists")
        old_callers = previous.get(policy, [])
        if not isinstance(old_callers, list) or not all(
            isinstance(caller, str) for caller in old_callers
        ):
            raise ValueError("storage policy baseline must map names to string lists")
        for caller in sorted(set(callers) - set(old_callers)):
            growth.append(f"{policy}: new caller {caller}")
    return growth


__all__ = [
    "POLICY_NAMES",
    "derive_storage_transaction_policy",
    "storage_transaction_policy_growth",
]
