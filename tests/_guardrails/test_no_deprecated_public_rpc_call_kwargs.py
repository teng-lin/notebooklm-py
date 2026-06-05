"""Lint: no unauthorized call to public NotebookLMClient.rpc_call
with deprecated kwargs (_is_retry, source_path, operation_variant).

Allowlist is keyed on (relative path, enclosing function name) so it
survives line-number reordering and `pytest.warns(...)` wraps. See
ADR-0007 for the file-/function-level allowlist rationale.
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
# Function name "*" means "any function in this file is allowed".
#
# The v0.6.0 cut removed the three deprecated public-client ``rpc_call``
# kwargs that this lint was guarding against, so the allowlist is empty:
# the public surface no longer accepts those kwargs at all, and any
# remaining static-use case would fail at type-check time rather than
# needing a runtime allowlist carve-out.
_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "tests/_guardrails",  # this file itself contains the kwarg literals
        "tests/cassettes",  # data only
        "tests/fixtures",  # data only
    }
)


def _is_public_client_rpc_call(node: ast.Call) -> bool:
    """Match `<receiver>.rpc_call(...)` where the receiver is named
    like a NotebookLMClient instance, NOT a Session/_core attribute.

    Heuristic (validated against repo audit):
    - receiver Name "client" / "nbclient" / "notebook_client" -> match
    - receiver Attribute ending in ".client" (e.g. self.client.rpc_call) -> match
    - receiver Attribute ending in "._core" / deleted session attr / ".core" / ".session" -> SKIP
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


class _OffenderCollector(ast.NodeVisitor):
    """Single-pass AST visitor that tracks the enclosing FunctionDef /
    AsyncFunctionDef while walking the tree, so each ``ast.Call`` match
    already knows its enclosing function name without rebuilding a
    parent map per call.

    Replaces the previous ``_enclosing_function_name`` helper, which
    walked the entire tree to rebuild parent links on every match
    (O(N) per offender). The visitor visits each node exactly once
    (O(N) per file) regardless of offender count.
    """

    def __init__(self, rel: str) -> None:
        super().__init__()
        self._rel = rel
        self._func_stack: list[str] = []
        self.offenders: list[tuple[str, int, str | None, set[str]]] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        try:
            self.generic_visit(node)
        finally:
            self._func_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if _is_public_client_rpc_call(node):
            kws = _deprecated_kwargs(node)
            if kws:
                # Innermost enclosing function (or None at module scope).
                func_name = self._func_stack[-1] if self._func_stack else None
                allowed_any = (self._rel, "*") in _ALLOWLIST
                allowed_func = func_name is not None and (self._rel, func_name) in _ALLOWLIST
                if not (allowed_any or allowed_func):
                    self.offenders.append((self._rel, node.lineno, func_name, kws))
        # Recurse into call args (e.g. nested calls) and keep walking.
        self.generic_visit(node)


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
            collector = _OffenderCollector(rel)
            collector.visit(tree)
            offenders.extend(collector.offenders)
    return offenders


def test_no_unauthorized_deprecated_public_rpc_call_kwargs() -> None:
    offenders = _iter_offenders()
    assert offenders == [], (
        "Unauthorized public client.rpc_call calls with removed kwargs "
        "found. The kwargs source_path, _is_retry, and operation_variant "
        "were removed from the public NotebookLMClient.rpc_call interface "
        "in v0.6.0 — any use of them now raises TypeError at runtime "
        "(not DeprecationWarning). Remove the kwarg from the call site; "
        "the canonical defaults still flow through RpcExecutor.rpc_call.\n"
        "Offenders:\n"
        + "\n".join(
            f"  {rel}:{lineno}  func={func!r}  kwargs={sorted(kws)}"
            for rel, lineno, func, kws in offenders
        )
    )
