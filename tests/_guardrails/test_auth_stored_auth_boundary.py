"""Fail-closed capability guard for the typed stored-auth application service."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
TOKENS_PATH = AUTH_ROOT / "tokens.py"
CLIENT_PATH = SRC_ROOT / "client.py"
FACADE_PATHS = (SRC_ROOT / "auth.py", SRC_ROOT / "__init__.py")

_TOKENS_MODULE = "notebooklm._auth.tokens"
_LOAD = "_load_stored_auth"
_RAW_PROJECTION = "_resolve_with_projection"
_TYPED_MERGE = "merge_cookie_observation"

Call = tuple[str, str]
Violation = tuple[str, str, str]

_VALUE_FIELDS: dict[str, tuple[str, ...]] = {
    "InlineAuthSource": ("document",),
    "FileAuthSource": ("store", "profile"),
    "SessionSeed": ("live", "baseline"),
    "LoadPolicy": ("allow_headless", "heal_psidts"),
    "TokenAcquisition": (
        "csrf_token",
        "session_id",
        "live",
        "baseline_before_fetch_mutations",
        "final_account",
    ),
    "InlineLoadedAuth": ("auth",),
    "FileLoadedAuth": ("auth", "store", "persistence_baseline"),
}
_REDACTED_VALUES = frozenset(_VALUE_FIELDS) - {"LoadPolicy"}
_SECRET_FIELDS: dict[str, frozenset[str]] = {
    "SessionSeed": frozenset({"live", "baseline"}),
    "TokenAcquisition": frozenset(
        {"csrf_token", "session_id", "live", "baseline_before_fetch_mutations"}
    ),
    "FileLoadedAuth": frozenset({"persistence_baseline"}),
}
_INTERNAL_NAMES = frozenset(
    {
        *_VALUE_FIELDS,
        "ResolvedAuthSource",
        "LoadedAuth",
        "SessionSeedLoader",
        "AccountRouteResolver",
        "TokenAcquirer",
        "StoredAuthLoader",
        _LOAD,
    }
)
_AUTH_FIELDS = frozenset(
    {
        "cookies",
        "csrf_token",
        "session_id",
        "storage_path",
        "cookie_jar",
        "authuser",
        "cookie_snapshot",
        "account_email",
        "_profile_session_generation",
    }
)
_FORBIDDEN_ACCOUNT_READERS = frozenset(
    {
        "read_account_metadata",
        "read_account_metadata_from_storage_state",
        "get_authuser_for_storage",
        "get_account_email_for_storage",
        "resolve_account_identity",
    }
)
_FORBIDDEN_TOKEN_MODULES = frozenset(
    {
        "filelock",
        "os",
        "subprocess",
        "threading",
        "notebooklm._atomic_io",
        "notebooklm._browser.browser_capture",
        "notebooklm._auth.credential_io",
        "notebooklm._browser.headless_reauth",
        "notebooklm._auth.keepalive",
        "notebooklm._auth.master_token",
        "notebooklm._auth.master_token_bootstrap",
        "notebooklm._auth.mint_service",
        "notebooklm._auth.recovery",
        "notebooklm._auth.storage_lock",
    }
)
_FORBIDDEN_TOKEN_CAPABILITIES = frozenset(
    {
        *_FORBIDDEN_ACCOUNT_READERS,
        "FileLock",
        "StorageLockManager",
        "_atomic_write_json_unchecked",
        "_commit_json_unchecked",
        "_commit_profile_json",
        "atomic_write_json",
        "build_httpx_cookies_from_storage",
        "_fetch_tokens_with_refresh",
        "save_cookies_to_storage",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "_run_refresh_cmd",
        "_coalesced_run_refresh_cmd",
        "_should_try_refresh",
        "NOTEBOOKLM_REFRESH_CMD_ENV",
        "_REFRESH_ATTEMPTED_CONTEXT",
        "_REFRESH_ATTEMPTED_ENV",
        "coalesced_cold_recovery",
        "ColdRecoveryCoordinator",
        "_run_cold_recovery",
        "try_headless_reauth",
        "attempt_headless_reauth",
        "try_master_token_reauth",
        "remint_from_stored_token",
        "mint_cookies",
        "MintService",
        "_MintError",
        "exchange",
        "mint",
        "read_master_token",
        "_recover_psidts_inline",
        "recover_psidts_in_memory",
        "_attempt_rotation",
        "_rotate_post",
        "_rotate_post_sync",
    }
)
_MUTABLE_FACTORIES = frozenset(
    {
        "dict",
        "list",
        "set",
        "defaultdict",
        "WeakKeyDictionary",
        "WeakValueDictionary",
        "Lock",
        "RLock",
        "Event",
        "Condition",
        "Semaphore",
    }
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _source_label(path: Path) -> str:
    if path == CLIENT_PATH:
        return "client.py"
    try:
        return path.relative_to(AUTH_ROOT).as_posix()
    except ValueError:
        return path.relative_to(SRC_ROOT).as_posix()


def _owner(class_stack: list[str], function_stack: list[str]) -> str:
    names = [*class_stack, *function_stack[:1]]
    return ".".join(names) if names else "<module>"


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        if owner is not None:
            return f"{owner}.{node.attr}"
    return None


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts = [_static_string(item) for item in node.values]
        return "".join(part for part in parts if part is not None) if None not in parts else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and not node.keywords
        and len(node.args) == 1
        and isinstance(node.args[0], ast.List | ast.Tuple)
    ):
        separator = _static_string(node.func.value)
        parts = [_static_string(item) for item in node.args[0].elts]
        if separator is not None and None not in parts:
            return separator.join(part for part in parts if part is not None)
    return None


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


def _target_names(node: ast.AST | None) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    if isinstance(node, ast.Tuple | ast.List):
        return set().union(*(_target_names(item) for item in node.elts), set())
    return set()


def _statement_bindings(node: ast.stmt) -> set[str]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {node.name}
    if isinstance(node, ast.Assign):
        return set().union(*(_target_names(target) for target in node.targets), set())
    if isinstance(node, ast.AnnAssign | ast.AugAssign):
        return _target_names(node.target)
    if isinstance(node, ast.Import):
        return {item.asname or item.name.split(".", 1)[0] for item in node.names}
    if isinstance(node, ast.ImportFrom):
        return {item.asname or item.name for item in node.names}
    return set()


class _LexicalBindings(ast.NodeVisitor):
    """Collect function-local bindings without leaking child lexical scopes."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

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
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.names.add(node.name)
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.names.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.names.add(node.rest)
        self.generic_visit(node)

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        results: tuple[ast.expr, ...],
    ) -> None:
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for result in results:
            self.visit(result)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))


