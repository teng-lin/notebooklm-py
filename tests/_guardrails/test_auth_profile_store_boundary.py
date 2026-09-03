"""Dependency, API, caller, and cycle guards for the path-owned profile store."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

import notebooklm._auth as auth_package
import notebooklm.auth as auth_facade
from notebooklm._auth import profile_migration, profile_store, storage
from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.profile_account import DomainSelection
from notebooklm._auth.profile_document import ProfileDocument

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
MODULE_PATH = AUTH_ROOT / "profile_store.py"
FACADE_PATH = SRC_ROOT / "auth.py"
AUTH_INIT_PATH = AUTH_ROOT / "__init__.py"
ImportRecord = tuple[str, int, str, str, str | None]
Caller = tuple[str, str, str]
NativeReplaceCaller = tuple[str, str, str]

_STORE_METHODS = {
    "_read_account_document",
    "_update_account_if_document_unchanged",
    "read_document",
    "read_session",
    "read_account",
    "update_account",
    "clear_account",
    "merge_cookie_observation",
    "merge_legacy_cookie_observation",
    "replace_from_remint",
    "replace_from_login",
    "replace_minted_session",
    "read_master_token",
    "write_master_token",
}
_CAPABILITY_ESCAPE = "<ProfileStore-capability-escape>"
_INSTANCE_ESCAPE = "<ProfileStore-instance-escape>"
_UNTRUSTED_PROVIDER = "<untrusted-ProfileStore-provider>"
_METHOD_ESCAPE_PREFIX = "<store-method-capability-escape:"
_RECEIVER_ESCAPE_PREFIX = "<unproven-store-receiver:"
_PROFILE_STORE_PROVIDERS = {
    "notebooklm._auth.profile_store",
    "notebooklm._auth.storage",
}
_PROFILE_MIGRATION_PROVIDER = "notebooklm._auth.profile_migration"
_PROFILE_MIGRATION_MODULE_BINDING = "<profile-migration-module>"
_MIGRATION_SERVICES = frozenset(
    {
        "LegacyAccountMigrator",
        "LegacyPromotionScheduler",
        "LoginProfileWriter",
        "AccountMetadataWriter",
    }
)

_NATIVE_REPLACE_CALLER_TESTS: dict[NativeReplaceCaller, tuple[str, ...]] = {
    ("_app/login_cookie.py", "import_cookie_payload", "replace_profile_from_login"): (
        "tests/unit/app/test_app_login_cookie.py::test_import_preserves_callback_order_identity_and_backup",
    ),
    ("_browser/browser_capture.py", "replace_captured_profile", "replace_from_remint"): (
        "tests/unit/test_browser_capture_headless_arm.py::test_headless_authenticated_landing_persists_storage",
        "tests/unit/test_browser_capture_cdp_arm.py::test_cdp_authenticated_landing_persists_and_filters",
    ),
    ("_auth/storage.py", "replace_from_login", "replace_profile_from_login"): (
        "tests/unit/test_storage_writer.py::test_replace_from_login_is_one_typed_store_delegation_and_exhaustive_projection",
    ),
    ("_auth/storage.py", "replace_from_remint", "replace_from_remint"): (
        "tests/unit/test_storage_writer.py::test_replace_from_remint_is_one_typed_store_delegation_and_exact_legacy_result",
    ),
    (
        "cli/_cookie_import.py",
        "_import_cookie_json",
        "replace_profile_from_login[injected]",
    ): (
        "tests/unit/cli/test_login_cookie_app_adapters.py::test_import_adapter_injects_native_login_operation",
    ),
    (
        "cli/services/login/cookie_writes.py",
        "_write_extracted_cookies",
        "replace_profile_from_login",
    ): (
        "tests/unit/cli/test_cookie_writes.py::TestWriteExtractedCookies.test_lock_unavailable_returns_outcome",
    ),
    (
        "cli/services/login/refresh.py",
        "_login_with_browser_cookies",
        "replace_profile_from_login",
    ): (
        "tests/unit/cli/test_login_refresh_coverage.py::test_login_with_cookies_lock_unavailable_exits",
    ),
    (
        "cli/services/login/refresh.py",
        "default_refresh_deps",
        "replace_profile_from_login[injected]",
    ): (
        "tests/unit/cli/test_login_refresh_coverage.py::test_default_refresh_deps_uses_native_login_operation",
    ),
}

_B3_ACCEPTANCE_TEST_FILES = frozenset(
    {
        "tests/_guardrails/test_auth_profile_store_boundary.py",
        "tests/_guardrails/test_auth_storage_compatibility.py",
        "tests/_guardrails/test_public_surface.py",
        "tests/_guardrails/test_storage_writer_boundary.py",
        "tests/unit/app/test_app_login_cookie.py",
        "tests/unit/cli/test_cookie_writes.py",
        "tests/unit/cli/test_login_cookie_app_adapters.py",
        "tests/unit/cli/test_login_refresh_coverage.py",
        "tests/unit/cli/test_playwright_login_render_contract.py",
        "tests/unit/test_auth_profile_migration.py",
        "tests/unit/test_auth_profile_store.py",
        "tests/unit/test_auth_profile_store_login.py",
        "tests/unit/test_auth_profile_store_remint.py",
        "tests/unit/test_browser_capture_cdp_arm.py",
        "tests/unit/test_browser_capture_headless_arm.py",
        "tests/unit/test_storage_writer.py",
    }
)


def _source_label(path: Path) -> str:
    if path.is_relative_to(AUTH_ROOT):
        return path.relative_to(AUTH_ROOT).as_posix()
    return path.relative_to(SRC_ROOT).as_posix()


class _NativeReplaceCallerCollector(ast.NodeVisitor):
    """Find bounded static native calls and dependency-injection boundaries.

    Resolution follows same-scope aliases, exact string constants, static
    dict/list/tuple entries, ``getattr``, ``partial``, and expanded keyword
    dictionaries. Values propagated through calls or selected by dynamic keys
    remain outside this structural proof.
    """

    _OPERATIONS = frozenset({"replace_from_remint", "replace_profile_from_login"})

    def __init__(self, path: Path, root: Path) -> None:
        self._path = path.relative_to(root).as_posix()
        self._scope: list[str] = []
        self._alias_scopes: list[dict[str, str]] = [{}]
        self._container_scopes: list[dict[str, dict[str | int, str]]] = [{}]
        self._string_scopes: list[dict[str, str]] = [{}]
        self._partial_scopes: list[set[str]] = [{"partial"}]
        self.callers: set[NativeReplaceCaller] = set()

    @staticmethod
    def _lookup(scopes: list[dict[str, object]], name: str) -> object | None:
        for bindings in reversed(scopes):
            if name in bindings:
                return bindings[name]
        return None

    def _static_string(self, expression: ast.expr) -> str | None:
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            return expression.value
        if isinstance(expression, ast.Name):
            value = self._lookup(self._string_scopes, expression.id)
            return value if isinstance(value, str) else None
        return None

    def _static_key(self, expression: ast.expr) -> str | int | None:
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str | int):
            return expression.value
        return self._static_string(expression)

    def _container_operations(self, expression: ast.expr) -> dict[str | int, str]:
        if isinstance(expression, ast.Name):
            value = self._lookup(self._container_scopes, expression.id)
            return dict(value) if isinstance(value, dict) else {}
        if isinstance(expression, ast.Dict):
            operations: dict[str | int, str] = {}
            for key_node, value_node in zip(expression.keys, expression.values, strict=True):
                if key_node is None:
                    continue
                key = self._static_key(key_node)
                operation = self._operation(value_node)
                if key is not None and operation is not None:
                    operations[key] = operation
            return operations
        if isinstance(expression, ast.List | ast.Tuple):
            return {
                index: operation
                for index, item in enumerate(expression.elts)
                if (operation := self._operation(item)) is not None
            }
        return {}

    def _is_partial_factory(self, expression: ast.expr) -> bool:
        if isinstance(expression, ast.Name):
            return any(expression.id in names for names in reversed(self._partial_scopes))
        return isinstance(expression, ast.Attribute) and expression.attr == "partial"

    def _operation(self, expression: ast.expr) -> str | None:
        if isinstance(expression, ast.Name):
            if expression.id in self._OPERATIONS:
                return expression.id
            value = self._lookup(self._alias_scopes, expression.id)
            return value if isinstance(value, str) else None
        if isinstance(expression, ast.Attribute) and expression.attr in self._OPERATIONS:
            return expression.attr
        if isinstance(expression, ast.Subscript):
            key = self._static_key(expression.slice)
            if isinstance(key, str) and key in self._OPERATIONS:
                return key
            return (
                self._container_operations(expression.value).get(key) if key is not None else None
            )
        if isinstance(expression, ast.Call):
            if (
                isinstance(expression.func, ast.Name)
                and expression.func.id == "getattr"
                and len(expression.args) >= 2
            ):
                name = self._static_string(expression.args[1])
                return name if name in self._OPERATIONS else None
            if self._is_partial_factory(expression.func) and expression.args:
                return self._operation(expression.args[0])
        return None

    def _record(self, operation: str) -> None:
        owner = ".".join(self._scope) if self._scope else "<module>"
        self.callers.add((self._path, owner, operation))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        for imported in node.names:
            if imported.name in self._OPERATIONS:
                self._alias_scopes[-1][imported.asname or imported.name] = imported.name
            if node.module == "functools" and imported.name == "partial":
                self._partial_scopes[-1].add(imported.asname or imported.name)

    def _bind_assignment(self, target: ast.expr, value: ast.expr) -> None:
        if not isinstance(target, ast.Name):
            return
        name = target.id
        self._alias_scopes[-1].pop(name, None)
        self._container_scopes[-1].pop(name, None)
        self._string_scopes[-1].pop(name, None)
        operation = self._operation(value)
        if operation is not None:
            self._alias_scopes[-1][name] = operation
        operations = self._container_operations(value)
        if operations:
            self._container_scopes[-1][name] = operations
        static_string = self._static_string(value)
        if static_string is not None:
            self._string_scopes[-1][name] = static_string

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            self._bind_assignment(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is not None:
            self._bind_assignment(node.target, node.value)
        self.generic_visit(node)

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        self._alias_scopes.append({})
        self._container_scopes.append({})
        self._string_scopes.append({})
        self._partial_scopes.append(set())
        self.generic_visit(node)
        self._partial_scopes.pop()
        self._string_scopes.pop()
        self._container_scopes.pop()
        self._alias_scopes.pop()
        self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_scope(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        operation = self._operation(node.func)
        if operation is not None:
            self._record(operation)
        for keyword in node.keywords:
            if keyword.arg in self._OPERATIONS:
                injected = self._operation(keyword.value)
                if injected == keyword.arg:
                    self._record(f"{keyword.arg}[injected]")
            elif keyword.arg is None:
                for name, injected in self._container_operations(keyword.value).items():
                    if isinstance(name, str) and name in self._OPERATIONS and injected == name:
                        self._record(f"{name}[injected]")
        self.generic_visit(node)


def _native_replace_callers(root: Path) -> set[NativeReplaceCaller]:
    callers: set[NativeReplaceCaller] = set()
    for path in sorted(root.rglob("*.py")):
        collector = _NativeReplaceCallerCollector(path, root)
        collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        callers.update(collector.callers)
    return callers


def _test_qualnames(path: Path) -> set[str]:
    names: set[str] = set()

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.scope.append(node.name)
            if node.name.startswith("test_"):
                names.add(".".join(self.scope))
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    Collector().visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return names


_EXPECTED_IMPORTS: list[ImportRecord] = [
    ("module", 0, "__future__", "annotations", None),
    ("module", 0, "contextlib", "", None),
    ("module", 0, "copy", "", None),
    ("module", 0, "json", "", None),
    ("module", 0, "logging", "", None),
    ("module", 0, "os", "", None),
    ("module", 0, "shutil", "", None),
    ("module", 0, "sys", "", None),
    ("module", 0, "collections.abc", "Callable", None),
    ("module", 0, "dataclasses", "dataclass", None),
    ("module", 0, "dataclasses", "field", None),
    ("module", 0, "enum", "Enum", None),
    ("module", 0, "pathlib", "Path", None),
    ("module", 0, "typing", "Any", None),
    ("module", 0, "typing", "Protocol", None),
    ("module", 0, "typing", "TypeVar", None),
    ("module", 0, "typing", "cast", None),
    ("module", 0, "notebooklm.paths", "", "_notebooklm_paths"),
    ("module", 2, "exceptions", "LockUnavailableError", None),
    ("module", 1, "", "cookie_merge", "_cookie_merge"),
    ("module", 1, "", "cookie_policy", "_cookie_policy"),
    ("module", 1, "cookie_filter", "filter_storage_state_cookies_by_domain_policy", None),
    ("module", 1, "cookie_merge", "RecoveryObservation", None),
    ("module", 1, "cookie_types", "CookieIdentity", None),
    ("module", 1, "cookie_types", "CookieJar", None),
    ("module", 1, "credential_io", "_commit_profile_json", None),
    ("module", 1, "master_token_file", "MasterTokenFile", None),
    ("module", 1, "master_token_types", "MasterToken", None),
    ("module", 1, "paths", "_storage_state_lock_path", None),
    ("module", 1, "paths", "canonical_storage_key", None),
    ("module", 1, "profile_account", "ACCOUNT_ROUTE_CLEARED_KEY", None),
    ("module", 1, "profile_account", "AccountDirective", None),
    ("module", 1, "profile_account", "AccountView", None),
    ("module", 1, "profile_account", "ClearAccount", None),
    ("module", 1, "profile_account", "DomainSelection", None),
    ("module", 1, "profile_account", "KeepAccount", None),
    ("module", 1, "profile_account", "ProfileAccount", None),
    ("module", 1, "profile_account", "SetAccount", None),
    ("module", 1, "profile_account", "StoredSession", None),
    ("module", 1, "profile_document", "ProfileDocument", None),
    ("module", 1, "profile_document", "_ProfileDocumentStructureError", None),
    ("module", 1, "storage_lock", "_LOCK_ACQUIRE_DEADLINE_SECONDS", None),
    ("module", 1, "storage_lock", "LockRequest", None),
    ("module", 1, "storage_lock", "LockState", None),
    ("module", 1, "storage_lock", "StorageLockManager", None),
]


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.function_depth = 0
        self.class_depth = 0
        self.records: list[ImportRecord] = []

    @property
    def scope(self) -> str:
        if self.function_depth:
            return "function"
        if self.class_depth:
            return "class"
        return "module"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_depth += 1
        self.generic_visit(node)
        self.class_depth -= 1

    def visit_Import(self, node: ast.Import) -> None:
        self.records.extend((self.scope, 0, item.name, "", item.asname) for item in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.records.extend(
            (self.scope, node.level, node.module or "", item.name, item.asname)
            for item in node.names
        )


def _collect_imports(tree: ast.AST) -> list[ImportRecord]:
    collector = _ImportCollector()
    collector.visit(tree)
    return collector.records


def _dependency_violations(tree: ast.AST) -> list[ImportRecord]:
    return [record for record in _collect_imports(tree) if record not in _EXPECTED_IMPORTS]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import httpx\n", id="httpx"),
        pytest.param("from .storage import CookieSaveResult\n", id="storage"),
        pytest.param("class Lazy:\n    from .cookies import load_cookies\n", id="cookies"),
        pytest.param("def lazy():\n    from .account import enumerate_accounts\n", id="account"),
        pytest.param("import notebooklm.auth\n", id="facade"),
        pytest.param("def lazy():\n    from .._runtime import lifecycle\n", id="runtime"),
        pytest.param("class Lazy:\n    from ..cli import main\n", id="cli"),
        pytest.param("from .master_token import read_master_token\n", id="master-token"),
        pytest.param(
            "def lazy():\n    from .profile_document import ProfileDocument\n",
            id="lazy-approved-leaf",
        ),
    ],
)
def test_dependency_detector_bites_at_module_function_and_class_scope(source: str) -> None:
    assert _dependency_violations(ast.parse(source))


def test_profile_store_imports_are_exact_and_module_level() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    assert _collect_imports(tree) == _EXPECTED_IMPORTS
    assert _dependency_violations(tree) == []


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


def _imports_profile_store(path: Path, tree: ast.AST) -> bool:
    target = "notebooklm._auth.profile_store"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(item.name == target for item in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            if resolved == target or any(
                f"{resolved}.{item.name}" == target for item in node.names
            ):
                return True
    return False


@pytest.mark.parametrize(
    "source",
    [
        "from .profile_store import ProfileStore\n",
        "def lazy():\n    from . import profile_store\n",
        "class Lazy:\n    import notebooklm._auth.profile_store\n",
    ],
)
def test_importer_detector_bites_at_every_scope(source: str) -> None:
    assert _imports_profile_store(AUTH_ROOT / "synthetic.py", ast.parse(source))


def test_production_importers_are_exactly_approved_store_owners_and_loader() -> None:
    actual = {
        _source_label(path)
        for path in SRC_ROOT.rglob("*.py")
        if path != MODULE_PATH
        and _imports_profile_store(
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
    }
    assert actual == {
        "_android/auth.py",
        "_web/transport/cookie_persistence.py",
        "_runtime/init.py",
        "account_email.py",
        "account_repair.py",
        "auth.py",
        "_browser/browser_capture.py",
        "master_token.py",
        "master_token_bootstrap.py",
        "profile_migration.py",
        "psidts_recovery.py",
        "refresh.py",
        "storage.py",
        "tokens.py",
    }


def test_external_source_importer_is_detected_with_stable_relative_label() -> None:
    path = SRC_ROOT / "cli" / "synthetic.py"
    tree = ast.parse("from notebooklm._auth.profile_store import ProfileStore\n")
    assert _imports_profile_store(path, tree)


def _profile_store_class(tree: ast.Module) -> ast.ClassDef:
    matches = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ProfileStore"
    ]
    assert len(matches) == 1
    return matches[0]


def test_profile_store_public_method_set_is_minimal_and_exact() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    cls = _profile_store_class(tree)
    methods = {
        node.name
        for node in cls.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    }
    assert methods == {
        "path",
        "ordering_key",
        "read_document",
        "read_session",
        "read_account",
        "update_account",
        "clear_account",
        "merge_cookie_observation",
        "merge_legacy_cookie_observation",
        "replace_from_remint",
        "replace_from_login",
        "replace_minted_session",
        "read_master_token",
        "write_master_token",
    }
    forbidden_future = {
        "persist_minted_session",
        "mutate",
    }
    assert methods.isdisjoint(forbidden_future)


def test_replacement_request_result_and_enum_shapes_are_minimal_and_exact() -> None:
    request_type = profile_store.RemintWriteRequest
    result_type = profile_store.ReplaceResult
    status_type = profile_store.ReplaceStatus

    assert request_type.__dataclass_params__.frozen is True
    assert request_type.__slots__ == ("source", "carry_account", "domain_selection")
    request_fields = dataclasses.fields(request_type)
    assert [field.name for field in request_fields] == [
        "source",
        "carry_account",
        "domain_selection",
    ]
    assert request_fields[0].repr is False
    assert request_fields[2].default == DomainSelection()
    assert str(inspect.signature(request_type)) == (
        "(source: 'ProfileDocument', carry_account: 'bool', "
        "domain_selection: 'DomainSelection' = DomainSelection(include_domains=frozenset(), "
        "include_optional=False)) -> None"
    )
    secret = "raw-cookie-value-must-not-escape"
    request = request_type(
        ProfileDocument.decode(
            {
                "cookies": [{"name": "SID", "value": secret, "domain": ".google.com", "path": "/"}],
                "notebooklm": {"account": {"email": secret}},
            }
        ),
        True,
    )
    assert secret not in repr(request)
    assert secret not in str(request)

    minted_type = profile_store.MintedSessionWriteRequest
    assert minted_type.__dataclass_params__.frozen is True
    assert minted_type.__slots__ == ("cookies", "email", "force", "refuse_unknown_owner")
    minted_fields = dataclasses.fields(minted_type)
    assert [field.name for field in minted_fields] == [
        "cookies",
        "email",
        "force",
        "refuse_unknown_owner",
    ]
    assert minted_fields[0].repr is False
    assert str(inspect.signature(minted_type)) == (
        "(cookies: 'CookieJar', email: 'str | None', force: 'bool' = False, "
        "refuse_unknown_owner: 'bool' = True) -> None"
    )
    minted = minted_type(
        CookieJar((Cookie("SID", ".google.com", "/", secret, same_site="None"),)),
        secret,
    )
    assert secret not in repr(minted)
    assert secret not in str(minted)
    with pytest.raises(TypeError, match="cookies must be a CookieJar"):
        minted_type({}, None)  # type: ignore[arg-type]

    login_type = profile_store.LoginWriteRequest
    assert login_type.__dataclass_params__.frozen is True
    assert login_type.__slots__ == ("source", "domain_selection", "account", "backup")
    login_fields = dataclasses.fields(login_type)
    assert [field.name for field in login_fields] == [
        "source",
        "domain_selection",
        "account",
        "backup",
    ]
    assert login_fields[0].repr is False
    assert str(inspect.signature(login_type)) == (
        "(source: 'ProfileDocument', domain_selection: 'DomainSelection', "
        "account: 'AccountDirective', backup: 'bool' = False) -> None"
    )

    assert [(member.name, member.value) for member in status_type] == [
        ("APPLIED", "applied"),
        ("LOCK_UNAVAILABLE", "lock_unavailable"),
        ("REQUIRED_COOKIES_DROPPED", "required_cookies_dropped"),
    ]
    assert result_type.__dataclass_params__.frozen is True
    assert result_type.__slots__ == (
        "status",
        "missing_required",
        "present_names",
        "backup_path",
    )
    assert [field.name for field in dataclasses.fields(result_type)] == [
        "status",
        "missing_required",
        "present_names",
        "backup_path",
    ]
    assert str(inspect.signature(result_type)) == (
        "(status: 'ReplaceStatus', missing_required: 'tuple[str, ...]' = (), "
        "present_names: 'tuple[str, ...]' = (), backup_path: 'Path | None' = None) -> None"
    )
    with pytest.raises(TypeError, match="status must be a ReplaceStatus"):
        result_type("applied")  # type: ignore[arg-type]


def test_replace_types_remain_internal_to_profile_store() -> None:
    names = {
        "RemintWriteRequest",
        "LoginWriteRequest",
        "MintedSessionWriteRequest",
        "ReplaceStatus",
    }
    assert names.isdisjoint(storage.__all__)
    assert all(not hasattr(auth_facade, name) for name in names)
    assert all(not hasattr(auth_package, name) for name in names)
    assert auth_facade.ReplaceResult is profile_store.ReplaceResult
    assert "ReplaceResult" not in auth_facade.__all__


def test_profile_store_constructor_and_replace_method_signatures_are_exact() -> None:
    assert str(inspect.signature(profile_store.ProfileStore)) == (
        "(path: 'Path', *, locks: 'StorageLockManager | None' = None) -> 'None'"
    )
    assert str(inspect.signature(profile_store.ProfileStore.replace_from_remint)) == (
        "(self, request: 'RemintWriteRequest') -> 'ReplaceResult'"
    )
    assert str(inspect.signature(profile_store.ProfileStore.replace_from_login)) == (
        "(self, request: 'LoginWriteRequest') -> 'ReplaceResult'"
    )
    assert str(inspect.signature(profile_store.ProfileStore.replace_minted_session)) == (
        "(self, request: 'MintedSessionWriteRequest') -> 'None'"
    )
    assert str(inspect.signature(profile_store.ProfileStore.read_master_token)) == (
        "(self) -> 'MasterToken | None'"
    )
    assert str(inspect.signature(profile_store.ProfileStore.write_master_token)) == (
        "(self, token: 'MasterToken') -> 'None'"
    )
    assert profile_store.ProfileStore.write_master_token.__annotations__["token"] == "MasterToken"
    assert MasterToken.__module__ == "notebooklm._auth.master_token_types"


def test_native_login_operation_signature_and_construction_are_exact() -> None:
    assert str(inspect.signature(profile_migration.replace_profile_from_login)) == (
        "(path: 'Path', state: 'Mapping[str, Any]', *, include_domains: 'set[str] | None', "
        "include_optional: 'bool' = False, account_mode: \"Literal['keep', 'clear', 'set']\" "
        "= 'keep', account_authuser: 'int | None' = None, account_email: 'str | None' = None, "
        "backup: 'bool' = False) -> 'ReplaceResult'"
    )
    path = AUTH_ROOT / "profile_migration.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    operation = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "replace_profile_from_login"
    )
    called = [ast.unparse(node.func) for node in ast.walk(operation) if isinstance(node, ast.Call)]
    assert called.count("ProfileStore") == 1
    assert called.count("LoginWriteRequest") == 1
    assert called.count("LoginProfileWriter") == 1
    assert called.count("LegacyAccountMigrator") == 1
    source = ast.unparse(operation)
    assert source.index("account_mode not in") < source.index("ProfileDocument.decode")
    assert source.index("account_mode == 'set'") < source.index("ProfileDocument.decode")
    assert all(name not in source for name in {"AccountRecord", "KEEP_ACCOUNT", "CLEAR_ACCOUNT"})


def test_every_native_replace_caller_has_a_live_b3_behavior_test() -> None:
    assert _native_replace_callers(SRC_ROOT) == set(_NATIVE_REPLACE_CALLER_TESTS)
    for targets in _NATIVE_REPLACE_CALLER_TESTS.values():
        assert targets
        for target in targets:
            relative, qualname = target.split("::", 1)
            assert relative in _B3_ACCEPTANCE_TEST_FILES
            assert qualname in _test_qualnames(REPO_ROOT / relative)


def test_native_replace_caller_inventory_bites_on_static_indirection(tmp_path: Path) -> None:
    root = tmp_path / "notebooklm"
    root.mkdir()
    (root / "feature.py").write_text(
        "import functools\n"
        "import notebooklm.auth as auth\n"
        "from notebooklm.auth import replace_profile_from_login as native_login\n"
        "LOGIN_NAME = 'replace_profile_from_login'\n"
        "OPERATIONS = {'login': auth.replace_profile_from_login}\n"
        "def direct(store, request):\n"
        "    store.replace_from_remint(request)\n"
        "    native_login('path', {})\n"
        "def via_getattr(store, request):\n"
        "    getattr(store, 'replace_from_remint')(request)\n"
        "    getattr(auth, LOGIN_NAME)('path', {})\n"
        "def via_container(store, request):\n"
        "    operations = {'remint': store.replace_from_remint}\n"
        "    operations['remint'](request)\n"
        "    OPERATIONS['login']('path', {})\n"
        "def via_list(store, request):\n"
        "    operations = [store.replace_from_remint]\n"
        "    operations[0](request)\n"
        "def via_tuple():\n"
        "    operations = (auth.replace_profile_from_login,)\n"
        "    operations[0]('path', {})\n"
        "def via_partial():\n"
        "    functools.partial(auth.replace_profile_from_login)('path', {})\n"
        "def explicit_adapter(consume):\n"
        "    consume(replace_profile_from_login=native_login)\n"
        "def expanded_adapter(consume):\n"
        "    kwargs = {'replace_profile_from_login': auth.replace_profile_from_login}\n"
        "    consume(**kwargs)\n",
        encoding="utf-8",
    )
    assert _native_replace_callers(root) == {
        ("feature.py", "direct", "replace_from_remint"),
        ("feature.py", "direct", "replace_profile_from_login"),
        ("feature.py", "via_getattr", "replace_from_remint"),
        ("feature.py", "via_getattr", "replace_profile_from_login"),
        ("feature.py", "via_container", "replace_from_remint"),
        ("feature.py", "via_container", "replace_profile_from_login"),
        ("feature.py", "via_list", "replace_from_remint"),
        ("feature.py", "via_tuple", "replace_profile_from_login"),
        ("feature.py", "via_partial", "replace_profile_from_login"),
        ("feature.py", "explicit_adapter", "replace_profile_from_login[injected]"),
        ("feature.py", "expanded_adapter", "replace_profile_from_login[injected]"),
    }


def _master_token_method_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    cls = _profile_store_class(tree)
    methods = {
        node.name: node
        for node in cls.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    violations: list[str] = []
    initializer = methods["__init__"]
    retained = {
        node.attr
        for node in ast.walk(initializer)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, ast.Store)
    }
    if retained != {"_path", "_locks"}:
        violations.append(f"retained:{sorted(retained)!r}")

    expected_terminal = {
        "read_master_token": "return token_file.read()",
        "write_master_token": "token_file.write(token)",
    }
    for name, terminal in expected_terminal.items():
        method = methods[name]
        statements = [
            node
            for node in method.body
            if not (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            )
        ]
        if len(statements) != 2 or not isinstance(statements[0], ast.Assign):
            violations.append(f"{name}:statements")
            continue
        assignment = statements[0]
        if (
            len(assignment.targets) != 1
            or not isinstance(assignment.targets[0], ast.Name)
            or assignment.targets[0].id != "token_file"
            or not isinstance(assignment.value, ast.Call)
            or not isinstance(assignment.value.func, ast.Name)
            or assignment.value.func.id != "MasterTokenFile"
        ):
            violations.append(f"{name}:file-construction")
            continue
        constructor = assignment.value
        if len(constructor.args) != 1 or not (
            isinstance(constructor.args[0], ast.Call)
            and ast.unparse(constructor.args[0].func) == "_notebooklm_paths.master_token_path_for"
            and [ast.unparse(arg) for arg in constructor.args[0].args] == ["self._path"]
            and not constructor.args[0].keywords
        ):
            violations.append(f"{name}:late-derived-path")
        if [(item.arg, ast.unparse(item.value)) for item in constructor.keywords] != [
            ("locks", "self._locks")
        ]:
            violations.append(f"{name}:lock-identity")
        if ast.unparse(statements[1]) != terminal:
            violations.append(f"{name}:terminal:{ast.unparse(statements[1])}")
        forbidden = {
            node.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute) and node.attr in {"_read_with_raw", "raw"}
        }
        if forbidden:
            violations.append(f"{name}:raw-capability:{sorted(forbidden)!r}")
    return violations


def test_master_token_methods_derive_late_share_locks_and_retain_no_file() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert _master_token_method_violations(source) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "        self._token_file = token_file\n",
        "        return token_file._read_with_raw()\n",
        "            self._path.parent / 'master_token.json',\n",
        "            locks=StorageLockManager.process_default(),\n",
    ],
)
def test_master_token_method_guard_bites_on_retention_raw_paths_and_wrong_locks(
    mutation: str,
) -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    if "self._token_file" in mutation:
        source = source.replace(
            "        self._locks = locks if locks is not None else _STORAGE_LOCKS\n",
            "        self._locks = locks if locks is not None else _STORAGE_LOCKS\n" + mutation,
            1,
        )
    elif "_read_with_raw" in mutation:
        source = source.replace("        return token_file.read()\n", mutation, 1)
    elif "self._path.parent" in mutation:
        source = source.replace(
            "            _notebooklm_paths.master_token_path_for(self._path),\n",
            mutation,
            1,
        )
    else:
        source = source.replace("            locks=self._locks,\n", mutation, 1)
    assert _master_token_method_violations(source)


def _function_owner(stack: list[str]) -> str:
    return ".".join(stack) if stack else "<module>"


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        if owner is not None:
            return f"{owner}.{node.attr}"
    return None


def _binding_names(node: ast.stmt) -> set[str]:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {node.name}
    if isinstance(node, ast.Assign):
        return {target.id for target in node.targets if isinstance(target, ast.Name)}
    if isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    return set()


def _profile_store_bindings(
    path: Path, tree: ast.AST
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Resolve final globals because deferred bodies use runtime module lookup."""
    classes: set[str] = set()
    modules: dict[str, str] = {}
    candidate_classes: set[str] = set()
    candidate_modules: set[str] = set()

    def clear(bound: str) -> None:
        classes.discard(bound)
        for reference, root in list(modules.items()):
            if root == bound:
                del modules[reference]

    top_level = tree.body if isinstance(tree, ast.Module) else []
    for node in top_level:
        if isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            for item in node.names:
                bound = item.asname or item.name
                clear(bound)
                if resolved in _PROFILE_STORE_PROVIDERS and item.name == "ProfileStore":
                    classes.add(bound)
                    candidate_classes.add(bound)
                elif (
                    resolved == "notebooklm._auth"
                    and f"{resolved}.{item.name}" in _PROFILE_STORE_PROVIDERS
                ):
                    modules[bound] = bound
                    candidate_modules.add(bound)
            continue
        if isinstance(node, ast.Import):
            for item in node.names:
                bound = item.asname or item.name.split(".", 1)[0]
                clear(bound)
                if item.name in _PROFILE_STORE_PROVIDERS:
                    reference = item.asname or item.name
                    modules[reference] = bound
                    candidate_modules.add(reference)
            continue
        for bound in _binding_names(node):
            clear(bound)
        if path == MODULE_PATH and isinstance(node, ast.ClassDef) and node.name == "ProfileStore":
            classes.add(node.name)
            candidate_classes.add(node.name)

    return classes, set(modules), candidate_classes, candidate_modules


