"""Guards for the middleware ``RpcRequest.context`` vocabulary."""

from __future__ import annotations

import ast
from pathlib import Path

from notebooklm._middleware.context import (
    ALLOWED_RPC_CONTEXT_KEYS,
    RPC_CONTEXT_AUTH_REFRESHED,
    RPC_CONTEXT_AUTH_SNAPSHOT,
    RPC_CONTEXT_BUILD_REQUEST,
    RPC_CONTEXT_DISABLE_INTERNAL_RETRIES,
    RPC_CONTEXT_LOG_LABEL,
    RPC_CONTEXT_REFRESH_BUDGET,
    RPC_CONTEXT_RPC_METHOD,
    RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS,
)

ROOT = Path(__file__).resolve().parents[2]
# Keep this list to modules that intentionally own ``RpcRequest.context``:
# middleware implementations plus the transport entry/leaf that seeds and
# consumes context for the chain. ``_session.py`` and ``_rpc_executor.py``
# are intentionally absent; after the transport/middleware extraction they
# should not read or write request context directly.
PRODUCTION_CONTEXT_FILES = [
    *sorted((ROOT / "src/notebooklm/_middleware").glob("*.py")),
    ROOT / "src/notebooklm/_runtime/transport.py",
]


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _subscript_key(node: ast.AST) -> ast.AST:
    return node.slice


class _ContextLiteralVisitor(ast.NodeVisitor):
    """Find literal-key access against simple request-context aliases.

    This guard is intentionally narrow: it blocks accidental ad-hoc string
    keys on ``request.context`` and ``context = {...}`` literals in the
    modules above. It is not a whole-program data-flow analysis; if context
    ownership moves to another module, add that module to
    ``PRODUCTION_CONTEXT_FILES``.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.context_aliases: list[set[str]] = [set()]
        self.violations: list[str] = []

    @property
    def aliases(self) -> set[str]:
        return self.context_aliases[-1]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.context_aliases.append(set())
        self.generic_visit(node)
        self.context_aliases.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.context_aliases.append(set())
        self.generic_visit(node)
        self.context_aliases.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_context_expression(node.value):
            for target in node.targets:
                self._record_alias_target(target)
        self._record_context_dict_literal_assignment(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and self._is_context_expression(node.value):
            self._record_alias_target(node.target)
        if node.value is not None:
            self._record_context_dict_literal_assignment([node.target], node.value)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if self._is_context_expression(node.value):
            key = _literal_string(_subscript_key(node))
            if key is not None:
                self._record_key(key, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in {"get", "pop", "setdefault"}
            and self._is_context_expression(func.value)
            and node.args
        ):
            key = _literal_string(node.args[0])
            if key is not None:
                self._record_key(key, node.lineno)

        if (
            isinstance(func, ast.Attribute)
            and func.attr == "update"
            and self._is_context_expression(func.value)
        ):
            if node.args and isinstance(node.args[0], ast.Dict):
                for key_node in node.args[0].keys:
                    key = _literal_string(key_node)
                    if key is not None:
                        self._record_key(key, node.lineno)
            for keyword in node.keywords:
                if keyword.arg is not None:
                    self._record_key(keyword.arg, node.lineno)

        self.generic_visit(node)

    def _is_context_expression(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Attribute) and node.attr == "context":
            return True
        return isinstance(node, ast.Name) and node.id in self.aliases

    def _record_alias_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self.aliases.add(target.id)

    def _record_context_dict_literal_assignment(
        self,
        targets: list[ast.expr],
        value: ast.AST,
    ) -> None:
        if not any(isinstance(target, ast.Name) and target.id == "context" for target in targets):
            return
        if not isinstance(value, ast.Dict):
            return
        for key_node in value.keys:
            key = _literal_string(key_node)
            if key is not None:
                self._record_key(key, value.lineno)

    def _record_key(self, key: str, lineno: int) -> None:
        if key in ALLOWED_RPC_CONTEXT_KEYS:
            return
        relpath = self.path.relative_to(ROOT).as_posix()
        self.violations.append(f"{relpath}:{lineno}: {key!r}")


def test_allowed_rpc_context_keys_match_adr_vocabulary() -> None:
    assert {
        RPC_CONTEXT_RPC_METHOD,
        RPC_CONTEXT_DISABLE_INTERNAL_RETRIES,
        RPC_CONTEXT_BUILD_REQUEST,
        RPC_CONTEXT_LOG_LABEL,
        RPC_CONTEXT_AUTH_SNAPSHOT,
        RPC_CONTEXT_AUTH_REFRESHED,
        RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS,
        RPC_CONTEXT_REFRESH_BUDGET,
    } == ALLOWED_RPC_CONTEXT_KEYS


def test_context_literal_visitor_records_update_keyword_keys() -> None:
    tree = ast.parse(
        """
async def call(request):
    context = request.context
    context.update(ad_hoc=True, **{})
"""
    )
    visitor = _ContextLiteralVisitor(ROOT / "src/notebooklm/_middleware/core.py")

    visitor.visit(tree)

    assert visitor.violations == ["src/notebooklm/_middleware/core.py:4: 'ad_hoc'"]


def test_production_context_literal_keys_stay_in_allowed_vocabulary() -> None:
    violations: list[str] = []
    for path in PRODUCTION_CONTEXT_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _ContextLiteralVisitor(path)
        visitor.visit(tree)
        violations.extend(visitor.violations)

    assert violations == []
