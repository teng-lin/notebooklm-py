"""Shared AST helpers for the reach-in / runtime-import boundary lints.

This is a non-test helper module: its name does not match pytest's
``test_*.py`` collection glob (the ``_`` prefix also marks it private), so
pytest never collects it as a test. It holds the reusable AST visitors and
accessor helpers that BOTH the gate test
(:mod:`tests._guardrails.test_no_facade_reach_in`) and the
construction-behaviour test (:mod:`tests.unit.test_init_order`) need, so the
behaviour test no longer imports from another *test* module.

Only the reusable visitors / accessors live here — the gate's ``test_*``
functions and module-deletion asserts stay in
``test_no_facade_reach_in.py``.
"""

from __future__ import annotations

import ast


def _owned_attr_name(node: ast.AST, owner: str = "self") -> str | None:
    """``<owner>.<attr>`` → ``attr`` (``owner`` is the host-object name).

    ``owner`` defaults to ``"self"`` for method bodies; the client-assembly
    checks pass ``owner="client"`` because the construction seam
    (``notebooklm._client_assembly._assemble_client``) binds the instance
    to a ``client`` parameter instead of ``self``.
    """
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == owner
    ):
        return node.attr
    return None


def _assigned_owned_attr_name(node: ast.AST, owner: str = "self") -> str | None:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            attr_name = _owned_attr_name(target, owner=owner)
            if attr_name is not None:
                return attr_name
    if isinstance(node, ast.AnnAssign):
        return _owned_attr_name(node.target, owner=owner)
    return None


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Assign):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _owned_attr_assignment(
    body: list[ast.stmt], attr_name: str, owner: str = "self"
) -> tuple[int, ast.stmt]:
    for index, statement in enumerate(body):
        if _assigned_owned_attr_name(statement, owner=owner) == attr_name:
            return index, statement
    raise AssertionError(f"{owner}.{attr_name} assignment not found")


def _module_function_body(tree: ast.AST, function_name: str) -> list[ast.stmt]:
    """Body of a module-level (non-class) function definition."""
    if not isinstance(tree, ast.Module):
        raise AssertionError("expected an ast.Module")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node.body
    raise AssertionError(f"module-level function {function_name} not found")


def _facade_call_name(node: ast.AST, facade_names: set[str]) -> str | None:
    if isinstance(node, ast.Name) and node.id in facade_names:
        return node.id
    if isinstance(node, ast.Attribute):
        if node.attr in facade_names:
            return node.attr
        return _facade_call_name(node.value, facade_names)
    return None


def _facade_construction_lines(tree: ast.AST, facade_names: set[str]) -> dict[str, list[int]]:
    lines: dict[str, list[int]] = {facade_name: [] for facade_name in facade_names}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        facade_name = _facade_call_name(node.func, facade_names)
        if facade_name is not None:
            lines[facade_name].append(node.lineno)
    return {facade_name: found for facade_name, found in lines.items() if found}


def _call_keyword_value(call: ast.Call, keyword_name: str) -> ast.AST:
    for keyword in call.keywords:
        if keyword.arg == keyword_name:
            return keyword.value
    raise AssertionError(f"keyword argument {keyword_name!r} not found")


def _is_type_checking_guard(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute)
        and node.attr == "TYPE_CHECKING"
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
    )


class _RuntimeImportVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        forbidden_names: set[str],
        forbidden_modules: set[str],
    ) -> None:
        self._forbidden_names = forbidden_names
        self._forbidden_modules = forbidden_modules
        self.forbidden: list[str] = []

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_guard(node.test):
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    @staticmethod
    def _is_dunder_name(name: str) -> bool:
        return name.startswith("__") and name.endswith("__")

    @classmethod
    def _is_forbidden_module_reference(cls, name: str, forbidden_modules: set[str]) -> bool:
        if not name:
            return False

        if any(cls._is_dunder_name(part) for part in name.split(".")):
            return False

        for forbidden_module in forbidden_modules:
            if cls._is_dunder_name(forbidden_module):
                continue
            if name == forbidden_module or name.startswith(f"{forbidden_module}."):
                return True

        return False

    def visit_Import(self, node: ast.Import) -> None:
        self.forbidden.extend(
            alias.name
            for alias in node.names
            if self._is_forbidden_module_reference(alias.name, self._forbidden_modules)
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if self._is_forbidden_module_reference(module, self._forbidden_modules):
            self.forbidden.extend(f"{module}.{alias.name}" for alias in node.names)
            return

        self.forbidden.extend(
            alias.name
            for alias in node.names
            if alias.name in self._forbidden_names
            or self._is_forbidden_module_reference(alias.name, self._forbidden_modules)
        )