def _lexical_bindings(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> set[str]:
    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    if node.args.vararg is not None:
        arguments.append(node.args.vararg)
    if node.args.kwarg is not None:
        arguments.append(node.args.kwarg)
    collector = _LexicalBindings()
    body: Iterable[ast.AST] = (node.body,) if isinstance(node, ast.Lambda) else node.body
    for item in body:
        collector.visit(item)
    return (
        ({argument.arg for argument in arguments} | collector.names)
        - collector.global_names
        - collector.nonlocal_names
    )


def _module_load_bindings(path: Path, tree: ast.Module) -> dict[str, str]:
    """Return final module globals relevant to deferred runtime lookup."""
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                bound = item.asname or item.name.split(".", 1)[0]
                if item.name == _TOKENS_MODULE:
                    bindings[bound] = "provider" if item.asname is not None else "provider_root"
                else:
                    bindings.pop(bound, None)
            continue
        if isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            for item in node.names:
                bound = item.asname or item.name
                if resolved == _TOKENS_MODULE and item.name == _LOAD:
                    bindings[bound] = "direct"
                elif resolved == "notebooklm._auth" and item.name == "tokens":
                    bindings[bound] = "provider"
                else:
                    bindings.pop(bound, None)
            continue
        for name in _statement_bindings(node):
            bindings.pop(name, None)
        if (
            path == TOKENS_PATH
            and isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == _LOAD
        ):
            bindings[node.name] = "direct"
    return bindings


class _StoredLoadCollector(ast.NodeVisitor):
    """Find exact load calls and reject every way the capability can escape."""

    def __init__(self, path: Path, tree: ast.Module) -> None:
        self.path = path
        self.module = _source_label(path)
        self.bindings = _module_load_bindings(path, tree)
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []
        self.scope_frames: list[tuple[str, set[str]]] = []
        self.callees: set[int] = set()
        self.calls: set[Call] = set()
        self.violations: set[Violation] = set()

    @property
    def owner(self) -> str:
        return _owner(self.class_stack, self.function_stack)

    def _shadowed(self, name: str) -> bool:
        inside_function = False
        for kind, names in reversed(self.scope_frames):
            if kind in {"function", "lambda"}:
                inside_function = True
            if kind == "class" and inside_function:
                continue
            if name in names:
                return True
        return False

    def _provider(self, node: ast.expr) -> bool:
        qualified = _qualified_name(node)
        if qualified == _TOKENS_MODULE:
            root = qualified.split(".", 1)[0]
            return self.bindings.get(root) == "provider_root" and not self._shadowed(root)
        return (
            isinstance(node, ast.Name)
            and self.bindings.get(node.id) == "provider"
            and not self._shadowed(node.id)
        )

    def _direct(self, node: ast.Name) -> bool:
        return self.bindings.get(node.id) == "direct" and not self._shadowed(node.id)

    def _record_violation(self, kind: str) -> None:
        self.violations.add((self.module, self.owner, kind))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        lexical = _LexicalBindings()
        for statement in node.body:
            lexical.visit(statement)
        self.class_stack.append(node.name)
        self.scope_frames.append(("class", lexical.names))
        for statement in node.body:
            self.visit(statement)
        self.scope_frames.pop()
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        self.function_stack.append(node.name)
        self.scope_frames.append(("function", _lexical_bindings(node)))
        for statement in node.body:
            self.visit(statement)
        self.scope_frames.pop()
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in node.args.defaults:
            self.visit(default)
        self.function_stack.append("<lambda>")
        self.scope_frames.append(("lambda", _lexical_bindings(node)))
        self.visit(node.body)
        self.scope_frames.pop()
        self.function_stack.pop()

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        results: tuple[ast.expr, ...],
    ) -> None:
        self.scope_frames.append(("comprehension", set()))
        for generator in generators:
            self.visit(generator.iter)
            self.scope_frames[-1][1].update(_target_names(generator.target))
            for condition in generator.ifs:
                self.visit(condition)
        for result in results:
            self.visit(result)
        self.scope_frames.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def visit_Call(self, node: ast.Call) -> None:
        dynamic = (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == _LOAD
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "__getattribute__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == _LOAD
        )
        if dynamic:
            self._record_violation("dynamic-access")
        if isinstance(node.func, ast.Attribute) and node.func.attr == _LOAD:
            self.callees.add(id(node.func))
            if self._provider(node.func.value):
                self.calls.add((self.module, self.owner))
            else:
                self._record_violation("untrusted-qualified-call")
        elif isinstance(node.func, ast.Name) and (
            node.func.id == _LOAD or self.bindings.get(node.func.id) == "direct"
        ):
            self.callees.add(id(node.func))
            if self._direct(node.func):
                self.calls.add((self.module, self.owner))
            else:
                self._record_violation("untrusted-direct-call")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if _resolved_module(self.path, node) == _TOKENS_MODULE and any(
            item.name in {_LOAD, "*"} for item in node.names
        ):
            self._record_violation("direct-import-alias")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == _LOAD and id(node) not in self.callees:
            self._record_violation("capability-escape")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and id(node) not in self.callees
            and (node.id == _LOAD or self.bindings.get(node.id) == "direct")
        ):
            self._record_violation("capability-escape")


