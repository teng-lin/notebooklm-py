"""Fail-closed capability boundary for cold-recovery composition."""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import notebooklm._auth as auth_package
import notebooklm.auth as auth_facade
from notebooklm._auth import recovery
from notebooklm._auth.cookie_types import CookieJar
from notebooklm._auth.cookies import _LoadedCookiePair
from notebooklm._auth.extraction import _LoginRedirectError
from tests._guardrails._ast_semantics import semantic_hash as _portable_semantic_hash

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTH_ROOT = REPO_ROOT / "src" / "notebooklm" / "_auth"
RECOVERY_PATH = AUTH_ROOT / "recovery.py"
REFRESH_PATH = AUTH_ROOT / "refresh.py"

ImportRecord = tuple[str, int, str, str, str | None]

_CALLBACK_FIELDS = frozenset(
    {
        "_should_try_refresh",
        "_resolve_refresh_path",
        "_run_refresh_attempt",
        "_load_cookie_pair",
        "_run_headless_attempt",
        "_run_master_token_attempt",
        "_validate_recovered",
        "_fetch_recovered",
        "_replace_cookie_jar",
        "_snapshot_cookie_jar",
        "_clone_cookie_jar",
    }
)
_DETECTOR_CALLBACK_FIELDS = _CALLBACK_FIELDS | {"_run_cold_recovery"}
_CONSTRUCTOR_PARAMETERS = (
    "state",
    "single_flight",
    "should_try_refresh",
    "resolve_refresh_path",
    "run_refresh_attempt",
    "load_cookie_pair",
    "run_headless_attempt",
    "run_master_token_attempt",
    "validate_recovered",
    "fetch_recovered",
    "replace_cookie_jar",
    "snapshot_cookie_jar",
    "clone_cookie_jar",
)
_CLOSURES = {
    "should_try_refresh",
    "resolve_refresh_path",
    "run_refresh_attempt",
    "validate_recovered",
    "fetch_recovered",
}
_FORBIDDEN_IMPORTS = {
    "notebooklm.auth",
    "notebooklm.client",
    "notebooklm._runtime",
    "notebooklm._auth.master_token_bootstrap",
    "notebooklm._auth.mint_service",
    "notebooklm._auth.profile_store",
    "notebooklm._auth.refresh",
    "notebooklm._auth.session",
    "notebooklm._auth.storage_lock",
    "notebooklm._auth.tokens",
    "notebooklm.cli",
}
_FORBIDDEN_CAPABILITIES = {
    "FileLock",
    "MasterTokenBootstrapper",
    "MintService",
    "ProfileStore",
    "Protocol",
    "StorageLockManager",
    "TaskGroup",
    "__dict__",
    "__import__",
    "create_task",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "import_module",
    "locals",
    "open",
    "setattr",
}
_EXPECTED_IMPORTS: tuple[ImportRecord, ...] = (
    ("module", 0, "__future__", "annotations", None),
    ("module", 0, "asyncio", "", None),
    ("module", 0, "logging", "", None),
    ("module", 0, "threading", "", None),
    ("module", 0, "weakref", "", None),
    ("module", 0, "collections.abc", "Awaitable", None),
    ("module", 0, "collections.abc", "Callable", None),
    ("module", 0, "collections.abc", "Coroutine", None),
    ("module", 0, "dataclasses", "dataclass", None),
    ("module", 0, "dataclasses", "field", None),
    ("module", 0, "pathlib", "Path", None),
    ("module", 0, "typing", "TYPE_CHECKING", None),
    ("module", 0, "typing", "Any", None),
    ("module", 0, "typing", "ClassVar", None),
    ("module", 0, "typing", "cast", None),
    ("module", 0, "httpx", "", None),
    ("module", 1, "", "single_flight", "_single_flight"),
    ("module", 1, "cookie_types", "CookieJar", None),
    ("module", 1, "paths", "canonical_storage_key", None),
    ("module", 1, "cookies", "_LoadedCookiePair", None),
    ("module", 1, "extraction", "_LoginRedirectError", None),
    ("module", 1, "profile_migration", "_LoadedProfilePair", None),
    ("module", 1, "storage", "CookieSnapshot", None),
    ("ColdRecoveryCoordinator.recover", 1, "extraction", "_LoginRedirectError", None),
    ("ColdRecoveryCoordinator._drive_cold", 1, "extraction", "_LoginRedirectError", None),
    ("ColdRecoveryCoordinator._coalesce_cold", 1, "extraction", "_LoginRedirectError", None),
    ("_run_cold_recovery", 1, "cookies", "_build_cookie_pair_from_storage", None),
    ("_run_cold_recovery", 1, "storage", "snapshot_cookie_jar", None),
    ("coalesced_cold_recovery", 1, "cookies", "_build_cookie_pair_from_storage", None),
    ("coalesced_cold_recovery", 1, "cookies", "_clone_cookie_jar", None),
    ("coalesced_cold_recovery", 1, "storage", "snapshot_cookie_jar", None),
    ("try_storage_cookie_reload", 1, "cookies", "_replace_cookie_jar", None),
    ("try_storage_cookie_reload", 1, "profile_migration", "_load_profile_pair_pure", None),
    ("_try_headless_reauth_result", 1, "cookies", "_build_cookie_pair_from_storage", None),
    ("_try_headless_reauth_result", 1, "recovery_rungs", "installed_headless_rung", None),
    ("try_headless_reauth", 1, "cookies", "_replace_cookie_jar", None),
    ("_run_master_token_reauth", 1, "cookies", "_build_cookie_pair_from_storage", None),
    ("_run_master_token_reauth", 1, "master_token", "MasterTokenError", None),
    ("_run_master_token_reauth", 1, "master_token", "remint_from_stored_token", None),
    ("_try_master_token_reauth_result", 2, "paths", "master_token_path_for", None),
    ("_try_master_token_reauth_result", 1, "cookies", "_clone_cookie_jar", None),
    ("_try_master_token_reauth_result", 1, "cookies", "_LoadedCookiePair", None),
    ("try_master_token_reauth", 1, "cookies", "_replace_cookie_jar", None),
)
_CLASS_HASH = "ebc51a5732119e65bc53999db2749985464ee2a587127c6f32db08a414455c19"
_ADAPTER_HASH = "53737bcc7a75cd7c388108b0f5180df1defdbf2f48fedf2cb5eefc6260aafdb2"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class(tree: ast.Module) -> ast.ClassDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ColdRecoveryCoordinator"
    ]
    assert len(matches) == 1
    return matches[0]


