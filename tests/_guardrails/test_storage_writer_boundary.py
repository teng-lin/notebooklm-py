"""Capability gate for credential commits and storage-state writers.

ADR-0034 PR6 seals the unchecked atomic primitive behind ``credential_io``.
PR7A moves typed in-band account update/clear into ``ProfileStore`` beside its
cookie transactions; PR7B moves remint replacement and PR7C moves login/import
replacement onto the same store while ``storage.py`` retains the v0.x adapters
and PR7D leaves only the minted-session snapshot/error adapter there.
The legacy arbitrary-path token writer uses the distinct typed token commit.
All caller sets below are equality assertions at function/method granularity.
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"

_BYPASS = "_atomic_write_json_unchecked"
_RAW_COMMIT = "_commit_json_unchecked"
_PROFILE_COMMIT = "_commit_profile_json"
_TOKEN_COMMIT = "_commit_master_token_json"

_BYPASS_IMPORTERS = frozenset({"_auth/credential_io.py"})
_BYPASS_CALLERS = frozenset({"_auth/credential_io._commit_json_unchecked"})
_RAW_COMMIT_CALLERS = frozenset(
    {
        "_auth/credential_io._commit_master_token_json",
        "_auth/credential_io._commit_profile_json",
    }
)
_PROFILE_COMMIT_CALLERS = frozenset(
    {
        "_auth/profile_store.ProfileStore.merge_cookie_observation",
        "_auth/profile_store.ProfileStore.merge_legacy_cookie_observation",
        "_auth/profile_store.ProfileStore.replace_from_login",
        "_auth/profile_store.ProfileStore.replace_minted_session",
        "_auth/profile_store.ProfileStore.replace_from_remint",
        "_auth/profile_store.ProfileStore.clear_account",
        "_auth/profile_store.ProfileStore.update_account",
    }
)
_TOKEN_COMMIT_CALLERS = frozenset({"_auth/master_token_file.MasterTokenFile.write"})

_PUBLIC_ATOMIC_IMPORTERS = frozenset(
    {"_auth/profile_migration.py", "cli/context.py", "io.py", "mcp/_oauth.py"}
)
_PUBLIC_ATOMIC_CALLERS = frozenset(
    {
        "_auth/profile_migration.LegacyAccountContext.scrub",
        "cli/context._clear_context_file_locked",
        "mcp/_oauth.SelfHostedOAuthProvider._write_state_file",
    }
)
_REPLACE_FILE_IMPORTERS = frozenset({"cli/skill_cmd.py", "io.py", "migration.py"})
_STORAGE_STATE_WRITE_EXEMPTIONS = frozenset({"migration.py"})
_AUTH_ATOMIC_PRIMITIVE_IMPORTERS = frozenset(
    {"_auth/credential_io.py", "_auth/profile_migration.py"}
)

_ATOMIC_MODULES = frozenset({"_atomic_io", "io", "notebooklm._atomic_io", "notebooklm.io"})
_WRITE_PRIMITIVES = frozenset({"atomic_write_json", "replace_file_atomically", _BYPASS})


def _iter_src_files() -> list[Path]:
    return sorted(path for path in SRC_ROOT.rglob("*.py") if "__pycache__" not in path.parts)


def _rel(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix()


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


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        if owner is not None:
            return f"{owner}.{node.attr}"
    return None


def _symbol_bindings(
    path: Path,
    tree: ast.AST,
    *,
    target_module: str,
    symbols: frozenset[str],
) -> tuple[dict[str, str], set[str]]:
    direct: dict[str, str] = {}
    modules: set[str] = set()
    parent, _, leaf = target_module.rpartition(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            if resolved == target_module:
                for item in node.names:
                    if item.name in symbols:
                        direct[item.asname or item.name] = item.name
            elif resolved == parent:
                for item in node.names:
                    if item.name == leaf:
                        modules.add(item.asname or item.name)
        elif isinstance(node, ast.Import):
            for item in node.names:
                if item.name == target_module:
                    modules.add(item.asname or item.name)
    if target_module == "notebooklm._auth.credential_io":
        module_name = ".".join(path.relative_to(REPO_ROOT / "src").with_suffix("").parts)
        if module_name == target_module:
            direct.update({symbol: symbol for symbol in symbols})
    return direct, modules


class _CapabilityCollector(ast.NodeVisitor):
    def __init__(
        self,
        module: str,
        direct: dict[str, str],
        modules: set[str],
        symbols: frozenset[str],
    ) -> None:
        self.module = module.removesuffix(".py")
        self.direct = direct
        self.modules = modules
        self.symbols = symbols
        self.class_stack: list[str] = []
        self.function_stack: list[str] = []
        self.calls: dict[str, set[str]] = defaultdict(set)
        self.callee_nodes: set[int] = set()

    @property
    def owner(self) -> str:
        suffix = [*self.class_stack, *self.function_stack[:1]]
        return ".".join([self.module, *suffix]) if suffix else f"{self.module}.<module>"

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
        canonical: str | None = None
        if isinstance(node.func, ast.Name):
            canonical = self.direct.get(node.func.id)
        else:
            qualified = _qualified_name(node.func)
            if qualified is not None:
                module, separator, member = qualified.rpartition(".")
                if separator and module in self.modules and member in self.symbols:
                    canonical = member
        if canonical is not None:
            self.calls[canonical].add(self.owner)
            self.callee_nodes.add(id(node.func))
        self.generic_visit(node)


def _capability_projection(
    *, target_module: str, symbols: frozenset[str]
) -> tuple[dict[str, set[str]], dict[str, list[int]]]:
    calls: dict[str, set[str]] = defaultdict(set)
    escapes: dict[str, list[int]] = defaultdict(list)
    for path in _iter_src_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        direct, modules = _symbol_bindings(path, tree, target_module=target_module, symbols=symbols)
        if not direct and not modules:
            continue
        collector = _CapabilityCollector(_rel(path), direct, modules, symbols)
        collector.visit(tree)
        for symbol, owners in collector.calls.items():
            calls[symbol].update(owners)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and node.id in direct
                and isinstance(node.ctx, ast.Load)
                and id(node) not in collector.callee_nodes
            ):
                escapes[_rel(path)].append(node.lineno)
            elif isinstance(node, ast.Attribute):
                qualified = _qualified_name(node)
                if qualified is None:
                    continue
                module, separator, member = qualified.rpartition(".")
                if (
                    separator
                    and module in modules
                    and member in symbols
                    and id(node) not in collector.callee_nodes
                ):
                    escapes[_rel(path)].append(node.lineno)
    return calls, {path: sorted(lines) for path, lines in escapes.items()}


def _imports_named_symbol(path: Path, tree: ast.AST, symbol: str) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and any(item.name == symbol for item in node.names)
        for node in ast.walk(tree)
    )


def test_unchecked_bypass_importer_and_caller_are_exact() -> None:
    importers = {
        _rel(path)
        for path in _iter_src_files()
        if _imports_named_symbol(
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
            _BYPASS,
        )
    }
    calls, escapes = _capability_projection(
        target_module="notebooklm._atomic_io", symbols=frozenset({_BYPASS})
    )
    assert importers == set(_BYPASS_IMPORTERS)
    assert calls[_BYPASS] == set(_BYPASS_CALLERS)
    assert escapes == {}


def test_raw_commit_callers_are_exactly_the_two_typed_wrappers() -> None:
    calls, escapes = _capability_projection(
        target_module="notebooklm._auth.credential_io",
        symbols=frozenset({_RAW_COMMIT}),
    )
    assert calls[_RAW_COMMIT] == set(_RAW_COMMIT_CALLERS)
    assert escapes == {}


def test_typed_profile_commit_callers_are_exact() -> None:
    calls, escapes = _capability_projection(
        target_module="notebooklm._auth.credential_io",
        symbols=frozenset({_PROFILE_COMMIT}),
    )
    assert calls[_PROFILE_COMMIT] == set(_PROFILE_COMMIT_CALLERS)
    assert escapes == {}


def _storage_function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(
        (AUTH_ROOT / "storage.py").read_text(encoding="utf-8"),
        filename=str(AUTH_ROOT / "storage.py"),
    )
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


_MINTED_ADAPTER_CALLS: Counter[str] = Counter(
    {
        "Cookie": 1,
        "CookieJar": 1,
        "MintedSessionWriteRequest": 1,
        "ProfileStore": 1,
        "ProfileStore(path).replace_minted_session": 1,
        "_cookie_is_http_only": 1,
        "MasterTokenError": 1,
        "cast": 1,
        "len": 1,
        "required_order.index": 1,
        "sorted": 1,
        "str": 1,
    }
)


def _assert_minted_adapter_call_multiset_is_exact(adapter: ast.AST) -> None:
    actual = Counter(
        ast.unparse(node.func) for node in ast.walk(adapter) if isinstance(node, ast.Call)
    )
    assert actual == _MINTED_ADAPTER_CALLS


def test_remint_adapter_has_no_writer_transaction_read_or_filter_capability() -> None:
    adapter = _storage_function("replace_from_remint")
    called_members = [
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    ]
    assert set(called_members).isdisjoint(
        {
            _PROFILE_COMMIT,
            _RAW_COMMIT,
            _TOKEN_COMMIT,
            _BYPASS,
            "acquire",
            "in_storage_transaction",
            "_under_bounded_lock",
            "exists",
            "open",
            "read_bytes",
            "read_document",
            "read_text",
            "filter_storage_state_cookies_by_domain_policy",
        }
    )
    assert called_members.count("replace_from_remint") == 1
    assert not any(
        isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
        for node in ast.walk(adapter)
    )


def test_login_adapter_has_no_writer_transaction_read_or_filter_capability() -> None:
    adapter = _storage_function("replace_from_login")
    called_members = [
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    ]
    assert set(called_members).isdisjoint(
        {
            _PROFILE_COMMIT,
            _RAW_COMMIT,
            _TOKEN_COMMIT,
            _BYPASS,
            "acquire",
            "in_storage_transaction",
            "_under_bounded_lock",
            "exists",
            "open",
            "read_bytes",
            "read_document",
            "read_text",
            "filter_storage_state_cookies_by_domain_policy",
            "cookie_names_from_storage",
            "copy2",
            "chmod",
        }
    )
    assert called_members.count("write") == 1
    assert "replace_from_login" not in called_members
    assert not any(
        isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
        for node in ast.walk(adapter)
    )


def test_minted_adapter_has_only_snapshot_delegation_and_error_projection() -> None:
    adapter = _storage_function("persist_minted_jar")
    _assert_minted_adapter_call_multiset_is_exact(adapter)
    assert not any(
        isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
        for node in ast.walk(adapter)
    )

    handlers = [node for node in ast.walk(adapter) if isinstance(node, ast.ExceptHandler)]
    raises = [node for node in ast.walk(adapter) if isinstance(node, ast.Raise)]
    assert len(handlers) == len(raises) == 1
    assert isinstance(handlers[0].type, ast.Name)
    assert handlers[0].type.id == "_MintedSessionOwnershipRefused"
    assert not any(isinstance(node, ast.Raise) for node in ast.walk(handlers[0]))
    assert handlers[0].end_lineno is not None and raises[0].lineno > handlers[0].end_lineno
    assert raises[0].cause is None


@pytest.mark.parametrize("helper", ["_read_in_band_account", "_read_legacy_account"])
def test_minted_adapter_call_gate_bites_on_legacy_account_helpers(helper: str) -> None:
    adapter = _storage_function("persist_minted_jar")
    adapter.body.append(
        ast.Expr(
            value=ast.Call(
                func=ast.Name(id=helper, ctx=ast.Load()),
                args=[ast.Name(id="path", ctx=ast.Load())],
                keywords=[],
            )
        )
    )
    with pytest.raises(AssertionError):
        _assert_minted_adapter_call_multiset_is_exact(adapter)


def test_typed_master_token_commit_caller_is_exact() -> None:
    calls, escapes = _capability_projection(
        target_module="notebooklm._auth.credential_io",
        symbols=frozenset({_TOKEN_COMMIT}),
    )
    assert calls[_TOKEN_COMMIT] == set(_TOKEN_COMMIT_CALLERS)
    assert escapes == {}


def _assert_token_adapter_shape(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    statements = [
        item
        for item in node.body
        if not (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        )
    ]
    assert len(statements) == 1
    assert ast.unparse(statements[0]) == (
        "MasterTokenFile(path).write(MasterToken(email=email, android_id=android_id, "
        "secret=master_token))"
    )
    calls = Counter(ast.unparse(item.func) for item in ast.walk(node) if isinstance(item, ast.Call))
    assert calls == Counter(
        {"MasterTokenFile(path).write": 1, "MasterTokenFile": 1, "MasterToken": 1}
    )
    assert {
        name
        for name in ("in_storage_transaction", _TOKEN_COMMIT, "read_text", "exists")
        if any(
            isinstance(item, ast.Name | ast.Attribute)
            and (item.id if isinstance(item, ast.Name) else item.attr) == name
            for item in ast.walk(node)
        )
    } == set()


def test_storage_master_token_writer_is_only_the_typed_file_adapter() -> None:
    _assert_token_adapter_shape(_storage_function("write_master_token"))


@pytest.mark.parametrize(
    "replacement",
    [
        "MasterTokenFile(path).write(MasterToken(email=email, android_id=android_id, secret='x'))",
        "MasterTokenFile(path).read()",
        "_commit_master_token_json(path, {})",
        "in_storage_transaction(path, lambda: None)",
    ],
)
def test_storage_master_token_adapter_guard_bites_on_secret_rewrite_read_and_old_owners(
    replacement: str,
) -> None:
    node = ast.parse(
        f"def write_master_token(path, *, email, master_token, android_id):\n    {replacement}\n"
    ).body[0]
    assert isinstance(node, ast.FunctionDef)
    with pytest.raises(AssertionError):
        _assert_token_adapter_shape(node)


@pytest.mark.parametrize(
    "source",
    [
        "def forbidden():\n    return _commit_profile_json\n",
        "def forbidden(fn=_commit_master_token_json):\n    return fn\n",
        "def forbidden():\n    return Store(commit=_commit_profile_json)\n",
        "class Injected:\n    commit = _commit_master_token_json\n",
        "REGISTRY = {_commit_profile_json}\n",
    ],
)
def test_bare_callable_default_constructor_and_module_escapes_are_detected(source: str) -> None:
    path = AUTH_ROOT / "synthetic.py"
    tree = ast.parse(
        "from .credential_io import _commit_profile_json, _commit_master_token_json\n" + source
    )
    direct, modules = _symbol_bindings(
        path,
        tree,
        target_module="notebooklm._auth.credential_io",
        symbols=frozenset({_PROFILE_COMMIT, _TOKEN_COMMIT}),
    )
    collector = _CapabilityCollector(
        "_auth/synthetic.py", direct, modules, frozenset(direct.values())
    )
    collector.visit(tree)
    callee_nodes = collector.callee_nodes
    assert any(
        isinstance(node, ast.Name)
        and node.id in direct
        and isinstance(node.ctx, ast.Load)
        and id(node) not in callee_nodes
        for node in ast.walk(tree)
    )


def test_extra_nested_class_and_module_alias_callers_are_detected() -> None:
    path = AUTH_ROOT / "synthetic.py"
    tree = ast.parse(
        "from . import credential_io as commits\n"
        "class Service:\n"
        "    def forbidden(self, path, payload):\n"
        "        def nested():\n"
        "            commits._commit_profile_json(path, payload)\n"
        "        return nested()\n"
    )
    direct, modules = _symbol_bindings(
        path,
        tree,
        target_module="notebooklm._auth.credential_io",
        symbols=frozenset({_PROFILE_COMMIT}),
    )
    collector = _CapabilityCollector(
        "_auth/synthetic.py", direct, modules, frozenset({_PROFILE_COMMIT})
    )
    collector.visit(tree)
    assert collector.calls[_PROFILE_COMMIT] == {"_auth/synthetic.Service.forbidden"}
    assert not collector.calls[_PROFILE_COMMIT] <= _PROFILE_COMMIT_CALLERS


def test_public_atomic_write_json_importers_and_callers_remain_exact() -> None:
    importers = {
        _rel(path)
        for path in _iter_src_files()
        if _imports_named_symbol(
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
            "atomic_write_json",
        )
    }
    calls, escapes = _capability_projection(
        target_module="notebooklm._atomic_io",
        symbols=frozenset({"atomic_write_json"}),
    )
    # ``cli.context`` imports the public re-export from ``notebooklm.io``; add
    # that exact projection without widening the low-level module binding.
    facade_calls, facade_escapes = _capability_projection(
        target_module="notebooklm.io", symbols=frozenset({"atomic_write_json"})
    )
    callers = calls["atomic_write_json"] | facade_calls["atomic_write_json"]
    assert importers == set(_PUBLIC_ATOMIC_IMPORTERS)
    assert callers == set(_PUBLIC_ATOMIC_CALLERS)
    assert escapes == {}
    assert facade_escapes == {}


def test_replace_file_atomically_importers_remain_exact() -> None:
    actual = {
        _rel(path)
        for path in _iter_src_files()
        if _imports_named_symbol(
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
            "replace_file_atomically",
        )
    }
    assert actual == set(_REPLACE_FILE_IMPORTERS)


def test_auth_atomic_primitive_importers_are_exact() -> None:
    actual = {
        _rel(path)
        for path in AUTH_ROOT.glob("*.py")
        if any(
            _imports_named_symbol(
                path,
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
                symbol,
            )
            for symbol in _WRITE_PRIMITIVES
        )
    }
    assert actual == set(_AUTH_ATOMIC_PRIMITIVE_IMPORTERS)


def _atomic_module_object_imported(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            item.name == "notebooklm._atomic_io" for item in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "") in {"", "notebooklm"}:
            if any(item.name == "_atomic_io" for item in node.names):
                return True
    return False


def test_no_production_module_imports_atomic_io_as_a_module_object() -> None:
    actual = {
        _rel(path)
        for path in _iter_src_files()
        if _atomic_module_object_imported(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    }
    assert actual == set()


@pytest.mark.parametrize(
    "source",
    [
        "import notebooklm._atomic_io\n",
        "import notebooklm._atomic_io as atomic\n",
        "from notebooklm import _atomic_io\n",
        "from .. import _atomic_io as atomic\n",
    ],
)
def test_atomic_module_object_import_detector_bites(source: str) -> None:
    assert _atomic_module_object_imported(ast.parse(source))


def _has_write_primitive_seam_binding(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and any(
            keyword.arg in _WRITE_PRIMITIVES for keyword in node.keywords
        ):
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Attribute) and target.attr in _WRITE_PRIMITIVES
            for target in node.targets
        ):
            return True
    return False


def test_write_primitive_dependency_seams_remain_empty() -> None:
    actual = {
        _rel(path)
        for path in _iter_src_files()
        if _has_write_primitive_seam_binding(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    }
    assert actual == set()


def _is_storage_state_literal(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "storage_state.json" in node.value
    )


def _literal_write_calls(tree: ast.AST) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open" and node.args:
            mode = node.args[1] if len(node.args) > 1 else None
            writing = mode is None or (
                isinstance(mode, ast.Constant)
                and isinstance(mode.value, str)
                and any(flag in mode.value for flag in ("w", "a", "x", "+"))
            )
            if writing and _is_storage_state_literal(node.args[0]):
                hits.append(node.lineno)
        elif isinstance(func, ast.Attribute):
            if (
                isinstance(func.value, ast.Name)
                and func.value.id == "os"
                and func.attr == "replace"
                and len(node.args) > 1
                and _is_storage_state_literal(node.args[1])
            ):
                hits.append(node.lineno)
            elif func.attr in {"write_text", "write_bytes"}:
                receiver = func.value
                if (
                    isinstance(receiver, ast.Call)
                    and receiver.args
                    and _is_storage_state_literal(receiver.args[0])
                ):
                    hits.append(node.lineno)
    return hits


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ('open("storage_state.json", "w")', True),
        ('open("storage_state.json", "r")', False),
        ('os.replace(tmp, "storage_state.json")', True),
        ('Path("storage_state.json").write_text(data)', True),
        ('Path("context.json").write_text(data)', False),
    ],
)
def test_literal_writer_detector_bites(source: str, flagged: bool) -> None:
    assert bool(_literal_write_calls(ast.parse(source))) is flagged


def test_no_storage_state_literal_write_outside_migration_exemption() -> None:
    violations = {
        _rel(path): hits
        for path in _iter_src_files()
        if _rel(path) not in _STORAGE_STATE_WRITE_EXEMPTIONS
        if (hits := _literal_write_calls(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert violations == {}


def _filelock_users_from_tree(tree: ast.AST) -> set[str]:
    bindings = {
        item.asname or item.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "filelock"
        for item in node.names
    }
    users: set[str] = set()

    class Collector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.classes: list[str] = []
            self.functions: list[str] = []

        @property
        def owner(self) -> str:
            return ".".join([*self.classes, *self.functions[:1]])

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.classes.append(node.name)
            self.generic_visit(node)
            self.classes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id in bindings:
                users.add(self.owner or "<module>")

    Collector().visit(tree)
    return users


def _filelock_users(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _filelock_users_from_tree(tree)


def test_filelock_owner_detector_bites_at_class_method_scope() -> None:
    tree = ast.parse(
        "from filelock import FileLock\nclass Extra:\n    def write(self): FileLock()\n"
    )
    assert _filelock_users_from_tree(tree) == {"Extra.write"}


def test_context_filelock_use_moved_wholly_to_profile_migration() -> None:
    assert _filelock_users(AUTH_ROOT / "storage.py") == set()
    assert _filelock_users(AUTH_ROOT / "profile_migration.py") == {"LegacyAccountContext.scrub"}


_SCHEDULER_STATE_NAMES = frozenset(
    {
        "_process_default_scheduler",
        "_registry_lock",
        "_once_paths",
        "_workers",
        "_thread_factory",
        "_PROMOTION_EXIT_JOIN_SECONDS",
    }
)


def _assigned_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets.append(node.target)
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def test_scheduler_state_and_exit_lifecycle_have_exactly_one_auth_owner() -> None:
    trees = {
        _rel(path): ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in AUTH_ROOT.glob("*.py")
    }
    state_owners = {
        name: {path for path, tree in trees.items() if name in _assigned_names(tree)}
        for name in _SCHEDULER_STATE_NAMES
    }
    assert state_owners == {name: {"_auth/profile_migration.py"} for name in _SCHEDULER_STATE_NAMES}

    atexit_importers = {
        path
        for path, tree in trees.items()
        if any(
            isinstance(node, ast.Import) and any(item.name == "atexit" for item in node.names)
            for node in ast.walk(tree)
        )
    }
    assert atexit_importers == {"_auth/profile_migration.py"}

    migration_tree = trees["_auth/profile_migration.py"]
    exit_hooks = [
        node
        for node in ast.walk(migration_tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "atexit"
        and node.attr == "register"
    ]
    thread_capabilities = [
        node
        for node in ast.walk(migration_tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "threading"
        and node.attr in {"Thread", "current_thread"}
    ]
    assert len(exit_hooks) == 1
    assert {node.attr for node in thread_capabilities} == {"Thread", "current_thread"}
    assert _SCHEDULER_STATE_NAMES.isdisjoint(_assigned_names(trees["_auth/storage.py"]))


def test_every_allowlisted_module_exists() -> None:
    paths = (
        _BYPASS_IMPORTERS
        | _PUBLIC_ATOMIC_IMPORTERS
        | _REPLACE_FILE_IMPORTERS
        | _STORAGE_STATE_WRITE_EXEMPTIONS
        | _AUTH_ATOMIC_PRIMITIVE_IMPORTERS
    )
    assert all((SRC_ROOT / path).is_file() for path in paths)