def _stored_load_projection(path: Path, tree: ast.Module) -> tuple[set[Call], set[Violation]]:
    collector = _StoredLoadCollector(path, tree)
    collector.visit(tree)
    return collector.calls, collector.violations


class _PrivateMemberCollector(ast.NodeVisitor):
    def __init__(self, module: str, target: str) -> None:
        self.module = module
        self.target = target
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []
        self.callees: set[int] = set()
        self.calls: set[Call] = set()
        self.escapes: set[Call] = set()

    @property
    def owner(self) -> str:
        return _owner(self.class_stack, self.function_stack)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        offloaded = (
            _qualified_name(node.func) == "asyncio.to_thread"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == self.target
        )
        if offloaded:
            callback = node.args[0]
            if not isinstance(callback, ast.Attribute):  # pragma: no cover - narrowed above
                raise AssertionError("offloaded callback must be an attribute")
            self.callees.add(id(callback))
            self.calls.add((self.module, self.owner))
        elif isinstance(node.func, ast.Attribute) and node.func.attr == self.target:
            self.callees.add(id(node.func))
            self.calls.add((self.module, self.owner))
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == self.target
        ) or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "__getattribute__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == self.target
        ):
            self.escapes.add((self.module, self.owner))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == self.target and id(node) not in self.callees:
            self.escapes.add((self.module, self.owner))
        self.generic_visit(node)