def _migration_service_bindings(path: Path, tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    services: dict[str, str] = {}
    modules: set[str] = set()
    top_level = tree.body if isinstance(tree, ast.Module) else []
    for node in top_level:
        if isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            if resolved == _PROFILE_MIGRATION_PROVIDER:
                services.update(
                    {
                        item.asname or item.name: item.name
                        for item in node.names
                        if item.name in _MIGRATION_SERVICES
                    }
                )
            elif resolved == "notebooklm._auth":
                modules.update(
                    item.asname or item.name
                    for item in node.names
                    if f"{resolved}.{item.name}" == _PROFILE_MIGRATION_PROVIDER
                )
        elif isinstance(node, ast.Import):
            modules.update(
                item.asname or item.name
                for item in node.names
                if item.name == _PROFILE_MIGRATION_PROVIDER
            )
    for node in top_level:
        rebound: set[str] = set()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            rebound.add(node.name)
        elif isinstance(node, ast.Assign):
            rebound.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(node.target, ast.Name):
            rebound.add(node.target.id)
        for name in rebound:
            services.pop(name, None)
            modules.discard(name)
    return services, modules


class _StoreCallCollector(ast.NodeVisitor):
    def __init__(
        self,
        module: str,
        class_aliases: set[str],
        module_aliases: set[str],
        candidate_class_aliases: set[str],
        candidate_module_aliases: set[str],
        service_aliases: dict[str, str],
        service_module_aliases: set[str],
    ) -> None:
        self.module = module
        self.class_aliases = class_aliases
        self.module_aliases = module_aliases
        self.candidate_class_aliases = candidate_class_aliases
        self.candidate_module_aliases = candidate_module_aliases
        self.fail_closed = bool(candidate_class_aliases or candidate_module_aliases)
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []
        self.binding_frames: list[tuple[str, dict[str, bool]]] = [("lexical", {})]
        self.tracked_frames: list[set[str]] = [set()]
        self.service_frames: list[dict[str, str]] = [
            {
                **service_aliases,
                **{
                    alias: _PROFILE_MIGRATION_MODULE_BINDING
                    for alias in service_module_aliases
                    if "." not in alias
                },
            }
        ]
        self.store_attribute_frames: list[set[str]] = []
        self.calls: set[Caller] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        attribute_states: dict[str, bool] = {}
        for member in node.body:
            if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            arguments = [*member.args.posonlyargs, *member.args.args, *member.args.kwonlyargs]
            typed_store_names = {
                argument.arg
                for argument in arguments
                if self._annotation_is_store(argument.annotation)
            }
            for statement in member.body:
                for inner in ast.walk(statement):
                    targets: list[ast.expr]
                    value: ast.expr | None
                    if isinstance(inner, ast.Assign):
                        targets = list(inner.targets)
                        value = inner.value
                    elif isinstance(inner, ast.AnnAssign):
                        targets = [inner.target]
                        value = inner.value
                    elif isinstance(inner, ast.AugAssign):
                        targets = [inner.target]
                        value = None
                    else:
                        continue
                    is_store = (
                        isinstance(value, ast.Name) and value.id in typed_store_names
                    ) or self._value_is_store(value)
                    for attribute in (
                        target.attr
                        for target in targets
                        if isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        attribute_states[attribute] = (
                            attribute_states.get(attribute, True) and is_store
                        )
        store_attributes = {
            attribute for attribute, is_store in attribute_states.items() if is_store
        }
        self._bind_service_name(node.name, None)
        self.class_stack.append(node.name)
        self.service_frames.append({})
        self.store_attribute_frames.append(store_attributes)
        self.generic_visit(node)
        self.store_attribute_frames.pop()
        self.service_frames.pop()
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._bind_service_name(node.name, None)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        self.function_stack.append(node.name)
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        self.binding_frames.append(
            (
                "lexical",
                {
                    argument.arg: self._annotation_is_store(argument.annotation)
                    for argument in arguments
                },
            )
        )
        self.tracked_frames.append(set())
        self.service_frames.append({argument.arg: "" for argument in arguments})
        for statement in node.body:
            self.visit(statement)
        self.service_frames.pop()
        self.tracked_frames.pop()
        self.binding_frames.pop()
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        self.binding_frames.append(("lexical", {argument.arg: False for argument in arguments}))
        self.tracked_frames.append(set())
        self.service_frames.append({argument.arg: "" for argument in arguments})
        self.visit(node.body)
        self.service_frames.pop()
        self.tracked_frames.pop()
        self.binding_frames.pop()

    def _annotation_is_store(self, annotation: ast.expr | None) -> bool:
        if annotation is None:
            return False
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self._annotation_is_store(annotation.left) or self._annotation_is_store(
                annotation.right
            )
        if isinstance(annotation, ast.Name):
            return annotation.id in self.class_aliases
        return self._is_store_constructor(annotation)

    def _bind_receiver(
        self,
        target: ast.expr,
        *,
        is_store: bool,
        skip_comprehensions: bool = False,
    ) -> None:
        if isinstance(target, ast.Name):
            for index in range(len(self.binding_frames) - 1, -1, -1):
                kind, bindings = self.binding_frames[index]
                if skip_comprehensions and kind == "comprehension":
                    continue
                bindings[target.id] = is_store
                if is_store:
                    self.tracked_frames[index].add(target.id)
                return

    def _value_is_store(self, value: ast.expr | None) -> bool:
        return isinstance(value, ast.Call) and (
            self._is_store_constructor(value.func)
            or (
                self.class_stack[-1:] == ["CookiePersistence"]
                and isinstance(value.func, ast.Attribute)
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id == "self"
                and value.func.attr == "_resolve_store"
            )
        )

    def _bound_service_name(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            for frame in reversed(self.service_frames):
                if node.id in frame:
                    return frame[node.id] or None
        return None

    def _service_symbol(self, node: ast.expr) -> str | None:
        binding = self._bound_service_name(node)
        if binding in _MIGRATION_SERVICES:
            return binding
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _MIGRATION_SERVICES
            and self._bound_service_name(node.value) == _PROFILE_MIGRATION_MODULE_BINDING
        ):
            return node.attr
        return None

    def _service_constructor(self, node: ast.expr | None) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        service = self._service_symbol(node.func)
        if service is not None:
            return service
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "process_default"
            and self._service_symbol(node.func.value) == "LegacyPromotionScheduler"
        ):
            return "LegacyPromotionScheduler"
        return None

    def _bind_service(self, target: ast.expr, value: ast.expr | None) -> None:
        if not isinstance(target, ast.Name):
            return
        service = self._service_constructor(value)
        self._bind_service_name(target.id, service)

    def _bind_service_name(self, name: str, service: str | None) -> None:
        self.service_frames[-1][name] = service or ""

    def _service_instance(self, node: ast.expr) -> str | None:
        direct = self._service_constructor(node)
        if direct is not None:
            return direct
        if isinstance(node, ast.Name):
            for frame in reversed(self.service_frames):
                if node.id in frame:
                    return frame[node.id] or None
        return None

    def _trusted_store_argument_positions(self, node: ast.Call) -> set[int]:
        if (
            self.class_stack[-1:] == ["CookiePersistence"]
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_initialize"
        ):
            return {0}
        if self._service_symbol(node.func) in {
            "LoginProfileWriter",
            "AccountMetadataWriter",
        }:
            return {0}
        if not isinstance(node.func, ast.Attribute):
            return set()
        service = self._service_instance(node.func.value)
        if node.func.attr == "_resolve_with_projection" and service == "LegacyAccountMigrator":
            return {0}
        if node.func.attr == "schedule" and service == "LegacyPromotionScheduler":
            return {0}
        return set()

    def _is_store_constructor(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.class_aliases
        qualified = _qualified_name(node)
        if (
            qualified in {f"{provider}.ProfileStore" for provider in _PROFILE_STORE_PROVIDERS}
            and qualified.removesuffix(".ProfileStore") in self.module_aliases
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "ProfileStore":
            module = _qualified_name(node.value)
            return module is not None and module in self.module_aliases
        return False

    def _is_candidate_constructor(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.candidate_class_aliases
        if isinstance(node, ast.Attribute) and node.attr == "ProfileStore":
            module = _qualified_name(node.value)
            return module is not None and (
                module in self.candidate_module_aliases or module in _PROFILE_STORE_PROVIDERS
            )
        return False

    def _is_provider_module(self, node: ast.expr) -> bool:
        qualified = _qualified_name(node)
        return qualified is not None and qualified in self.module_aliases

    def visit_Assign(self, node: ast.Assign) -> None:
        is_store = self._value_is_store(node.value)
        for target in node.targets:
            self._bind_receiver(target, is_store=is_store)
            self._bind_service(target, node.value)
        if (
            self.class_stack[-1:] == ["CookiePersistence"]
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "self"
            and node.targets[0].attr == "_default_store"
            and self._is_store_receiver(node.value)
        ):
            self.visit(node.targets[0])
            return
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind_receiver(node.target, is_store=self._value_is_store(node.value))
        self._bind_service(node.target, node.value)
        if node.value is not None:
            self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind_receiver(
            node.target,
            is_store=self._value_is_store(node.value),
            skip_comprehensions=True,
        )
        self._bind_service(node.target, node.value)
        self.generic_visit(node)

    def _iter_yields_store(self, node: ast.expr) -> bool:
        return isinstance(node, ast.List | ast.Set | ast.Tuple) and any(
            self._value_is_store(item) for item in node.elts
        )

    def _visit_comprehension(
        self, generators: list[ast.comprehension], result_nodes: tuple[ast.expr, ...]
    ) -> None:
        self.binding_frames.append(("comprehension", {}))
        self.tracked_frames.append(set())
        self.service_frames.append({})
        for generator in generators:
            self.visit(generator.iter)
            self._bind_receiver(
                generator.target,
                is_store=self._iter_yields_store(generator.iter),
            )
            self._bind_service(generator.target, None)
            for condition in generator.ifs:
                self.visit(condition)
        for result_node in result_nodes:
            self.visit(result_node)
        self.service_frames.pop()
        self.tracked_frames.pop()
        self.binding_frames.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,))

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value))

    def _is_store_receiver(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            if node.id == "self" and self.class_stack[-1:] == ["ProfileStore"]:
                return True
            for _, bindings in reversed(self.binding_frames):
                if node.id in bindings:
                    return bindings[node.id]
            return False
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            return bool(
                self.store_attribute_frames and node.attr in self.store_attribute_frames[-1]
            )
        if isinstance(node, ast.NamedExpr):
            is_store = self._value_is_store(node.value)
            self._bind_receiver(
                node.target,
                is_store=is_store,
                skip_comprehensions=True,
            )
            return is_store
        return isinstance(node, ast.Call) and self._is_store_constructor(node.func)

    def _record(self, target: str) -> None:
        owner = _function_owner([*self.class_stack, *self.function_stack[:1]])
        self.calls.add((self.module, owner, target))

    def _is_tracked_store_name(self, node: ast.Name) -> bool:
        for index in range(len(self.binding_frames) - 1, -1, -1):
            _, bindings = self.binding_frames[index]
            if node.id in bindings:
                return node.id in self.tracked_frames[index]
        return False

    def visit_Name(self, node: ast.Name) -> None:
        if self.fail_closed and isinstance(node.ctx, ast.Load):
            if self._is_store_constructor(node):
                self._record(_CAPABILITY_ESCAPE)
            elif self._is_candidate_constructor(node):
                self._record(_UNTRUSTED_PROVIDER)
            elif self._is_tracked_store_name(node):
                self._record(_INSTANCE_ESCAPE)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self.fail_closed and isinstance(node.ctx, ast.Load):
            if node.attr in {"ordering_key", "path"} and self._is_store_receiver(node.value):
                return
            if self._is_store_constructor(node):
                self._record(_CAPABILITY_ESCAPE)
                return
            if self._is_candidate_constructor(node):
                self._record(_UNTRUSTED_PROVIDER)
                return
            if node.attr in _STORE_METHODS and not self._is_provider_module(node.value):
                self._record(f"{_METHOD_ESCAPE_PREFIX}{node.attr}>")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        values = [node.left, *node.comparators]
        if (
            len(node.ops) == 1
            and isinstance(node.ops[0], ast.Is | ast.IsNot)
            and any(self._is_store_receiver(value) for value in values)
            and any(isinstance(value, ast.Constant) and value.value is None for value in values)
        ):
            for value in values:
                if not self._is_store_receiver(value):
                    self.visit(value)
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target: str | None = None
        receiver: ast.expr | None = None
        provider_call = False
        is_bootstrap_write_offload = (
            self.module == "master_token_bootstrap.py"
            and self.class_stack[-1:] == ["MasterTokenBootstrapper"]
            and self.function_stack[:1] == ["bootstrap_from_oauth_token"]
            and ast.unparse(node.func) == "asyncio.to_thread"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "write_master_token"
            and self._is_store_receiver(node.args[0].value)
        )
        is_legacy_storage_writer = (
            self.module == "master_token.py"
            and self.function_stack[:1] == ["write_master_token"]
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "storage"
            and node.func.attr == "write_master_token"
        )
        if is_bootstrap_write_offload:
            self._record("write_master_token")
            self.visit(node.func)
            for argument in node.args[1:]:
                self.visit(argument)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        if is_legacy_storage_writer:
            for argument in node.args:
                self.visit(argument)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        if self._is_store_constructor(node.func):
            target = "ProfileStore"
        elif self._is_candidate_constructor(node.func):
            target = _UNTRUSTED_PROVIDER
        elif isinstance(node.func, ast.Attribute) and node.func.attr in _STORE_METHODS:
            receiver = node.func.value
            if self._is_provider_module(receiver):
                provider_call = True
            else:
                target = (
                    node.func.attr
                    if self._is_store_receiver(receiver)
                    else f"{_RECEIVER_ESCAPE_PREFIX}{node.func.attr}>"
                    if self.fail_closed
                    else None
                )
        if target is not None:
            self._record(target)
        if target in {"ProfileStore", _UNTRUSTED_PROVIDER} or receiver is not None:
            if receiver is not None and not isinstance(receiver, ast.Name) and not provider_call:
                self.visit(receiver)
            for argument in node.args:
                self.visit(argument)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and len(node.args) == 2
            and self._is_store_receiver(node.args[0])
            and self._is_store_constructor(node.args[1])
        ):
            self.visit(node.func)
            return
        trusted_positions = self._trusted_store_argument_positions(node)
        self.visit(node.func)
        for index, argument in enumerate(node.args):
            if index not in trusted_positions or not self._is_store_receiver(argument):
                self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)


