"""Lint: no unauthorized call to public NotebookLMClient.rpc_call
with deprecated kwargs (_is_retry, source_path, operation_variant).

Allowlist is keyed on (relative path, enclosing function name) so it
survives line-number reordering and `pytest.warns(...)` wraps. See
ADR-007 for the file-/function-level allowlist rationale.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TESTS_ROOT.parent

_DEPRECATED_KW: frozenset[str] = frozenset(
    {
        "_is_retry",
        "source_path",
        "operation_variant",
    }
)

# Allowlist of (path-relative-to-repo, enclosing-function-name).
# Function name "*" means "any function in this file is allowed"
# (used for the new test file that exists solely to exercise the
# deprecated surface).
_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("tests/unit/test_rpc_call_public_surface.py", "*"),
        (
            "tests/unit/test_public_shims.py",
            "test_client_rpc_call_delegates_keyword_for_keyword",
        ),
    }
)

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "tests/_lint",  # this file itself contains the kwarg literals
        "tests/cassettes",  # data only
        "tests/fixtures",  # data only
    }
)


def _enclosing_function_name(tree: ast.Module, target: ast.Call) -> str | None:
    """Return the name of the FunctionDef / AsyncFunctionDef enclosing
    ``target``, or None if the call is at module scope. Uses parent
    links computed by walking the tree."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    cur: ast.AST | None = target
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(id(cur))
    return None


def _is_public_client_rpc_call(node: ast.Call) -> bool:
    """Match `<receiver>.rpc_call(...)` where the receiver is named
    like a NotebookLMClient instance, NOT a Session/_core attribute.

    Heuristic (validated against repo audit):
    - receiver Name "client" / "nbclient" / "notebook_client" -> match
    - receiver Attribute ending in ".client" (e.g. self.client.rpc_call) -> match
    - receiver Attribute ending in "._core" / "._session" / ".core" / ".session" -> SKIP
    """
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "rpc_call":
        return False
    recv = node.func.value
    if isinstance(recv, ast.Name):
        name = recv.id.lower()
        # Only flag if the name contains "client" but not "core"/"session"
        return "client" in name and "core" not in name and "session" not in name
    if isinstance(recv, ast.Attribute):
        attr = recv.attr.lower()
        return "client" in attr and "core" not in attr and "session" not in attr
    return False


def _deprecated_kwargs(node: ast.Call) -> set[str]:
    return {kw.arg for kw in node.keywords if kw.arg in _DEPRECATED_KW}


def _iter_offenders() -> list[tuple[str, int, str | None, set[str]]]:
    offenders: list[tuple[str, int, str | None, set[str]]] = []
    for root in (_REPO_ROOT / "src", _REPO_ROOT / "tests"):
        for path in root.rglob("*.py"):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if any(rel.startswith(skip + "/") for skip in _SKIP_DIRS):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and _is_public_client_rpc_call(node)):
                    continue
                kws = _deprecated_kwargs(node)
                if not kws:
                    continue
                func_name = _enclosing_function_name(tree, node)
                if (rel, "*") in _ALLOWLIST:
                    continue
                if func_name is not None and (rel, func_name) in _ALLOWLIST:
                    continue
                offenders.append((rel, node.lineno, func_name, kws))
    return offenders


def test_no_unauthorized_deprecated_public_rpc_call_kwargs() -> None:
    offenders = _iter_offenders()
    assert offenders == [], (
        "Unauthorized public client.rpc_call calls with deprecated "
        "kwargs found. Either remove the deprecated kwarg (preferred), "
        "wrap the call in pytest.warns(DeprecationWarning), or — if "
        "the call intentionally exercises the deprecated surface — "
        "add (relative_path, function_name) to _ALLOWLIST.\n"
        "Offenders:\n"
        + "\n".join(
            f"  {rel}:{lineno}  func={func!r}  kwargs={sorted(kws)}"
            for rel, lineno, func, kws in offenders
        )
    )