def _member_projection(target: str) -> tuple[set[Call], set[Call]]:
    calls: set[Call] = set()
    escapes: set[Call] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        collector = _PrivateMemberCollector(_source_label(path), target)
        collector.visit(_tree(path))
        calls.update(collector.calls)
        escapes.update(collector.escapes)
    return calls, escapes


def _top_level_classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _top_level_functions(
    tree: ast.Module,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _class_methods(
    node: ast.ClassDef,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        member.name: member
        for member in node.body
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _class_fields(node: ast.ClassDef) -> tuple[str, ...]:
    return tuple(
        member.target.id
        for member in node.body
        if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name)
    )


def _dataclass_options(node: ast.ClassDef) -> dict[str, object]:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call) and _qualified_name(decorator.func) in {
            "dataclass",
            "dataclasses.dataclass",
        }:
            return {
                keyword.arg: ast.literal_eval(keyword.value)
                for keyword in decorator.keywords
                if keyword.arg is not None
            }
    return {}


def _field_is_repr_false(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and _qualified_name(node.func) in {"field", "dataclasses.field"}
        and any(
            keyword.arg == "repr"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in node.keywords
        )
    )


def _union_members(tree: ast.Module, name: str) -> tuple[str, ...]:
    values = {
        node.target.id: node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.value is not None
    }
    value = values[name]

    def flatten(item: ast.expr) -> list[str]:
        if isinstance(item, ast.BinOp) and isinstance(item.op, ast.BitOr):
            return [*flatten(item.left), *flatten(item.right)]
        return [ast.unparse(item)]

    return tuple(flatten(value))


def _protocol_classes(tree: ast.Module) -> set[str]:
    bindings: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and _resolved_module(TOKENS_PATH, node) == "typing":
            for item in node.names:
                bound = item.asname or item.name
                if item.name == "Protocol":
                    bindings[bound] = "protocol"
                else:
                    bindings.pop(bound, None)
            continue
        if isinstance(node, ast.Import):
            for item in node.names:
                bound = item.asname or item.name.split(".", 1)[0]
                if item.name == "typing":
                    bindings[bound] = "typing"
                else:
                    bindings.pop(bound, None)
            continue
        for bound in _statement_bindings(node):
            bindings.pop(bound, None)
    protocols: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            direct = isinstance(base, ast.Name) and bindings.get(base.id) == "protocol"
            qualified = (
                isinstance(base, ast.Attribute)
                and base.attr == "Protocol"
                and isinstance(base.value, ast.Name)
                and bindings.get(base.value.id) == "typing"
            )
            if direct or qualified:
                protocols.add(node.name)
    return protocols


def _find_method(tree: ast.Module, class_name: str, method_name: str) -> ast.AST:
    cls = _top_level_classes(tree)[class_name]
    return _class_methods(cls)[method_name]


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _token_capability_violations(tree: ast.Module) -> set[str]:
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if any(
                    item.name == module or item.name.startswith(f"{module}.")
                    for module in _FORBIDDEN_TOKEN_MODULES
                ):
                    violations.add(f"import:{item.name}")
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(TOKENS_PATH, node)
            targets = {resolved, *(f"{resolved}.{item.name}" for item in node.names)}
            for target in targets:
                if any(
                    target == module or target.startswith(f"{module}.")
                    for module in _FORBIDDEN_TOKEN_MODULES
                ):
                    violations.add(f"import:{target}")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in _FORBIDDEN_TOKEN_CAPABILITIES:
                violations.add(f"call:{name}")
            qualified = _qualified_name(node.func)
            if (
                qualified in {"importlib.import_module", "__import__", "import_module"}
                and node.args
            ):
                target = _static_string(node.args[0])
                if target is not None and any(
                    target == module or target.startswith(f"{module}.")
                    for module in _FORBIDDEN_TOKEN_MODULES
                ):
                    violations.add(f"dynamic-import:{target}")
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and _static_string(node.args[1]) in _FORBIDDEN_TOKEN_CAPABILITIES
            ):
                violations.add(f"dynamic:{_static_string(node.args[1])}")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in _FORBIDDEN_TOKEN_CAPABILITIES
        ):
            violations.add(f"load:{node.attr}")
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in _FORBIDDEN_TOKEN_CAPABILITIES
        ):
            violations.add(f"load:{node.id}")
    return violations