def _store_calls(path: Path, tree: ast.AST) -> set[Caller]:
    (
        class_aliases,
        module_aliases,
        candidate_class_aliases,
        candidate_module_aliases,
    ) = _profile_store_bindings(path, tree)
    service_aliases, service_module_aliases = _migration_service_bindings(path, tree)
    collector = _StoreCallCollector(
        _source_label(path),
        class_aliases,
        module_aliases,
        candidate_class_aliases,
        candidate_module_aliases,
        service_aliases,
        service_module_aliases,
    )
    collector.visit(tree)
    return collector.calls


def test_direct_production_store_callers_are_exact_and_function_granular() -> None:
    actual: set[Caller] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        actual.update(_store_calls(path, tree))
    assert actual == {
        ("_android/auth.py", "_make_bearer_provider", "ProfileStore"),
        (
            "_android/auth.py",
            "BearerProvider.activate",
            "<store-method-capability-escape:read_master_token>",
        ),
        ("_web/transport/cookie_persistence.py", "CookiePersistence.__init__", "ProfileStore"),
        (
            "_web/transport/cookie_persistence.py",
            "CookiePersistence._resolve_store",
            "ProfileStore",
        ),
        (
            "_web/transport/cookie_persistence.py",
            "CookiePersistence._save_canonical",
            "merge_cookie_observation",
        ),
        ("_runtime/init.py", "build_collaborators", "ProfileStore"),
        ("account_email.py", "_read_matching_account_heal_document", "ProfileStore"),
        ("account_email.py", "_read_matching_account_heal_document", "read_document"),
        (
            "account_email.py",
            "_write_account_metadata_if_document_unchanged",
            "ProfileStore",
        ),
        (
            "account_email.py",
            "_write_account_metadata_if_document_unchanged",
            "_update_account_if_document_unchanged",
        ),
        ("_browser/browser_capture.py", "replace_captured_profile", "ProfileStore"),
        ("_browser/browser_capture.py", "replace_captured_profile", "replace_from_remint"),
        ("master_token.py", "_bootstrapper", "ProfileStore"),
        (
            "master_token_bootstrap.py",
            "MasterTokenBootstrapper._read_master_token",
            "read_master_token",
        ),
        (
            "master_token_bootstrap.py",
            "MasterTokenBootstrapper._replace_minted_session",
            "replace_minted_session",
        ),
        (
            "master_token_bootstrap.py",
            "MasterTokenBootstrapper.bootstrap_from_oauth_token",
            "write_master_token",
        ),
        ("profile_store.py", "ProfileStore.read_session", "read_document"),
        ("profile_store.py", "ProfileStore.replace_from_remint", "read_document"),
        ("profile_store.py", "ProfileStore.replace_minted_session", "read_document"),
        ("profile_store.py", "ProfileStore._read_account_document", "read_document"),
        ("profile_store.py", "ProfileStore.read_account", "_read_account_document"),
        ("profile_store.py", "ProfileStore.clear_account", "read_document"),
        ("profile_store.py", "ProfileStore._read_cookie_document", "read_document"),
        (
            "profile_store.py",
            "ProfileStore._update_account_if_document_unchanged",
            "read_document",
        ),
        ("profile_store.py", "in_storage_transaction", "ProfileStore"),
        (
            "profile_migration.py",
            "LegacyAccountMigrator._resolve_with_projection",
            "_read_account_document",
        ),
        ("profile_migration.py", "LegacyAccountMigrator.promote", "update_account"),
        ("profile_migration.py", "replace_profile_from_login", "ProfileStore"),
        ("profile_migration.py", "replace_profile_from_login", _INSTANCE_ESCAPE),
        ("profile_migration.py", "LoginProfileWriter.write", "replace_from_login"),
        ("profile_migration.py", "AccountMetadataWriter.write", "update_account"),
        ("profile_migration.py", "AccountMetadataWriter.clear", "clear_account"),
        ("psidts_recovery.py", "_read_storage_for_recovery", "ProfileStore"),
        ("psidts_recovery.py", "_read_storage_for_recovery", "read_document"),
        ("psidts_recovery.py", "_attempt_rotation", "ProfileStore"),
        ("psidts_recovery.py", "_attempt_rotation", "merge_cookie_observation"),
        ("refresh.py", "_merge_domain_fetch_observation", "merge_cookie_observation"),
        ("refresh.py", "fetch_tokens_with_domains", _INSTANCE_ESCAPE),
        ("refresh.py", "fetch_tokens_with_domains", "ProfileStore"),
        ("storage.py", "clear_in_band_account", "ProfileStore"),
        ("storage.py", "clear_in_band_account", "clear_account"),
        ("storage.py", "clear_account_metadata", "ProfileStore"),
        ("storage.py", "merge_cookie_delta", "ProfileStore"),
        ("storage.py", "merge_cookie_delta", "merge_cookie_observation"),
        ("storage.py", "merge_cookie_delta", "merge_legacy_cookie_observation"),
        ("storage.py", "promote_legacy_account", "ProfileStore"),
        ("storage.py", "read_account_metadata", "ProfileStore"),
        ("storage.py", "replace_from_remint", "ProfileStore"),
        ("storage.py", "replace_from_remint", "replace_from_remint"),
        ("storage.py", "persist_minted_jar", "ProfileStore"),
        ("storage.py", "persist_minted_jar", "replace_minted_session"),
        ("storage.py", "update_account_metadata", "ProfileStore"),
        ("storage.py", "update_account_metadata", "update_account"),
        ("storage.py", "write_account_metadata", "ProfileStore"),
        ("tokens.py", "FileAuthSource.__post_init__", _CAPABILITY_ESCAPE),
        ("tokens.py", "FileLoadedAuth.__post_init__", _CAPABILITY_ESCAPE),
        ("tokens.py", "StoredAuthLoader._merge_observation", "merge_cookie_observation"),
        ("tokens.py", "StoredAuthLoader._resolve_source", "ProfileStore"),
    }


