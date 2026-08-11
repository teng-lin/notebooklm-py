"""Fail-closed ownership and capability guard for ``MasterTokenFile``."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap
from pathlib import Path

import pytest

from notebooklm._auth.master_token_file import MasterTokenFile, _MasterTokenRead
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.storage_lock import StorageLockManager

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
MODULE_PATH = AUTH_ROOT / "master_token_file.py"
BOOTSTRAP_PATH = AUTH_ROOT / "master_token_bootstrap.py"
ImportRecord = tuple[str, int, str, str, str | None]

_MODULE_NAME = "notebooklm._auth.master_token_file"
_EXPECTED_IMPORTS: list[ImportRecord] = [
    ("module", 0, "__future__", "annotations", None),
    ("module", 0, "contextlib", "", None),
    ("module", 0, "json", "", None),
    ("module", 0, "os", "", None),
    ("module", 0, "sys", "", None),
    ("module", 0, "dataclasses", "dataclass", None),
    ("module", 0, "dataclasses", "field", None),
    ("module", 0, "pathlib", "Path", None),
    ("module", 0, "typing", "Any", None),
    ("module", 2, "exceptions", "LockUnavailableError", None),
    ("module", 1, "credential_io", "_commit_master_token_json", None),
    ("module", 1, "master_token_types", "MasterToken", None),
    (
        "module",
        1,
        "master_token_types",
        "_master_token_from_legacy_record",
        None,
    ),
    (
        "module",
        1,
        "master_token_types",
        "_master_token_to_legacy_record",
        None,
    ),
    ("module", 1, "paths", "_storage_state_lock_path", None),
    ("module", 1, "storage_lock", "_LOCK_ACQUIRE_DEADLINE_SECONDS", None),
    ("module", 1, "storage_lock", "LockRequest", None),
    ("module", 1, "storage_lock", "LockState", None),
    ("module", 1, "storage_lock", "StorageLockManager", None),
]

_EXPECTED_CONSTRUCTORS = {
    "_auth/master_token.py:read_master_token",
    "_auth/profile_store.py:ProfileStore.read_master_token",
    "_auth/profile_store.py:ProfileStore.write_master_token",
    "_auth/storage.py:write_master_token",
}
_EXPECTED_RAW_BRIDGE_CALLERS = {
    "_auth/master_token_file.py:MasterTokenFile.read",
}
_EXPECTED_PRESENT_RAW_CALLERS = {
    "_auth/master_token.py:read_master_token",
    "_auth/master_token_file.py:MasterTokenFile._read_with_raw",
}
_EXPECTED_WRITE_CALLERS = {
    "_auth/profile_store.py:ProfileStore.write_master_token",
    "_auth/storage.py:write_master_token",
}


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


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _method(tree: ast.Module, name: str) -> ast.FunctionDef:
    cls = _class(tree, "MasterTokenFile")
    matches = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _call_name(node: ast.Call) -> str:
    return ast.unparse(node.func)


def _calls_in_source_order(node: ast.AST) -> list[ast.Call]:
    return sorted(
        (item for item in ast.walk(node) if isinstance(item, ast.Call)),
        key=lambda item: (item.lineno, item.col_offset),
    )


def _module_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    violations: list[str] = []
    imports = _collect_imports(tree)
    if imports != _EXPECTED_IMPORTS:
        violations.append(f"imports:{imports!r}")

    top_level = [
        node
        for node in tree.body
        if not isinstance(node, ast.Import | ast.ImportFrom)
        and not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    shape = [(type(node).__name__, getattr(node, "name", "<assignment>")) for node in top_level]
    if shape != [
        ("ClassDef", "_MasterTokenRead"),
        ("FunctionDef", "_ensure_secure_parent_dir"),
        ("ClassDef", "MasterTokenFile"),
    ]:
        violations.append(f"module-shape:{shape!r}")

    secure_parent = _function(tree, "_ensure_secure_parent_dir")
    parent_calls = _calls_in_source_order(secure_parent)
    if [_call_name(call) for call in parent_calls] != [
        "parent.mkdir",
        "contextlib.suppress",
        "os.chmod",
    ]:
        violations.append(
            f"secure-parent-call-chain:{[_call_name(call) for call in parent_calls]!r}"
        )
    mkdir_calls = [call for call in parent_calls if _call_name(call) == "parent.mkdir"]
    if len(mkdir_calls) != 1 or [
        (item.arg, ast.unparse(item.value)) for item in mkdir_calls[0].keywords
    ] != [("parents", "True"), ("exist_ok", "True")]:
        violations.append("secure-parent-mkdir")
    chmod_calls = [call for call in parent_calls if _call_name(call) == "os.chmod"]
    if len(chmod_calls) != 1 or [ast.unparse(arg) for arg in chmod_calls[0].args] != [
        "parent",
        "448",
    ]:
        violations.append("secure-parent-mode")
    if "if sys.platform != 'win32':" not in ast.unparse(secure_parent):
        violations.append("secure-parent-platform")

    file_class = _class(tree, "MasterTokenFile")
    methods = [
        node.name
        for node in file_class.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    if methods != [
        "__init__",
        "_read_present_with_raw",
        "_read_with_raw",
        "read",
        "write",
    ]:
        violations.append(f"class-members:{methods!r}")

    init = _method(tree, "__init__")
    stored = {
        node.attr
        for node in ast.walk(init)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, ast.Store)
    }
    if stored != {"_path", "_locks"}:
        violations.append(f"instance-state:{sorted(stored)!r}")
    init_calls = [_call_name(call) for call in _calls_in_source_order(init)]
    if init_calls != ["StorageLockManager.process_default"]:
        violations.append(f"init-calls:{init_calls!r}")

    read_core = _method(tree, "_read_present_with_raw")
    if any(
        isinstance(node, ast.Try) or type(node).__name__ == "TryStar"
        for node in ast.walk(read_core)
    ):
        violations.append("read-core-wraps-errors")
    read_calls = _calls_in_source_order(read_core)
    if [_call_name(call) for call in read_calls] != [
        "json.loads",
        "self._path.read_text",
        "_master_token_from_legacy_record",
        "_MasterTokenRead",
    ]:
        violations.append(f"read-call-chain:{[_call_name(call) for call in read_calls]!r}")
    read_text_calls = [call for call in read_calls if _call_name(call) == "self._path.read_text"]
    if len(read_text_calls) != 1 or [
        (item.arg, ast.literal_eval(item.value)) for item in read_text_calls[0].keywords
    ] != [("encoding", "utf-8")]:
        violations.append("read-text-contract")
    if "return _MasterTokenRead(token=token, raw=raw)" not in ast.unparse(read_core):
        violations.append("read-pair-projection")

    read_probe = _method(tree, "_read_with_raw")
    if any(
        isinstance(node, ast.Try) or type(node).__name__ == "TryStar"
        for node in ast.walk(read_probe)
    ):
        violations.append("read-probe-wraps-errors")
    probe_calls = _calls_in_source_order(read_probe)
    if [_call_name(call) for call in probe_calls] != [
        "self._path.exists",
        "self._read_present_with_raw",
    ]:
        violations.append(f"read-probe-call-chain:{[_call_name(call) for call in probe_calls]!r}")
    if ast.unparse(read_probe).count("self._path.exists()") != 1:
        violations.append("read-probe-count")

    read = _method(tree, "read")
    if [_call_name(call) for call in _calls_in_source_order(read)] != ["self._read_with_raw"]:
        violations.append("typed-read-call-count")
    returns = [node for node in ast.walk(read) if isinstance(node, ast.Return)]
    if len(returns) != 1 or ast.unparse(returns[0].value) != (
        "None if result is None else result.token"
    ):
        violations.append("typed-read-projection")

    write = _method(tree, "write")
    write_calls = _calls_in_source_order(write)
    call_names = [_call_name(call) for call in write_calls]
    if call_names != [
        "_master_token_to_legacy_record",
        "_ensure_secure_parent_dir",
        "_storage_state_lock_path",
        "LockRequest",
        "self._locks.acquire",
        "LockUnavailableError",
        "_commit_master_token_json",
    ]:
        violations.append(f"write-call-chain:{call_names!r}")
    request_calls = [call for call in write_calls if _call_name(call) == "LockRequest"]
    if len(request_calls) != 1 or [
        (item.arg, ast.unparse(item.value)) for item in request_calls[0].keywords
    ] != [
        ("path", "lock_path"),
        ("blocking", "False"),
        ("operation", "'write_master_token'"),
        ("deadline_seconds", "_LOCK_ACQUIRE_DEADLINE_SECONDS"),
    ]:
        violations.append("lock-request")
    commit_calls = [call for call in write_calls if _call_name(call) == "_commit_master_token_json"]
    if len(commit_calls) != 1 or [ast.unparse(arg) for arg in commit_calls[0].args] != [
        "self._path",
        "payload",
    ]:
        violations.append("commit-call")
    with_nodes = [node for node in ast.walk(write) if isinstance(node, ast.With)]
    if (
        len(with_nodes) != 1
        or commit_calls
        and not (with_nodes[0].lineno < commit_calls[0].lineno <= (with_nodes[0].end_lineno or 0))
    ):
        violations.append("commit-outside-lock")
    comparisons = [node for node in ast.walk(write) if isinstance(node, ast.Compare)]
    if len(comparisons) != 1 or ast.unparse(comparisons[0]) != "state is not LockState.HELD":
        violations.append("lock-state-projection")
    raises = [node for node in ast.walk(write) if isinstance(node, ast.Raise)]
    if len(raises) != 1 or ast.unparse(raises[0].exc) != (
        "LockUnavailableError(f'write_master_token: storage lock unavailable at {lock_path}')"
    ):
        violations.append("lock-error-projection")
    secret_escapes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        and any(name in ast.unparse(node) for name in ("raw", "payload", "token.secret", "token"))
        and "lock_path" not in ast.unparse(node)
    ]
    if secret_escapes:
        violations.append("secret-interpolation")
    self_stores = {
        (method.name, node.attr)
        for method in file_class.body
        if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(method)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and isinstance(node.ctx, ast.Store)
    }
    if self_stores != {("__init__", "_path"), ("__init__", "_locks")}:
        violations.append(f"self-stores:{sorted(self_stores)!r}")
    if any(isinstance(node, ast.Yield | ast.YieldFrom) for node in ast.walk(file_class)):
        violations.append("generator-escape")
    for method in file_class.body:
        if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(method):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            value = ast.unparse(node.value)
            allowed = {
                ("_read_present_with_raw", "_MasterTokenRead(token=token, raw=raw)"),
                ("_read_with_raw", "None"),
                ("_read_with_raw", "self._read_present_with_raw()"),
                ("read", "None if result is None else result.token"),
            }
            if (method.name, value) not in allowed:
                violations.append(f"return-escape:{method.name}:{value}")
            continue
        sensitive = {
            "_read_present_with_raw": {"raw", "token"},
            "read": {"result"},
            "write": {"token", "payload"},
        }.get(method.name, set())
        allowed_assignments = {
            (
                "_read_present_with_raw",
                "token = _master_token_from_legacy_record(raw)",
            ),
            ("write", "payload = _master_token_to_legacy_record(token)"),
        }
        for node in ast.walk(method):
            if not isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
                continue
            value = node.value
            if (
                any(
                    isinstance(item, ast.Name)
                    and isinstance(item.ctx, ast.Load)
                    and item.id in sensitive
                    for item in ast.walk(value)
                )
                and (method.name, ast.unparse(node)) not in allowed_assignments
            ):
                violations.append(f"mutable-secret-escape:{method.name}:{ast.unparse(node)}")
    forbidden_calls = {
        name
        for name in call_names
        if name in {"open", "self._path.read_text", "self._path.resolve", "getattr", "setattr"}
    }
    if forbidden_calls:
        violations.append(f"forbidden-write-capability:{sorted(forbidden_calls)!r}")
    return violations


def test_live_module_has_exact_lower_imports_state_and_algorithms() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert _module_violations(source) == []


def _legacy_reader_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    reader = _function(tree, "read_master_token")
    violations: list[str] = []
    statements = [node for node in reader.body if not isinstance(node, ast.Expr)]
    if [type(node).__name__ for node in statements] != [
        "If",
        "Assign",
        "AnnAssign",
        "Try",
        "If",
        "Return",
    ]:
        violations.append("legacy-reader-shape")
        return violations
    probe, construct, _marker, protected, malformed, projection = statements
    if not (
        isinstance(probe, ast.If)
        and ast.unparse(probe.test) == "not path.exists()"
        and len(probe.body) == 1
        and isinstance(probe.body[0], ast.Return)
        and ast.unparse(probe.body[0].value) == "None"
    ):
        violations.append("legacy-probe")
    if not (
        isinstance(construct, ast.Assign)
        and ast.unparse(construct) == "token_file = MasterTokenFile(path)"
    ):
        violations.append("legacy-construction")
    if not isinstance(protected, ast.Try):
        violations.append("legacy-protected-shape")
        return violations
    protected_calls = [_call_name(call) for call in _calls_in_source_order(protected.body[0])]
    if protected_calls != ["token_file._read_present_with_raw"]:
        violations.append(f"legacy-present-call:{protected_calls!r}")
    handler_types = [ast.unparse(handler.type) for handler in protected.handlers]
    if handler_types != ["(OSError, json.JSONDecodeError)", "_MasterTokenRecordError"]:
        violations.append(f"legacy-handlers:{handler_types!r}")
    if not (
        isinstance(malformed, ast.If)
        and ast.unparse(malformed.test) == "malformed_message is not None"
    ):
        violations.append("legacy-malformed-outside-handler")
    if not (
        isinstance(projection, ast.Return) and ast.unparse(projection.value) == "dict(result.raw)"
    ):
        violations.append("legacy-raw-projection")
    if (
        sum(
            1
            for node in ast.walk(reader)
            if isinstance(node, ast.Call) and _call_name(node) == "path.exists"
        )
        != 1
    ):
        violations.append("legacy-probe-count")
    if "_read_with_raw" in ast.unparse(reader):
        violations.append("legacy-optional-helper")
    return violations


def test_legacy_reader_owns_one_outside_probe_and_narrow_present_projection() -> None:
    source = (AUTH_ROOT / "master_token.py").read_text(encoding="utf-8")
    assert _legacy_reader_violations(source) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "    if not path.exists():\n        return None\n",
            "    try:\n        if not path.exists():\n            return None\n"
            "    except OSError:\n        return None\n",
        ),
        (
            "    token_file = MasterTokenFile(path)\n"
            "    malformed_message: str | None = None\n"
            "    try:\n",
            "    malformed_message: str | None = None\n"
            "    try:\n"
            "        token_file = MasterTokenFile(path)\n",
        ),
        ("token_file._read_present_with_raw()", "token_file._read_with_raw()"),
        (
            "except (OSError, json.JSONDecodeError) as exc:",
            "except (OSError, json.JSONDecodeError, ValueError) as exc:",
        ),
    ],
)
def test_legacy_reader_guard_bites_on_probe_construction_helper_and_handler_drift(
    old: str, new: str
) -> None:
    source = (AUTH_ROOT / "master_token.py").read_text(encoding="utf-8")
    assert old in source
    assert _legacy_reader_violations(source.replace(old, new, 1))


def test_runtime_shapes_paths_lock_identity_and_redacted_pair_are_exact(tmp_path: Path) -> None:
    assert str(inspect.signature(MasterTokenFile)) == (
        "(path: 'Path', *, locks: 'StorageLockManager | None' = None) -> 'None'"
    )
    assert str(inspect.signature(MasterTokenFile._read_with_raw)) == (
        "(self) -> '_MasterTokenRead | None'"
    )
    assert str(inspect.signature(MasterTokenFile._read_present_with_raw)) == (
        "(self) -> '_MasterTokenRead'"
    )
    assert str(inspect.signature(MasterTokenFile.read)) == "(self) -> 'MasterToken | None'"
    assert str(inspect.signature(MasterTokenFile.write)) == (
        "(self, token: 'MasterToken') -> 'None'"
    )
    marker = object()
    raw_path = tmp_path / "relative" / ".." / "master_token.json"
    instance = MasterTokenFile(raw_path, locks=marker)  # type: ignore[arg-type]
    assert instance.__dict__ == {"_path": raw_path, "_locks": marker}
    default = MasterTokenFile(raw_path)
    assert default._locks is StorageLockManager.process_default()

    assert [field.name for field in dataclasses.fields(_MasterTokenRead)] == ["token", "raw"]
    assert _MasterTokenRead.__dataclass_params__.frozen is True
    assert _MasterTokenRead.__dataclass_params__.repr is False
    assert _MasterTokenRead.__slots__ == ("token", "raw")
    secret = "DO-NOT-RENDER"
    pair = _MasterTokenRead(
        token=MasterToken(email="e@example.com", android_id="aid", secret=secret),
        raw={"master_token": secret},
    )
    assert secret not in repr(pair)
    assert not hasattr(pair, "__dict__")


def _resolved_import(path: Path, node: ast.ImportFrom, *, source_root: Path) -> str | None:
    if node.level == 0:
        return node.module or ""
    package = ("notebooklm", *path.relative_to(source_root).parent.parts)
    parents = node.level - 1
    if parents >= len(package):
        return None
    prefix = package[: len(package) - parents]
    suffix = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*prefix, *suffix))


_PROVIDER = f"{_MODULE_NAME}.MasterTokenFile"
_SENSITIVE_METHODS = {"_read_present_with_raw", "_read_with_raw", "write"}
_BUILTIN_GETATTR = "builtins.getattr"
_GETATTR_ALIAS = "<getattr-alias>"


def _target_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    if isinstance(node, ast.Tuple | ast.List):
        return {name for item in node.elts for name in _target_names(item)}
    if isinstance(node, ast.MatchAs):
        names = _target_names(node.pattern)
        if node.name is not None:
            names.add(node.name)
        return names
    if isinstance(node, ast.MatchStar):
        return {node.name} if node.name is not None else set()
    if isinstance(node, ast.MatchMapping):
        names = {name for pattern in node.patterns for name in _target_names(pattern)}
        if node.rest is not None:
            names.add(node.rest)
        return names
    if isinstance(node, ast.MatchSequence):
        return {name for pattern in node.patterns for name in _target_names(pattern)}
    if isinstance(node, ast.MatchClass):
        return {
            name
            for pattern in (*node.patterns, *node.kwd_patterns)
            for name in _target_names(pattern)
        }
    if isinstance(node, ast.MatchOr):
        return {name for pattern in node.patterns for name in _target_names(pattern)}
    return set()


@dataclasses.dataclass(frozen=True)
class _BindingEvent:
    provider: str | None = None
    receiver: ast.AST | None = None
    certain: bool = True


def _argument_names(arguments: ast.arguments) -> set[str]:
    positional = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    names = {item.arg for item in positional}
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


class _ScopeBindingCollector(ast.NodeVisitor):
    """Collect one lexical scope without trusting branch-dependent bindings."""

    def __init__(self, path: Path, *, source_root: Path) -> None:
        self.path = path
        self.source_root = source_root
        self.uncertain = 0
        self.events: dict[str, list[_BindingEvent]] = {}
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def _record(
        self,
        names: set[str],
        *,
        provider: str | None = None,
        receiver: ast.AST | None = None,
    ) -> None:
        for name in names:
            self.events.setdefault(name, []).append(
                _BindingEvent(provider, receiver, self.uncertain == 0)
            )

    def _visit_uncertain(self, nodes: list[ast.stmt]) -> None:
        self.uncertain += 1
        for node in nodes:
            self.visit(node)
        self.uncertain -= 1

    def visit_Import(self, node: ast.Import) -> None:
        for item in node.names:
            name = item.asname or item.name.split(".", 1)[0]
            provider = item.name if item.asname else name
            self._record({name}, provider=provider)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _resolved_import(self.path, node, source_root=self.source_root)
        for item in node.names:
            if item.name == "*":
                self._record({"MasterTokenFile"})
            else:
                provider = f"{module}.{item.name}" if module is not None else None
                self._record({item.asname or item.name}, provider=provider)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record({node.name})

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        provider = None
        if (
            self.path == self.source_root / "_auth" / "master_token_file.py"
            and node.name == "MasterTokenFile"
        ):
            provider = _PROVIDER
        self._record({node.name}, provider=provider)

    def visit_Assign(self, node: ast.Assign) -> None:
        simple = len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
        provider = (
            _GETATTR_ALIAS
            if simple and isinstance(node.value, ast.Name) and node.value.id == "getattr"
            else None
        )
        for target in node.targets:
            self._record(
                _target_names(target),
                provider=provider,
                receiver=node.value if simple else None,
            )
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        provider = (
            _GETATTR_ALIAS
            if isinstance(node.value, ast.Name) and node.value.id == "getattr"
            else None
        )
        self._record(_target_names(node.target), provider=provider)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record(_target_names(node.target))
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        provider = (
            _GETATTR_ALIAS
            if isinstance(node.value, ast.Name) and node.value.id == "getattr"
            else None
        )
        self._record(_target_names(node.target), provider=provider)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._record(_target_names(target))

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._visit_uncertain(node.body)
        self._visit_uncertain(node.orelse)

    visit_While = visit_If

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self.uncertain += 1
        self._record(_target_names(node.target))
        for statement in (*node.body, *node.orelse):
            self.visit(statement)
        self.uncertain -= 1

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            self.uncertain += 1
            self._record(_target_names(item.optional_vars))
            self.uncertain -= 1
        self._visit_uncertain(node.body)

    visit_AsyncWith = visit_With

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_uncertain(node.body)
        for handler in node.handlers:
            self.visit(handler)
        self._visit_uncertain(node.orelse)
        self._visit_uncertain(node.finalbody)

    visit_TryStar = visit_Try

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.uncertain += 1
        if node.name is not None:
            self._record({node.name})
        for statement in node.body:
            self.visit(statement)
        self.uncertain -= 1

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            self.uncertain += 1
            self._record(_target_names(case.pattern))
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)
            self.uncertain -= 1

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)


def _scope_bindings(
    path: Path,
    body: list[ast.stmt],
    *,
    source_root: Path,
    arguments: ast.arguments | None = None,
) -> tuple[
    dict[str, str | None],
    dict[str, ast.AST],
    set[str],
    set[str],
    set[str],
    set[str],
]:
    collector = _ScopeBindingCollector(path, source_root=source_root)
    if arguments is not None:
        collector._record(_argument_names(arguments))
    for statement in body:
        collector.visit(statement)
    bindings: dict[str, str | None] = {}
    receivers: dict[str, ast.AST] = {}
    receiver_candidates: set[str] = set()
    getattr_candidates: set[str] = set()
    for name, events in collector.events.items():
        if any(event.provider == _GETATTR_ALIAS for event in events):
            getattr_candidates.add(name)
        if any(
            event.receiver is not None
            and any(
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name | ast.Attribute)
                and ast.unparse(item.func).split(".")[-1] == "MasterTokenFile"
                for item in ast.walk(event.receiver)
            )
            for event in events
        ):
            receiver_candidates.add(name)
        if len(events) == 1 and events[0].certain:
            bindings[name] = events[0].provider
            if events[0].receiver is not None:
                receivers[name] = events[0].receiver
        else:
            bindings[name] = None
    for name in collector.global_names | collector.nonlocal_names:
        bindings[name] = None
        receivers.pop(name, None)
    return (
        bindings,
        receivers,
        collector.global_names,
        collector.nonlocal_names,
        receiver_candidates,
        getattr_candidates,
    )


@dataclasses.dataclass
class _Scope:
    bindings: dict[str, str | None]
    receivers: dict[str, ast.AST]
    receiver_candidates: set[str]
    getattr_candidates: set[str]
    is_file_method: bool = False
    receiver_barrier: bool = True


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        for value in node.values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            pieces.append(value.value)
        return "".join(pieces)
    return None


class _CapabilityCollector(ast.NodeVisitor):
    def __init__(self, path: Path, tree: ast.Module, *, source_root: Path) -> None:
        self.path = path
        self.source_root = source_root
        (
            bindings,
            _receivers,
            globals_,
            nonlocals,
            _receiver_candidates,
            getattr_candidates,
        ) = _scope_bindings(path, tree.body, source_root=source_root)
        if globals_ or nonlocals:
            for name in globals_ | nonlocals:
                bindings[name] = None
        self.module_bindings = bindings
        self.module_getattr_candidates = getattr_candidates
        for item in ast.walk(tree):
            if isinstance(item, ast.Global | ast.Nonlocal):
                for name in item.names:
                    self.module_bindings[name] = None
        self.scopes: list[_Scope] = []
        self.stack: list[str] = []
        self.parents: list[ast.AST] = []
        self.constructors: set[str] = set()
        self.raw_bridge_calls: set[str] = set()
        self.present_raw_calls: set[str] = set()
        self.write_calls: set[str] = set()
        self.violations: set[str] = set()

    @property
    def owner(self) -> str:
        return ".".join(self.stack) if self.stack else "<module>"

    @property
    def label(self) -> str:
        return f"{self.path.relative_to(self.source_root).as_posix()}:{self.owner}"

    def generic_visit(self, node: ast.AST) -> None:
        self.parents.append(node)
        super().generic_visit(node)
        self.parents.pop()

    def _resolve_name(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope.bindings:
                value = scope.bindings[name]
                return _BUILTIN_GETATTR if value == _GETATTR_ALIAS else value
        value = self.module_bindings.get(name, name)
        return _BUILTIN_GETATTR if value == _GETATTR_ALIAS else value

    def _resolve_expr(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._resolve_name(node.id)
        if isinstance(node, ast.Attribute):
            prefix = self._resolve_expr(node.value)
            return f"{prefix}.{node.attr}" if prefix else None
        return None

    def _provider_available(self) -> bool:
        return any(value == _PROVIDER for value in self.module_bindings.values()) or any(
            value == _PROVIDER for scope in self.scopes for value in scope.bindings.values()
        )

    def _is_constructor(self, node: ast.AST, seen: set[str] | None = None) -> bool:
        return isinstance(node, ast.Call) and self._resolve_expr(node.func) == _PROVIDER

    def _trusted_receiver(self, node: ast.AST) -> bool:
        if self._is_constructor(node):
            return True
        if isinstance(node, ast.Name):
            if node.id == "self" and self.scopes and self.scopes[-1].is_file_method:
                return True
            for scope in reversed(self.scopes):
                if node.id in scope.bindings:
                    source = scope.receivers.get(node.id)
                    return source is not None and self._is_constructor(source)
                if scope.receiver_barrier:
                    return False
        return False

    def _potential_receiver(self, node: ast.AST) -> bool:
        if self._trusted_receiver(node):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute):
            return ast.unparse(node.func).split(".")[-1] == "MasterTokenFile"
        if isinstance(node, ast.Name):
            return any(node.id in scope.receiver_candidates for scope in reversed(self.scopes))
        return False

    def _potential_getattr_callable(self, node: ast.AST) -> bool:
        if self._resolve_expr(node) == _BUILTIN_GETATTR:
            return True
        if not isinstance(node, ast.Name):
            return False
        return (
            node.id == "getattr"
            or node.id in self.module_getattr_candidates
            or any(node.id in scope.getattr_candidates for scope in reversed(self.scopes))
        )

    def _visit_function_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        if node.returns is not None:
            self.visit(node.returns)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_signature(node)
        (
            bindings,
            receivers,
            globals_,
            nonlocals,
            receiver_candidates,
            getattr_candidates,
        ) = _scope_bindings(self.path, node.body, source_root=self.source_root, arguments=node.args)
        for name in globals_ | nonlocals:
            bindings[name] = None
            receivers.pop(name, None)
        is_file_method = (
            self.path == self.source_root / "_auth" / "master_token_file.py"
            and self.stack == ["MasterTokenFile"]
        )
        self.stack.append(node.name)
        self.scopes.append(
            _Scope(bindings, receivers, receiver_candidates, getattr_candidates, is_file_method)
        )
        self.parents.append(node)
        for statement in node.body:
            self.visit(statement)
        self.parents.pop()
        self.scopes.pop()
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for item in (*node.decorator_list, *node.bases):
            self.visit(item)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self.stack.append(node.name)
        self.parents.append(node)
        for statement in node.body:
            self.visit(statement)
        self.parents.pop()
        self.stack.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        bindings = dict.fromkeys(_argument_names(node.args))
        self.stack.append(f"<lambda>@{node.lineno}")
        self.scopes.append(_Scope(bindings, {}, set(), set()))
        self.parents.append(node)
        self.visit(node.body)
        self.parents.pop()
        self.scopes.pop()
        self.stack.pop()

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> None:
        first, *remaining = node.generators
        self.visit(first.iter)
        names = _target_names(first.target)
        names.update(name for item in remaining for name in _target_names(item.target))
        self.stack.append(f"<comprehension>@{node.lineno}")
        self.scopes.append(_Scope(dict.fromkeys(names), {}, set(), set()))
        self.parents.append(node)
        for condition in first.ifs:
            self.visit(condition)
        for item in remaining:
            self.visit(item.iter)
            for condition in item.ifs:
                self.visit(condition)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.parents.pop()
        self.scopes.pop()
        self.stack.pop()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension

    def visit_Call(self, node: ast.Call) -> None:
        provider = self._resolve_expr(node.func)
        if provider == _PROVIDER:
            self.constructors.add(self.label)
            parent = self.parents[-1] if self.parents else None
            legal_assignment = (
                isinstance(parent, ast.Assign)
                and parent.value is node
                and len(parent.targets) == 1
                and isinstance(parent.targets[0], ast.Name)
                and self._trusted_receiver(parent.targets[0])
            )
            legal_direct_call = (
                isinstance(parent, ast.Attribute)
                and parent.value is node
                and parent.attr in _SENSITIVE_METHODS
            )
            if not legal_assignment and not legal_direct_call:
                self.violations.add(f"{self.label}:constructor-result-escape")
        elif (
            isinstance(node.func, ast.Name | ast.Attribute)
            and ast.unparse(node.func).split(".")[-1] == "MasterTokenFile"
            and provider != _PROVIDER
        ):
            self.violations.add(f"{self.label}:untrusted-constructor")
        if isinstance(node.func, ast.Attribute) and node.func.attr in _SENSITIVE_METHODS:
            if node.func.attr == "write" and not self._potential_receiver(node.func.value):
                pass
            elif not self._trusted_receiver(node.func.value):
                self.violations.add(f"{self.label}:untrusted-{node.func.attr}")
            elif node.func.attr == "_read_present_with_raw":
                self.present_raw_calls.add(self.label)
            elif node.func.attr == "_read_with_raw":
                self.raw_bridge_calls.add(self.label)
            else:
                self.write_calls.add(self.label)
        if self._potential_getattr_callable(node.func) and len(node.args) >= 2:
            attribute = _static_string(node.args[1])
            if attribute in _SENSITIVE_METHODS or attribute is None and self._provider_available():
                self.violations.add(f"{self.label}:dynamic-access")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parent = self.parents[-1] if self.parents else None
        if node.attr in _SENSITIVE_METHODS and not (
            isinstance(parent, ast.Call) and parent.func is node
        ):
            if node.attr != "write" or self._potential_receiver(node.value):
                self.violations.add(f"{self.label}:{node.attr}-escape")
        if self._resolve_expr(node) == _PROVIDER and isinstance(node.ctx, ast.Load):
            if not (isinstance(parent, ast.Call) and parent.func is node):
                self.violations.add(f"{self.label}:file-capability-escape")
        if (
            self.path == self.source_root / "_auth" / "master_token_file.py"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and self.stack
            and self.stack[0] == "MasterTokenFile"
        ):
            method = self.stack[1] if len(self.stack) > 1 else "<class>"
            allowed: dict[str, set[str]] = {
                "__init__": {"_path", "_locks"},
                "_read_present_with_raw": {"_path"},
                "_read_with_raw": {"_path", "_read_present_with_raw"},
                "read": {"_read_with_raw"},
                "write": {"_path", "_locks"},
            }
            direct_method = bool(self.scopes and self.scopes[-1].is_file_method)
            if not direct_method or node.attr not in allowed.get(method, set()):
                self.violations.add(f"{self.label}:unowned-self-{node.attr}")
            if node.attr == "_locks" and isinstance(node.ctx, ast.Load):
                grandparent = self.parents[-2] if len(self.parents) > 1 else None
                if not (
                    method == "write"
                    and isinstance(parent, ast.Attribute)
                    and parent.value is node
                    and parent.attr == "acquire"
                    and isinstance(grandparent, ast.Call)
                    and grandparent.func is parent
                ):
                    self.violations.add(f"{self.label}:lock-capability-escape")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        provider = self._resolve_name(node.id)
        parent = self.parents[-1] if self.parents else None
        if provider == _PROVIDER and isinstance(node.ctx, ast.Load):
            if not (isinstance(parent, ast.Call) and parent.func is node):
                self.violations.add(f"{self.label}:file-capability-escape")


def _capability_projection(
    source_root: Path,
) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    constructors: set[str] = set()
    raw_calls: set[str] = set()
    present_calls: set[str] = set()
    write_calls: set[str] = set()
    violations: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _CapabilityCollector(path, tree, source_root=source_root)
        collector.visit(tree)
        constructors.update(collector.constructors)
        raw_calls.update(collector.raw_bridge_calls)
        present_calls.update(collector.present_raw_calls)
        write_calls.update(collector.write_calls)
        violations.update(collector.violations)
    return constructors, raw_calls, present_calls, write_calls, violations


def test_file_construction_and_private_raw_bridge_callers_are_exact() -> None:
    constructors, raw_calls, present_calls, write_calls, violations = _capability_projection(
        SRC_ROOT
    )
    assert constructors == _EXPECTED_CONSTRUCTORS
    assert raw_calls == _EXPECTED_RAW_BRIDGE_CALLERS
    assert present_calls == _EXPECTED_PRESENT_RAW_CALLERS
    assert write_calls == _EXPECTED_WRITE_CALLERS
    assert violations == set()
    bootstrap_label = "_auth/master_token_bootstrap.py:"
    assert all(not owner.startswith(bootstrap_label) for owner in constructors)
    assert all(not owner.startswith(bootstrap_label) for owner in raw_calls)
    assert all(not owner.startswith(bootstrap_label) for owner in present_calls)
    assert all(not owner.startswith(bootstrap_label) for owner in write_calls)


def test_bootstrapper_has_zero_master_token_file_capability() -> None:
    source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(BOOTSTRAP_PATH))
    assert "MasterTokenFile" not in source
    assert "master_token_file" not in source
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name | ast.Attribute)
        and ast.unparse(node.func).endswith((".read", ".write", "open"))
        for node in ast.walk(tree)
    )


def test_raw_projection_exists_only_in_the_public_legacy_reader() -> None:
    actual: set[str] = set()

    class Collector(ast.NodeVisitor):
        def __init__(self, path: Path) -> None:
            self.path = path
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if node.attr == "raw":
                actual.add(f"{self.path.name}:{'.'.join(self.stack) or '<module>'}")
            self.generic_visit(node)

    for path in AUTH_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        Collector(path).visit(tree)
    assert actual == {"master_token.py:read_master_token"}


@pytest.mark.parametrize(
    "mutation",
    [
        "from .storage import write_master_token\n",
        "REGISTRY = {}\n",
        "logger.info(raw)\n",
        "        self._cached = None\n",
        "        return _master_token_to_legacy_record(result.token)\n",
        "        _commit_master_token_json(self._path, payload)\n",
        "            return payload\n",
        "            self._cached = payload\n",
        "            leaks['token'] = payload\n",
        "            yield payload\n",
    ],
)
def test_module_guard_bites_on_upward_imports_state_raw_rendering_and_moved_commit(
    mutation: str,
) -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    if (
        mutation.startswith("from ")
        or mutation.startswith("REGISTRY")
        or mutation.startswith("logger")
    ):
        source += "\n" + mutation
    elif mutation == "        self._cached = None\n":
        source = source.replace(
            "        self._path = path\n", "        self._path = path\n" + mutation
        )
    elif "_master_token_to_legacy_record(result.token)" in mutation:
        source = source.replace(
            "        return None if result is None else result.token\n", mutation, 1
        )
    elif mutation.startswith("        _commit"):
        source = source.replace(
            "        with self._locks.acquire(request) as state:\n",
            mutation + "        with self._locks.acquire(request) as state:\n",
            1,
        )
    else:
        source = source.replace(
            "            _commit_master_token_json(self._path, payload)\n",
            "            _commit_master_token_json(self._path, payload)\n" + mutation,
            1,
        )
    assert _module_violations(source)


@pytest.mark.parametrize(
    "body",
    [
        "from notebooklm._auth.master_token_file import MasterTokenFile as File\nfn = File\n",
        "from notebooklm._auth.master_token_file import MasterTokenFile\n"
        "def f(path):\n    return MasterTokenFile(path)._read_with_raw\n",
        "from notebooklm._auth.master_token_file import MasterTokenFile\n"
        "def f(path):\n    return getattr(MasterTokenFile(path), '_read_with_raw')()\n",
        "from notebooklm._auth.master_token_file import MasterTokenFile\n"
        "MasterTokenFile = object\ndef f(path):\n    return MasterTokenFile(path)\n",
    ],
)
def test_capability_detector_bites_on_alias_escape_dynamic_access_and_rebinding(
    tmp_path: Path, body: str
) -> None:
    source_root = tmp_path / "notebooklm"
    source_root.mkdir()
    source = source_root / "consumer.py"
    source.write_text(textwrap.dedent(body), encoding="utf-8")
    _constructors, _raw_calls, _present_calls, _write_calls, violations = _capability_projection(
        source_root
    )
    assert violations


def _mutated_full_source_projection(
    tmp_path: Path,
    relative: str,
    old: str,
    new: str,
) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    source_root = tmp_path / "notebooklm"
    destination = source_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    original = (SRC_ROOT / relative).read_text(encoding="utf-8")
    assert old in original
    destination.write_text(original.replace(old, new, 1), encoding="utf-8")
    return _capability_projection(source_root)


@pytest.mark.parametrize(
    "binding",
    [
        "    MasterTokenFile = object\n",
        "    if condition:\n        MasterTokenFile = object\n",
        "    MasterTokenFile, ignored = (object, None)\n",
        "    MasterTokenFile: object = object\n",
        "    MasterTokenFile += object\n",
        "    if (MasterTokenFile := object):\n        pass\n",
        "    for MasterTokenFile in providers:\n        pass\n",
        "    with provider() as MasterTokenFile:\n        pass\n",
        "    try:\n        pass\n    except Exception as MasterTokenFile:\n        pass\n",
        "    match provider:\n        case MasterTokenFile:\n            pass\n",
        "    def MasterTokenFile(path):\n        return object()\n",
        "    async def MasterTokenFile(path):\n        return object()\n",
        "    class MasterTokenFile:\n        pass\n",
        "    global MasterTokenFile\n",
    ],
)
def test_full_source_collector_bites_on_same_owner_lexical_provider_conflicts(
    tmp_path: Path, binding: str
) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/master_token.py",
        "    token_file = MasterTokenFile(path)\n",
        binding + "    token_file = MasterTokenFile(path)\n",
    )
    assert projection[-1]


def test_full_source_collector_bites_on_later_and_deferred_provider_binding(
    tmp_path: Path,
) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/master_token.py",
        "        result = token_file._read_present_with_raw()\n",
        "        result = token_file._read_present_with_raw()\n        MasterTokenFile = object\n",
    )
    assert projection[-1]


@pytest.mark.parametrize(
    "replacement",
    [
        "    MasterTokenFile(path)\n"
        "    token_file = forged\n"
        "    malformed_message: str | None = None\n",
        "    token_file = MasterTokenFile(path)\n"
        "    token_file: object = forged\n"
        "    malformed_message: str | None = None\n",
        "    token_file = MasterTokenFile(path)\n"
        "    token_file, ignored = forged, None\n"
        "    malformed_message: str | None = None\n",
        "    token_file = MasterTokenFile(path)\n"
        "    if condition:\n"
        "        token_file = forged\n"
        "    malformed_message: str | None = None\n",
        "    token_file = MasterTokenFile(path)\n"
        "    token_file += forged\n"
        "    malformed_message: str | None = None\n",
        "    token_file = MasterTokenFile(path)\n"
        "    if (token_file := forged):\n"
        "        pass\n"
        "    malformed_message: str | None = None\n",
        "    token_file = MasterTokenFile(path)\n"
        "    for token_file in files:\n"
        "        pass\n"
        "    malformed_message: str | None = None\n",
        "    token_file = MasterTokenFile(path)\n"
        "    with provider() as token_file:\n"
        "        pass\n"
        "    malformed_message: str | None = None\n",
        "    token_file = MasterTokenFile(path)\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as token_file:\n"
        "        pass\n"
        "    malformed_message: str | None = None\n",
        "    token_file = MasterTokenFile(path)\n"
        "    match provider:\n"
        "        case token_file:\n"
        "            pass\n"
        "    malformed_message: str | None = None\n",
    ],
)
def test_full_source_collector_bites_on_receiver_loss_and_uncertainty(
    tmp_path: Path, replacement: str
) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/master_token.py",
        "    token_file = MasterTokenFile(path)\n    malformed_message: str | None = None\n",
        replacement,
    )
    assert projection[-1]


@pytest.mark.parametrize(
    "replacement",
    [
        "        result = getattr(token_file, '_' + 'read_present_with_raw')()\n",
        "        result = (lambda file: file._read_present_with_raw())(token_file)\n",
        "        result = next(\n"
        "            file._read_present_with_raw() for file in [token_file]\n"
        "        )\n",
    ],
)
def test_full_source_collector_bites_on_computed_and_deferred_raw_access(
    tmp_path: Path, replacement: str
) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/master_token.py",
        "        result = token_file._read_present_with_raw()\n",
        replacement,
    )
    assert projection[-1]


def _assert_mutated_legacy_reader_keeps_live_projections_and_bites(
    projection: tuple[set[str], set[str], set[str], set[str], set[str]],
) -> None:
    constructors, raw_calls, present_calls, write_calls, violations = projection
    assert constructors == {"_auth/master_token.py:read_master_token"}
    assert raw_calls == set()
    assert present_calls == {"_auth/master_token.py:read_master_token"}
    assert write_calls == set()
    assert violations


def test_full_source_collector_bites_on_aliased_builtin_getattr(tmp_path: Path) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/master_token.py",
        "        result = token_file._read_present_with_raw()\n",
        "        getter = getattr\n"
        "        getter(token_file, '_read_present_with_raw')\n"
        "        result = token_file._read_present_with_raw()\n",
    )
    _assert_mutated_legacy_reader_keeps_live_projections_and_bites(projection)


def test_full_source_collector_keeps_getattr_alias_poisoned_after_rebinding(
    tmp_path: Path,
) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/master_token.py",
        "        result = token_file._read_present_with_raw()\n",
        "        getter = getattr\n"
        "        getter = replacement\n"
        "        getter(token_file, '_read_present_with_raw')\n"
        "        result = token_file._read_present_with_raw()\n",
    )
    _assert_mutated_legacy_reader_keeps_live_projections_and_bites(projection)


def test_full_source_collector_bites_on_constructor_callback_escape(tmp_path: Path) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/master_token.py",
        "    token_file = MasterTokenFile(path)\n",
        "    leak(MasterTokenFile)\n    token_file = MasterTokenFile(path)\n",
    )
    _assert_mutated_legacy_reader_keeps_live_projections_and_bites(projection)


def test_full_source_collector_bites_on_writer_bound_method_escape(tmp_path: Path) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/storage.py",
        "    MasterTokenFile(path).write(\n"
        "        MasterToken(email=email, android_id=android_id, secret=master_token)\n"
        "    )\n",
        "    writer = MasterTokenFile(path).write\n"
        "    writer(MasterToken(email=email, android_id=android_id, secret=master_token))\n",
    )
    assert projection[-1]


def test_full_source_collector_bites_on_injected_self_lock_capability(tmp_path: Path) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/master_token_file.py",
        "    def write(self, token: MasterToken) -> None:\n",
        "    def leak_locks(self) -> object:\n"
        "        return self._locks\n\n"
        "    def write(self, token: MasterToken) -> None:\n",
    )
    assert projection[-1]


def test_full_source_collector_bites_on_later_module_provider_rebinding(tmp_path: Path) -> None:
    projection = _mutated_full_source_projection(
        tmp_path,
        "_auth/master_token.py",
        "# --- the transaction",
        "MasterTokenFile = object\n\n# --- the transaction",
    )
    assert projection[-1]