def _mutable_value(node: ast.expr | None) -> bool:
    if isinstance(node, ast.List | ast.Dict | ast.Set | ast.ListComp | ast.DictComp | ast.SetComp):
        return True
    return (
        isinstance(node, ast.Call)
        and (_qualified_name(node.func) or "").rsplit(".", 1)[-1] in _MUTABLE_FACTORIES
    )


def _mutable_process_state(tree: ast.Module) -> set[str]:
    violations: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and _mutable_value(node.value):
            for target in node.targets:
                violations.update(name for name in _target_names(target) if name != "__all__")
        elif isinstance(node, ast.AnnAssign) and _mutable_value(node.value):
            violations.update(name for name in _target_names(node.target) if name != "__all__")
        elif isinstance(node, ast.AugAssign):
            violations.update(_target_names(node.target))
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.Assign) and _mutable_value(member.value):
                    violations.update(
                        f"{node.name}.{name}"
                        for target in member.targets
                        for name in _target_names(target)
                    )
                elif isinstance(member, ast.AnnAssign) and _mutable_value(member.value):
                    violations.update(
                        f"{node.name}.{name}" for name in _target_names(member.target)
                    )
    if any(isinstance(node, ast.Global) for node in ast.walk(tree)):
        violations.add("<global-statement>")
    return violations


def _stored_self_attributes(node: ast.ClassDef) -> set[str]:
    attributes: set[str] = set()
    for member in node.body:
        if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(member):
            targets: list[ast.expr] = []
            if isinstance(inner, ast.Assign):
                targets.extend(inner.targets)
            elif isinstance(inner, ast.AnnAssign | ast.AugAssign):
                targets.append(inner.target)
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    attributes.add(target.attr)
    return attributes


def _all_value(tree: ast.Module) -> tuple[str, ...]:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
            and isinstance(node.value, ast.List | ast.Tuple)
        ):
            return tuple(
                item.value
                for item in node.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    raise AssertionError("tokens.__all__ must be a literal list or tuple")


def _explicit_exports(tree: ast.Module) -> set[str]:
    exports: set[str] = set()
    for node in tree.body:
        value: ast.expr | None = None
        assigned = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        )
        annotated = (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        )
        if assigned or annotated:
            value = node.value
        if isinstance(value, ast.List | ast.Tuple | ast.Set):
            exports.update(
                item.value
                for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return exports


def test_internal_value_shapes_protocol_and_composition_family_are_exact() -> None:
    tree = _tree(TOKENS_PATH)
    classes = _top_level_classes(tree)
    for name, fields in _VALUE_FIELDS.items():
        assert _class_fields(classes[name]) == fields
        options = _dataclass_options(classes[name])
        assert options.get("frozen") is True
        assert options.get("slots") is True
        if name in _REDACTED_VALUES:
            assert options.get("repr") is False
    for class_name, secret_fields in _SECRET_FIELDS.items():
        field_nodes = {
            member.target.id: member
            for member in classes[class_name].body
            if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name)
        }
        assert all(_field_is_repr_false(field_nodes[name].value) for name in secret_fields)
    assert _union_members(tree, "ResolvedAuthSource") == ("InlineAuthSource", "FileAuthSource")
    assert _union_members(tree, "LoadedAuth") == ("InlineLoadedAuth", "FileLoadedAuth")
    assert _protocol_classes(tree) == {"TokenAcquirer"}
    assert {name for name in classes if name.endswith(("Loader", "Resolver"))} == {
        "SessionSeedLoader",
        "AccountRouteResolver",
        "StoredAuthLoader",
    }
    concrete_acquirers = {
        name
        for name, node in classes.items()
        if name != "TokenAcquirer" and "acquire" in _class_methods(node)
    }
    assert len(concrete_acquirers) == 1
    assert all(name.startswith("_") for name in concrete_acquirers)
    assert {name for name in _top_level_functions(tree) if "stored_auth" in name} == {_LOAD}