def test_private_account_document_callers_are_exact() -> None:
    actual: set[tuple[str, str]] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        actual.update(
            (module, owner)
            for module, owner, target in _store_calls(path, tree)
            if target == "_read_account_document"
        )
    assert actual == {
        ("profile_store.py", "ProfileStore.read_account"),
        ("profile_migration.py", "LegacyAccountMigrator._resolve_with_projection"),
    }


class _PrivateCallCollector(ast.NodeVisitor):
    def __init__(self, module: str, target: str) -> None:
        self.module = module
        self.target = target
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []
        self.callee_nodes: set[int] = set()
        self.calls: set[tuple[str, str]] = set()
        self.escapes: set[tuple[str, str]] = set()

    @property
    def owner(self) -> str:
        return _function_owner([*self.class_stack, *self.function_stack[:1]])

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
        if isinstance(node.func, ast.Attribute) and node.func.attr == self.target:
            self.callee_nodes.add(id(node.func))
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
        if node.attr == self.target and id(node) not in self.callee_nodes:
            self.escapes.add((self.module, self.owner))
        self.generic_visit(node)


def _private_call_projection(target: str) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    calls: set[tuple[str, str]] = set()
    escapes: set[tuple[str, str]] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _PrivateCallCollector(_source_label(path), target)
        collector.visit(tree)
        calls.update(collector.calls)
        escapes.update(collector.escapes)
    return calls, escapes


