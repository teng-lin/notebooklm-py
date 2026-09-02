"""Fail-closed capability and consumer guards for the master-token value leaf."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import textwrap
from pathlib import Path

import pytest

import notebooklm.auth as auth_facade
from notebooklm._auth import master_token, master_token_types, storage

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
MODULE_PATH = AUTH_ROOT / "master_token_types.py"

_CODEC_NAMES = {
    "MasterToken",
    "_MasterTokenRecordError",
    "_master_token_from_legacy_record",
    "_master_token_to_legacy_record",
}
_MODULE_NAME = "notebooklm._auth.master_token_types"
_EXPECTED_IMPORTS = [
    ("module", 0, "__future__", "annotations", None),
    ("module", 0, "dataclasses", "dataclass", None),
    ("module", 0, "dataclasses", "field", None),
    ("module", 0, "typing", "Any", None),
]


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope = "module"
        self.records: list[tuple[str, int, str, str, str | None]] = []

    def _visit_deferred(self, node: ast.AST, scope: str) -> None:
        prior = self.scope
        self.scope = scope
        self.generic_visit(node)
        self.scope = prior

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_deferred(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_deferred(node, "function")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_deferred(node, "lambda")

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_deferred(node, "comprehension")

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_deferred(node, "class")

    def visit_Import(self, node: ast.Import) -> None:
        self.records.extend((self.scope, 0, item.name, "", item.asname) for item in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.records.extend(
            (self.scope, node.level, node.module or "", item.name, item.asname)
            for item in node.names
        )


def _collect_imports(tree: ast.AST) -> list[tuple[str, int, str, str, str | None]]:
    collector = _ImportCollector()
    collector.visit(tree)
    return collector.records


def _module_nodes(tree: ast.Module) -> list[ast.stmt]:
    return [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        and not isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def _single_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _single_class(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _function_signature(node: ast.FunctionDef) -> tuple[list[tuple[str, str | None]], str | None]:
    assert not node.args.posonlyargs
    assert not node.args.kwonlyargs
    assert node.args.vararg is None
    assert node.args.kwarg is None
    assert not node.args.defaults
    assert not node.args.kw_defaults
    return (
        [
            (arg.arg, ast.unparse(arg.annotation) if arg.annotation else None)
            for arg in node.args.args
        ],
        ast.unparse(node.returns) if node.returns else None,
    )


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return f"{node.func.value.id}.{node.func.attr}"
    return "<dynamic>"


def _live_structure_violations(tree: ast.Module) -> list[str]:
    """Validate the complete live module shape, calls, projections, and bindings."""
    violations: list[str] = []
    imports = _collect_imports(tree)
    if imports != _EXPECTED_IMPORTS:
        violations.append(f"imports:{imports!r}")

    nodes = _module_nodes(tree)
    expected_kinds = [
        (ast.ClassDef, "MasterTokenError"),
        (ast.Assign, "MasterTokenError.__module__"),
        (ast.ClassDef, "MasterToken"),
        (ast.Assign, "_MASTER_TOKEN_RECORD_VERSION"),
        (ast.ClassDef, "_MasterTokenRecordError"),
        (ast.FunctionDef, "_master_token_from_legacy_record"),
        (ast.FunctionDef, "_master_token_to_legacy_record"),
    ]
    actual_kinds: list[tuple[type[ast.stmt], str]] = []
    for node in nodes:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            actual_kinds.append((type(node), node.name))
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            actual_kinds.append((type(node), node.targets[0].id))
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            actual_kinds.append((type(node), ast.unparse(node.targets[0])))
        else:
            actual_kinds.append((type(node), "<unsupported>"))
    if actual_kinds != expected_kinds:
        violations.append(f"module-shape:{actual_kinds!r}")
        return violations

    token_class = _single_class(tree, "MasterToken")
    if len(token_class.decorator_list) != 1:
        violations.append("MasterToken-decorators")
    else:
        decorator = token_class.decorator_list[0]
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "dataclass"
            and not decorator.args
            and [(item.arg, ast.literal_eval(item.value)) for item in decorator.keywords]
            == [("frozen", True), ("repr", False)]
        ):
            violations.append("MasterToken-decorator")

    class_body = [
        node
        for node in token_class.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    field_nodes: list[ast.AnnAssign] = []
    if len(class_body) != 4 or not all(isinstance(node, ast.AnnAssign) for node in class_body[:3]):
        violations.append("MasterToken-body")
    else:
        field_nodes = [node for node in class_body[:3] if isinstance(node, ast.AnnAssign)]
        field_shape = [
            (
                node.target.id if isinstance(node.target, ast.Name) else "<dynamic>",
                ast.unparse(node.annotation),
                ast.unparse(node.value) if node.value else None,
            )
            for node in field_nodes
        ]
        if field_shape != [
            ("email", "str", None),
            ("android_id", "str", None),
            ("secret", "str", "field(repr=False)"),
        ]:
            violations.append(f"MasterToken-fields:{field_shape!r}")
    repr_nodes = [
        node
        for node in token_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__repr__"
    ]
    if len(repr_nodes) != 1 or _function_signature(repr_nodes[0]) != ([("self", None)], "str"):
        violations.append("MasterToken-repr-signature")
    else:
        repr_node = repr_nodes[0]
        calls = [_call_name(node) for node in ast.walk(repr_node) if isinstance(node, ast.Call)]
        attrs = {
            (node.value.id, node.attr)
            for node in ast.walk(repr_node)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        constants = [
            node.value
            for node in ast.walk(repr_node)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        if calls or attrs != {("self", "email"), ("self", "android_id")}:
            violations.append(f"MasterToken-repr-effects:{calls!r}:{attrs!r}")
        if "<redacted>" not in "".join(constants) or any(
            "secret=" in item and "<redacted>" not in item for item in constants
        ):
            violations.append("MasterToken-repr-redaction")

    public_error = _single_class(tree, "MasterTokenError")
    if len(public_error.bases) != 1 or ast.unparse(public_error.bases[0]) != "Exception":
        violations.append("public-error-base")
    public_error_state = [
        node
        for node in public_error.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        and not isinstance(node, ast.Pass)
    ]
    if public_error_state:
        violations.append("public-error-state")
    module_rebind = nodes[1]
    if ast.unparse(module_rebind) != (
        "MasterTokenError.__module__ = 'notebooklm._auth.master_token'"
    ):
        violations.append("public-error-module")

    version = nodes[3]
    if not (
        isinstance(version, ast.Assign)
        and len(version.targets) == 1
        and isinstance(version.targets[0], ast.Name)
        and version.targets[0].id == "_MASTER_TOKEN_RECORD_VERSION"
        and isinstance(version.value, ast.Constant)
        and type(version.value.value) is int
        and version.value.value == 1
    ):
        violations.append("record-version")

    allowed_assignments = {id(module_rebind), id(version), *(id(node) for node in field_nodes)}
    for node in ast.walk(tree):
        if (
            isinstance(
                node,
                (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr, ast.Delete),
            )
            and id(node) not in allowed_assignments
        ):
            violations.append(f"unexpected-binding:{type(node).__name__}:{node.lineno}")

    error_class = _single_class(tree, "_MasterTokenRecordError")
    if len(error_class.bases) != 1 or ast.unparse(error_class.bases[0]) != "ValueError":
        violations.append("record-error-base")
    error_state = [
        node
        for node in error_class.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        and not isinstance(node, ast.Pass)
    ]
    if error_state:
        violations.append("record-error-state")

    decoder = _single_function(tree, "_master_token_from_legacy_record")
    encoder = _single_function(tree, "_master_token_to_legacy_record")
    if _function_signature(decoder) != ([("record", "object")], "MasterToken"):
        violations.append("decoder-signature")
    if _function_signature(encoder) != ([("token", "MasterToken")], "dict[str, Any]"):
        violations.append("encoder-signature")

    decode_calls = [_call_name(node) for node in ast.walk(decoder) if isinstance(node, ast.Call)]
    if sorted(decode_calls) != sorted(
        [
            "isinstance",
            "record.get",
            "record.get",
            "_MasterTokenRecordError",
            "_MasterTokenRecordError",
            "_MasterTokenRecordError",
            "MasterToken",
        ]
    ):
        violations.append(f"decoder-calls:{decode_calls!r}")
    decode_attrs = {
        (node.value.id, node.attr)
        for node in ast.walk(decoder)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }
    if decode_attrs != {("record", "get")}:
        violations.append(f"decoder-attributes:{decode_attrs!r}")
    loops = [node for node in ast.walk(decoder) if isinstance(node, ast.For)]
    if len(loops) != 1 or ast.unparse(loops[0].iter) != "('master_token', 'email', 'android_id')":
        violations.append("decoder-field-order")
    decode_subscripts = [
        ast.unparse(node) for node in ast.walk(decoder) if isinstance(node, ast.Subscript)
    ]
    if sorted(decode_subscripts) != sorted(
        ["record['email']", "record['android_id']", "record['master_token']"]
    ):
        violations.append(f"decoder-projection:{decode_subscripts!r}")
    decoder_strings = {
        node.value
        for node in ast.walk(decoder)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    expected_strings = {
        "Decode the historical version-1 mapping predicate without tightening it.",
        "master_token.json is malformed or an unsupported version.",
        "version",
        "master_token",
        "email",
        "android_id",
    }
    if decoder_strings != expected_strings:
        violations.append(f"decoder-strings:{decoder_strings!r}")

    encode_calls = [_call_name(node) for node in ast.walk(encoder) if isinstance(node, ast.Call)]
    if sorted(encode_calls) != sorted(["type", "_MasterTokenRecordError"]):
        # The fixed TypeError is intentionally the only other constructor.
        if sorted(encode_calls) != sorted(["type", "TypeError"]):
            violations.append(f"encoder-calls:{encode_calls!r}")
    encode_attrs = [
        ast.unparse(node) for node in ast.walk(encoder) if isinstance(node, ast.Attribute)
    ]
    if sorted(encode_attrs) != sorted(["token.email", "token.android_id", "token.secret"]):
        violations.append(f"encoder-projection:{encode_attrs!r}")
    returns = [node for node in encoder.body if isinstance(node, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.Dict):
        violations.append("encoder-return")
    else:
        keys = [ast.literal_eval(key) for key in returns[0].value.keys]
        values = [ast.unparse(value) for value in returns[0].value.values]
        if keys != ["version", "email", "android_id", "master_token"] or values != [
            "_MASTER_TOKEN_RECORD_VERSION",
            "token.email",
            "token.android_id",
            "token.secret",
        ]:
            violations.append(f"encoder-mapping:{keys!r}:{values!r}")

    forbidden_nodes = (
        ast.AsyncFunctionDef,
        ast.Await,
        ast.Yield,
        ast.YieldFrom,
        ast.Lambda,
        ast.ListComp,
        ast.SetComp,
        ast.DictComp,
        ast.GeneratorExp,
        ast.Global,
        ast.Nonlocal,
        ast.With,
        ast.AsyncWith,
        ast.Try,
        ast.Match,
    )
    for node in ast.walk(tree):
        if isinstance(node, forbidden_nodes) or type(node).__name__ == "TryStar":
            violations.append(f"forbidden-node:{type(node).__name__}:{node.lineno}")
    return violations


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _source_package(path: Path, src_root: Path) -> tuple[str, ...]:
    relative = path.relative_to(src_root)
    return ("notebooklm", *relative.parent.parts)


def _resolve_import_from(node: ast.ImportFrom, *, path: Path, src_root: Path) -> str | None:
    """Resolve every legal relative level from the source file's actual package."""
    if node.level == 0:
        return node.module or ""
    package = _source_package(path, src_root)
    parents = node.level - 1
    if parents >= len(package):
        return None
    prefix = package[: len(package) - parents]
    suffix = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*prefix, *suffix))