def _method(node: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        member
        for member in node.body
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and member.name == name
    ]
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


def _semantic_hash(node: ast.AST) -> str:
    return _portable_semantic_hash(node)


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.records: list[ImportRecord] = []

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

    def visit_Import(self, node: ast.Import) -> None:
        self.records.extend((self.owner, 0, item.name, "", item.asname) for item in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.records.extend(
            (self.owner, node.level, node.module or "", item.name, item.asname)
            for item in node.names
        )


def _imports(tree: ast.AST) -> tuple[ImportRecord, ...]:
    collector = _ImportCollector()
    collector.visit(tree)
    return tuple(collector.records)


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        return left + right if left is not None and right is not None else None
    return None


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return None


def _matches_forbidden_module(name: str) -> bool:
    return any(name == item or name.startswith(f"{item}.") for item in _FORBIDDEN_IMPORTS)


def _capability_violations(tree: ast.AST) -> set[str]:
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if _matches_forbidden_module(item.name):
                    violations.add(f"import:{item.name}")
        elif isinstance(node, ast.ImportFrom):
            rendered = "." * node.level + (node.module or "")
            absolute = node.module or ""
            if _matches_forbidden_module(absolute) or any(
                item.name in {"refresh", "session", "tokens", "master_token_bootstrap"}
                for item in node.names
            ):
                violations.add(f"import:{rendered}")
        elif isinstance(node, ast.Call):
            name = (_qualified_name(node.func) or "").rsplit(".", 1)[-1]
            if name in _FORBIDDEN_CAPABILITIES:
                violations.add(f"call:{name}")
            if name in {"import_module", "__import__"} and node.args:
                target = _static_string(node.args[0])
                if target is not None and _matches_forbidden_module(target):
                    violations.add(f"dynamic-import:{target}")
            if name == "getattr" and len(node.args) >= 2:
                target = _static_string(node.args[1])
                if target in _FORBIDDEN_CAPABILITIES | _DETECTOR_CALLBACK_FIELDS:
                    violations.add(f"dynamic:{target}")
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_CAPABILITIES:
            violations.add(f"attribute:{node.attr}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in _FORBIDDEN_CAPABILITIES:
                violations.add(f"name:{node.id}")
    return violations


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _is_self_attribute(node: ast.AST, name: str, context: type[ast.expr_context]) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr == name
        and isinstance(node.ctx, context)
    )


def _callback_escape_violations(method: ast.AST) -> set[str]:
    parents = _parents(method)
    violations: set[str] = set()
    for node in ast.walk(method):
        if not (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in _DETECTOR_CALLBACK_FIELDS
        ):
            continue
        parent = parents[node]
        direct_call = isinstance(parent, ast.Call) and parent.func is node
        validator_handoff = (
            isinstance(parent, ast.keyword)
            and parent.value is node
            and parent.arg == node.attr.removeprefix("_")
            and isinstance(parents[parent], ast.Call)
            and _is_self_attribute(parents[parent].func, "_coalesce_cold", ast.Load)
        )
        ancestor = parent
        nested_scope = False
        while ancestor is not method:
            if isinstance(ancestor, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                nested_scope = True
                break
            ancestor = parents[ancestor]
        if nested_scope or not (direct_call or validator_handoff):
            violations.add(f"callback-escape:{node.attr}:{type(parent).__name__}")
    return violations


def _one_shot_violations(method: ast.AsyncFunctionDef) -> set[str]:
    violations = _callback_escape_violations(method)
    if len(method.body) != 2:
        violations.add("recover-body-shape")
        return violations

    claim, winner = method.body
    if not (
        isinstance(claim, ast.With)
        and len(claim.items) == 1
        and _is_self_attribute(claim.items[0].context_expr, "_claim_lock", ast.Load)
        and len(claim.body) == 2
    ):
        violations.add("claim-gate")
        return violations
    loser, mark = claim.body
    if not (
        isinstance(loser, ast.If)
        and _is_self_attribute(loser.test, "_used", ast.Load)
        and len(loser.body) == 1
        and isinstance(loser.body[0], ast.Raise)
        and isinstance(loser.body[0].exc, ast.Call)
        and _qualified_name(loser.body[0].exc.func) == "RuntimeError"
        and len(loser.body[0].exc.args) == 1
        and _static_string(loser.body[0].exc.args[0])
        == "ColdRecoveryCoordinator.recover() is one-shot"
        and not loser.orelse
    ):
        violations.add("loser-gate")

    if not (
        isinstance(mark, ast.Assign)
        and len(mark.targets) == 1
        and _is_self_attribute(mark.targets[0], "_used", ast.Store)
        and isinstance(mark.value, ast.Constant)
        and mark.value.value is True
    ):
        violations.add("winner-mark")

    deleted: list[str] = []
    if isinstance(winner, ast.Try):
        for statement in winner.finalbody:
            if isinstance(statement, ast.Delete) and len(statement.targets) == 1:
                target = statement.targets[0]
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    deleted.append(target.attr)
        if len(winner.finalbody) != len(_CALLBACK_FIELDS):
            violations.add("scrub-count")
    else:
        violations.add("winner-finally")
    if set(deleted) != _CALLBACK_FIELDS or len(deleted) != len(set(deleted)):
        violations.add("scrub-fields")

    stores = [
        node.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ]
    if stores != ["_used"]:
        violations.add("recover-self-state")
    if any(
        isinstance(node, ast.For | ast.AsyncFor | ast.comprehension) for node in ast.walk(method)
    ):
        violations.add("generic-rung-loop")
    if _capability_violations(method):
        violations.add("forbidden-capability")
    return violations


def _constructor_projection(method: ast.FunctionDef) -> tuple[tuple[str, str], ...]:
    projection: list[tuple[str, str]] = []
    for statement in method.body:
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Attribute)
            and isinstance(statement.targets[0].value, ast.Name)
            and statement.targets[0].value.id == "self"
        ):
            return ()
        target = statement.targets[0].attr
        if isinstance(statement.value, ast.Name):
            value = statement.value.id
        elif isinstance(statement.value, ast.Constant):
            value = repr(statement.value.value)
        else:
            value = ast.unparse(statement.value)
        projection.append((target, value))
    return tuple(projection)