def test_raw_projection_core_callers_are_exact_and_capability_does_not_escape() -> None:
    calls, escapes = _private_call_projection("_resolve_with_projection")
    assert calls == {
        ("profile_migration.py", "LegacyAccountMigrator.resolve"),
        ("storage.py", "read_account_metadata"),
        ("tokens.py", "AccountRouteResolver.resolve"),
    }
    assert not escapes


def test_raw_projection_core_third_caller_and_capability_escape_are_rejected() -> None:
    tree = ast.parse(
        "def forbidden(migrator, store):\n"
        "    callback = migrator._resolve_with_projection\n"
        "    return migrator._resolve_with_projection(store), callback\n"
    )
    collector = _PrivateCallCollector("synthetic.py", "_resolve_with_projection")
    collector.visit(tree)
    assert collector.calls == {("synthetic.py", "forbidden")}
    assert collector.escapes == {("synthetic.py", "forbidden")}


def test_dynamic_raw_projection_core_access_fails_closed() -> None:
    tree = ast.parse(
        "def forbidden(migrator, store):\n"
        "    callback = getattr(migrator, '_resolve_with_projection')\n"
        "    return callback(store)\n"
    )
    collector = _PrivateCallCollector("synthetic.py", "_resolve_with_projection")
    collector.visit(tree)
    assert collector.calls == set()
    assert collector.escapes == {("synthetic.py", "forbidden")}