def _static_string(node: ast.AST) -> str | None:
    """Evaluate only side-effect-free literal string assembly used by import APIs."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        pieces = [_static_string(item) for item in node.values]
        return "".join(pieces) if all(item is not None for item in pieces) else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and not node.keywords
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        separator = _static_string(node.func.value)
        pieces = [_static_string(item) for item in node.args[0].elts]
        if separator is not None and all(item is not None for item in pieces):
            return separator.join(item for item in pieces if item is not None)
    return None


def _import_bindings(tree: ast.AST, *, path: Path, src_root: Path) -> dict[str, str]:
    """Conservatively retain every canonical import binding despite later ambiguity."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                binding = item.asname or item.name.split(".", 1)[0]
                bindings.setdefault(binding, item.name if item.asname else binding)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_import_from(node, path=path, src_root=src_root)
            if module is None:
                continue
            for item in node.names:
                if item.name == "*":
                    continue
                bindings.setdefault(item.asname or item.name, f"{module}.{item.name}")
    return bindings


def _bound_name(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _bound_name(node.value, bindings)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _expression_origin(node: ast.AST, bindings: dict[str, str]) -> str | None:
    origin = _bound_name(node, bindings)
    if origin is not None:
        return origin
    if isinstance(node, ast.Call):
        callable_name = _bound_name(node.func, bindings)
        if callable_name in {"importlib.import_module", "__import__"} and node.args:
            return _static_string(node.args[0])
        if callable_name == "getattr" and len(node.args) >= 2:
            owner = _expression_origin(node.args[0], bindings)
            member = _static_string(node.args[1])
            if owner is not None and member is not None:
                return f"{owner}.{member}"
    return None


def _bind_assignment(target: ast.AST, value: ast.AST, bindings: dict[str, str]) -> bool:
    changed = False
    if isinstance(target, ast.Name):
        origin = _expression_origin(value, bindings)
        if origin is not None and target.id not in bindings:
            bindings[target.id] = origin
            changed = True
    elif (
        isinstance(target, ast.Tuple | ast.List)
        and isinstance(value, ast.Tuple | ast.List)
        and len(target.elts) == len(value.elts)
    ):
        for child_target, child_value in zip(target.elts, value.elts, strict=True):
            changed |= _bind_assignment(child_target, child_value, bindings)
    return changed


def _alias_bindings(tree: ast.AST, *, path: Path, src_root: Path) -> dict[str, str]:
    """Conservatively retain canonical bindings through later/local aliases."""
    bindings = _import_bindings(tree, path=path, src_root=src_root)
    assignments: list[tuple[ast.AST, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assignments.extend((target, node.value) for target in node.targets)
        elif (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(
            node, ast.NamedExpr
        ):
            assignments.append((node.target, node.value))
    for _ in range(len(assignments) + 1):
        if not any(_bind_assignment(target, value, bindings) for target, value in assignments):
            break
    return bindings


class _ConstructorOwnerCollector(ast.NodeVisitor):
    def __init__(self, bindings: dict[str, str]) -> None:
        self.bindings = bindings
        self.owners: set[str] = set()
        self.scopes: list[str] = []

    @property
    def owner(self) -> str:
        return ".".join(self.scopes) if self.scopes else "<module>"

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self.scopes.append(name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_scope(node, "<lambda>")

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_scope(node, "<comprehension>")

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Call(self, node: ast.Call) -> None:
        if _expression_origin(node.func, self.bindings) == f"{_MODULE_NAME}.MasterToken":
            self.owners.add(self.owner)
        self.generic_visit(node)


def _master_token_constructor_owners(src_root: Path) -> set[tuple[str, str]]:
    owners: set[tuple[str, str]] = set()
    leaf_path = src_root / "_auth" / "master_token_types.py"
    for path in sorted(src_root.rglob("*.py")):
        if path == leaf_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _ConstructorOwnerCollector(_alias_bindings(tree, path=path, src_root=src_root))
        collector.visit(tree)
        owners.update((path.relative_to(src_root).as_posix(), owner) for owner in collector.owners)
    return owners


def _production_consumers(src_root: Path) -> list[str]:
    """Find any static, dynamic, aliased, deferred, or spelled consumer outside the leaf."""
    violations: list[str] = []
    leaf_path = src_root / "_auth" / "master_token_types.py"
    for path in sorted(src_root.rglob("*.py")):
        if path == leaf_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bindings = _import_bindings(tree, path=path, src_root=src_root)
        relative = path.relative_to(src_root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(item.name == _MODULE_NAME for item in node.names):
                    violations.append(f"{relative}:{node.lineno}:import")
            elif isinstance(node, ast.ImportFrom):
                module = _resolve_import_from(node, path=path, src_root=src_root)
                if module == _MODULE_NAME or (
                    module == "notebooklm._auth"
                    and any(item.name == "master_token_types" for item in node.names)
                ):
                    violations.append(f"{relative}:{node.lineno}:from-import")
            elif _static_string(node) == _MODULE_NAME:
                violations.append(f"{relative}:{node.lineno}:dynamic-target")
            elif isinstance(node, ast.Attribute):
                qualified = _bound_name(node, bindings)
                if qualified and qualified.startswith(_MODULE_NAME + "."):
                    violations.append(f"{relative}:{node.lineno}:qualified-access")
            elif isinstance(node, ast.Call):
                callable_name = _bound_name(node.func, bindings)
                if callable_name in {"importlib.import_module", "__import__"} and node.args:
                    if _static_string(node.args[0]) == _MODULE_NAME:
                        violations.append(f"{relative}:{node.lineno}:dynamic-import")
                if callable_name == "getattr" and len(node.args) >= 2:
                    owner = _bound_name(node.args[0], bindings)
                    member = _static_string(node.args[1])
                    if owner == "notebooklm._auth" and member == "master_token_types":
                        violations.append(f"{relative}:{node.lineno}:dynamic-access")
                if isinstance(node.func, ast.Name) and node.func.id in _CODEC_NAMES:
                    violations.append(f"{relative}:{node.lineno}:bare-call:{node.func.id}")
    return sorted(set(violations))


def test_live_module_has_exact_value_codec_and_no_capabilities() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    assert _live_structure_violations(tree) == []


def test_runtime_value_shape_and_redaction_are_pinned() -> None:
    error = master_token_types.MasterTokenError
    assert master_token.MasterTokenError is error
    assert storage.MasterTokenError is error
    assert auth_facade.MasterTokenError is error
    assert error.__module__ == "notebooklm._auth.master_token"
    assert error.__qualname__ == "MasterTokenError"
    assert [item.name for item in dataclasses.fields(master_token_types.MasterToken)] == [
        "email",
        "android_id",
        "secret",
    ]
    assert master_token_types.MasterToken.__dataclass_params__.frozen is True
    assert master_token_types.MasterToken.__dataclass_params__.repr is False
    assert inspect.signature(master_token_types.MasterToken) == inspect.Signature(
        [
            inspect.Parameter("email", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation="str"),
            inspect.Parameter(
                "android_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation="str"
            ),
            inspect.Parameter("secret", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation="str"),
        ],
        return_annotation=None,
    )
    assert master_token_types._MASTER_TOKEN_RECORD_VERSION == 1
    assert issubclass(master_token_types._MasterTokenRecordError, ValueError)
    error = master_token_types._MasterTokenRecordError("fixed")
    assert error.args == ("fixed",)
    assert error.__dict__ == {}


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("import json as harmless\n", id="aliased-import"),
        pytest.param(
            "from pathlib import Path as Harmless\nHarmless('token')\n", id="aliased-call"
        ),
        pytest.param("cache = {}\n", id="assignment"),
        pytest.param("callback = record.get\n", id="bound-method-escape"),
        pytest.param("sink(_master_token_to_legacy_record)\n", id="callback-escape"),
        pytest.param("getattr(token, 'secret')\n", id="dynamic-getattr"),
        pytest.param("dataclass = evil\n", id="module-shadowing"),
        pytest.param("def later():\n    open('token')\n", id="deferred-function"),
        pytest.param("leak = lambda: repr(token.secret)\n", id="deferred-lambda"),
        pytest.param("leaks = [str(token.secret) for token in tokens]\n", id="comprehension"),
        pytest.param("message = f'{token.secret}'\n", id="secret-f-string"),
        pytest.param("message = repr(token.secret)\n", id="secret-repr"),
        pytest.param("async def later():\n    await write(token)\n", id="async-effect"),
    ],
)
def test_capability_detector_bites_on_aliases_escapes_and_deferred_scopes(source: str) -> None:
    live_source = MODULE_PATH.read_text(encoding="utf-8")
    marker = "    if not isinstance(record, dict):\n"
    mutated = live_source.replace(marker, textwrap.indent(source, "    ") + marker, 1)
    assert mutated != live_source
    assert _live_structure_violations(ast.parse(mutated))


def test_production_consumers_are_exactly_the_phase_11d_owners() -> None:
    normalized = {
        (path, detail)
        for item in _production_consumers(SRC_ROOT)
        for path, _line, detail in [item.split(":", 2)]
    }
    assert normalized == {
        ("_android/auth.py", "from-import"),
        ("_auth/master_token.py", "from-import"),
        ("_auth/master_token.py", "bare-call:MasterToken"),
        ("_auth/master_token_bootstrap.py", "from-import"),
        ("_auth/master_token_file.py", "from-import"),
        (
            "_auth/master_token_file.py",
            "bare-call:_master_token_from_legacy_record",
        ),
        (
            "_auth/master_token_file.py",
            "bare-call:_master_token_to_legacy_record",
        ),
        ("_auth/profile_store.py", "from-import"),
        ("_auth/mint_service.py", "bare-call:MasterToken"),
        ("_auth/mint_service.py", "from-import"),
        ("_auth/storage.py", "bare-call:MasterToken"),
        ("_auth/storage.py", "from-import"),
        ("_browser/oauth_token.py", "from-import"),
    }


def test_master_token_constructors_have_exact_typed_and_compatibility_owners() -> None:
    assert _master_token_constructor_owners(SRC_ROOT) == {
        ("_auth/master_token.py", "mint_cookies"),
        ("_auth/mint_service.py", "MintService.exchange"),
        ("_auth/storage.py", "write_master_token"),
    }


@pytest.mark.parametrize(
    "body",
    [
        "from notebooklm._auth.master_token_types import MasterToken as Credential\n"
        "def later():\n    return Credential(email='e', android_id='a', secret='s')\n",
        "import notebooklm._auth.master_token_types as tokens\n"
        "build = lambda: tokens.MasterToken(email='e', android_id='a', secret='s')\n",
        "import importlib\n"
        "tokens = importlib.import_module('notebooklm._auth.' + 'master_token_types')\n"
        "items = [tokens.MasterToken(email='e', android_id='a', secret='s') for _ in (0,)]\n",
        "import notebooklm._auth as auth\n"
        "tokens = getattr(auth, 'master_' + 'token_types')\n"
        "if ready:\n    value = tokens.MasterToken(email='e', android_id='a', secret='s')\n",
        "from notebooklm._auth.master_token_types import MasterToken\n"
        "def later():\n"
        "    Constructor = MasterToken\n"
        "    MasterToken = object\n"
        "    return Constructor(email='e', android_id='a', secret='s')\n",
    ],
)
def test_constructor_owner_collector_bites_aliases_and_every_deferred_scope(
    tmp_path: Path, body: str
) -> None:
    src_root = tmp_path / "notebooklm"
    source = src_root / "_auth" / "consumer.py"
    source.parent.mkdir(parents=True)
    source.write_text(body, encoding="utf-8")
    assert _master_token_constructor_owners(src_root)


@pytest.mark.parametrize(
    ("relative", "body"),
    [
        pytest.param(
            "consumer.py",
            "import notebooklm._auth.master_token_types as tokens\n",
            id="aliased-module-import",
        ),
        pytest.param(
            "consumer.py",
            "from notebooklm._auth.master_token_types import MasterToken as Credential\n",
            id="aliased-from-import",
        ),
        pytest.param(
            "consumer.py",
            "from notebooklm._auth import master_token_types as tokens\n",
            id="package-from-import",
        ),
        pytest.param(
            "consumer.py",
            "def later():\n    from notebooklm._auth.master_token_types import MasterToken\n",
            id="deferred-import",
        ),
        pytest.param(
            "_auth/consumer.py",
            "from .master_token_types import MasterToken\n",
            id="auth-relative-level-one",
        ),
        pytest.param(
            "_auth/consumer.py",
            "from . import master_token_types\n",
            id="auth-relative-package-member",
        ),
        pytest.param(
            "__init__.py",
            "from ._auth.master_token_types import MasterToken\n",
            id="top-package-relative-level-one",
        ),
        pytest.param(
            "nested/consumer.py",
            "from .._auth.master_token_types import MasterToken\n",
            id="nested-relative-level-two",
        ),
        pytest.param(
            "nested/deeper/consumer.py",
            "from ..._auth.master_token_types import MasterToken\n",
            id="deep-relative-level-three",
        ),
        pytest.param(
            "consumer.py",
            "import importlib\n"
            "tokens = importlib.import_module('notebooklm._auth.' + 'master_token_types')\n",
            id="dynamic-import-concatenated-target",
        ),
        pytest.param(
            "consumer.py",
            "from importlib import import_module as load\n"
            "tokens = load('.'.join(('notebooklm', '_auth', 'master_token_types')))\n",
            id="aliased-dynamic-import-split-target",
        ),
        pytest.param(
            "consumer.py",
            "import notebooklm._auth as auth\ntokens = getattr(auth, 'master_' + 'token_types')\n",
            id="dynamic-module-access",
        ),
        pytest.param(
            "consumer.py",
            "def build():\n    return _master_token_from_legacy_record({})\n",
            id="bare-codec-call",
        ),
        pytest.param(
            "consumer.py",
            "value = notebooklm._auth.master_token_types.MasterToken\n",
            id="qualified-access",
        ),
    ],
)
def test_external_consumer_detector_bites_on_fake_source_tree(
    tmp_path: Path, relative: str, body: str
) -> None:
    src_root = tmp_path / "notebooklm"
    source = src_root / relative
    source.parent.mkdir(parents=True)
    source.write_text(body, encoding="utf-8")
    assert _production_consumers(src_root)


def test_no_other_production_module_duplicates_the_value_or_codec_owner() -> None:
    duplicates: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == MODULE_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in _CODEC_NAMES:
                duplicates.append(f"{path.relative_to(SRC_ROOT)}:{node.name}")
    assert duplicates == []