def _scrubbed(coordinator: recovery.ColdRecoveryCoordinator) -> None:
    assert set(vars(coordinator)) == {"_claim_lock", "_state", "_single_flight", "_used"}
    assert vars(coordinator)["_used"] is True
    assert all(not hasattr(coordinator, field) for field in _CALLBACK_FIELDS)


def _make_coordinator(
    *,
    should_try_refresh: Callable[[Exception, bool], bool],
    run_refresh_attempt: Callable[
        [Path], Awaitable[tuple[str, str, dict[Any, Any], CookieJar | None]]
    ],
    validate_recovered: Callable[[httpx.Cookies], Awaitable[None]] | None = None,
) -> recovery.ColdRecoveryCoordinator:
    def resolve_refresh_path(_error: ValueError) -> Path:
        return Path("/tmp/cold-recovery-boundary.json")

    def unused_load(_path: Path) -> _LoadedCookiePair:
        raise AssertionError("cold recovery should not load")

    async def unused_headless(_path: Path | None, _allow: bool) -> _LoadedCookiePair | None:
        raise AssertionError("headless recovery should not run")

    async def unused_master(_path: Path | None) -> _LoadedCookiePair | None:
        raise AssertionError("master-token recovery should not run")

    async def unused_validate(_jar: httpx.Cookies) -> None:
        raise AssertionError("validation should not run")

    async def unused_fetch(_jar: httpx.Cookies) -> tuple[str, str]:
        raise AssertionError("final fetch should not run")

    def replace_cookie_jar(_target: httpx.Cookies, _source: httpx.Cookies) -> None:
        raise AssertionError("jar replacement should not run")

    return recovery.ColdRecoveryCoordinator(
        state=recovery.ColdRecoveryState(),
        single_flight=recovery._single_flight.SingleFlight(),
        should_try_refresh=should_try_refresh,
        resolve_refresh_path=resolve_refresh_path,
        run_refresh_attempt=run_refresh_attempt,
        load_cookie_pair=unused_load,
        run_headless_attempt=unused_headless,
        run_master_token_attempt=unused_master,
        validate_recovered=validate_recovered or unused_validate,
        fetch_recovered=unused_fetch,
        replace_cookie_jar=replace_cookie_jar,
        snapshot_cookie_jar=lambda _jar: {},
        clone_cookie_jar=lambda jar: httpx.Cookies(jar),
    )