def test_protocol_and_second_family_detectors_bite() -> None:
    protocol_tree = ast.parse(
        "from typing import Protocol\n"
        "class TokenAcquirer(Protocol): pass\n"
        "class SecondPort(Protocol): pass\n"
    )
    assert _protocol_classes(protocol_tree) == {"TokenAcquirer", "SecondPort"}
    shadowed = ast.parse(
        "from typing import Protocol\nclass TokenAcquirer(Protocol): pass\nProtocol = object\n"
    )
    assert _protocol_classes(shadowed) == set()
    family = _top_level_classes(
        ast.parse("class SessionSeedLoader: pass\nclass ShadowLoader: pass\n")
    )
    assert {name for name in family if name.endswith("Loader")} == {
        "SessionSeedLoader",
        "ShadowLoader",
    }


def test_auth_type_is_a_required_concrete_class_on_both_load_boundaries() -> None:
    tree = _tree(TOKENS_PATH)
    method = _class_methods(_top_level_classes(tree)["StoredAuthLoader"])["load"]
    function = _top_level_functions(tree)[_LOAD]
    for node in (method, function):
        keyword_args = {argument.arg: index for index, argument in enumerate(node.args.kwonlyargs)}
        assert "auth_type" in keyword_args
        index = keyword_args["auth_type"]
        assert ast.unparse(node.args.kwonlyargs[index].annotation) == "type[AuthTokens]"
        assert node.args.kw_defaults[index] is None


def test_canonical_stored_load_callers_are_exact_and_capability_never_escapes() -> None:
    calls: set[Call] = set()
    violations: set[Violation] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        found, rejected = _stored_load_projection(path, _tree(path))
        calls.update(found)
        violations.update(rejected)
    assert calls == {
        ("tokens.py", "AuthTokens.from_storage"),
        ("client.py", "_FromStorageContext._build"),
    }
    assert violations == set()


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        (
            "from notebooklm._auth import tokens as provider\n"
            "def forbidden():\n    callback = provider._load_stored_auth\n    return callback\n",
            "capability-escape",
        ),
        (
            "from notebooklm._auth import tokens as provider\n"
            "def forbidden(consume):\n"
            "    return consume(callback=provider._load_stored_auth)\n",
            "capability-escape",
        ),
        (
            "from notebooklm._auth import tokens as provider\n"
            "def forbidden():\n    return getattr(provider, '_load_stored_auth')\n",
            "dynamic-access",
        ),
        (
            "from notebooklm._auth import tokens as provider\n"
            "def forbidden():\n    return provider._load_stored_auth()\n"
            "provider = object()\n",
            "untrusted-qualified-call",
        ),
        (
            "def forbidden():\n"
            "    from notebooklm._auth.tokens import _load_stored_auth as load\n"
            "    return load()\n",
            "direct-import-alias",
        ),
        (
            "from notebooklm._auth import tokens as provider\n"
            "def forbidden():\n    provider = object()\n    return provider._load_stored_auth()\n",
            "untrusted-qualified-call",
        ),
        (
            "from notebooklm._auth import tokens as provider\n"
            "def forbidden(items):\n"
            "    return [provider._load_stored_auth() for provider in items]\n",
            "untrusted-qualified-call",
        ),
        (
            "from notebooklm._auth import tokens as provider\n"
            "def forbidden(value):\n"
            "    return (lambda provider: provider._load_stored_auth())(value)\n",
            "untrusted-qualified-call",
        ),
    ],
    ids=[
        "callback-assignment",
        "callback-keyword",
        "dynamic",
        "deferred-module",
        "local-direct-alias",
        "local-shadow",
        "comprehension",
        "lambda",
    ],
)
def test_stored_load_binding_and_escape_detector_bites(source: str, kind: str) -> None:
    path = SRC_ROOT / "synthetic.py"
    calls, violations = _stored_load_projection(path, ast.parse(source))
    assert calls == set()
    assert any(violation_kind == kind for _, _, violation_kind in violations)


def test_qualified_third_stored_load_caller_is_rejected() -> None:
    path = SRC_ROOT / "synthetic.py"
    calls, violations = _stored_load_projection(
        path,
        ast.parse(
            "from notebooklm._auth import tokens as provider\n"
            "def forbidden():\n    return provider._load_stored_auth()\n"
        ),
    )
    assert calls == {("synthetic.py", "forbidden")}
    assert violations == set()