def test_synthetic_extra_store_caller_is_rejected() -> None:
    path = AUTH_ROOT / "storage.py"
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def forbidden(path):\n"
        "    return ProfileStore(path).read_document()\n"
    )
    assert _imports_profile_store(path, tree)
    actual = _store_calls(path, tree)
    assert actual == {
        ("storage.py", "forbidden", "ProfileStore"),
        ("storage.py", "forbidden", "read_document"),
    }


def test_synthetic_third_private_account_read_caller_is_rejected() -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def forbidden(path):\n"
        "    return ProfileStore(path)._read_account_document()\n"
    )
    assert _store_calls(AUTH_ROOT / "migration.py", tree) == {
        ("migration.py", "forbidden", "ProfileStore"),
        ("migration.py", "forbidden", "_read_account_document"),
    }


def test_module_alias_private_read_caller_is_detected() -> None:
    tree = ast.parse(
        "from notebooklm._auth import profile_store as ps\n"
        "def forbidden(path):\n"
        "    return ps.ProfileStore(path)._read_account_document()\n"
    )
    assert _store_calls(AUTH_ROOT / "migration.py", tree) == {
        ("migration.py", "forbidden", "ProfileStore"),
        ("migration.py", "forbidden", "_read_account_document"),
    }


def test_direct_class_alias_binds_receiver_and_method_call() -> None:
    tree = ast.parse(
        "from notebooklm._auth.profile_store import ProfileStore as Store\n"
        "def forbidden(path):\n"
        "    store = Store(path)\n"
        "    return store.clear_account()\n"
    )
    assert _store_calls(AUTH_ROOT / "migration.py", tree) == {
        ("migration.py", "forbidden", "ProfileStore"),
        ("migration.py", "forbidden", "clear_account"),
    }