def test_coordinator_api_state_imports_and_semantic_bodies_are_exact() -> None:
    tree = _tree(RECOVERY_PATH)
    coordinator = _class(tree)
    methods = {
        member.name: member
        for member in coordinator.body
        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert set(methods) == {"__init__", "recover", "_drive_cold", "_coalesce_cold"}
    assert coordinator.bases == []
    assert coordinator.keywords == []
    assert _semantic_hash(coordinator) == _CLASS_HASH
    assert _imports(tree) == _EXPECTED_IMPORTS
    assert _capability_violations(tree) == set()

    constructor = _method(coordinator, "__init__")
    assert isinstance(constructor, ast.FunctionDef)
    assert [argument.arg for argument in constructor.args.args] == ["self"]
    assert [argument.arg for argument in constructor.args.kwonlyargs] == list(
        _CONSTRUCTOR_PARAMETERS
    )
    assert constructor.args.kw_defaults == [None] * len(_CONSTRUCTOR_PARAMETERS)
    assert constructor.args.vararg is None
    assert constructor.args.kwarg is None
    assert _constructor_projection(constructor) == (
        ("_claim_lock", "threading.Lock()"),
        ("_state", "state"),
        ("_single_flight", "single_flight"),
        *((f"_{name}", name) for name in _CONSTRUCTOR_PARAMETERS[2:]),
        ("_used", "False"),
    )

    recover_method = _method(coordinator, "recover")
    assert isinstance(recover_method, ast.AsyncFunctionDef)
    assert _one_shot_violations(recover_method) == set()

    assert str(inspect.signature(recovery.ColdRecoveryCoordinator)) == (
        "(*, state: 'ColdRecoveryState', "
        "single_flight: '_single_flight.SingleFlight', "
        "should_try_refresh: 'Callable[[Exception, bool], bool]', "
        "resolve_refresh_path: 'Callable[[ValueError], Path]', "
        "run_refresh_attempt: 'Callable[[Path], "
        "Awaitable[tuple[str, str, CookieSnapshot, CookieJar | None]]]', "
        "load_cookie_pair: 'Callable[[Path], _LoadedCookiePair]', "
        "run_headless_attempt: 'Callable[[Path | None, bool], "
        "Awaitable[_LoadedCookiePair | None]]', "
        "run_master_token_attempt: 'Callable[[Path | None], "
        "Awaitable[_LoadedCookiePair | None]]', "
        "validate_recovered: 'Callable[[httpx.Cookies], Awaitable[None]]', "
        "fetch_recovered: 'Callable[[httpx.Cookies], Awaitable[tuple[str, str]]]', "
        "replace_cookie_jar: 'Callable[[httpx.Cookies, httpx.Cookies], None]', "
        "snapshot_cookie_jar: 'Callable[[httpx.Cookies], CookieSnapshot]', "
        "clone_cookie_jar: 'Callable[[httpx.Cookies], httpx.Cookies]') -> 'None'"
    )
    assert str(inspect.signature(recovery.ColdRecoveryCoordinator.recover)) == (
        "(self, *, initial_error: 'ValueError', cookie_jar: 'httpx.Cookies', "
        "storage_path: 'Path | None', env_auth: 'bool', allow_headless: 'bool', "
        "baseline: 'CookieJar | None') -> "
        "'tuple[str, str, bool, CookieSnapshot | None, CookieJar | None]'"
    )


def test_refresh_adapter_is_one_exact_late_bound_composition_site() -> None:
    adapter = _function(_tree(REFRESH_PATH), "_cold_fallbacks")
    assert _semantic_hash(adapter) == _ADAPTER_HASH
    closures = {
        node.name
        for node in adapter.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert closures == _CLOSURES
    assert all(
        not node.args.defaults
        and not any(default is not None for default in node.args.kw_defaults)
        and node.args.vararg is None
        and node.args.kwarg is None
        for node in adapter.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )

    constructor_calls = [
        node
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call)
        and _qualified_name(node.func) == "_auth_recovery.ColdRecoveryCoordinator"
    ]
    assert len(constructor_calls) == 1
    constructor_call = constructor_calls[0]
    assert constructor_call.args == []
    assert [(keyword.arg, ast.unparse(keyword.value)) for keyword in constructor_call.keywords] == [
        ("state", "_auth_recovery.ColdRecoveryState.process_default()"),
        ("single_flight", "_single_flight.SingleFlight.process_default()"),
        ("should_try_refresh", "should_try_refresh"),
        ("resolve_refresh_path", "resolve_refresh_path"),
        ("run_refresh_attempt", "run_refresh_attempt"),
        ("load_cookie_pair", "lambda path: _auth_cookies._build_cookie_pair_from_storage(path)"),
        (
            "run_headless_attempt",
            "lambda path, headless: _auth_recovery._try_headless_reauth_result("
            "storage_path=path, allow_headless=headless)",
        ),
        (
            "run_master_token_attempt",
            "lambda path: _auth_recovery._try_master_token_reauth_result(storage_path=path)",
        ),
        ("validate_recovered", "validate_recovered"),
        ("fetch_recovered", "fetch_recovered"),
        (
            "replace_cookie_jar",
            "lambda target, source: _replace_cookie_jar(target, source)",
        ),
        ("snapshot_cookie_jar", "lambda jar: _auth_storage.snapshot_cookie_jar(jar)"),
        ("clone_cookie_jar", "lambda jar: _auth_cookies._clone_cookie_jar(jar)"),
    ]

    recover_calls = [
        node
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call) and _qualified_name(node.func) == "coordinator.recover"
    ]
    assert len(recover_calls) == 1
    assert recover_calls[0].args == []
    assert [keyword.arg for keyword in recover_calls[0].keywords] == [
        "initial_error",
        "cookie_jar",
        "storage_path",
        "env_auth",
        "allow_headless",
        "baseline",
    ]
    assert not any(
        isinstance(node, ast.Call)
        and (_qualified_name(node.func) or "").rsplit(".", 1)[-1]
        in {"create_task", "ensure_future", "Task", "TaskGroup"}
        for node in ast.walk(adapter)
    )