def test_direct_import_alias_third_caller_and_capability_escape_are_rejected() -> None:
    path = SRC_ROOT / "synthetic.py"
    calls, violations = _stored_load_projection(
        path,
        ast.parse(
            "from notebooklm._auth.tokens import _load_stored_auth as load\n"
            "def forbidden(consume):\n"
            "    result = load()\n"
            "    return consume(load), result\n"
        ),
    )
    assert calls == {("synthetic.py", "forbidden")}
    assert {kind for _, _, kind in violations} == {
        "capability-escape",
        "direct-import-alias",
    }


def test_public_classmethod_is_only_an_adapter_and_passes_runtime_cls() -> None:
    tree = _tree(TOKENS_PATH)
    method = _find_method(tree, "AuthTokens", "from_storage")
    load_calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and _call_name(node) == _LOAD
    ]
    assert len(load_calls) == 1
    auth_type = [keyword for keyword in load_calls[0].keywords if keyword.arg == "auth_type"]
    assert len(auth_type) == 1
    assert isinstance(auth_type[0].value, ast.Name) and auth_type[0].value.id == "cls"
    forbidden = _FORBIDDEN_ACCOUNT_READERS | {
        "_load_storage_state",
        "build_httpx_cookies_from_storage",
        "_fetch_tokens_with_refresh",
        "save_cookies_to_storage",
        "merge_cookie_observation",
        "NotebookLMClient",
    }
    assert {
        _call_name(node)
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and _call_name(node) in forbidden
    } == set()


def test_client_has_no_direct_authtokens_loader_and_uses_late_module_provider() -> None:
    tree = _tree(CLIENT_PATH)
    build = _find_method(tree, "_FromStorageContext", "_build")
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_storage"
        for node in ast.walk(build)
    )
    load_calls = [
        node
        for node in ast.walk(build)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == _LOAD
    ]
    assert len(load_calls) == 1
    assert isinstance(load_calls[0].func.value, ast.Name)
    assert load_calls[0].func.value.id == "_auth_tokens"
    assert any(
        isinstance(node, ast.ImportFrom)
        and _resolved_module(CLIENT_PATH, node) == "notebooklm._auth"
        and any(item.name == "tokens" and item.asname == "_auth_tokens" for item in node.names)
        for node in tree.body
    )


@pytest.mark.asyncio
async def test_client_loader_patch_installed_after_import_bites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import notebooklm.client as client_module
    from notebooklm._auth import tokens as token_module

    jar = httpx.Cookies()
    auth = token_module.AuthTokens({}, "csrf", "session", cookie_jar=jar)
    calls: list[type[token_module.AuthTokens]] = []

    async def fake_loader(**kwargs: object) -> object:
        auth_type = kwargs["auth_type"]
        assert isinstance(auth_type, type)
        calls.append(auth_type)
        return token_module.InlineLoadedAuth(auth)

    monkeypatch.setattr(token_module, _LOAD, fake_loader)
    context = client_module.NotebookLMClient.from_storage()
    built = await context._build()
    assert built.auth is auth
    assert calls == [token_module.AuthTokens]


def test_raw_projection_and_typed_merge_callers_are_exact_and_do_not_escape() -> None:
    projection_calls, projection_escapes = _member_projection(_RAW_PROJECTION)
    assert projection_calls == {
        ("profile_migration.py", "LegacyAccountMigrator.resolve"),
        ("storage.py", "read_account_metadata"),
        ("tokens.py", "AccountRouteResolver.resolve"),
    }
    assert projection_escapes == set()
    merge_calls, merge_escapes = _member_projection(_TYPED_MERGE)
    assert merge_calls == {
        ("_web/transport/cookie_persistence.py", "CookiePersistence._save_canonical"),
        ("psidts_recovery.py", "_attempt_rotation"),
        ("refresh.py", "_merge_domain_fetch_observation"),
        ("storage.py", "merge_cookie_delta"),
        ("tokens.py", "StoredAuthLoader._merge_observation"),
    }
    assert merge_escapes == set()


@pytest.mark.parametrize("target", [_RAW_PROJECTION, _TYPED_MERGE])
def test_private_capability_callback_bound_method_and_dynamic_access_bite(target: str) -> None:
    tree = ast.parse(
        "def forbidden(service, value):\n"
        f"    callback = service.{target}\n"
        f"    direct = service.{target}(value)\n"
        f"    dynamic = getattr(service, {target!r})\n"
        "    return direct, callback, dynamic\n"
    )
    collector = _PrivateMemberCollector("synthetic.py", target)
    collector.visit(tree)
    assert collector.calls == {("synthetic.py", "forbidden")}
    assert collector.escapes == {("synthetic.py", "forbidden")}