def test_same_owner_later_rebinding_invalidates_deferred_store_provider() -> None:
    tree = ast.parse(
        "from notebooklm._auth.profile_store import ProfileStore\n"
        "class CookiePersistence:\n"
        "    def create(self, path):\n"
        "        return ProfileStore(path)\n"
        "ProfileStore = object\n"
    )
    assert _store_calls(SRC_ROOT / "_web/transport/cookie_persistence.py", tree) == {
        (
            "_web/transport/cookie_persistence.py",
            "CookiePersistence.create",
            _UNTRUSTED_PROVIDER,
        )
    }


def test_arbitrary_store_sink_is_distinct_from_owned_projections_and_closure() -> None:
    persistence_path = SRC_ROOT / "_web/transport/cookie_persistence.py"
    source = persistence_path.read_text(encoding="utf-8")
    marker = "                result = store.merge_cookie_observation(\n"
    assert source.count(marker) == 1
    mutated = source.replace(marker, "                leak(store)\n" + marker)
    base = _store_calls(persistence_path, ast.parse(source))
    bitten = _store_calls(persistence_path, ast.parse(mutated))
    assert bitten - base == {
        (
            "_web/transport/cookie_persistence.py",
            "CookiePersistence._save_canonical",
            _INSTANCE_ESCAPE,
        )
    }


def test_shadowed_writer_name_does_not_receive_trusted_store_capability() -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def AccountMetadataWriter(value):\n"
        "    return value\n"
        "def forbidden(path):\n"
        "    store = ProfileStore(path)\n"
        "    return AccountMetadataWriter(store)\n"
    )
    assert _store_calls(AUTH_ROOT / "synthetic.py", tree) == {
        ("synthetic.py", "forbidden", "ProfileStore"),
        ("synthetic.py", "forbidden", _INSTANCE_ESCAPE),
    }


def test_later_module_rebinding_invalidates_deferred_writer_lookup() -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "from .profile_migration import AccountMetadataWriter\n"
        "def forbidden(path):\n"
        "    store = ProfileStore(path)\n"
        "    return AccountMetadataWriter(store)\n"
        "AccountMetadataWriter = lambda value: value\n"
    )
    assert _store_calls(AUTH_ROOT / "synthetic.py", tree) == {
        ("synthetic.py", "forbidden", "ProfileStore"),
        ("synthetic.py", "forbidden", _INSTANCE_ESCAPE),
    }


def test_conflicting_injected_store_field_fails_closed() -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "class Service:\n"
        "    def __init__(self, store: ProfileStore, condition):\n"
        "        if condition:\n"
        "            self._store = store\n"
        "        else:\n"
        "            self._store = object()\n"
        "    def forbidden(self, record):\n"
        "        return self._store.update_account(record)\n"
    )
    assert _store_calls(AUTH_ROOT / "synthetic.py", tree) == {
        (
            "synthetic.py",
            "Service.forbidden",
            f"{_RECEIVER_ESCAPE_PREFIX}update_account>",
        )
    }


@pytest.mark.parametrize(
    "source",
    [
        (
            "from notebooklm._auth.storage import ProfileStore as Store\n"
            "def forbidden(path):\n"
            "    return Store(path).read_session()\n"
        ),
        (
            "from notebooklm._auth import storage as provider\n"
            "def forbidden(path):\n"
            "    return provider.ProfileStore(path).read_session()\n"
        ),
        (
            "import notebooklm._auth.storage\n"
            "def forbidden(path):\n"
            "    return notebooklm._auth.storage.ProfileStore(path).read_session()\n"
        ),
    ],
    ids=["direct", "module", "fully-qualified"],
)
def test_storage_profile_store_provider_forms_are_detected(source: str) -> None:
    assert _store_calls(AUTH_ROOT / "migration.py", ast.parse(source)) == {
        ("migration.py", "forbidden", "ProfileStore"),
        ("migration.py", "forbidden", "read_session"),
    }


def test_external_cli_caller_uses_stable_source_relative_label() -> None:
    path = SRC_ROOT / "cli" / "synthetic.py"
    tree = ast.parse(
        "from notebooklm._auth.profile_store import ProfileStore\n"
        "def forbidden(path):\n"
        "    return ProfileStore(path).read_document()\n"
    )
    assert _store_calls(path, tree) == {
        ("cli/synthetic.py", "forbidden", "ProfileStore"),
        ("cli/synthetic.py", "forbidden", "read_document"),
    }


@pytest.mark.parametrize("method", ["read_document", "read_session"])
def test_document_and_session_reads_are_audited_direct_methods(method: str) -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def forbidden(path):\n"
        f"    return ProfileStore(path).{method}()\n"
    )
    assert _store_calls(AUTH_ROOT / "migration.py", tree) == {
        ("migration.py", "forbidden", "ProfileStore"),
        ("migration.py", "forbidden", method),
    }


@pytest.mark.parametrize(
    ("binding", "calls_replace"),
    [
        ("store: ProfileStore = ProfileStore(path)\n", False),
        ("(store := ProfileStore(path)).replace_from_login(request)\n", True),
    ],
    ids=["annotated-assignment", "named-expression"],
)
def test_lexical_receiver_bindings_expose_private_read_in_allowed_login_owner(
    binding: str, calls_replace: bool
) -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path, request):\n"
        f"    {binding}"
        "    return store._read_account_document()\n"
    )
    expected = {
        ("storage.py", "replace_from_login", "ProfileStore"),
        ("storage.py", "replace_from_login", "_read_account_document"),
    }
    if calls_replace:
        expected.add(("storage.py", "replace_from_login", "replace_from_login"))
    assert _store_calls(AUTH_ROOT / "storage.py", tree) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "[store._read_account_document() for store in [ProfileStore(path)]]",
        "{store._read_account_document() for store in {ProfileStore(path)}}",
        "{store: store._read_account_document() for store in (ProfileStore(path),)}",
        "(store._read_account_document() for store in [ProfileStore(path)])",
    ],
    ids=["list", "set", "dict", "generator"],
)
def test_comprehension_receiver_binding_exposes_private_read_in_allowed_login_owner(
    expression: str,
) -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path):\n"
        f"    return {expression}\n"
    )
    expected = {
        ("storage.py", "replace_from_login", "ProfileStore"),
        ("storage.py", "replace_from_login", "_read_account_document"),
    }
    if expression.startswith("{store:"):
        expected.add(("storage.py", "replace_from_login", _INSTANCE_ESCAPE))
    assert _store_calls(AUTH_ROOT / "storage.py", tree) == expected


def test_comprehension_receiver_binding_does_not_leak_into_function_scope() -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path, store):\n"
        "    [store.path for store in [ProfileStore(path)]]\n"
        "    return store._read_account_document()\n"
    )
    assert _store_calls(AUTH_ROOT / "storage.py", tree) == {
        ("storage.py", "replace_from_login", "ProfileStore"),
        (
            "storage.py",
            "replace_from_login",
            f"{_RECEIVER_ESCAPE_PREFIX}_read_account_document>",
        ),
    }