def test_coordinator_is_internal_and_retains_no_public_surface() -> None:
    assert recovery.ColdRecoveryCoordinator.__module__ == "notebooklm._auth.recovery"
    assert recovery.ColdRecoveryCoordinator.__qualname__ == "ColdRecoveryCoordinator"
    assert "ColdRecoveryCoordinator" not in getattr(recovery, "__all__", ())
    assert "ColdRecoveryCoordinator" not in getattr(auth_package, "__all__", ())
    assert "ColdRecoveryCoordinator" not in auth_facade.__all__
    assert not hasattr(auth_facade, "ColdRecoveryCoordinator")


@pytest.mark.asyncio
async def test_callback_fields_scrub_after_success_saved_error_cancellation_and_baseexception() -> (
    None
):
    async def success(_path: Path) -> tuple[str, str, dict[Any, Any], CookieJar | None]:
        return "csrf", "session", {}, None

    coordinator = _make_coordinator(
        should_try_refresh=lambda *_args: True, run_refresh_attempt=success
    )
    assert set(vars(coordinator)) == _CALLBACK_FIELDS | {
        "_claim_lock",
        "_state",
        "_single_flight",
        "_used",
    }
    assert vars(coordinator)["_used"] is False
    assert await coordinator.recover(
        initial_error=ValueError("initial"),
        cookie_jar=httpx.Cookies(),
        storage_path=None,
        env_auth=False,
        allow_headless=False,
        baseline=None,
    ) == ("csrf", "session", True, {}, None)
    _scrubbed(coordinator)

    saved = ValueError("saved")

    async def fail_saved(_path: Path) -> tuple[str, str, dict[Any, Any], CookieJar | None]:
        raise saved

    coordinator = _make_coordinator(
        should_try_refresh=lambda *_args: True, run_refresh_attempt=fail_saved
    )
    with pytest.raises(ValueError) as raised:
        await coordinator.recover(
            initial_error=ValueError("initial"),
            cookie_jar=httpx.Cookies(),
            storage_path=None,
            env_auth=False,
            allow_headless=False,
            baseline=None,
        )
    assert raised.value is saved
    _scrubbed(coordinator)

    async def cancel(_path: Path) -> tuple[str, str, dict[Any, Any], CookieJar | None]:
        raise asyncio.CancelledError

    coordinator = _make_coordinator(
        should_try_refresh=lambda *_args: True, run_refresh_attempt=cancel
    )
    with pytest.raises(asyncio.CancelledError):
        await coordinator.recover(
            initial_error=ValueError("initial"),
            cookie_jar=httpx.Cookies(),
            storage_path=None,
            env_auth=False,
            allow_headless=False,
            baseline=None,
        )
    _scrubbed(coordinator)

    class Stop(BaseException):
        pass

    async def stop(_path: Path) -> tuple[str, str, dict[Any, Any], CookieJar | None]:
        raise Stop

    coordinator = _make_coordinator(
        should_try_refresh=lambda *_args: True, run_refresh_attempt=stop
    )
    with pytest.raises(Stop):
        await coordinator.recover(
            initial_error=ValueError("initial"),
            cookie_jar=httpx.Cookies(),
            storage_path=None,
            env_auth=False,
            allow_headless=False,
            baseline=None,
        )
    _scrubbed(coordinator)