def test_account_route_resolver_imports_no_storage_facade_account_readers() -> None:
    tree = _tree(TOKENS_PATH)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(item.name for item in node.names)
    assert imported.isdisjoint(_FORBIDDEN_ACCOUNT_READERS)
    resolver = _top_level_classes(tree)["AccountRouteResolver"]
    used = {
        name
        for node in ast.walk(resolver)
        if isinstance(node, ast.Call) and (name := _call_name(node)) is not None
    }
    assert used.isdisjoint(_FORBIDDEN_ACCOUNT_READERS)


def test_tokens_has_no_ladder_copy_raw_write_lock_thread_or_process_state() -> None:
    tree = _tree(TOKENS_PATH)
    assert _token_capability_violations(tree) == set()
    assert _mutable_process_state(tree) == set()


def test_forbidden_token_capability_and_mutable_state_detectors_bite() -> None:
    capability = ast.parse(
        "from .master_token import remint_from_stored_token\n"
        "async def copied(path):\n    return await remint_from_stored_token(path)\n"
    )
    assert _token_capability_violations(capability)
    mutable = ast.parse("_REGISTRY = {}\n")
    assert _mutable_process_state(mutable) == {"_REGISTRY"}


@pytest.mark.parametrize(
    "source",
    [
        "from .mint_service import MintService as Service\nService()\n",
        "import importlib\nservice = importlib.import_module('notebooklm._auth.' + 'mint_service')\n",
        "def later(service):\n    return service.mint\n",
        "callback = getattr(service, 'mi' + 'nt')\nconsume(callback)\n",
        "run = lambda service: service.exchange(token)\n",
        "callbacks = [service._rotate_post for service in services]\n",
        "def later(service):\n    callback = service._rotate_post_sync\n    return callback\n",
    ],
)
def test_new_mint_service_capability_detector_bites_every_deferred_escape(source: str) -> None:
    assert _token_capability_violations(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "from .recovery import ColdRecoveryCoordinator as Coordinator\nCoordinator()\n",
        "def later(recovery):\n    return recovery.ColdRecoveryCoordinator\n",
        "callback = getattr(recovery, 'ColdRecovery' + 'Coordinator')\nconsume(callback)\n",
    ],
)
def test_cold_recovery_coordinator_capability_detector_bites_every_escape(source: str) -> None:
    assert _token_capability_violations(ast.parse(source))


def test_authtokens_retains_no_new_service_or_typed_baseline_capability() -> None:
    tree = _tree(TOKENS_PATH)
    auth = _top_level_classes(tree)["AuthTokens"]
    assert frozenset(_class_fields(auth)) == _AUTH_FIELDS
    assert _stored_self_attributes(auth) <= _AUTH_FIELDS
    annotations = " ".join(
        ast.unparse(member.annotation) for member in auth.body if isinstance(member, ast.AnnAssign)
    )
    for forbidden in (
        "ProfileStore",
        "LegacyAccountMigrator",
        "LegacyPromotionScheduler",
        "StoredAuthLoader",
        "TokenAcquirer",
        "CookieJar",
    ):
        assert forbidden not in annotations


def test_internal_values_and_composition_stay_unexported() -> None:
    assert _all_value(_tree(TOKENS_PATH)) == ("AuthTokens", "load_auth_from_storage")
    for path in FACADE_PATHS:
        tree = _tree(path)
        imported = {
            item.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for item in node.names
        }
        assert imported.isdisjoint(_INTERNAL_NAMES)
        assert _explicit_exports(tree).isdisjoint(_INTERNAL_NAMES)


def test_synthetic_authtokens_service_retention_is_rejected() -> None:
    tree = ast.parse(
        "class AuthTokens:\n"
        "    cookies: dict\n"
        "    loader: StoredAuthLoader\n"
        "    def bind(self, store):\n        self.store = store\n"
    )
    auth = _top_level_classes(tree)["AuthTokens"]
    assert frozenset(_class_fields(auth)) != _AUTH_FIELDS
    assert _stored_self_attributes(auth) == {"store"}
