"""Fail-closed dependency, ownership, and secret boundary for ``MintService``."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from notebooklm._auth import master_token, mint_service

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
SERVICE_PATH = AUTH_ROOT / "mint_service.py"
ADAPTER_PATH = AUTH_ROOT / "master_token.py"
SERVICE_MODULE = "notebooklm._auth.mint_service"

_EXPECTED_IMPORTS = {
    ("module", "__future__", "annotations", 0, None),
    ("module", "asyncio", "", 0, None),
    ("module", "logging", "", 0, None),
    ("module", "threading", "", 0, None),
    ("module", "collections.abc", "Iterator", 0, None),
    ("module", "contextlib", "contextmanager", 0, None),
    ("module", "dataclasses", "dataclass", 0, None),
    ("module", "dataclasses", "field", 0, None),
    ("module", "typing", "Any", 0, None),
    ("module", "httpx", "", 0, None),
    ("module", "exceptions", "MissingDependencyError", 2, None),
    ("module", "master_token_types", "MasterToken", 1, None),
    ("_require_gpsoauth", "gpsoauth", "", 0, None),
}
_EXPECTED_ASSIGNMENTS = {
    "_MASTER_APP",
    "_MASTER_SIG",
    "_OAUTHLOGIN_SERVICE",
    "_CHROMECAST_OAUTH_SPEC",
    "_REQUIRED_MINTED_COOKIES",
    "KEEPALIVE_ROTATE_URL",
    "_KEEPALIVE_ROTATE_HEADERS",
    "_KEEPALIVE_ROTATE_BODY",
    "_KEEPALIVE_POKE_TIMEOUT",
    "_ROTATE_POST_KWARGS",
    "logger",
    "_LOG_LOCK",
}
_FORBIDDEN_IMPORT_PARTS = {
    "filelock",
    "json",
    "os",
    "pathlib",
    "subprocess",
    "notebooklm.auth",
    "notebooklm.client",
    "notebooklm._atomic_io",
    "notebooklm._auth.credential_io",
    "notebooklm._auth.keepalive",
    "notebooklm._auth.master_token",
    "notebooklm._auth.master_token_file",
    "notebooklm._auth.paths",
    "notebooklm._auth.profile_store",
    "notebooklm._auth.psidts_recovery",
    "notebooklm._auth.recovery",
    "notebooklm._auth.storage",
    "notebooklm._auth.storage_lock",
}
_FORBIDDEN_CAPABILITIES = {
    "FileLock",
    "MasterTokenFile",
    "Path",
    "ProfileStore",
    "StorageLockManager",
    "TaskGroup",
    "__import__",
    "create_task",
    "eval",
    "exec",
    "getattr",
    "globals",
    "import_module",
    "locals",
    "open",
    "setattr",
    "shield",
    "sleep",
    "wait_for",
}
_SECRET_NAMES = {
    "oauth_token",
    "master_token",
    "result",
    "token",
    "oauth",
    "bearer",
    "uberauth",
    "authorization",
}
_ALLOWED_SECRET_CALLS = {
    "MasterToken",
    "MintedOAuthToken",
    "asyncio.to_thread",
    "client.get",
    "gpsoauth.exchange_token",
    "gpsoauth.perform_oauth",
    "self.mint_oauth",
    "str",
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        pieces = [_static_string(item) for item in node.values]
        return (
            "".join(piece for piece in pieces if piece is not None) if None not in pieces else None
        )
    return None


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.records: set[tuple[str, str, str, int, str | None]] = set()

    @property
    def owner(self) -> str:
        return ".".join(self.scopes) if self.scopes else "module"

    def _scope(self, node: ast.AST, name: str) -> None:
        self.scopes.append(name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node, node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._scope(node, "<lambda>")

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._scope(node, "<comprehension>")

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Import(self, node: ast.Import) -> None:
        self.records.update((self.owner, item.name, "", 0, item.asname) for item in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.records.update(
            (self.owner, node.module or "", item.name, node.level, item.asname)
            for item in node.names
        )


def _imports(tree: ast.AST) -> set[tuple[str, str, str, int, str | None]]:
    collector = _ImportCollector()
    collector.visit(tree)
    return collector.records


def _top_level_assignments(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _method(tree: ast.Module, class_name: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in _class(tree, class_name).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        return f"{owner}.{node.attr}" if owner else None
    return None


def _forbidden_capabilities(tree: ast.Module) -> set[str]:
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = {item.name for item in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            targets = {module, *(f"{module}.{item.name}" for item in node.names)}
        else:
            targets = set()
        for target in targets:
            if any(
                target == item or target.startswith(f"{item}.") for item in _FORBIDDEN_IMPORT_PARTS
            ):
                violations.add(f"import:{target}")
        if isinstance(node, ast.Call):
            name = (_qualified_name(node.func) or "").rsplit(".", 1)[-1]
            if name in _FORBIDDEN_CAPABILITIES:
                violations.add(f"call:{name}")
        if isinstance(node, ast.Attribute | ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.attr if isinstance(node, ast.Attribute) else node.id
            if name in _FORBIDDEN_CAPABILITIES:
                violations.add(f"load:{name}")
    return violations


def _stored_instance_attributes(tree: ast.Module) -> set[str]:
    stored: set[str] = set()
    service = _class(tree, "MintService")
    for node in ast.walk(service):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            targets = [node.target]
        for target in targets:
            for item in ast.walk(target):
                if (
                    isinstance(item, ast.Attribute)
                    and isinstance(item.value, ast.Name)
                    and item.value.id in {"self", "cls"}
                ):
                    stored.add(item.attr)
    return stored


def _target_names(node: ast.AST) -> set[str]:
    return {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
    }


def _contains_secret(node: ast.AST, tainted_names: set[str] | None = None) -> bool:
    names = _SECRET_NAMES if tainted_names is None else tainted_names
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute) and node.attr == "secret":
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        key = _static_string(node.args[0]) if node.func.attr == "get" and node.args else None
        if key in {"Token", "Auth"}:
            return True
        if key in {"Error", "Expiry"}:
            return False
    return any(_contains_secret(child, names) for child in ast.iter_child_nodes(node))


def _class_namespace(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.ClassDef | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            return None
        if isinstance(current, ast.ClassDef):
            return current
        current = parents.get(current)
    return None


def _module_namespace(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            return False
        if isinstance(current, ast.Module):
            return True
        current = parents.get(current)
    return False


def _pattern_names(node: ast.pattern) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.MatchAs | ast.MatchStar) and item.name:
            names.add(item.name)
        elif isinstance(item, ast.MatchMapping) and item.rest:
            names.add(item.rest)
    return names


def _scope_captures_secret(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    tainted_names: set[str],
) -> bool:
    bound: set[str] = set()
    global_or_nonlocal: set[str] = set()
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        arguments = node.args
        bound.update(
            argument.arg
            for argument in [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ]
        )
        if arguments.vararg:
            bound.add(arguments.vararg.arg)
        if arguments.kwarg:
            bound.add(arguments.kwarg.arg)

    class BindingCollector(ast.NodeVisitor):
        def visit_Name(self, item: ast.Name) -> None:
            if isinstance(item.ctx, ast.Store):
                bound.add(item.id)

        def visit_Global(self, item: ast.Global) -> None:
            global_or_nonlocal.update(item.names)

        visit_Nonlocal = visit_Global

        def visit_FunctionDef(self, item: ast.FunctionDef) -> None:
            bound.add(item.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, item: ast.ClassDef) -> None:
            bound.add(item.name)

        def visit_Lambda(self, item: ast.Lambda) -> None:
            return

    binding_collector = BindingCollector()
    for statement in node.body:
        binding_collector.visit(statement)
    bound -= global_or_nonlocal

    captured = False

    class LoadCollector(ast.NodeVisitor):
        def visit_Name(self, item: ast.Name) -> None:
            nonlocal captured
            if isinstance(item.ctx, ast.Load) and item.id in tainted_names - bound:
                captured = True

        def visit_FunctionDef(self, item: ast.FunctionDef) -> None:
            return

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, item: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, item: ast.Lambda) -> None:
            return

    load_collector = LoadCollector()
    for statement in node.body:
        load_collector.visit(statement)
    return captured


def _definition_expressions(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> list[ast.AST]:
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)]
    arguments = node.args
    annotations = [
        argument.annotation
        for argument in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
        if argument.annotation is not None
    ]
    if arguments.vararg and arguments.vararg.annotation:
        annotations.append(arguments.vararg.annotation)
    if arguments.kwarg and arguments.kwarg.annotation:
        annotations.append(arguments.kwarg.annotation)
    if node.returns:
        annotations.append(node.returns)
    return [
        *node.decorator_list,
        *node.args.defaults,
        *(item for item in node.args.kw_defaults if item),
        *annotations,
    ]


def _tainted_names(tree: ast.Module) -> set[str]:
    names = set(_SECRET_NAMES)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                additions = (
                    {node.name}
                    if any(_contains_secret(item, names) for item in _definition_expressions(node))
                    or _scope_captures_secret(node, names)
                    else set()
                )
            elif isinstance(node, ast.ClassDef):
                additions = (
                    {node.name}
                    if any(_contains_secret(item, names) for item in _definition_expressions(node))
                    or _scope_captures_secret(node, names)
                    or any(
                        isinstance(item, ast.Assign | ast.AnnAssign | ast.AugAssign)
                        and item.value is not None
                        and _contains_secret(item.value, names)
                        and _class_namespace(item, parents) is node
                        for item in ast.walk(node)
                    )
                    else set()
                )
            elif isinstance(node, ast.Assign):
                value = node.value
                targets = list(node.targets)
                additions = (
                    {name for target in targets for name in _target_names(target)}
                    if _contains_secret(value, names)
                    else set()
                )
            elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
                value = node.value
                targets = [node.target]
                additions = (
                    {name for target in targets for name in _target_names(target)}
                    if value is not None and _contains_secret(value, names)
                    else set()
                )
            elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
                additions = (
                    _target_names(node.target) if _contains_secret(node.iter, names) else set()
                )
            elif isinstance(node, ast.With | ast.AsyncWith):
                additions = {
                    name
                    for item in node.items
                    if item.optional_vars is not None and _contains_secret(item.context_expr, names)
                    for name in _target_names(item.optional_vars)
                }
            elif isinstance(node, ast.Match):
                additions = (
                    {name for case in node.cases for name in _pattern_names(case.pattern)}
                    if _contains_secret(node.subject, names)
                    else set()
                )
            else:
                continue
            additions -= names
            if additions:
                names.update(additions)
                changed = True
    return names


def _retention_violations(tree: ast.Module) -> set[str]:
    violations: set[str] = set()
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    tainted_names = _tainted_names(tree)
    nonlocal_targets = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Global | ast.Nonlocal)
        for name in node.names
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _qualified_name(node.func) or ""
            secret_args = any(
                _contains_secret(item, tainted_names)
                for item in [*node.args, *(k.value for k in node.keywords)]
            )
            if secret_args and name not in _ALLOWED_SECRET_CALLS:
                violations.add(f"secret-call:{name or type(node.func).__name__}")
            if name.startswith("logger.") and _contains_secret(node, tainted_names):
                violations.add("secret-log")
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
            value = node.value
            targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
            if value is not None and _contains_secret(value, tainted_names):
                if (
                    any(
                        isinstance(item, ast.Attribute | ast.Subscript)
                        for target in targets
                        for item in ast.walk(target)
                    )
                    or any(_target_names(target) & nonlocal_targets for target in targets)
                    or isinstance(parents.get(node), ast.Module)
                    or _class_namespace(node, parents) is not None
                ):
                    violations.add("secret-field")
                if isinstance(value, ast.List | ast.Set | ast.Tuple | ast.Lambda):
                    violations.add("secret-carrier")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if any(
                _contains_secret(item, tainted_names) for item in _definition_expressions(node)
            ) or _scope_captures_secret(node, tainted_names):
                violations.add("secret-closure")
        elif isinstance(node, ast.ClassDef):
            if any(
                _contains_secret(item, tainted_names) for item in _definition_expressions(node)
            ) or _scope_captures_secret(node, tainted_names):
                violations.add("secret-carrier")
        elif isinstance(node, ast.For | ast.AsyncFor):
            if _contains_secret(node.iter, tainted_names) and (
                _module_namespace(node, parents)
                or _class_namespace(node, parents) is not None
                or any(
                    isinstance(item, ast.Attribute | ast.Subscript)
                    for item in ast.walk(node.target)
                )
            ):
                violations.add("secret-field")
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                if (
                    item.optional_vars is not None
                    and _contains_secret(item.context_expr, tainted_names)
                    and (
                        _module_namespace(node, parents)
                        or _class_namespace(node, parents) is not None
                        or any(
                            isinstance(target, ast.Attribute | ast.Subscript)
                            for target in ast.walk(item.optional_vars)
                        )
                    )
                ):
                    violations.add("secret-field")
        elif isinstance(node, ast.Match):
            if _contains_secret(node.subject, tainted_names) and (
                _module_namespace(node, parents) or _class_namespace(node, parents) is not None
            ):
                violations.add("secret-field")
        elif (
            isinstance(node, ast.Return)
            and node.value is not None
            and _contains_secret(node.value, tainted_names)
        ):
            allowed_worker_return = isinstance(node.value, ast.Call) and _qualified_name(
                node.value.func
            ) in {"MasterToken", "MintedOAuthToken", "gpsoauth.perform_oauth"}
            if not allowed_worker_return:
                violations.add("secret-return")
        elif (
            isinstance(node, ast.Raise)
            and node.exc is not None
            and _contains_secret(node.exc, tainted_names)
        ):
            violations.add("secret-error")
        elif isinstance(node, ast.Yield | ast.YieldFrom) and node.value is not None:
            if _contains_secret(node.value, tainted_names):
                violations.add("secret-return")
        elif isinstance(node, ast.Lambda) and _contains_secret(node, tainted_names):
            violations.add("secret-closure")
        elif isinstance(node, ast.Attribute) and node.attr in {"exchange", "mint"}:
            parent = parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                violations.add(f"callable-escape:{node.attr}")
    return violations


def _adapter_projection_violations(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    violations: list[str] = []
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)
    if not body or ast.unparse(body[0]) != "caller_exception = sys.exc_info()[1]":
        return ["missing-entry-exception-snapshot"]
    if len(body) < 2 or not isinstance(body[1], ast.Try):
        return ["missing-service-try"]
    attempt = body[1]
    if len(attempt.handlers) != 1:
        violations.append("handler-count")
        return violations
    handler = attempt.handlers[0]
    if not (
        isinstance(handler.type, ast.Name)
        and handler.type.id == "_MintError"
        and handler.name == "exc"
    ):
        violations.append("handler-type")
    expected_handler = [
        "error_context = exc.__context__",
        "if exc.__cause__ is None and error_context is caller_exception:\n    error_context = None",
        "error_snapshot = (str(exc), exc.__cause__, error_context, exc.__suppress_context__)",
    ]
    if [ast.unparse(item) for item in handler.body] != expected_handler:
        violations.append("handler-body")
    tail = body[2:]
    if (
        len(tail) != 3
        or ast.unparse(tail[0]) != "caller_exception = None"
        or ast.unparse(tail[1]) != "error = MasterTokenError(error_snapshot[0])"
    ):
        violations.append("outside-handler-tail")
    elif not isinstance(tail[2], ast.Try):
        violations.append("missing-public-reraise")
    else:
        public_raise = tail[2]
        public_handler = public_raise.handlers[0] if len(public_raise.handlers) == 1 else None
        if (
            [ast.unparse(item) for item in public_raise.body] != ["raise error"]
            or public_raise.orelse
            or public_raise.finalbody
            or public_handler is None
            or not isinstance(public_handler.type, ast.Name)
            or public_handler.type.id != "MasterTokenError"
            or public_handler.name is not None
            or [ast.unparse(item) for item in public_handler.body]
            != [
                "error.__cause__ = error_snapshot[1]",
                "error.__context__ = error_snapshot[2]",
                "error.__suppress_context__ = error_snapshot[3]",
                "raise",
            ]
        ):
            violations.append("public-reraise-shape")
    if any(
        isinstance(node, ast.Name) and node.id == "_MintError"
        for statement in body[2:]
        for node in ast.walk(statement)
    ):
        violations.append("private-error-retained")
    return violations


def _source_package(path: Path, src_root: Path) -> tuple[str, ...]:
    relative = path.relative_to(src_root)
    return ("notebooklm", *relative.parent.parts)


def _resolve_import_from(node: ast.ImportFrom, *, path: Path, src_root: Path) -> str | None:
    if node.level == 0:
        return node.module or ""
    package = _source_package(path, src_root)
    parents = node.level - 1
    if parents >= len(package):
        return None
    prefix = package[: len(package) - parents]
    suffix = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*prefix, *suffix))


def _origin(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _origin(node.value, bindings)
        return f"{owner}.{node.attr}" if owner else None
    if isinstance(node, ast.Call):
        callable_name = _origin(node.func, bindings)
        if callable_name in {"importlib.import_module", "__import__"} and node.args:
            return _static_string(node.args[0])
        if callable_name == "getattr" and len(node.args) >= 2:
            owner = _origin(node.args[0], bindings)
            member = _static_string(node.args[1])
            if owner is not None and member is not None:
                return f"{owner}.{member}"
        if callable_name == f"{SERVICE_MODULE}.MintService":
            return callable_name
    return None


def _bind(target: ast.AST, value: ast.AST, bindings: dict[str, str]) -> bool:
    if isinstance(target, ast.Name):
        origin = _origin(value, bindings)
        if origin is not None and target.id not in bindings:
            bindings[target.id] = origin
            return True
    if (
        isinstance(target, ast.Tuple | ast.List)
        and isinstance(value, ast.Tuple | ast.List)
        and len(target.elts) == len(value.elts)
    ):
        return any(
            _bind(left, right, bindings)
            for left, right in zip(target.elts, value.elts, strict=True)
        )
    return False


def _bindings(tree: ast.Module, *, path: Path, src_root: Path) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                bound = item.asname or item.name.split(".", 1)[0]
                bindings.setdefault(bound, item.name if item.asname else bound)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_import_from(node, path=path, src_root=src_root)
            if module is not None:
                for item in node.names:
                    if item.name != "*":
                        bindings.setdefault(item.asname or item.name, f"{module}.{item.name}")
    assignments: list[tuple[ast.AST, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assignments.extend((target, node.value) for target in node.targets)
        elif (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(
            node, ast.NamedExpr
        ):
            assignments.append((node.target, node.value))
    for _ in range(len(assignments) + 1):
        changed = False
        for target, value in assignments:
            changed |= _bind(target, value, bindings)
        if not changed:
            break
    return bindings


class _ServiceCallCollector(ast.NodeVisitor):
    def __init__(self, bindings: dict[str, str], *, module: str) -> None:
        self.bindings = bindings
        self.module = module
        self.scopes: list[str] = []
        self.calls: set[tuple[str, str]] = set()

    @property
    def owner(self) -> str:
        return ".".join(self.scopes) if self.scopes else "<module>"

    def _scope(self, node: ast.AST, name: str) -> None:
        self.scopes.append(name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node, node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._scope(node, "<lambda>")

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._scope(node, "<comprehension>")

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Call(self, node: ast.Call) -> None:
        origin = _origin(node.func, self.bindings)
        if origin == f"{SERVICE_MODULE}.MintService":
            self.calls.add((self.owner, "construct"))
        elif origin in {
            f"{SERVICE_MODULE}.MintService.exchange",
            f"{SERVICE_MODULE}.MintService.mint",
        }:
            self.calls.add((self.owner, origin.rsplit(".", 1)[-1]))
        elif (
            self.module == "_auth/master_token_bootstrap.py"
            and self.owner
            in {
                "MasterTokenBootstrapper._exchange",
                "MasterTokenBootstrapper._mint",
            }
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
            and node.func.value.attr == "_mint_service"
            and node.func.attr in {"exchange", "mint"}
        ):
            self.calls.add((self.owner, node.func.attr))
        self.generic_visit(node)


def _service_calls(root: Path = SRC_ROOT) -> set[tuple[str, str, str]]:
    calls: set[tuple[str, str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        tree = _tree(path)
        relative = path.relative_to(root).as_posix()
        collector = _ServiceCallCollector(
            _bindings(tree, path=path, src_root=root), module=relative
        )
        collector.visit(tree)
        calls.update((relative, owner, kind) for owner, kind in collector.calls)
    return calls


def _module_importers(module: str, root: Path = SRC_ROOT) -> set[str]:
    importers: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = _tree(path)
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.Import):
                hit = any(item.name == module for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_import_from(node, path=path, src_root=root)
                hit = resolved == module or (
                    resolved == module.rsplit(".", 1)[0]
                    and any(item.name == module.rsplit(".", 1)[-1] for item in node.names)
                )
            elif isinstance(node, ast.Call):
                name = _qualified_name(node.func)
                hit = bool(
                    name in {"importlib.import_module", "__import__"}
                    and node.args
                    and _static_string(node.args[0]) == module
                )
            if hit:
                importers.add(path.relative_to(root).as_posix())
    return importers


def _production_perform_oauth_calls(root: Path = SRC_ROOT) -> set[tuple[str, str, str]]:
    calls: set[tuple[str, str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        tree = _tree(path)
        bindings = _bindings(tree, path=path, src_root=root)
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and _origin(node.func, bindings) == "gpsoauth.perform_oauth"
            ):
                continue
            owner = "<module>"
            current = parents.get(node)
            while current is not None:
                if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
                    owner = current.name
                    break
                current = parents.get(current)
            calls.add((path.relative_to(root).as_posix(), owner, ast.unparse(node)))
    return calls


def test_service_surface_imports_state_and_signatures_are_exact() -> None:
    tree = _tree(SERVICE_PATH)
    assert _imports(tree) == _EXPECTED_IMPORTS
    assert _top_level_assignments(tree) == _EXPECTED_ASSIGNMENTS
    assert {node.name for node in tree.body if isinstance(node, ast.ClassDef)} == {
        "_MintError",
        "OAuthMintError",
        "OAuthClientSpec",
        "MintedOAuthToken",
        "MintService",
    }
    assert {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    } == {
        "_require_gpsoauth",
        "_quiet_gpsoauth_logging",
        "_perform_oauth",
        "_parse_oauth_expiry",
        "_rotate_post",
        "_rotate_post_sync",
    }
    assert [
        node.name
        for node in _class(tree, "MintService").body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ] == ["exchange", "mint_oauth", "mint"]
    assert inspect.signature(mint_service.MintService) == inspect.Signature()
    assert str(inspect.signature(mint_service.MintService.exchange)) == (
        "(self, email: 'str', oauth_token: 'str', android_id: 'str') -> 'MasterToken'"
    )
    assert str(inspect.signature(mint_service.MintService.mint_oauth)) == (
        "(self, master_token: 'MasterToken', spec: 'OAuthClientSpec') -> 'MintedOAuthToken'"
    )
    assert str(inspect.signature(mint_service.MintService.mint)) == (
        "(self, token: 'MasterToken') -> 'httpx.Cookies'"
    )
    assert mint_service.MintService().__dict__ == {}
    assert _stored_instance_attributes(tree) == set()
    assert issubclass(mint_service._MintError, Exception)
    assert mint_service._MintError("fixed").__dict__ == {}
    assert issubclass(mint_service.OAuthMintError, mint_service._MintError)
    assert mint_service.OAuthMintError("fixed").__dict__ == {}
    assert mint_service.OAuthClientSpec.__dataclass_params__.frozen is True
    assert mint_service.MintedOAuthToken.__dataclass_params__.frozen is True
    token_field = mint_service.MintedOAuthToken.__dataclass_fields__["token"]
    assert token_field.repr is False
    assert mint_service.MintedOAuthToken("ya29.SECRET", None).__repr__() == (
        "MintedOAuthToken(expires_at=None)"
    )
    assert mint_service.logger.name == "notebooklm.auth.master_token"


def test_service_has_no_disk_upward_dynamic_or_scheduling_capability() -> None:
    tree = _tree(SERVICE_PATH)
    assert _forbidden_capabilities(tree) == set()
    to_threads = [
        ast.unparse(node)
        for node in ast.walk(_method(tree, "MintService", "mint_oauth"))
        if isinstance(node, ast.Call) and _qualified_name(node.func) == "asyncio.to_thread"
    ]
    assert to_threads == [
        "asyncio.to_thread(_perform_oauth, gpsoauth, master_token.email, "
        "master_token.secret, master_token.android_id, spec)"
    ]


def test_exchange_mint_http_and_error_shapes_are_exact() -> None:
    tree = _tree(SERVICE_PATH)
    exchange = _method(tree, "MintService", "exchange")
    mint_oauth = _method(tree, "MintService", "mint_oauth")
    mint = _method(tree, "MintService", "mint")
    assert [
        ast.unparse(node)
        for node in sorted(
            (item for item in ast.walk(exchange) if isinstance(item, ast.Call)),
            key=lambda item: (item.lineno, item.col_offset),
        )
        if (_qualified_name(node.func) or "").rsplit(".", 1)[-1]
        in {"exchange_token", "MasterToken"}
    ] == [
        "gpsoauth.exchange_token(email, oauth_token, android_id)",
        "MasterToken(email=email, android_id=android_id, secret=str(token))",
    ]
    assert [
        ast.unparse(node)
        for node in sorted(
            (item for item in ast.walk(mint) if isinstance(item, ast.Call)),
            key=lambda item: (item.lineno, item.col_offset),
        )
        if (_qualified_name(node.func) or "") in {"httpx.AsyncClient", "client.get", "_rotate_post"}
    ] == [
        "httpx.AsyncClient(follow_redirects=True, timeout=30.0)",
        "client.get('https://accounts.google.com/OAuthLogin', params={'source': "
        "'ChromiumBrowser', 'issueuberauth': '1'}, headers=authorization)",
        "client.get('https://accounts.google.com/MergeSession', params={'service': 'mail', "
        "'continue': 'https://www.google.com', 'uberauth': uberauth}, headers=authorization)",
        "_rotate_post(client)",
    ]
    assert [
        ast.unparse(node)
        for node in sorted(
            (item for item in ast.walk(mint_oauth) if isinstance(item, ast.Call)),
            key=lambda item: (item.lineno, item.col_offset),
        )
        if _qualified_name(node.func) == "asyncio.to_thread"
    ] == [
        "asyncio.to_thread(_perform_oauth, gpsoauth, master_token.email, "
        "master_token.secret, master_token.android_id, spec)"
    ]
    assert [
        ast.unparse(node)
        for node in ast.walk(mint)
        if isinstance(node, ast.Call) and _qualified_name(node.func) == "self.mint_oauth"
    ] == ["self.mint_oauth(token, _CHROMECAST_OAUTH_SPEC)"]
    raises: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            if _qualified_name(node.exc.func) in {"_MintError", "OAuthMintError"}:
                raises.add((ast.unparse(node.exc), ast.unparse(node.cause) if node.cause else "-"))
    assert len(raises) == 5
    assert {cause for _expression, cause in raises} == {"-", "None", "exc"}
    dependency_raises = [
        (ast.unparse(node.exc), ast.unparse(node.cause) if node.cause else "-")
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and _qualified_name(node.exc.func) == "MissingDependencyError"
    ]
    assert dependency_raises == [
        (
            'MissingDependencyError("Master-token auth needs gpsoauth. Install: pip install '
            "'notebooklm-py[headless]'\")",
            "exc",
        )
    ]
    assert mint_service._ROTATE_POST_KWARGS == {
        "headers": {
            "Content-Type": "application/json",
            "Origin": "https://accounts.google.com",
        },
        "content": '[000,"-0000000000000000000"]',
        "follow_redirects": True,
        "timeout": 15.0,
    }
    assert mint_service._ROTATE_POST_KWARGS["headers"] is mint_service._KEEPALIVE_ROTATE_HEADERS


def test_public_adapters_are_exact_handler_exit_projections() -> None:
    tree = _tree(ADAPTER_PATH)
    assert _adapter_projection_violations(_function(tree, "exchange_master_token")) == []
    assert _adapter_projection_violations(_function(tree, "mint_cookies")) == []
    source = ADAPTER_PATH.read_text(encoding="utf-8")
    for old_owner in (
        "_require_gpsoauth",
        "_quiet_gpsoauth_logging",
        "_perform_oauth",
        "_ROTATE_POST_KWARGS",
        "httpx.AsyncClient(",
        "gpsoauth.exchange_token",
        "gpsoauth.perform_oauth",
    ):
        assert old_owner not in source
    assert (
        master_token.MasterTokenError is importlib.import_module("notebooklm.auth").MasterTokenError
    )


def test_service_importers_callers_and_lock_boundary_are_exact() -> None:
    assert _module_importers(SERVICE_MODULE) == {
        "_android/auth.py",
        "_auth/keepalive.py",
        "_auth/master_token.py",
        "_auth/master_token_bootstrap.py",
    }
    assert _service_calls() == {
        ("_android/auth.py", "_make_bearer_provider", "construct"),
        ("_auth/master_token.py", "_bootstrapper", "construct"),
        ("_auth/master_token.py", "exchange_master_token", "construct"),
        ("_auth/master_token.py", "exchange_master_token", "exchange"),
        ("_auth/master_token.py", "mint_cookies", "construct"),
        ("_auth/master_token.py", "mint_cookies", "mint"),
        (
            "_auth/master_token_bootstrap.py",
            "MasterTokenBootstrapper._exchange",
            "exchange",
        ),
        ("_auth/master_token_bootstrap.py", "MasterTokenBootstrapper._mint", "mint"),
    }
    assert _module_importers("notebooklm._auth.storage_lock") == {
        "_auth/keepalive.py",
        "_auth/master_token_file.py",
        "_auth/profile_store.py",
        "_auth/storage.py",
    }


def test_source_tree_has_one_owned_perform_oauth_call_site() -> None:
    assert _module_importers("gpsoauth") == {"_auth/mint_service.py"}
    assert _production_perform_oauth_calls() == {
        (
            "_auth/mint_service.py",
            "_perform_oauth",
            "gpsoauth.perform_oauth(email, master_token, android_id, service=spec.service, "
            "app=spec.app, client_sig=spec.client_sig)",
        )
    }


def test_perform_oauth_call_guard_resolves_import_and_assignment_aliases(tmp_path: Path) -> None:
    source = tmp_path / "hidden.py"
    source.write_text(
        "from gpsoauth import perform_oauth as imported\n"
        "alias = imported\n"
        "def hidden():\n"
        "    return alias('e', 'm', 'a', service='s', app='p', client_sig='c')\n",
        encoding="utf-8",
    )

    assert _production_perform_oauth_calls(tmp_path) == {
        (
            "hidden.py",
            "hidden",
            "alias('e', 'm', 'a', service='s', app='p', client_sig='c')",
        )
    }


def test_live_service_has_only_the_sanctioned_secret_carriers() -> None:
    service_tree = _tree(SERVICE_PATH)
    adapter_tree = _tree(ADAPTER_PATH)
    assert _retention_violations(service_tree) == set()
    for name in ("exchange_master_token", "mint_cookies"):
        assert _adapter_projection_violations(_function(adapter_tree, name)) == []


@pytest.mark.parametrize(
    "source",
    [
        "def hidden():\n    from pathlib import Path\n    return Path('token')\n",
        "loader = lambda: __import__('notebooklm._auth.storage')\n",
        "items = [open(name) for name in names]\n",
        "callback = getattr(gateway, 'open')\n",
        "async def later():\n    return await asyncio.create_task(work())\n",
        "if enabled:\n    import notebooklm._auth.keepalive\n",
    ],
)
def test_capability_detector_bites_hidden_dynamic_and_deferred_scopes(source: str) -> None:
    assert _forbidden_capabilities(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "def leak(self, token):\n    self.secret = token.secret\n",
        "def leak(token):\n    _CACHE.append(token.secret)\n",
        "def leak(token, callback):\n    callback(token.secret)\n",
        "def leak(token):\n    values = [token.secret]\n",
        "def leak(token):\n    return lambda value=token.secret: value\n",
        "def leak(token):\n    logger.debug('%s', token.secret)\n",
        "def leak(token, callback):\n    alias = token.secret\n    callback(alias)\n",
        "def leak(token):\n    global CACHE\n    alias = token.secret\n    CACHE = alias\n",
        "def leak(result):\n    logger.debug('%r', result)\n",
        "def leak(token):\n    logger.debug('%s', token)\n",
        "def leak(oauth):\n    logger.debug('%r', oauth)\n",
        "def leak(response):\n    alias = response.get('Token')\n    logger.debug('%s', alias)\n",
        "def leak(response):\n    alias = response.get('Auth')\n    logger.debug('%s', alias)\n",
        "def leak(token):\n"
        "    alias = token.secret\n"
        "    def later(value=alias):\n"
        "        return value\n"
        "    return later\n",
        "def leak(token):\n"
        "    alias = token.secret\n"
        "    class Holder:\n"
        "        value = alias\n"
        "    return Holder\n",
        "def leak(token):\n"
        "    alias = token.secret\n"
        "    class Holder:\n"
        "        if ready:\n"
        "            value = alias\n"
        "    return Holder\n",
        "def leak(token):\n"
        "    alias = token.secret\n"
        "    class Holder:\n"
        "        try:\n"
        "            value = alias\n"
        "        except Exception:\n"
        "            pass\n"
        "    return Holder\n",
        "def leak(token):\n"
        "    alias = token.secret\n"
        "    @register\n"
        "    def later():\n"
        "        if alias:\n"
        "            return True\n",
        "def leak(token):\n"
        "    alias = token.secret\n"
        "    def later():\n"
        "        return alias\n"
        "    return later\n",
        "alias = master_token\nCACHE = alias\n",
        "def leak(token):\n    alias = token.secret\n    yield alias\n",
        "def leak(token):\n    for alias in [token.secret]:\n        pass\n    return alias\n",
        "def leak(token, callback):\n    for alias in [token.secret]:\n        callback(alias)\n",
        "def leak(token, callback):\n    values = [callback(alias) for alias in [token.secret]]\n",
        "async def leak(token, values):\n"
        "    async for alias in values(token.secret):\n"
        "        return alias\n",
        "def leak(token, context):\n"
        "    with context(token.secret) as alias:\n"
        "        return alias\n",
        "def leak(token):\n"
        "    match token.secret:\n"
        "        case alias:\n"
        "            return alias\n",
        "def leak(token):\n"
        "    alias = token.secret\n"
        "    def later(value: alias):\n"
        "        return value\n",
        "def leak(token):\n    alias = token.secret\n    class Holder(alias):\n        pass\n",
        "def leak(token):\n    raise _MintError(f'{token.secret}')\n",
        "def leak(token):\n    return token.secret\n",
        "def leak(service, consume):\n    consume(service.mint)\n",
        "def leak(service):\n    callback = service.exchange\n    return callback\n",
    ],
)
def test_secret_and_callable_carrier_detector_bites_every_third_escape(source: str) -> None:
    assert _retention_violations(ast.parse(source))


@pytest.mark.parametrize(
    "body",
    [
        "from notebooklm._auth.mint_service import MintService as Service\n"
        "def later():\n    return Service().exchange('e', 'o', 'a')\n",
        "import notebooklm._auth.mint_service as service\n"
        "run = lambda token: service.MintService().mint(token)\n",
        "import importlib\n"
        "service = importlib.import_module('notebooklm._auth.' + 'mint_service')\n"
        "calls = [service.MintService() for _ in (0,)]\n",
        "import notebooklm._auth as auth\n"
        "service = getattr(auth, 'mint_' + 'service')\n"
        "if ready:\n    service.MintService()\n",
        "from notebooklm._auth.mint_service import MintService\n"
        "def later():\n"
        "    Factory = MintService\n"
        "    MintService = object\n"
        "    return Factory()\n",
    ],
)
def test_service_caller_collector_bites_alias_local_dynamic_and_deferred_scopes(
    tmp_path: Path, body: str
) -> None:
    root = tmp_path / "notebooklm"
    source = root / "_auth" / "consumer.py"
    source.parent.mkdir(parents=True)
    source.write_text(body, encoding="utf-8")
    assert _service_calls(root)