@pytest.mark.parametrize(
    "comprehension",
    [
        "[(store := ProfileStore(path)) for _ in [0]]",
        "[_ for _ in [0] if (store := ProfileStore(path))]",
    ],
    ids=["element", "if-clause"],
)
def test_comprehension_named_expression_binds_enclosing_login_scope(
    comprehension: str,
) -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path):\n"
        f"    {comprehension}\n"
        "    return store._read_account_document()\n"
    )
    assert _store_calls(AUTH_ROOT / "storage.py", tree) == {
        ("storage.py", "replace_from_login", "ProfileStore"),
        ("storage.py", "replace_from_login", "_read_account_document"),
    }


def test_comprehension_named_expression_invalidates_enclosing_receiver_binding() -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path):\n"
        "    store = ProfileStore(path)\n"
        "    [(store := object()) for _ in [0]]\n"
        "    return store._read_account_document()\n"
    )
    assert _store_calls(AUTH_ROOT / "storage.py", tree) == {
        ("storage.py", "replace_from_login", "ProfileStore"),
        (
            "storage.py",
            "replace_from_login",
            f"{_RECEIVER_ESCAPE_PREFIX}_read_account_document>",
        ),
    }


def test_lambda_local_walrus_does_not_invalidate_enclosing_receiver_binding() -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path):\n"
        "    store = ProfileStore(path)\n"
        "    [(lambda: (store := object()))() for _ in [0]]\n"
        "    return store._read_account_document()\n"
    )
    assert _store_calls(AUTH_ROOT / "storage.py", tree) == {
        ("storage.py", "replace_from_login", "ProfileStore"),
        ("storage.py", "replace_from_login", "_read_account_document"),
    }


def test_lambda_local_walrus_does_not_create_enclosing_receiver_binding() -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path, store):\n"
        "    [(lambda *_args, **_kwargs: (store := ProfileStore(path)))() for _ in [0]]\n"
        "    return store._read_account_document()\n"
    )
    assert _store_calls(AUTH_ROOT / "storage.py", tree) == {
        ("storage.py", "replace_from_login", "ProfileStore"),
        (
            "storage.py",
            "replace_from_login",
            f"{_RECEIVER_ESCAPE_PREFIX}_read_account_document>",
        ),
    }


@pytest.mark.parametrize(
    "body",
    [
        (
            "    class Holder: pass\n"
            "    Holder.store = ProfileStore(path)\n"
            "    return Holder.store._read_account_document()\n"
        ),
        (
            "    def nested(store=ProfileStore(path)):\n"
            "        return store._read_account_document()\n"
            "    return nested()\n"
        ),
        (
            "    store = ProfileStore(path)\n"
            "    def nested():\n"
            "        nonlocal store\n"
            "        store = object()\n"
            "        return store._read_account_document()\n"
            "    return nested()\n"
        ),
        (
            "    store = ProfileStore(path)\n"
            "    alias = store\n"
            "    return alias._read_account_document()\n"
        ),
        (
            "    store = passthrough(ProfileStore(path))\n"
            "    return store._read_account_document()\n"
        ),
        (
            "    for store in [ProfileStore(path)]:\n"
            "        pass\n"
            "    return store._read_account_document()\n"
        ),
        (
            "    store = ProfileStore(path) if condition else object()\n"
            "    return store._read_account_document()\n"
        ),
        (
            "    try:\n"
            "        store = ProfileStore(path)\n"
            "    except Exception:\n"
            "        store = object()\n"
            "    return store._read_account_document()\n"
        ),
        (
            "    match value:\n"
            "        case 0:\n"
            "            store = ProfileStore(path)\n"
            "        case _:\n"
            "            store = object()\n"
            "    return store._read_account_document()\n"
        ),
    ],
    ids=["class", "default", "nonlocal", "alias", "rhs", "for", "conditional", "try", "match"],
)
def test_uncertain_store_receiver_fails_closed_inside_allowed_login_owner(body: str) -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path, condition=False, passthrough=lambda value: value, value=0):\n"
        f"{body}"
    )
    calls = _store_calls(AUTH_ROOT / "storage.py", tree)
    assert (
        "storage.py",
        "replace_from_login",
        f"{_RECEIVER_ESCAPE_PREFIX}_read_account_document>",
    ) in calls


def test_factory_alias_capability_and_receiver_both_fail_closed() -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path):\n"
        "    factory = ProfileStore\n"
        "    store = factory(path)\n"
        "    return store._read_account_document()\n"
    )
    calls = _store_calls(AUTH_ROOT / "storage.py", tree)
    assert ("storage.py", "replace_from_login", _CAPABILITY_ESCAPE) in calls
    assert (
        "storage.py",
        "replace_from_login",
        f"{_RECEIVER_ESCAPE_PREFIX}_read_account_document>",
    ) in calls


@pytest.mark.parametrize(
    ("body", "method"),
    [
        ("    callback = store.read_document\n    return callback()\n", "read_document"),
        ("    return consume(callback=store.read_session)\n", "read_session"),
        ("    return store.read_account\n", "read_account"),
    ],
    ids=["assigned", "keyword", "bare"],
)
def test_bound_store_method_capability_loads_fail_closed(body: str, method: str) -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path, consume=None):\n"
        "    store = ProfileStore(path)\n"
        f"{body}"
    )
    calls = _store_calls(AUTH_ROOT / "storage.py", tree)
    assert (
        "storage.py",
        "replace_from_login",
        f"{_METHOD_ESCAPE_PREFIX}{method}>",
    ) in calls


@pytest.mark.parametrize(
    "body",
    [
        "    escaped = store\n    return escaped\n",
        "    return store\n",
        "    return consume(store)\n",
        "    return getattr(store, 'read_document')\n",
    ],
    ids=["assignment", "return", "callback", "getattr"],
)
def test_store_instance_name_capability_loads_fail_closed(body: str) -> None:
    tree = ast.parse(
        "from .profile_store import ProfileStore\n"
        "def replace_from_login(path, consume=None):\n"
        "    store = ProfileStore(path)\n"
        f"{body}"
    )
    assert (
        "storage.py",
        "replace_from_login",
        _INSTANCE_ESCAPE,
    ) in _store_calls(AUTH_ROOT / "storage.py", tree)


def test_unrelated_coincidentally_named_method_is_not_credited() -> None:
    tree = ast.parse(
        "class Other:\n"
        "    def read_account(self): ...\n"
        "def allowed(other):\n"
        "    return other.read_account()\n"
    )
    assert _store_calls(AUTH_ROOT / "synthetic.py", tree) == set()


def test_account_method_signatures_and_storage_leaf_import_are_exact() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    cls = _profile_store_class(tree)
    methods = {
        node.name: node
        for node in cls.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert ast.unparse(methods["read_account"].args) == "self"
    assert ast.unparse(methods["update_account"].args) == (
        "self, record: ProfileAccount, *, only_if_absent: bool=False"
    )
    assert ast.unparse(methods["clear_account"].args) == "self"

    storage_tree = ast.parse(
        (AUTH_ROOT / "storage.py").read_text(encoding="utf-8"),
        filename=str(AUTH_ROOT / "storage.py"),
    )
    imports = _collect_imports(storage_tree)
    assert [record for record in imports if record[2] == "profile_account"] == [
        ("module", 1, "profile_account", "DomainSelection", None),
        ("module", 1, "profile_account", "ProfileAccount", None),
    ]


def _auth_edges() -> dict[str, set[str]]:
    modules = {path.stem for path in AUTH_ROOT.glob("*.py")}
    graph = {module: set() for module in modules}
    prefix = "notebooklm._auth."
    for path in AUTH_ROOT.glob("*.py"):
        source = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolved_module(path, node)
                targets = [resolved, *(f"{resolved}.{item.name}" for item in node.names)]
            for target in targets:
                if target.startswith(prefix):
                    candidate = target.removeprefix(prefix).split(".", 1)[0]
                    if candidate in modules and candidate != source:
                        graph[source].add(candidate)
    return graph


def _reachable(graph: dict[str, set[str]], source: str, target: str) -> bool:
    pending = list(graph.get(source, set()))
    seen: set[str] = set()
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(graph.get(node, set()))
    return False


def test_profile_store_participates_in_no_strongly_connected_component() -> None:
    graph = _auth_edges()
    assert not any(
        _reachable(graph, "profile_store", module) and _reachable(graph, module, "profile_store")
        for module in graph
        if module != "profile_store"
    )


def test_cycle_detector_bites_on_synthetic_two_node_cycle() -> None:
    graph = {"profile_store": {"storage_lock"}, "storage_lock": {"profile_store"}}
    assert _reachable(graph, "profile_store", "storage_lock")
    assert _reachable(graph, "storage_lock", "profile_store")


def test_profile_store_is_not_exposed_by_facade_or_auth_package() -> None:
    for path in (FACADE_PATH, AUTH_INIT_PATH):
        source = path.read_text(encoding="utf-8")
        assert "ProfileStore" not in source
    facade_imports = _collect_imports(
        ast.parse(FACADE_PATH.read_text(encoding="utf-8"), filename=str(FACADE_PATH))
    )
    assert [record for record in facade_imports if record[2] == "_auth.profile_store"] == [
        ("module", 1, "_auth.profile_store", "ReplaceResult", None)
    ]
    assert "profile_store" not in AUTH_INIT_PATH.read_text(encoding="utf-8")
