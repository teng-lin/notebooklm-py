"""Fail-closed composition and capability boundary for master-token bootstrap."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import notebooklm.auth as auth_facade
from notebooklm._auth import master_token, master_token_bootstrap
from tests._guardrails._ast_semantics import semantic_hash as _portable_semantic_hash

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
MODULE_PATH = AUTH_ROOT / "master_token_bootstrap.py"
ADAPTER_PATH = AUTH_ROOT / "master_token.py"

ImportRecord = tuple[str, int, str, str, str | None]

_EXPECTED_IMPORTS: list[ImportRecord] = [
    ("module", 0, "__future__", "annotations", None),
    ("module", 0, "asyncio", "", None),
    ("module", 0, "enum", "", None),
    ("module", 0, "json", "", None),
    ("module", 0, "logging", "", None),
    ("module", 0, "sys", "", None),
    ("module", 0, "collections.abc", "Awaitable", None),
    ("module", 0, "collections.abc", "Callable", None),
    ("module", 0, "pathlib", "Path", None),
    ("module", 0, "typing", "cast", None),
    ("module", 0, "httpx", "", None),
    ("module", 0, "filelock", "FileLock", None),
    ("module", 0, "filelock", "Timeout", None),
    ("module", 2, "paths", "master_token_path_for", None),
    ("module", 1, "cookie_semantics", "cookie_is_http_only", None),
    ("module", 1, "cookie_types", "Cookie", None),
    ("module", 1, "cookie_types", "CookieJar", None),
    ("module", 1, "master_token_types", "MasterToken", None),
    ("module", 1, "master_token_types", "_MasterTokenRecordError", None),
    ("module", 1, "mint_service", "MintService", None),
    ("module", 1, "mint_service", "_MintError", None),
    ("module", 1, "profile_store", "MintedSessionWriteRequest", None),
    ("module", 1, "profile_store", "ProfileStore", None),
    ("module", 1, "profile_store", "_MintedSessionOwnershipRefused", None),
]

_METHOD_HASHES = {
    "__init__": "fe7f475f5eaf98691e79e203fa2606d73a5df30d0d4dec57a28ae85bfbd1c37f",
    "_acquire_bootstrap_lock": "68fd395a9407ca4007864d5532412505b4c20fa81357def3c10ca5e58cfeee87",
    "_exchange": "39b69a1dbbfade967791d42860df3225f06238df034372671e1e61c75d328d6c",
    "_mint": "f7aca3d4e7135e944feb55abd43a5f80d4c92daf0a416126961c9a30f8cfd107",
    "_minted_session_request": "8c5750553e3d51ef136ac138f211653928588c8c45bdeb31dc3a14bb01a31c69",
    "_read_master_token": "da12ade1d52b2e819016e247f28452f3c21b3398a863b504469c1492c5ff859d",
    "_replace_minted_session": "e3e2bd9d4927a62b516c837675e8704ee8e4e081514da43ba4f7613ef3faa48d",
    "_resolve_android_id": "0ff09eced4e0862a004af91fac5cde14823bfd41ff7e67d2abcaee36e40e9f79",
    "_run_remint_to_settlement": "f79ba1a2e15f8fe10b822f79a4a9424dff69b13fd28079ab606d7175dbafbcbe",
    "assert_account_writable": "3a98efea113ed30b1c200429479e89a871e3602d478e94ea580612c2e4513d3e",
    "bootstrap_from_oauth_token": "5d2a2f8b3bb94cc398f88a232640e3ff42800379d301e97656bcbe846038b9de",
    "bootstrap_storage": "4d509a0fd1d77fc9a13eb9a26f508d9e77ade387a2e773427e953636846ea1e6",
    "remint_from_stored_token": "6955eaf1c625654f6b2a3c842fdd41f3baf4a5eb55b44393bd192bab35d8d148",
}

_ADAPTER_HASHES = {
    "_android_id_generator": "2d4f7f939bcf5d40a3084d99da970d8b48dcf9392d255810b691951e51bf524f",
    "_bootstrapper": "e0a7ee8aba56403651b9a94c5e40e0d4bcd9b28986143b7391b8fcd6cda2ec09",
    "_session_owner_reader": "ae9e4392bd8d94008fe2f0ac93a3351e064ba8f99d6556c69a3608915479f4bf",
    "_strict_loader": "29109798c66e9918f3a5e25459ea7733642657fb76d4de245f3803981db08faf",
    "_verifier": "2453c4a5fc904a1df9c42147900bc1c2918e9a28c20b8a1594737c83b95db09b",
    "assert_account_writable": "9209e5ae1d2d617067d888a36109d034226a5386710e2b0397c750023e89344e",
    "bootstrap_from_oauth_token": "6d40ca20b01a84b236cbc25ee83a8a748cba4bfdfd70fbed5c4058322b17817f",
    "bootstrap_missing_storage_from_master_token": "9abd23fb9ff23b32fbc24430bef6aef833f111b1a766b39e1dc7d8ba0262babf",
    "bootstrap_storage_from_master_token": "2807649b65e3f67e575c8221d0384377f58a8ce5ca1b1a9edc08eefbffa8568a",
    "remint_from_stored_token": "143add0fd1b4b2eccba5a7ff6391a47720d00d001f9610b5d219c35bd8e8cc8a",
}

_FORBIDDEN_IMPORT_FRAGMENTS = {
    "notebooklm.auth",
    "notebooklm.client",
    "notebooklm._runtime",
    "notebooklm._auth.cookies",
    "notebooklm._auth.keepalive",
    "notebooklm._auth.master_token",
    "notebooklm._auth.master_token_file",
    "notebooklm._auth.profile_migration",
    "notebooklm._auth.recovery",
    "notebooklm._auth.storage",
    "notebooklm.cli",
}
_FORBIDDEN_NAMES = {
    "MasterTokenFile",
    "NotebookLMClient",
    "Protocol",
    "StorageLockManager",
    "__import__",
    "eval",
    "exec",
    "getattr",
    "globals",
    "import_module",
    "locals",
    "open",
    "setattr",
}
_FIELDS = {"_mint_service", "_store", "_bootstrap_lock", "_verifier"}


def _tree(path: Path = MODULE_PATH) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class(tree: ast.Module) -> ast.ClassDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MasterTokenBootstrapper"
    ]
    assert len(matches) == 1
    return matches[0]


def _methods(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in _class(tree).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _semantic_hash(node: ast.AST) -> str:
    return _portable_semantic_hash(node)


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope = "module"
        self.records: list[ImportRecord] = []

    def _deferred(self, node: ast.AST, scope: str) -> None:
        prior = self.scope
        self.scope = scope
        self.generic_visit(node)
        self.scope = prior

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._deferred(node, "function")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._deferred(node, "class")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._deferred(node, "lambda")

    def visit_Import(self, node: ast.Import) -> None:
        self.records.extend((self.scope, 0, item.name, "", item.asname) for item in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.records.extend(
            (self.scope, node.level, node.module or "", item.name, item.asname)
            for item in node.names
        )


def _imports(tree: ast.AST) -> list[ImportRecord]:
    collector = _ImportCollector()
    collector.visit(tree)
    return collector.records


def _boundary_violations(source: str) -> set[str]:
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if any(part in item.name for part in _FORBIDDEN_IMPORT_FRAGMENTS):
                    violations.add(f"import:{item.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            rendered = "." * node.level + module
            if any(
                part.rsplit(".", 1)[-1] in module.split(".") for part in _FORBIDDEN_IMPORT_FRAGMENTS
            ):
                violations.add(f"import:{rendered}")
            if any(item.name in _FORBIDDEN_NAMES for item in node.names):
                violations.add(f"import-name:{rendered}")
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_NAMES:
            violations.add(f"name:{node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_NAMES:
            violations.add(f"attribute:{node.attr}")
        elif isinstance(node, ast.Global | ast.Nonlocal) and set(node.names) & _FIELDS:
            violations.add("scope-rebinding")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            owner = parents.get(node)
            while owner is not None and not isinstance(
                owner, ast.FunctionDef | ast.AsyncFunctionDef
            ):
                owner = parents.get(owner)
            if not (
                isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef)
                and owner.name == "__init__"
                and node.attr in _FIELDS
                and isinstance(node.ctx, ast.Store)
            ):
                violations.add(f"self-state:{node.attr}")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr in {"_store", "_mint_service"}
            and node.attr != "path"
        ):
            parent = parents.get(node)
            direct = isinstance(parent, ast.Call) and parent.func is node
            offload = (
                isinstance(parent, ast.Call)
                and bool(parent.args)
                and parent.args[0] is node
                and ast.unparse(parent.func) == "asyncio.to_thread"
                and node.value.attr == "_store"
                and node.attr == "write_master_token"
            )
            if not direct and not offload:
                violations.add(f"bound-capability:{node.value.attr}.{node.attr}")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == "_verifier"
        ):
            parent = parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                violations.add("callback-capability:_verifier")
    return violations


def _composition_reference_violations(source: str) -> set[str]:
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {
        child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
    }
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for item in node.names:
                if item.name == "MasterTokenBootstrapper" and item.asname is not None:
                    violations.add("aliased-import")
        elif isinstance(node, ast.Attribute) and node.attr == "MasterTokenBootstrapper":
            violations.add("qualified-reference")
        elif isinstance(node, ast.Constant) and node.value == "MasterTokenBootstrapper":
            violations.add("dynamic-name")
        elif isinstance(node, ast.Name) and node.id == "MasterTokenBootstrapper":
            parent = parents.get(node)
            direct_constructor = (
                isinstance(parent, ast.Call)
                and parent.func is node
                and isinstance(node.ctx, ast.Load)
            )
            return_annotation = (
                isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef)
                and parent.returns is node
                and parent.name == "_bootstrapper"
            )
            if not direct_constructor and not return_annotation:
                violations.add(f"capability-reference:{type(node.ctx).__name__}")
    return violations


def test_imports_internal_values_api_and_four_field_state_are_exact() -> None:
    tree = _tree()
    assert _imports(tree) == _EXPECTED_IMPORTS
    top_names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert top_names == {"_BootstrapError", "BootstrapOutcome", "MasterTokenBootstrapper"}
    assert not any(
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
        for node in tree.body
    )
    assert tuple(
        (member.name, member.value) for member in master_token_bootstrap.BootstrapOutcome
    ) == (
        ("MINTED", "minted"),
        ("PRESENT_AFTER_WAIT", "present_after_wait"),
        ("PRESENT_ON_ENTRY", "present_on_entry"),
        ("NO_TOKEN", "no_token"),
    )
    cls = master_token_bootstrap.MasterTokenBootstrapper
    assert str(inspect.signature(cls)) == (
        "(*, mint_service: 'MintService', store: 'ProfileStore', bootstrap_lock: 'FileLock', "
        "verifier: 'Callable[[Path], Awaitable[int]]') -> 'None'"
    )
    assert str(inspect.signature(cls.assert_account_writable)) == (
        "(self, *, email: 'str', force: 'bool' = False, "
        "session_owner_reader: 'Callable[[Path], str | None]') -> 'None'"
    )
    assert str(inspect.signature(cls.bootstrap_from_oauth_token)) == (
        "(self, *, email: 'str', oauth_token: 'str', android_id: 'str | None' = None, "
        "verify: 'bool' = True, force: 'bool' = False, session_owner_reader: "
        "'Callable[[Path], str | None]', android_id_generator: 'Callable[[], str]') -> 'int'"
    )
    assert str(inspect.signature(cls.remint_from_stored_token)) == (
        "(self, *, strict_loader: 'Callable[[Path], httpx.Cookies]') -> 'httpx.Cookies'"
    )
    assert str(inspect.signature(cls.bootstrap_storage)) == (
        "(self, *, strict_loader: 'Callable[[Path], httpx.Cookies]') -> 'BootstrapOutcome'"
    )
    value = cls(
        mint_service=object(),  # type: ignore[arg-type]
        store=object(),  # type: ignore[arg-type]
        bootstrap_lock=object(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
    )
    assert set(value.__dict__) == _FIELDS
    assert _class(tree).bases == []
    assert not any(
        isinstance(node, ast.ClassDef)
        and any(ast.unparse(base).endswith("Protocol") for base in node.bases)
        for node in tree.body
    )


def test_semantic_method_and_adapter_shapes_are_exact() -> None:
    methods = _methods(_tree())
    assert set(methods) == set(_METHOD_HASHES)
    assert {name: _semantic_hash(node) for name, node in methods.items()} == _METHOD_HASHES
    functions = _functions(_tree(ADAPTER_PATH))
    assert {name: _semantic_hash(functions[name]) for name in _ADAPTER_HASHES} == _ADAPTER_HASHES


def test_composition_owner_provenance_and_exports_are_exact() -> None:
    owners: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        tree = _tree(path)

        class Collector(ast.NodeVisitor):
            def __init__(self, label: str) -> None:
                self.label = label
                self.stack: list[str] = []

            def _scope(self, node: ast.AST, name: str) -> None:
                self.stack.append(name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._scope(node, node.name)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._scope(node, node.name)

            def visit_Call(self, node: ast.Call) -> None:
                if isinstance(node.func, ast.Name) and node.func.id == "MasterTokenBootstrapper":
                    owners.add(f"{self.label}:{'.'.join(self.stack)}")
                self.generic_visit(node)

        Collector(path.relative_to(SRC_ROOT).as_posix()).visit(tree)
    assert owners == {"_auth/master_token.py:_bootstrapper"}
    import_records: list[tuple[str, int, str, str, str | None]] = []
    reference_violations: dict[str, set[str]] = {}
    for path in SRC_ROOT.rglob("*.py"):
        relative = path.relative_to(SRC_ROOT).as_posix()
        tree = _tree(path)
        import_records.extend(
            (relative, node.level, node.module or "", item.name, item.asname)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for item in node.names
            if item.name == "MasterTokenBootstrapper"
        )
        violations = _composition_reference_violations(path.read_text(encoding="utf-8"))
        if violations:
            reference_violations[relative] = violations
    assert import_records == [
        ("_auth/master_token.py", 1, "master_token_bootstrap", "MasterTokenBootstrapper", None)
    ]
    assert reference_violations == {}
    adapter = _functions(_tree(ADAPTER_PATH))["_bootstrapper"]
    returns = [node for node in ast.walk(adapter) if isinstance(node, ast.Return)]
    assert len(returns) == 1
    assert ast.unparse(returns[0].value) == (
        "MasterTokenBootstrapper(mint_service=MintService(), store=ProfileStore(storage_path), "
        "bootstrap_lock=FileLock(str(_bootstrap_lock_path(storage_path))), verifier=_verifier)"
    )
    for path in (SRC_ROOT / "auth.py", SRC_ROOT / "__init__.py", AUTH_ROOT / "__init__.py"):
        tree = _tree(path)
        imported = {
            item.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for item in node.names
        }
        exported = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert imported.isdisjoint({"MasterTokenBootstrapper", "BootstrapOutcome"})
        assert exported.isdisjoint({"MasterTokenBootstrapper", "BootstrapOutcome"})
    assert master_token.BootstrapOutcome is master_token_bootstrap.BootstrapOutcome
    assert not hasattr(auth_facade, "MasterTokenBootstrapper")
    assert not hasattr(auth_facade, "BootstrapOutcome")


def test_order_cancellation_lock_and_ephemeral_callable_contract_is_exact() -> None:
    methods = _methods(_tree())
    oauth = ast.unparse(methods["bootstrap_from_oauth_token"])
    ordered = (
        "asyncio.to_thread(self._resolve_android_id",
        "asyncio.to_thread(self.assert_account_writable",
        "asyncio.to_thread(self._exchange",
        "await self._mint(token)",
        "asyncio.to_thread(self._replace_minted_session, request)",
        "asyncio.to_thread(self._store.write_master_token, token)",
        "await self._verifier(self._store.path)",
    )
    positions = [oauth.index(fragment) for fragment in ordered]
    assert positions == sorted(positions)
    assert "asyncio.shield" not in oauth
    assert oauth.count("asyncio.to_thread") == 5
    assert "del oauth_token, session_owner_reader, android_id_generator" in oauth

    remint = ast.unparse(methods["remint_from_stored_token"])
    assert remint.count("asyncio.to_thread") == 3
    assert (
        remint.index("asyncio.to_thread(self._read_master_token)")
        < remint.index("await self._mint(token)")
        < remint.index("asyncio.to_thread(self._replace_minted_session, request)")
        < remint.index("asyncio.to_thread(strict_loader, self._store.path)")
    )
    assert "del strict_loader" in remint

    acquire = ast.unparse(methods["_acquire_bootstrap_lock"])
    assert "self._bootstrap_lock.acquire(blocking=False)" in acquire
    assert "deadline = loop.time() + 90.0" in acquire
    assert "remaining = deadline - loop.time()" in acquire
    assert "if remaining <= 0:" in acquire
    assert "raise _BootstrapError(" in acquire
    assert "from exc" in acquire
    assert "await asyncio.sleep(min(0.05, remaining))" in acquire
    settlement = ast.unparse(methods["_run_remint_to_settlement"])
    assert settlement.count("asyncio.shield(task)") == 2
    assert "while not task.done()" in settlement
    assert "task.exception()" in settlement
    storage = ast.unparse(methods["bootstrap_storage"])
    assert storage.index("storage_path.exists()") < storage.index("token_path.exists()")
    assert storage.count("storage_path.exists()") == 2
    assert storage.count("token_path.exists()") == 2
    assert storage.index("self._bootstrap_lock.release()") < storage.index("logger.debug")
    assert (
        "logger.debug('bootstrap_storage_from_master_token(%s) -> %s', storage_path, outcome)"
        in storage
    )


def test_cookie_projection_store_calls_and_error_boundary_are_exact() -> None:
    methods = _methods(_tree())
    projection = methods["_minted_session_request"]
    cookie_calls = [
        node
        for node in ast.walk(projection)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "Cookie"
    ]
    assert len(cookie_calls) == 1
    assert [(item.arg, ast.unparse(item.value)) for item in cookie_calls[0].keywords] == [
        ("name", "cookie.name"),
        ("value", "cast(str, cookie.value)"),
        ("domain", "cookie.domain"),
        ("path", "cookie.path or '/'"),
        ("expires", "cookie.expires"),
        ("http_only", "cookie_is_http_only(cookie)"),
        ("secure", "cookie.secure"),
        ("same_site", "'None'"),
    ]
    request_calls = [
        node
        for node in ast.walk(projection)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "MintedSessionWriteRequest"
    ]
    assert len(request_calls) == 1
    assert [ast.unparse(arg) for arg in request_calls[0].args] == [
        "cookies",
        "email",
        "force",
        "refuse_unknown_owner",
    ]
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert _boundary_violations(source) == set()
    assert source.count("self._store = store") == 1
    assert source.count("self._mint_service = mint_service") == 1
    assert "MasterTokenFile" not in source
    assert "storage_state_from_jar" not in source
    assert "persist_minted_jar" not in source
    assert "CookieJar.from_httpx" not in source
    assert "logger.info" not in source
    assert "logger.warning" not in source
    assert "logger.error" not in source


@pytest.mark.parametrize(
    "source",
    [
        "from .master_token_file import MasterTokenFile as TokenFile\n",
        "def later():\n    from .storage import write_master_token\n",
        "import importlib\nmodule = importlib.import_module('notebooklm._auth.' + 'storage')\n",
        "callback = getattr(store, 'write_' + 'master_token')\n",
        "class X:\n    def f(self):\n        callback = self._store.write_master_token\n",
        "class X:\n    def f(self, consume):\n        consume(self._verifier)\n",
        "def deco(value): return value\n@deco(MasterTokenFile)\ndef later(): pass\n",
        "def later(value=MasterTokenFile): pass\n",
        "class Later(MasterTokenFile): pass\n",
        "items = [getattr(value, 'write_master_token') for value in values]\n",
        "run = lambda value: getattr(value, 'read_master_token')\n",
        "class X:\n    def f(self, values):\n        for self._store in values:\n            pass\n",
        "class X:\n    def f(self, value):\n        self._token = value\n",
        "class X:\n    def f(self):\n        global _store\n        _store = object()\n",
    ],
)
def test_boundary_detector_bites_alias_dynamic_deferred_and_escape_shapes(source: str) -> None:
    assert _boundary_violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "from .master_token_bootstrap import MasterTokenBootstrapper as Bootstrap\n",
        "Alias = MasterTokenBootstrapper\n",
        "factory = provider.MasterTokenBootstrapper\n",
        "factory = getattr(provider, 'MasterTokenBootstrapper')\n",
        "@decorate(MasterTokenBootstrapper)\ndef later(): pass\n",
        "def later(value=MasterTokenBootstrapper): pass\n",
        "class Later(MasterTokenBootstrapper): pass\n",
        "values = [MasterTokenBootstrapper for _ in items]\n",
        "later = lambda: MasterTokenBootstrapper\n",
        "for MasterTokenBootstrapper in values:\n    pass\n",
        "def later():\n    global MasterTokenBootstrapper\n    MasterTokenBootstrapper = object\n",
    ],
)
def test_composition_reference_guard_bites_every_deferred_and_rebinding_shape(
    source: str,
) -> None:
    assert _composition_reference_violations(source)


def test_only_logger_is_module_state_and_instance_state_never_grows() -> None:
    tree = _tree()
    assignments = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert assignments == {"logger"}
    methods = _methods(tree)
    for name, method in methods.items():
        assigned = {
            node.attr
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        assert assigned == (_FIELDS if name == "__init__" else set())