@pytest.mark.asyncio
async def test_losing_concurrent_and_post_completion_calls_cannot_scrub_or_invoke_callbacks() -> (
    None
):
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def pause(_path: Path) -> tuple[str, str, dict[Any, Any], CookieJar | None]:
        nonlocal attempts
        attempts += 1
        entered.set()
        await release.wait()
        return "csrf", "session", {}, None

    coordinator = _make_coordinator(
        should_try_refresh=lambda *_args: True, run_refresh_attempt=pause
    )
    call = {
        "initial_error": ValueError("initial"),
        "cookie_jar": httpx.Cookies(),
        "storage_path": None,
        "env_auth": False,
        "allow_headless": False,
        "baseline": None,
    }
    winner = asyncio.create_task(coordinator.recover(**call))
    await entered.wait()
    with pytest.raises(RuntimeError, match=r"^ColdRecoveryCoordinator\.recover\(\) is one-shot$"):
        await coordinator.recover(**call)
    assert attempts == 1
    assert set(vars(coordinator)) == _CALLBACK_FIELDS | {
        "_claim_lock",
        "_state",
        "_single_flight",
        "_used",
    }
    release.set()
    assert await winner == ("csrf", "session", True, {}, None)
    _scrubbed(coordinator)
    with pytest.raises(RuntimeError, match=r"^ColdRecoveryCoordinator\.recover\(\) is one-shot$"):
        await coordinator.recover(**call)
    assert attempts == 1
    _scrubbed(coordinator)


