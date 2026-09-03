"""Fail-closed ownership and capability guard for cookie persistence."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import pytest

from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._web.transport.cookie_persistence import (
    FailedBaseline,
    ReadyBaseline,
    UninitializedBaseline,
    _PathSaveState,
)

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
PERSISTENCE_PATH = SRC_ROOT / "_web/transport/cookie_persistence.py"
LIFECYCLE_PATH = SRC_ROOT / "_runtime" / "lifecycle.py"
WEB_LIFECYCLE_PATH = SRC_ROOT / "_web" / "transport" / "lifecycle.py"
INIT_PATH = SRC_ROOT / "_web" / "transport" / "init.py"
CLIENT_PATH = SRC_ROOT / "client.py"

Call = tuple[str, str]
Escape = tuple[str, str, str]

_PRIVATE_MEMBERS = frozenset(
    {
        "_from_store",
        "register_open_baseline",
        "_prepare_open_baseline",
        "_save_canonical",
        "_save_v0_callback",
    }
)
_PERSISTENCE_FIELDS = {
    "_default_store",
    "save_lock",
    "_save_seq",
    "_states",
    "_legacy",
}
_ADAPTER_FIELDS = {"_default_key", "_auth", "_snapshots"}
_FORBIDDEN_DYNAMIC_MEMBERS = _PRIVATE_MEMBERS | {
    "merge_cookie_observation",
    "_load_cookie_pair_pure",
}
_PERSISTENCE_MODULE = "notebooklm._web.transport.cookie_persistence"

CookieSaveResultUse = tuple[str, str, str]


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _label(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix()


def _owner(classes: list[str], functions: list[str]) -> str:
    names = [*classes, *functions[:1]]
    return ".".join(names) if names else "<module>"


def _static_cookie_save_result_names(statements: Iterable[ast.stmt]) -> set[str]:
    """Resolve same-scope names assigned the exact legacy result string."""
    names: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Assign(self, node: ast.Assign) -> None:
            if isinstance(node.value, ast.Constant) and node.value.value == "CookieSaveResult":
                for target in node.targets:
                    names.update(_target_names(target))
            self.visit(node.value)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if (
                node.value is not None
                and isinstance(node.value, ast.Constant)
                and node.value.value == "CookieSaveResult"
            ):
                names.update(_target_names(node.target))
            if node.value is not None:
                self.visit(node.value)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

    visitor = Visitor()
    for statement in statements:
        visitor.visit(statement)
    return names


def _cookie_save_result_inventory(path: Path, tree: ast.Module) -> Counter[CookieSaveResultUse]:
    """Conservatively inventory every direct or qualified legacy-result spelling.

    Attribute receivers are intentionally not narrowed to known storage-module
    aliases: treating every ``*.CookieSaveResult`` as a candidate makes module
    aliases, facade aliases, and future indirect import spellings fail closed.
    """
    inventory: Counter[CookieSaveResultUse] = Counter()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.classes: list[str] = []
            self.functions: list[str] = []
            self.static_string_names = [_static_cookie_save_result_names(tree.body)]

        @property
        def owner(self) -> str:
            return _owner(self.classes, self.functions)

        def record(self, kind: str) -> None:
            inventory[(_label(path), self.owner, kind)] += 1

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if node.name == "CookieSaveResult":
                self.record("definition")
            self.classes.append(node.name)
            self.static_string_names.append(_static_cookie_save_result_names(node.body))
            self.generic_visit(node)
            self.static_string_names.pop()
            self.classes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.static_string_names.append(_static_cookie_save_result_names(node.body))
            self.generic_visit(node)
            self.static_string_names.pop()
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for alias in node.names:
                if alias.name == "CookieSaveResult":
                    self.record(f"import:{alias.asname or 'direct'}")

        def visit_Name(self, node: ast.Name) -> None:
            if node.id == "CookieSaveResult":
                self.record(f"name:{type(node.ctx).__name__.lower()}")

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr == "CookieSaveResult":
                self.record(f"attribute:{type(node.ctx).__name__.lower()}")
            self.generic_visit(node)

        def is_cookie_save_result_key(self, node: ast.AST) -> bool:
            if isinstance(node, ast.Constant):
                return node.value == "CookieSaveResult"
            return isinstance(node, ast.Name) and any(
                node.id in names for names in reversed(self.static_string_names)
            )

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "hasattr"}
                and len(node.args) >= 2
                and self.is_cookie_save_result_key(node.args[1])
            ):
                self.record(f"dynamic:{node.func.id}")
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            if self.is_cookie_save_result_key(node.slice):
                self.record("dynamic:subscript")
            self.generic_visit(node)

    Visitor().visit(tree)
    return inventory


def _cookie_save_result_branch_owners(path: Path, tree: ast.Module) -> set[tuple[str, str]]:
    owners: set[tuple[str, str]] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.classes: list[str] = []
            self.functions: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.classes.append(node.name)
            self.generic_visit(node)
            self.classes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
                and any(
                    (isinstance(argument, ast.Name) and argument.id == "CookieSaveResult")
                    or (isinstance(argument, ast.Attribute) and argument.attr == "CookieSaveResult")
                    for argument in node.args[1:]
                )
            ):
                owners.add((_label(path), _owner(self.classes, self.functions)))
            self.generic_visit(node)

    Visitor().visit(tree)
    return owners


def _target_names(node: ast.AST | None) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    if isinstance(node, ast.Tuple | ast.List):
        return set().union(*(_target_names(item) for item in node.elts), set())
    return set()


def _pattern_names(node: ast.pattern) -> set[str]:
    if isinstance(node, ast.MatchAs):
        names = _pattern_names(node.pattern) if node.pattern is not None else set()
        if node.name is not None:
            names.add(node.name)
        return names
    if isinstance(node, ast.MatchStar):
        return {node.name} if node.name is not None else set()
    if isinstance(node, ast.MatchMapping):
        names = set().union(*(_pattern_names(item) for item in node.patterns), set())
        if node.rest is not None:
            names.add(node.rest)
        return names
    if isinstance(node, ast.MatchSequence | ast.MatchOr):
        return set().union(*(_pattern_names(item) for item in node.patterns), set())
    if isinstance(node, ast.MatchClass):
        return set().union(
            *(_pattern_names(item) for item in [*node.patterns, *node.kwd_patterns]),
            set(),
        )
    return set()


def _argument_nodes(arguments: ast.arguments) -> list[ast.arg]:
    result = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    if arguments.vararg is not None:
        result.append(arguments.vararg)
    if arguments.kwarg is not None:
        result.append(arguments.kwarg)
    return result


def _resolved_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    relative = path.relative_to(REPO_ROOT / "src").with_suffix("")
    package = list(relative.parts[:-1])
    keep = len(package) - (node.level - 1)
    base = package[:keep]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


class _NamedExpressionBindings(ast.NodeVisitor):
    """Collect module-scope walrus targets without comprehension loop targets."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.names.update(_target_names(node.target))
        self.visit(node.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _named_expression_bindings(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    collector = _NamedExpressionBindings()
    collector.visit(node)
    return collector.names


def _module_persistence_bindings(path: Path, tree: ast.Module) -> dict[str, str]:
    """Resolve only unambiguous providers across every module binding path."""
    canonical: dict[str, set[str]] = {}
    invalid: set[str] = set()
    star_import = False

    def mark_canonical(name: str, kind: str) -> None:
        canonical.setdefault(name, set()).add(kind)

    def mark_invalid(names: Iterable[str]) -> None:
        invalid.update(names)

    def scan_expressions(*nodes: ast.AST | None) -> None:
        for node in nodes:
            mark_invalid(_named_expression_bindings(node))

    def scan_statements(statements: Iterable[ast.stmt]) -> None:
        for statement in statements:
            scan_statement(statement)

    def scan_statement(node: ast.stmt) -> None:
        nonlocal star_import
        if isinstance(node, ast.Import):
            for item in node.names:
                bound = item.asname or item.name.split(".", 1)[0]
                if item.name == _PERSISTENCE_MODULE:
                    mark_canonical(bound, "module" if item.asname else "root")
                else:
                    mark_invalid({bound})
            return
        if isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            for item in node.names:
                if item.name == "*":
                    star_import = True
                    continue
                bound = item.asname or item.name
                if resolved == _PERSISTENCE_MODULE and item.name == "CookiePersistence":
                    mark_canonical(bound, "class")
                elif resolved == "notebooklm" and item.name == "_cookie_persistence":
                    mark_canonical(bound, "module")
                else:
                    mark_invalid({bound})
            return
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            scan_expressions(
                *node.decorator_list,
                *node.args.defaults,
                *node.args.kw_defaults,
                node.returns,
                *(
                    argument.annotation
                    for argument in _argument_nodes(node.args)
                    if argument.annotation is not None
                ),
            )
            mark_invalid({node.name})
            return
        if isinstance(node, ast.ClassDef):
            scan_expressions(
                *node.decorator_list, *node.bases, *(item.value for item in node.keywords)
            )
            if path == PERSISTENCE_PATH and node.name == "CookiePersistence":
                mark_canonical(node.name, "class")
            else:
                mark_invalid({node.name})
            return
        if isinstance(node, ast.Assign):
            scan_expressions(node.value)
            mark_invalid(set().union(*(_target_names(target) for target in node.targets), set()))
            return
        if isinstance(node, ast.AnnAssign):
            scan_expressions(node.annotation, node.value)
            mark_invalid(_target_names(node.target))
            return
        if isinstance(node, ast.AugAssign):
            scan_expressions(node.target, node.value)
            mark_invalid(_target_names(node.target))
            return
        if isinstance(node, ast.Delete):
            mark_invalid(set().union(*(_target_names(target) for target in node.targets), set()))
            return
        if isinstance(node, ast.If | ast.While):
            scan_expressions(node.test)
            scan_statements([*node.body, *node.orelse])
            return
        if isinstance(node, ast.For | ast.AsyncFor):
            scan_expressions(node.iter)
            mark_invalid(_target_names(node.target))
            scan_statements([*node.body, *node.orelse])
            return
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                scan_expressions(item.context_expr)
                mark_invalid(_target_names(item.optional_vars))
            scan_statements(node.body)
            return
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            scan_statements(node.body)
            for handler in node.handlers:
                scan_expressions(handler.type)
                if handler.name is not None:
                    mark_invalid({handler.name})
                scan_statements(handler.body)
            scan_statements([*node.orelse, *node.finalbody])
            return
        if isinstance(node, ast.Match):
            scan_expressions(node.subject)
            for case in node.cases:
                mark_invalid(_pattern_names(case.pattern))
                scan_expressions(case.pattern, case.guard)
                scan_statements(case.body)
            return
        scan_expressions(node)

    scan_statements(tree.body)
    for declaration in ast.walk(tree):
        if isinstance(declaration, ast.Global | ast.Nonlocal):
            mark_invalid(declaration.names)
    if star_import:
        return {}
    return {
        name: next(iter(kinds))
        for name, kinds in canonical.items()
        if name not in invalid and len(kinds) == 1
    }


class _LexicalBindings(ast.NodeVisitor):
    """Collect bindings in one lexical scope without entering child scopes."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(item.asname or item.name.split(".", 1)[0] for item in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.update(item.asname or item.name for item in node.names)

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocals.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.names.update(_pattern_names(case.pattern))
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)


def _lexical_bindings(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    arguments = _argument_nodes(node.args)
    collector = _LexicalBindings()
    body: Iterable[ast.AST] = (node.body,) if isinstance(node, ast.Lambda) else node.body
    for item in body:
        collector.visit(item)
    return (
        ({argument.arg for argument in arguments} | collector.names)
        - collector.globals
        - collector.nonlocals
    )


class _MemberCollector(ast.NodeVisitor):
    """Collect sensitive member calls and reject callback/dynamic escapes."""

    def __init__(self, path: Path, tree: ast.Module, members: frozenset[str]) -> None:
        self.path = path
        self.module = _label(path)
        self.members = members
        self.bindings = _module_persistence_bindings(path, tree)
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.scopes: list[set[str]] = []
        self.instance_scopes: list[tuple[str, dict[str, bool]]] = []
        self.client_scopes: list[tuple[str, dict[str, bool]]] = []
        self.control_flow_depth = 0
        self.callees: set[int] = set()
        self.calls: set[Call] = set()
        self.escapes: set[Escape] = set()

    @property
    def owner(self) -> str:
        return _owner(self.classes, self.functions)

    def _shadowed(self, name: str) -> bool:
        return any(name in names for names in reversed(self.scopes))

    def _class_receiver(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return self.bindings.get(node.id) == "class" and not self._shadowed(node.id)
        if isinstance(node, ast.Attribute) and node.attr == "CookiePersistence":
            if isinstance(node.value, ast.Name):
                return self.bindings.get(node.value.id) == "module" and not self._shadowed(
                    node.value.id
                )
            qualified = ast.unparse(node.value)
            return (
                qualified == "notebooklm._web.transport.cookie_persistence"
                and self.bindings.get("notebooklm") == "root"
                and not self._shadowed("notebooklm")
            )
        return False

    def _instance_receiver(self, node: ast.expr) -> bool:
        if (
            isinstance(node, ast.Name)
            and node.id == "self"
            and self.classes[-1:] == ["CookiePersistence"]
            and len(self.functions) == 1
        ):
            return True
        if isinstance(node, ast.Name):
            for kind, bindings in reversed(self.instance_scopes):
                if node.id in bindings:
                    return bindings[node.id]
                if kind in {"function", "lambda"}:
                    return False
            return False
        return (
            self.path == CLIENT_PATH
            and self.owner == "_FromStorageContext._build"
            and len(self.functions) == 1
            and ast.unparse(node) == "client._web_runtime.cookie_persistence"
            and self._local_client_is_canonical("client")
        )

    def _local_client_is_canonical(self, name: str) -> bool:
        for kind, bindings in reversed(self.client_scopes):
            if name in bindings:
                return bindings[name]
            if kind in {"function", "lambda"}:
                return False
        return False

    def _set_local_client(self, name: str, value: bool) -> None:
        for _, bindings in reversed(self.client_scopes):
            bindings[name] = value
            return

    def _canonical_client_construction(self, node: ast.expr) -> bool:
        return (
            self.path == CLIENT_PATH
            and self.owner == "_FromStorageContext._build"
            and len(self.functions) == 1
            and self.control_flow_depth == 0
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "_cls"
        )

    def _canonical_instance_annotation(self, annotation: ast.expr | None) -> bool:
        if annotation is None:
            return False
        if (
            isinstance(annotation, ast.Name)
            and self.bindings.get(annotation.id) == "class"
            and not self._shadowed(annotation.id)
        ):
            return True
        return self._class_receiver(annotation)

    def _trusted_receiver(self, node: ast.expr, member: str) -> bool:
        if member == "_from_store":
            return self._class_receiver(node)
        if (
            self.path == WEB_LIFECYCLE_PATH
            and self.owner
            in {
                "WebTransportLifecycle.open",
                "WebTransportLifecycle.save_cookies",
            }
            and len(self.functions) == 1
            and ast.unparse(node) == "self._cookie_persistence"
        ):
            # The concrete web transport receives this collaborator through
            # its typed constructor and owns both persistence phases.
            return True
        if (
            member == "merge_cookie_observation"
            and self.path == PERSISTENCE_PATH
            and self.owner == "CookiePersistence._save_canonical"
            and isinstance(node, ast.Name)
            and node.id == "store"
        ):
            # Receiver provenance is independently pinned by the ProfileStore guard.
            return True
        return self._instance_receiver(node)

    def _invalidate_receiver_name(self, name: str, *, skip_comprehensions: bool = False) -> None:
        for kind, bindings in reversed(self.instance_scopes):
            if skip_comprehensions and kind == "comprehension":
                continue
            bindings[name] = False
            break
        for kind, bindings in reversed(self.client_scopes):
            if skip_comprehensions and kind == "comprehension":
                continue
            bindings[name] = False
            break

    def _invalidate_receiver_target(
        self, target: ast.expr, *, skip_comprehensions: bool = False
    ) -> None:
        for name in _target_names(target):
            self._invalidate_receiver_name(name, skip_comprehensions=skip_comprehensions)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._invalidate_receiver_name(node.name)
        self.classes.append(node.name)
        lexical = _LexicalBindings()
        for statement in node.body:
            lexical.visit(statement)
        self.scopes.append(lexical.names)
        self.instance_scopes.append(("class", {}))
        self.client_scopes.append(("class", {}))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()
        self.instance_scopes.pop()
        self.client_scopes.pop()
        self.classes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        self._invalidate_receiver_name(node.name)
        self.functions.append(node.name)
        lexical = _lexical_bindings(node)
        self.scopes.append(lexical)
        arguments = _argument_nodes(node.args)
        self.instance_scopes.append(
            (
                "function",
                {
                    argument.arg: self._canonical_instance_annotation(argument.annotation)
                    for argument in arguments
                },
            )
        )
        self.client_scopes.append(("function", {argument.arg: False for argument in arguments}))
        for statement in node.body:
            self.visit(statement)
        self.scopes.pop()
        self.instance_scopes.pop()
        self.client_scopes.pop()
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.functions.append("<lambda>")
        self.scopes.append(_lexical_bindings(node))
        self.instance_scopes.append(
            ("lambda", {argument.arg: False for argument in _argument_nodes(node.args)})
        )
        self.client_scopes.append(
            ("lambda", {argument.arg: False for argument in _argument_nodes(node.args)})
        )
        self.visit(node.body)
        self.scopes.pop()
        self.instance_scopes.pop()
        self.client_scopes.pop()
        self.functions.pop()

    def _visit_comprehension(
        self, generators: list[ast.comprehension], values: tuple[ast.expr, ...]
    ) -> None:
        self.scopes.append(set())
        self.instance_scopes.append(("comprehension", {}))
        self.client_scopes.append(("comprehension", {}))
        for generator in generators:
            self.visit(generator.iter)
            self.scopes[-1].update(_target_names(generator.target))
            self._invalidate_receiver_target(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)
        self.scopes.pop()
        self.instance_scopes.pop()
        self.client_scopes.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._invalidate_receiver_target(target)
            self.visit(target)
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "client"
            and self._canonical_client_construction(node.value)
        ):
            self._set_local_client("client", True)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self._invalidate_receiver_target(node.target)
        self.visit(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.target)
        self.visit(node.value)
        self._invalidate_receiver_target(node.target)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._invalidate_receiver_target(node.target, skip_comprehensions=True)
        self.visit(node.target)

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            self._invalidate_receiver_name(item.asname or item.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for item in node.names:
            self._invalidate_receiver_name(item.asname or item.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self._invalidate_receiver_name(node.name)
        for statement in node.body:
            self.visit(statement)

    def _visit_control_flow(self, statements: Iterable[ast.stmt]) -> None:
        self.control_flow_depth += 1
        try:
            for statement in statements:
                self.visit(statement)
        finally:
            self.control_flow_depth -= 1

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._visit_control_flow([*node.body, *node.orelse])

    visit_While = visit_If

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self.control_flow_depth += 1
        try:
            self.visit(node.target)
            for statement in [*node.body, *node.orelse]:
                self.visit(statement)
        finally:
            self.control_flow_depth -= 1

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
        self.control_flow_depth += 1
        try:
            for item in node.items:
                if item.optional_vars is not None:
                    self.visit(item.optional_vars)
            for statement in node.body:
                self.visit(statement)
        finally:
            self.control_flow_depth -= 1

    visit_AsyncWith = visit_With

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_control_flow(node.body)
        for handler in node.handlers:
            self.control_flow_depth += 1
            try:
                self.visit(handler)
            finally:
                self.control_flow_depth -= 1
        self._visit_control_flow([*node.orelse, *node.finalbody])

    visit_TryStar = visit_Try

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.control_flow_depth += 1
            try:
                self.visit(case.pattern)
                for name in _pattern_names(case.pattern):
                    self._invalidate_receiver_name(name)
                if case.guard is not None:
                    self.visit(case.guard)
                for statement in case.body:
                    self.visit(statement)
            finally:
                self.control_flow_depth -= 1

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self._invalidate_receiver_name(node.id)

    def visit_Global(self, node: ast.Global) -> None:
        for name in node.names:
            self._invalidate_receiver_name(name, skip_comprehensions=True)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        if len(self.instance_scopes) < 2:
            return
        current_instance = self.instance_scopes.pop()
        current_client = self.client_scopes.pop()
        for name in node.names:
            self._invalidate_receiver_name(name, skip_comprehensions=True)
        self.instance_scopes.append(current_instance)
        self.client_scopes.append(current_client)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in self.members
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "__getattribute__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in self.members
        ):
            self.escapes.add((self.module, self.owner, "dynamic-access"))
        if isinstance(node.func, ast.Attribute) and node.func.attr in self.members:
            self.callees.add(id(node.func))
            if self._trusted_receiver(node.func.value, node.func.attr):
                self.calls.add((self.module, self.owner))
            else:
                self.escapes.add((self.module, self.owner, f"untrusted-receiver:{node.func.attr}"))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self.members and id(node) not in self.callees:
            self.escapes.add((self.module, self.owner, f"capability:{node.attr}"))
        self.generic_visit(node)


def _member_projection(
    tree: ast.Module, path: Path, members: frozenset[str]
) -> tuple[set[Call], set[Escape]]:
    collector = _MemberCollector(path, tree, members)
    collector.visit(tree)
    return collector.calls, collector.escapes


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _methods(node: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        member.name: member
        for member in node.body
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _self_fields(node: ast.ClassDef) -> set[str]:
    fields: set[str] = set()
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Assign | ast.AnnAssign | ast.AugAssign):
            continue
        targets = inner.targets if isinstance(inner, ast.Assign) else [inner.target]
        for target in targets:
            fields.update(
                candidate.attr
                for candidate in ast.walk(target)
                if isinstance(candidate, ast.Attribute)
                and isinstance(candidate.value, ast.Name)
                and candidate.value.id == "self"
            )
    return fields


def _attribute_owners(path: Path, attribute: str) -> set[str]:
    tree = _tree(path)
    classes: list[str] = []
    functions: list[str] = []
    owners: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            classes.append(node.name)
            self.generic_visit(node)
            classes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            functions.append(node.name)
            self.generic_visit(node)
            functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr == attribute:
                owners.add(_owner(classes, functions))
            self.generic_visit(node)

    Visitor().visit(tree)
    return owners


def test_typed_states_are_exact_minimal_and_redacted() -> None:
    for state_type in (UninitializedBaseline, FailedBaseline):
        assert state_type.__dataclass_params__.frozen is True
        assert state_type.__slots__ == ()
        assert dataclasses.fields(state_type) == ()
    assert ReadyBaseline.__dataclass_params__.frozen is True
    assert ReadyBaseline.__slots__ == ("value",)
    fields = dataclasses.fields(ReadyBaseline)
    assert [field.name for field in fields] == ["value"]
    assert fields[0].repr is False
    cookie = Cookie("SID", ".google.com", "/", "do-not-render", same_site="None")
    source = CookieJar((cookie,))
    ready = ReadyBaseline(source)
    assert ready.value is not source and tuple(ready.value) == tuple(source)
    assert "do-not-render" not in repr(ready)
    assert [field.name for field in dataclasses.fields(_PathSaveState)] == [
        "baseline",
        "last_applied_sequence",
    ]
    state = _PathSaveState()
    assert isinstance(state.baseline, UninitializedBaseline)
    assert state.last_applied_sequence == -1


def test_persistence_and_legacy_adapter_own_exact_state() -> None:
    tree = _tree(PERSISTENCE_PATH)
    assert _self_fields(_class(tree, "CookiePersistence")) == _PERSISTENCE_FIELDS
    assert _self_fields(_class(tree, "_LegacySnapshotAdapter")) == _ADAPTER_FIELDS
    assert set(_methods(_class(tree, "_LegacySnapshotAdapter"))) == {
        "__init__",
        "auth",
        "snapshots",
        "set_default_key",
        "get",
        "get_default",
        "set",
        "project",
        "advance",
    }
    protocols = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases)
    }
    assert protocols == {"SaveCookiesToStorage", "ToThread"}
    module_assignments = {
        name
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        for name in _target_names(target)
    }
    assert module_assignments == {
        "__all__",
        "logger",
        "T",
        "BaselineState",
        "_BASELINE_ERRORS",
    }


def test_compat_constructor_and_runtime_factory_split_is_exact() -> None:
    methods = _methods(_class(_tree(PERSISTENCE_PATH), "CookiePersistence"))
    constructor = ast.unparse(methods["__init__"])
    factory = ast.unparse(methods["_from_store"])
    assert "auth=auth" in constructor
    assert "initial=auth.cookie_snapshot" in constructor
    assert "auth=None" in factory
    assert "initial=initial_snapshot" in factory
    assert "_typed_from_snapshot(initial_snapshot)" in factory
    assert "AuthTokens" not in factory
    assert ".auth" not in factory


def test_auth_tokens_and_cookie_snapshot_accesses_are_confined() -> None:
    assert _attribute_owners(PERSISTENCE_PATH, "cookie_snapshot") == {
        "_LegacySnapshotAdapter.get_default",
        "_LegacySnapshotAdapter.set",
        "CookiePersistence.__init__",
    }
    assert _attribute_owners(LIFECYCLE_PATH, "cookie_snapshot") == set()
    assert _attribute_owners(WEB_LIFECYCLE_PATH, "cookie_snapshot") == {
        "WebTransportLifecycle.open",
        "WebTransportLifecycle.save_cookies",
    }
    assert _attribute_owners(LIFECYCLE_PATH, "_auth") == set()
    assert _attribute_owners(WEB_LIFECYCLE_PATH, "_auth") == {
        "WebTransportLifecycle.__init__",
        "WebTransportLifecycle.open",
        "WebTransportLifecycle.save_cookies",
    }


def test_private_persistence_callers_and_capabilities_are_exact() -> None:
    calls: set[Call] = set()
    escapes: set[Escape] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        found, rejected = _member_projection(_tree(path), path, _PRIVATE_MEMBERS)
        calls.update(found)
        escapes.update(rejected)
    assert calls == {
        ("_web/transport/init.py", "_build_web_transport"),
        ("_web/transport/lifecycle.py", "WebTransportLifecycle.open"),
        ("_web/transport/lifecycle.py", "WebTransportLifecycle.save_cookies"),
        ("client.py", "_FromStorageContext._build"),
    }
    assert escapes == set()


def test_typed_merge_and_pair_parser_ownership_is_exact() -> None:
    calls, escapes = _member_projection(
        _tree(PERSISTENCE_PATH), PERSISTENCE_PATH, frozenset({"merge_cookie_observation"})
    )
    assert calls == {("_web/transport/cookie_persistence.py", "CookiePersistence._save_canonical")}
    assert escapes == set()
    methods = _methods(_class(_tree(PERSISTENCE_PATH), "CookiePersistence"))
    parser_owners = {
        name
        for name, method in methods.items()
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_load_cookie_pair_pure"
            for node in ast.walk(method)
        )
    }
    assert parser_owners == {
        "_adopt_reloaded_baseline",
        "_prepare_open_baseline",
        "_save_canonical",
        "_save_v0_callback",
    }


def test_canonical_and_explicit_v0_routes_keep_capabilities_separate() -> None:
    tree = _tree(PERSISTENCE_PATH)
    methods = _methods(_class(tree, "CookiePersistence"))
    canonical = ast.unparse(methods["_save_canonical"])
    legacy = ast.unparse(methods["_save_v0_callback"])
    assert "merge_cookie_observation" in canonical
    assert "save_cookies_to_storage" not in canonical
    assert "merge_cookie_observation" not in legacy
    assert "save_cookies_to_storage" in legacy
    lifecycle = ast.unparse(
        _methods(_class(_tree(WEB_LIFECYCLE_PATH), "WebTransportLifecycle"))["save_cookies"]
    )
    assert "if self._cookie_saver is None" in lifecycle
    assert lifecycle.index("self._cookie_saver is None") < lifecycle.index("_save_canonical")
    assert "_save_v0_callback" in lifecycle
    init = ast.unparse(
        _methods(_class(_tree(WEB_LIFECYCLE_PATH), "WebTransportLifecycle"))["__init__"]
    )
    assert "self._cookie_saver = cookie_saver" in init
    assert "_uses_default_cookie_saver" not in init


def test_cookie_save_result_imports_and_consumers_are_exact() -> None:
    inventory = sum(
        (
            _cookie_save_result_inventory(path, _tree(path))
            for path in sorted(SRC_ROOT.rglob("*.py"))
        ),
        Counter(),
    )
    assert inventory == Counter(
        {
            ("_auth/storage.py", "<module>", "definition"): 1,
            ("_auth/storage.py", "_cookie_save_return", "name:load"): 2,
            ("_auth/storage.py", "save_cookies_to_storage", "name:load"): 1,
            ("_auth/storage.py", "merge_cookie_delta", "name:load"): 4,
            ("_web/transport/cookie_persistence.py", "<module>", "import:direct"): 1,
            (
                "_web/transport/cookie_persistence.py",
                "SaveCookiesToStorage.__call__",
                "name:load",
            ): 1,
            (
                "_web/transport/cookie_persistence.py",
                "CookiePersistence._save_v0_callback",
                "name:load",
            ): 1,
            ("auth.py", "<module>", "name:store"): 1,
            ("auth.py", "<module>", "attribute:load"): 1,
        }
    )

    branch_owners = set().union(
        *(
            _cookie_save_result_branch_owners(path, _tree(path))
            for path in sorted(SRC_ROOT.rglob("*.py"))
        )
    )
    assert branch_owners == {
        ("_web/transport/cookie_persistence.py", "CookiePersistence._save_v0_callback")
    }


@pytest.mark.parametrize(
    "source",
    [
        "import notebooklm._auth.storage as s\ndef bad(value):\n return isinstance(value, s.CookieSaveResult)",
        "from notebooklm._auth import storage as s\ndef bad(value):\n return s.CookieSaveResult(False)",
        "from notebooklm._auth.storage import CookieSaveResult as CSR\ndef bad(value):\n return CSR(False)",
        "def bad(storage, value):\n return isinstance(value, getattr(storage, 'CookieSaveResult'))",
        "import notebooklm._auth.storage as s\ndef bad(value):\n return isinstance(value, s.__dict__['CookieSaveResult'])",
        "import notebooklm._auth.storage as s\nRESULT = 'CookieSaveResult'\ndef bad(value):\n return isinstance(value, getattr(s, RESULT))",
    ],
    ids=[
        "import-alias",
        "from-module-alias",
        "direct-type-alias",
        "dynamic",
        "module-dict-subscript",
        "constant-backed-dynamic",
    ],
)
def test_cookie_save_result_inventory_catches_qualified_bypasses(source: str) -> None:
    inventory = _cookie_save_result_inventory(SRC_ROOT / "synthetic.py", ast.parse(source))
    assert inventory


def test_persistence_route_debug_events_are_exact_and_value_free() -> None:
    method = _methods(_class(_tree(WEB_LIFECYCLE_PATH), "WebTransportLifecycle"))["save_cookies"]
    route = next(
        statement
        for statement in method.body
        if isinstance(statement, ast.If)
        and ast.unparse(statement.test) == "self._cookie_saver is None"
    )

    def debug_projection(statements: list[ast.stmt]) -> tuple[str, str]:
        calls = [
            node
            for statement in statements
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
            and node.func.attr == "debug"
        ]
        assert len(calls) == 1
        assert not calls[0].keywords
        assert len(calls[0].args) == 2
        message = calls[0].args[0]
        assert isinstance(message, ast.Constant) and isinstance(message.value, str)
        return message.value, ast.unparse(calls[0].args[1])

    assert debug_projection(route.body) == (
        "Cookie persistence route: type=canonical_store status=dispatch path=%s",
        "effective_path",
    )
    assert debug_projection(route.orelse) == (
        "Cookie persistence route: type=explicit_v0_callback status=dispatch path=%s",
        "effective_path",
    )


def test_runtime_factory_has_no_auth_tokens_persistence_handoff() -> None:
    tree = _tree(INIT_PATH)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_from_store"
    ]
    assert len(calls) == 1
    assert len(calls[0].args) == 1
    assert not any(keyword.arg == "auth" for keyword in calls[0].keywords)
    assert {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords} == {
        "initial_snapshot": "auth.cookie_snapshot"
    }


@pytest.mark.parametrize(
    "source",
    [
        "def bad(p):\n    callback = p._save_canonical\n    return callback\n",
        "def bad(p, sink):\n    return sink(callback=p._prepare_open_baseline)\n",
        "def bad(p):\n    return getattr(p, '_from_store')\n",
        "def bad(p):\n    return p.register_open_baseline\n",
        "def bad(items):\n    return [p._save_canonical() for p in items]\n",
        "def bad(value):\n    return (lambda p: p._prepare_open_baseline())(value)\n",
    ],
    ids=["assignment", "callback", "dynamic", "bound-method", "comprehension", "lambda"],
)
def test_private_capability_escape_detector_bites(source: str) -> None:
    path = SRC_ROOT / "synthetic.py"
    calls, escapes = _member_projection(ast.parse(source), path, _PRIVATE_MEMBERS)
    assert calls or escapes


def test_private_caller_and_dynamic_detectors_fail_closed_on_shadow_and_rebinding() -> None:
    path = SRC_ROOT / "synthetic.py"
    tree = ast.parse(
        "from notebooklm._web.transport.cookie_persistence import CookiePersistence as P\n"
        "def deferred():\n    return P._from_store(None)\n"
        "P = object()\n"
        "def local(P):\n    return P._save_canonical()\n"
    )
    calls, escapes = _member_projection(tree, path, _PRIVATE_MEMBERS)
    assert calls == set()
    assert escapes == {
        ("synthetic.py", "deferred", "untrusted-receiver:_from_store"),
        ("synthetic.py", "local", "untrusted-receiver:_save_canonical"),
    }
    dynamic_tree = ast.parse("def bad(p):\n    return p.__getattribute__('_save_canonical')\n")
    _, escapes = _member_projection(dynamic_tree, path, _FORBIDDEN_DYNAMIC_MEMBERS)
    assert ("synthetic.py", "bad", "dynamic-access") in escapes


def test_external_source_is_scanned_with_stable_label() -> None:
    path = SRC_ROOT / "plugin" / "synthetic.py"
    calls, escapes = _member_projection(
        ast.parse("def bad(p):\n    return p._save_canonical()\n"),
        path,
        _PRIVATE_MEMBERS,
    )
    assert calls == set()
    assert escapes == {("plugin/synthetic.py", "bad", "untrusted-receiver:_save_canonical")}


def test_same_owner_later_rebinding_invalidates_deferred_provider_lookup() -> None:
    tree = ast.parse(
        "from notebooklm._web.transport.cookie_persistence import CookiePersistence\n"
        "def build_collaborators():\n"
        "    return CookiePersistence._from_store(None)\n"
        "CookiePersistence = object\n"
    )
    calls, escapes = _member_projection(tree, INIT_PATH, _PRIVATE_MEMBERS)
    assert calls == set()
    assert escapes == {
        ("_web/transport/init.py", "build_collaborators", "untrusted-receiver:_from_store")
    }


@pytest.mark.parametrize(
    "rebind",
    [
        "        cookie_persistence = object()\n",
        "        cookie_persistence: object = object()\n",
        "        cookie_persistence += object()\n",
        "        (cookie_persistence := object())\n",
        "        cookie_persistence, other = object(), None\n",
        "        if condition:\n            cookie_persistence = object()\n",
        "        import builtins as cookie_persistence\n",
        "        from builtins import object as cookie_persistence\n",
        "        def cookie_persistence():\n            pass\n",
        "        async def cookie_persistence():\n            pass\n",
        "        class cookie_persistence:\n            pass\n",
        "        try:\n            pass\n        except Exception as cookie_persistence:\n"
        "            pass\n",
        "        match condition:\n            case {'value': cookie_persistence}:\n"
        "                pass\n",
    ],
    ids=[
        "assign",
        "annotated",
        "augmented",
        "walrus",
        "destructuring",
        "branch",
        "import",
        "from-import",
        "function",
        "async-function",
        "class",
        "except",
        "match",
    ],
)
def test_same_owner_receiver_rebinding_invalidates_annotated_capability(rebind: str) -> None:
    tree = ast.parse(
        "from notebooklm._web.transport.cookie_persistence import CookiePersistence\n"
        "class ClientLifecycle:\n"
        "    async def save_cookies(\n"
        "        self, cookie_persistence: CookiePersistence, condition=True\n"
        "    ):\n"
        f"{rebind}"
        "        await cookie_persistence._save_canonical(None, None, to_thread=None)\n"
    )
    calls, escapes = _member_projection(tree, LIFECYCLE_PATH, _PRIVATE_MEMBERS)
    assert calls == set()
    assert escapes == {
        (
            "_runtime/lifecycle.py",
            "ClientLifecycle.save_cookies",
            "untrusted-receiver:_save_canonical",
        )
    }


@pytest.mark.parametrize(
    "nested",
    [
        "        async def nested():\n"
        "            await cookie_persistence._save_canonical(None, None, to_thread=None)\n",
        "        async def nested(cookie_persistence):\n"
        "            await cookie_persistence._save_canonical(None, None, to_thread=None)\n",
        "        callback = lambda: cookie_persistence._save_canonical(\n"
        "            None, None, to_thread=None\n"
        "        )\n",
        "        callback = lambda cookie_persistence: cookie_persistence._save_canonical(\n"
        "            None, None, to_thread=None\n"
        "        )\n",
    ],
    ids=["function-closure", "function-param", "lambda-closure", "lambda-param"],
)
def test_nested_receiver_frames_never_fall_through_to_outer_capability(nested: str) -> None:
    tree = ast.parse(
        "from notebooklm._web.transport.cookie_persistence import CookiePersistence\n"
        "class ClientLifecycle:\n"
        "    async def save_cookies(\n"
        "        self, cookie_persistence: CookiePersistence\n"
        "    ):\n"
        f"{nested}"
    )
    calls, escapes = _member_projection(tree, LIFECYCLE_PATH, _PRIVATE_MEMBERS)
    assert calls == set()
    assert escapes == {
        (
            "_runtime/lifecycle.py",
            "ClientLifecycle.save_cookies",
            "untrusted-receiver:_save_canonical",
        )
    }


@pytest.mark.parametrize(
    "rebind",
    [
        "for CookiePersistence in providers:\n    pass\n",
        "with provider as CookiePersistence:\n    pass\n",
        "try:\n    pass\nexcept Exception as CookiePersistence:\n    pass\n",
        "match candidate:\n    case {'provider': CookiePersistence}:\n        pass\n",
        "from builtins import *\n",
    ],
    ids=["for-target", "with-alias", "except-alias", "match-capture", "star-import"],
)
def test_module_control_flow_bindings_poison_deferred_provider(rebind: str) -> None:
    tree = ast.parse(
        "from notebooklm._web.transport.cookie_persistence import CookiePersistence\n"
        f"{rebind}"
        "def build_collaborators():\n"
        "    return CookiePersistence._from_store(None)\n"
    )
    calls, escapes = _member_projection(tree, INIT_PATH, _PRIVATE_MEMBERS)
    assert calls == set()
    assert escapes == {
        ("_web/transport/init.py", "build_collaborators", "untrusted-receiver:_from_store")
    }


def test_later_canonical_import_does_not_rehabilitate_ambiguous_provider() -> None:
    tree = ast.parse(
        "CookiePersistence = object\n"
        "from notebooklm._web.transport.cookie_persistence import CookiePersistence\n"
        "def build_collaborators():\n"
        "    return CookiePersistence._from_store(None)\n"
    )
    calls, escapes = _member_projection(tree, INIT_PATH, _PRIVATE_MEMBERS)
    assert calls == set()
    assert escapes == {
        ("_web/transport/init.py", "build_collaborators", "untrusted-receiver:_from_store")
    }


def test_global_declaration_anywhere_poisons_deferred_module_provider() -> None:
    tree = ast.parse(
        "from notebooklm._web.transport.cookie_persistence import CookiePersistence\n"
        "def mutate_provider():\n"
        "    global CookiePersistence\n"
        "def build_collaborators():\n"
        "    return CookiePersistence._from_store(None)\n"
    )
    calls, escapes = _member_projection(tree, INIT_PATH, _PRIVATE_MEMBERS)
    assert calls == set()
    assert escapes == {
        ("_web/transport/init.py", "build_collaborators", "untrusted-receiver:_from_store")
    }


def test_client_registration_requires_exact_sequential_constructor_provenance() -> None:
    live_tree = ast.parse(
        "class _FromStorageContext:\n"
        "    def _build(self):\n"
        "        client = self._cls(auth=None)\n"
        "        client._web_runtime.cookie_persistence.register_open_baseline(None, None)\n"
    )
    calls, escapes = _member_projection(live_tree, CLIENT_PATH, _PRIVATE_MEMBERS)
    assert calls == {("client.py", "_FromStorageContext._build")}
    assert escapes == set()

    evil_tree = ast.parse(
        "class _FromStorageContext:\n"
        "    def _build(self):\n"
        "        client = self._cls(auth=None)\n"
        "        client = make_evil()\n"
        "        client._web_runtime.cookie_persistence.register_open_baseline(None, None)\n"
    )
    calls, escapes = _member_projection(evil_tree, CLIENT_PATH, _PRIVATE_MEMBERS)
    assert calls == set()
    assert escapes == {
        (
            "client.py",
            "_FromStorageContext._build",
            "untrusted-receiver:register_open_baseline",
        )
    }


def test_client_chain_spelling_without_constructor_provenance_is_untrusted() -> None:
    tree = ast.parse(
        "class _FromStorageContext:\n"
        "    def _build(self):\n"
        "        client._web_runtime.cookie_persistence.register_open_baseline(None, None)\n"
    )
    calls, escapes = _member_projection(tree, CLIENT_PATH, _PRIVATE_MEMBERS)
    assert calls == set()
    assert escapes == {
        (
            "client.py",
            "_FromStorageContext._build",
            "untrusted-receiver:register_open_baseline",
        )
    }


def test_public_exports_and_compatibility_signatures_remain_narrow() -> None:
    import notebooklm._web.transport.cookie_persistence as module
    from notebooklm._runtime.lifecycle import ClientLifecycle
    from notebooklm._web.transport.lifecycle import WebTransportLifecycle

    assert module.__all__ == ["CookiePersistence", "SaveCookiesToStorage"]
    assert str(inspect.signature(module.SaveCookiesToStorage.__call__)) == (
        "(self, cookie_jar: 'httpx.Cookies', path: 'Path', /, *, "
        "original_snapshot: 'CookieSnapshot | None', return_result: 'bool') -> "
        "'bool | CookieSaveResult'"
    )
    assert str(inspect.signature(module.CookiePersistence._save_v0_callback)) == (
        "(self, jar: 'httpx.Cookies', path: 'Path | None' = None, *, "
        "save_cookies_to_storage: 'SaveCookiesToStorage', to_thread: 'ToThread') -> 'None'"
    )
    assert "store" not in inspect.signature(module.CookiePersistence._save_v0_callback).parameters
    assert str(inspect.signature(module.CookiePersistence._save_canonical)) == (
        "(self, jar: 'httpx.Cookies', path: 'Path | None', *, to_thread: 'ToThread') -> 'None'"
    )
    assert str(inspect.signature(ClientLifecycle)) == (
        "(*, supervisor: 'LifecycleSupervisor', transports: 'Sequence[TransportLifecycle]', "
        "loop_participants: 'Sequence[LoopParticipant]') -> 'None'"
    )
    web_lifecycle_parameters = inspect.signature(WebTransportLifecycle).parameters
    assert web_lifecycle_parameters["auth"].default is inspect.Parameter.empty
    assert web_lifecycle_parameters["cookie_persistence_path"].default is inspect.Parameter.empty