@pytest.mark.asyncio
async def test_validator_is_the_only_direct_cold_flight_handoff_and_scrubs_after_settlement() -> (
    None
):
    entered = asyncio.Event()
    release = asyncio.Event()
    validated: list[httpx.Cookies] = []

    async def unused_refresh(_path: Path) -> tuple[str, str, dict[Any, Any], CookieJar | None]:
        raise AssertionError("refresh should not run")

    async def validate(jar: httpx.Cookies) -> None:
        validated.append(jar)
        entered.set()
        await release.wait()

    async def fetch(_jar: httpx.Cookies) -> tuple[str, str]:
        return "csrf", "session"

    coordinator = _make_coordinator(
        should_try_refresh=lambda *_args: False,
        run_refresh_attempt=unused_refresh,
        validate_recovered=validate,
    )
    candidate = httpx.Cookies()
    path = Path("/tmp/cold-recovery-boundary.json")
    coordinator._load_cookie_pair = lambda _path: _LoadedCookiePair(candidate, CookieJar(()))
    coordinator._fetch_recovered = fetch
    coordinator._replace_cookie_jar = lambda _target, _source: None
    canonical_path = recovery.canonical_storage_key(path)
    assert canonical_path is not None
    coordinator._state.note_success(canonical_path)
    winner = asyncio.create_task(
        coordinator.recover(
            initial_error=_LoginRedirectError("redirect"),
            cookie_jar=httpx.Cookies(),
            storage_path=path,
            env_auth=False,
            allow_headless=True,
            baseline=None,
        )
    )
    await entered.wait()
    assert len(validated) == 1
    assert hasattr(coordinator, "_validate_recovered")
    release.set()
    assert await winner == ("csrf", "session", True, {}, CookieJar(()))
    _scrubbed(coordinator)


@pytest.mark.parametrize(
    "source",
    [
        "class C:\n async def recover(self):\n  callback = self._fetch_recovered\n",
        "class C:\n async def recover(self):\n  return self._replace_cookie_jar\n",
        "class C:\n async def recover(self):\n  callbacks = [self._validate_recovered]\n",
        "class C:\n async def recover(self):\n  def later(cb=self._run_refresh_attempt): pass\n",
        "class C:\n async def recover(self):\n  later = lambda: self._fetch_recovered(jar)\n",
        "class C:\n async def recover(self):\n  await consume(self._validate_recovered)\n",
        "class C:\n async def recover(self):\n  await self._run_cold_recovery(p, h, self._validate_recovered, e, extra)\n",
    ],
)
def test_callback_alias_default_closure_container_and_validator_escape_detectors_bite(
    source: str,
) -> None:
    tree = ast.parse(source)
    method = _method(next(node for node in tree.body if isinstance(node, ast.ClassDef)), "recover")
    assert _callback_escape_violations(method)


@pytest.mark.parametrize(
    "source",
    [
        "import notebooklm._auth.refresh as late\n",
        "from notebooklm._auth import tokens as late\n",
        "import importlib\nlate = importlib.import_module('notebooklm._auth.' + 'session')\n",
        "def later(owner):\n return owner.MasterTokenBootstrapper\n",
        "def later(owner):\n return getattr(owner, 'create_' + 'task')\n",
    ],
)
def test_import_capability_and_dynamic_access_detectors_bite(source: str) -> None:
    assert _capability_violations(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        (
            "class C:\n"
            " async def recover(self):\n"
            "  self._used = True\n"
            "  if self._used: raise RuntimeError('ColdRecoveryCoordinator.recover() is one-shot')\n"
            "  try: pass\n"
            "  finally: pass\n"
        ),
        (
            "class C:\n"
            " async def recover(self):\n"
            "  if self._used: raise RuntimeError('wrong')\n"
            "  self._used = True\n"
            "  try: pass\n"
            "  finally: pass\n"
        ),
        (
            "class C:\n"
            " async def recover(self):\n"
            "  if self._used: raise RuntimeError('ColdRecoveryCoordinator.recover() is one-shot')\n"
            "  try:\n"
            "   self._used = True\n"
            "  finally:\n"
            "   del self._validate_recovered\n"
        ),
    ],
)
def test_loser_order_winner_mark_and_exact_scrub_detectors_bite(source: str) -> None:
    tree = ast.parse(source)
    method = _method(next(node for node in tree.body if isinstance(node, ast.ClassDef)), "recover")
    assert isinstance(method, ast.AsyncFunctionDef)
    assert _one_shot_violations(method)
