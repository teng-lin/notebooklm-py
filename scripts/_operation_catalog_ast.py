"""AST-derived public API, transport authority, and recency audits."""

from __future__ import annotations

import ast
import hashlib
import inspect
import typing
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from notebooklm._operations import Operation
from notebooklm.client import NotebookLMClient
from notebooklm.rpc import RPCMethod

if __package__:
    from ._operation_catalog_authorities import (
        _GET_SOURCES,
        _GET_TYPED,
        APP_AUTHORITY_SOURCE_CONTRACTS,
        APP_OPERATION_AUTHORITIES,
        NON_RPC_AUTHORITY_RULES,
        NON_RPC_SOURCE_CONTRACTS,
        RECENCY_CONTRACTS,
        SHARED_RPC_AUTHORITY_RULES,
        _rules,
    )
    from ._operation_catalog_specs import (
        NATIVE_BINDING_DISPOSITIONS,
        OPERATION_SPECS,
        NativeKey,
        OperationSpec,
        _b,
        _p,
        native_key_text,
    )
else:  # pragma: no cover - direct script execution
    from _operation_catalog_authorities import (
        _GET_SOURCES,
        _GET_TYPED,
        APP_AUTHORITY_SOURCE_CONTRACTS,
        APP_OPERATION_AUTHORITIES,
        NON_RPC_AUTHORITY_RULES,
        NON_RPC_SOURCE_CONTRACTS,
        RECENCY_CONTRACTS,
        SHARED_RPC_AUTHORITY_RULES,
        _rules,
    )
    from _operation_catalog_specs import (
        NATIVE_BINDING_DISPOSITIONS,
        OPERATION_SPECS,
        NativeKey,
        OperationSpec,
        _b,
        _p,
        native_key_text,
    )

if typing.TYPE_CHECKING:
    from scripts.audit_public_api_compat import CLIENT_NAMESPACE_ATTRIBUTES
elif __package__:
    from .audit_public_api_compat import CLIENT_NAMESPACE_ATTRIBUTES
else:  # pragma: no cover - direct script execution
    from audit_public_api_compat import CLIENT_NAMESPACE_ATTRIBUTES

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
APP_ROOT = SRC_ROOT / "_app"
_native_key_text = native_key_text


def _literal_string(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _qualname(stack: Sequence[str]) -> str:
    return ".".join(stack) if stack else "<module>"


def _attribute_parts(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


# Binding rows (P9.2). A module-level ``NAME = CodecBinding(definition=X_DEF, native=...)`` or
# ``CustomBinding(..., native=(spec, ...))`` — directly, inside a ``{Operation.X: row}`` table,
# or through a helper call that forwards a ``native=`` keyword — is an execution-authority
# site exactly like a ``_rpc_call(RPCMethod.X, ...)`` call site. The row's ``NativeCallSpec``
# is the sole authority for the natives it dispatches, so the walker reads the spec literally
# and reports anything it cannot resolve statically as an unresolved dispatch.
_ROW_CONSTRUCTORS = frozenset({"CodecBinding", "CustomBinding"})
_SPEC_CONSTRUCTORS = frozenset({"NativeCallSpec", "NativeChoice"})
_SPEC_FACTORIES = frozenset({"constant", "keyed"})

NativeName = tuple[str, str | None]


@dataclass(frozen=True, slots=True)
class BindingRowSite:
    """One binding row recognised at module scope."""

    site: str
    operation: Operation | None
    natives: tuple[NativeName, ...]
    unresolved: bool = False


def _rpc_method_member(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    parts = _attribute_parts(node)
    if len(parts) >= 2 and parts[-2] == "RPCMethod" and parts[-1] in RPCMethod.__members__:
        return parts[-1]
    return None


def _variant_literal(node: ast.AST | None) -> tuple[str | None, bool]:
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return None, True
    literal = _literal_string(node)
    return literal, literal is not None


def _call_argument(call: ast.Call, index: int, keyword: str) -> ast.AST | None:
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    return call.args[index] if 0 <= index < len(call.args) else None


def _is_spec_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    parts = _attribute_parts(node.func)
    if not parts:
        return False
    if parts[-1] in _SPEC_CONSTRUCTORS:
        return True
    return len(parts) >= 2 and parts[-2] == "NativeCallSpec" and parts[-1] in _SPEC_FACTORIES


def _is_row_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or _is_spec_call(node):
        return False
    parts = _attribute_parts(node.func)
    if parts and parts[-1] in _ROW_CONSTRUCTORS:
        return True
    return any(keyword.arg == "native" for keyword in node.keywords)


def _operation_member(node: ast.AST) -> str | None:
    parts = _attribute_parts(node)
    if len(parts) >= 2 and parts[-2] == "Operation" and parts[-1] in Operation.__members__:
        return parts[-1]
    return None


_DEFINITION_OPERATIONS: dict[str, Operation | None] = {}


def _operation_for_definition(name: str) -> Operation | None:
    """Map a ``<NAME>_DEF`` record name to its closed operation, or ``None``."""
    if name not in _DEFINITION_OPERATIONS:
        from notebooklm import _records
        from notebooklm._operations import OperationDef

        definition = getattr(_records, name, None)
        _DEFINITION_OPERATIONS[name] = (
            definition.key if isinstance(definition, OperationDef) else None
        )
    return _DEFINITION_OPERATIONS[name]


def _definition_name(call: ast.Call) -> str | None:
    node = _call_argument(call, 0, "definition")
    if node is None:
        return None
    parts = _attribute_parts(node)
    if parts and parts[-1].endswith("_DEF"):
        return parts[-1]
    return None


class _SpecResolver:
    """Resolve ``NativeCallSpec``/``NativeChoice`` literals to ``(method, variant)`` names."""

    def __init__(self, spec_bindings: Mapping[str, tuple[NativeName, ...] | None]) -> None:
        self._spec_bindings = spec_bindings
        self.natives: list[NativeName] = []
        self.unresolved: str | None = None

    def _fail(self, field: str) -> None:
        if self.unresolved is None:
            self.unresolved = field

    def _choice(self, method_node: ast.AST | None, variant_node: ast.AST | None) -> None:
        method = _rpc_method_member(method_node)
        if method is None:
            self._fail("method")
            return
        variant, resolved = _variant_literal(variant_node)
        if not resolved:
            self._fail("operation_variant")
            return
        self.natives.append((method, variant))

    def resolve(self, node: ast.AST | None) -> None:
        if node is None:
            self._fail("method")
        elif isinstance(node, ast.Name):
            bound = self._spec_bindings.get(node.id)
            if bound is None:
                self._fail("method")
            else:
                self.natives.extend(bound)
        elif isinstance(node, (ast.Tuple, ast.List)):
            if not node.elts:
                self._fail("method")
            for element in node.elts:
                self.resolve(element)
        elif isinstance(node, ast.Call):
            self._resolve_call(node)
        else:
            self._fail("method")

    def _resolve_call(self, call: ast.Call) -> None:
        parts = _attribute_parts(call.func)
        last = parts[-1] if parts else ""
        if (
            last == "NativeChoice"
            or last == "constant"
            and len(parts) >= 2
            and parts[-2] == "NativeCallSpec"
        ):
            self._choice(_call_argument(call, 0, "method"), _call_argument(call, 1, "variant"))
        elif last == "keyed" and len(parts) >= 2 and parts[-2] == "NativeCallSpec":
            has_selector_keyword = any(item.arg == "selector" for item in call.keywords)
            choices = list(call.args if has_selector_keyword else call.args[1:])
            if not choices:
                self._fail("method")
            for choice in choices:
                self.resolve(choice)
        elif last == "NativeCallSpec":
            self.resolve(_call_argument(call, 0, "choices"))
        else:
            self._fail("method")


def _iter_row_calls(
    node: ast.AST, key_operation: str | None = None
) -> typing.Iterator[tuple[ast.Call, str | None]]:
    """Yield ``(row_call, operation_member_from_table_key)`` pairs found under ``node``."""
    if _is_row_call(node):
        assert isinstance(node, ast.Call)
        yield node, key_operation
        return
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            member = _operation_member(key) if key is not None else None
            yield from _iter_row_calls(value, member or key_operation)
        return
    if isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            yield from _iter_row_calls(element, key_operation)
        return
    if isinstance(node, ast.Call) and not _is_spec_call(node):
        for argument in node.args:
            yield from _iter_row_calls(argument, key_operation)
        for keyword in node.keywords:
            yield from _iter_row_calls(keyword.value, key_operation)


class _ReferenceCollector(ast.NodeVisitor):
    """Collect RPCMethod references and public facade calls with owners."""

    def __init__(self, relative_path: str, namespace_names: set[str]) -> None:
        self.relative_path = relative_path
        self.namespace_names = namespace_names
        self.stack: list[str] = []
        self.bindings: list[dict[str, set[str]]] = []
        self.literal_bindings: list[dict[str, str]] = []
        self.rpc_references: list[tuple[str, str]] = []
        self.rpc_calls: list[tuple[str, str | None, str]] = []
        self.unresolved_rpc_calls: list[tuple[str, str]] = []
        self.public_calls: list[tuple[str, str]] = []
        self.binding_rows: list[BindingRowSite] = []
        self.spec_bindings: dict[str, tuple[NativeName, ...] | None] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.stack.append(node.name)
        self.bindings.append(defaultdict(set))
        literals: dict[str, str] = {}
        positional = [*node.args.posonlyargs, *node.args.args]
        if node.args.defaults:
            for arg, default in zip(
                positional[-len(node.args.defaults) :], node.args.defaults, strict=True
            ):
                if (value := _literal_string(default)) is not None:
                    literals[arg.arg] = value
        for arg, kw_default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True):
            if kw_default is not None and (value := _literal_string(kw_default)) is not None:
                literals[arg.arg] = value
        self.literal_bindings.append(literals)
        self.generic_visit(node)
        self.literal_bindings.pop()
        self.bindings.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        parts = _attribute_parts(node)
        if len(parts) >= 2 and parts[-2] == "RPCMethod" and parts[-1] in RPCMethod.__members__:
            self.rpc_references.append((parts[-1], _qualname(self.stack)))
        self.generic_visit(node)

    def _visit_module_assignment(self, target: ast.AST, value: ast.AST) -> None:
        """Record module-level native specs and binding rows as execution-authority sites."""
        if not isinstance(target, ast.Name):
            return
        if _is_spec_call(value):
            resolver = _SpecResolver(self.spec_bindings)
            resolver.resolve(value)
            self.spec_bindings[target.id] = (
                None if resolver.unresolved is not None else tuple(resolver.natives)
            )
            return
        for index, (row, key_member) in enumerate(_iter_row_calls(value)):
            definition = _definition_name(row)
            operation = _operation_for_definition(definition) if definition is not None else None
            unresolved: str | None = None
            if definition is not None and operation is None:
                unresolved = "definition"
            if key_member is not None:
                if operation is None:
                    operation = Operation[key_member]
                elif operation.name != key_member:
                    unresolved = "definition"
            if operation is None:
                unresolved = "definition"
            resolver = _SpecResolver(self.spec_bindings)
            resolver.resolve(_call_argument(row, -1, "native"))
            if resolver.unresolved is not None and unresolved is None:
                unresolved = resolver.unresolved
            # A nested row is named by the table key the reader sees, so a key that
            # disagrees with the row's definition is reported at the offending entry.
            if row is value:
                site = target.id
            elif key_member is not None:
                site = f"{target.id}.{key_member}"
            elif operation is not None:
                site = f"{target.id}.{operation.name}"
            else:
                site = f"{target.id}.{index}"
            if unresolved is not None:
                self.unresolved_rpc_calls.append((site, unresolved))
            self.binding_rows.append(
                BindingRowSite(
                    site=site,
                    operation=operation,
                    natives=tuple(resolver.natives),
                    unresolved=unresolved is not None,
                )
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        if not self.bindings and not self.stack:
            for target in node.targets:
                self._visit_module_assignment(target, node.value)
        if self.bindings:
            methods = {
                parts[-1]
                for item in ast.walk(node.value)
                if isinstance(item, ast.Attribute)
                and len(parts := _attribute_parts(item)) >= 2
                and parts[-2] == "RPCMethod"
                and parts[-1] in RPCMethod.__members__
            }
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.bindings[-1][target.id].update(methods)
                    if (literal := _literal_string(node.value)) is not None:
                        self.literal_bindings[-1][target.id] = literal
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if not self.bindings and not self.stack and node.value is not None:
            self._visit_module_assignment(node.target, node.value)
        if self.bindings and isinstance(node.target, ast.Name) and node.value is not None:
            methods = {
                parts[-1]
                for item in ast.walk(node.value)
                if isinstance(item, ast.Attribute)
                and len(parts := _attribute_parts(item)) >= 2
                and parts[-2] == "RPCMethod"
                and parts[-1] in RPCMethod.__members__
            }
            self.bindings[-1][node.target.id].update(methods)
            if (literal := _literal_string(node.value)) is not None:
                self.literal_bindings[-1][node.target.id] = literal
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        parts = _attribute_parts(node.func)
        if parts and parts[-1] in {"rpc_call", "_rpc_call"}:
            method_node = (
                node.args[0]
                if node.args
                else next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "method"),
                    None,
                )
            )
            method_names = {
                item_parts[-1]
                for item in (ast.walk(method_node) if method_node is not None else ())
                if isinstance(item, ast.Attribute)
                and len(item_parts := _attribute_parts(item)) >= 2
                and item_parts[-2] == "RPCMethod"
                and item_parts[-1] in RPCMethod.__members__
            }
            if isinstance(method_node, ast.Name):
                for scope in reversed(self.bindings):
                    if method_node.id in scope:
                        method_names.update(scope[method_node.id])
                        break
            variant: str | None = None
            variant_resolved = True
            for keyword in node.keywords:
                if keyword.arg == "operation_variant":
                    variant = _literal_string(keyword.value)
                    if (
                        isinstance(keyword.value, ast.Constant) and keyword.value.value is None
                    ) or variant is not None:
                        variant_resolved = True
                    elif isinstance(keyword.value, ast.Name):
                        variant_resolved = False
                        for literal_scope in reversed(self.literal_bindings):
                            if keyword.value.id in literal_scope:
                                variant = literal_scope[keyword.value.id]
                                variant_resolved = True
                                break
                    else:
                        variant_resolved = False
            owner = _qualname(self.stack)
            if method_node is None or not method_names:
                self.unresolved_rpc_calls.append((owner, "method"))
            elif not variant_resolved:
                self.unresolved_rpc_calls.append((owner, "operation_variant"))
            for method_name in method_names:
                self.rpc_calls.append((method_name, variant, owner))
        for index, part in enumerate(parts[:-1]):
            if part in self.namespace_names:
                self.public_calls.append((f"{part}.{parts[index + 1]}", _qualname(self.stack)))
                break
        self.generic_visit(node)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def collect_public_client_namespaces() -> dict[str, type[object]]:
    """Return every public class-typed namespace annotation on the client."""

    hints = typing.get_type_hints(NotebookLMClient)
    return {
        namespace: cls
        for namespace, cls in sorted(hints.items())
        if not namespace.startswith("_") and inspect.isclass(cls)
    }


def audit_public_namespace_contract() -> list[str]:
    """Require compat-audit and live client namespace discovery to agree exactly."""

    discovered = set(collect_public_client_namespaces())
    expected = set(CLIENT_NAMESPACE_ATTRIBUTES)
    if discovered == expected:
        return []
    return [
        "public client namespaces disagree with CLIENT_NAMESPACE_ATTRIBUTES: "
        f"missing={sorted(discovered - expected)}, stale={sorted(expected - discovered)}"
    ]


def collect_public_namespace_methods() -> dict[str, str]:
    """Return every public callable on each annotated client namespace.

    Reading class annotations avoids constructing a client/HTTP session.  The
    MRO walk deliberately includes inherited public helpers, which is why
    ``chat.set_bound_loop`` and ``chat.reset_after_open`` cannot disappear from
    this inventory.
    """
    methods: dict[str, str] = {}
    for namespace, cls in collect_public_client_namespaces().items():
        for base in reversed(cls.__mro__):
            if base is object or not base.__module__.startswith("notebooklm"):
                continue
            for name, raw in vars(base).items():
                if name.startswith("_"):
                    continue
                target = raw.__func__ if isinstance(raw, (classmethod, staticmethod)) else raw
                if inspect.isfunction(target) or inspect.ismethoddescriptor(target):
                    methods[f"{namespace}.{name}"] = f"{base.__module__}.{base.__qualname__}"
    return dict(sorted(methods.items()))


def collect_public_client_members() -> dict[str, dict[str, str]]:
    """Inventory every public root-client method/property across its MRO."""
    members: dict[str, dict[str, str]] = {}
    for base in reversed(NotebookLMClient.__mro__):
        if base is object or not base.__module__.startswith("notebooklm"):
            continue
        for name, raw in vars(base).items():
            if name.startswith("_"):
                continue
            kind: str | None = None
            if isinstance(raw, property):
                kind = "property"
            elif isinstance(raw, classmethod):
                kind = "classmethod"
            elif isinstance(raw, staticmethod):
                kind = "staticmethod"
            elif inspect.isfunction(raw) or inspect.ismethoddescriptor(raw):
                kind = "method"
            if kind is not None:
                members[name] = {
                    "declared_by": f"{base.__module__}.{base.__qualname__}",
                    "kind": kind,
                }
    return dict(sorted(members.items()))


def collect_app_callers(namespace_names: set[str] | None = None) -> dict[str, list[str]]:
    """AST-walk ``_app`` and map namespace method calls to their owners."""
    if namespace_names is None:
        namespace_names = {name.split(".", 1)[0] for name in collect_public_namespace_methods()}
    callers: dict[str, set[str]] = defaultdict(set)
    for path in sorted(APP_ROOT.glob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        collector = _ReferenceCollector(relative, namespace_names)
        collector.visit(_parse(path))
        for method, owner in collector.public_calls:
            callers[method].add(f"{relative}:{owner}")
    for site, methods in collect_dynamic_app_dispatches().items():
        for method in methods:
            callers[method].add(site)
    return {method: sorted(owners) for method, owners in sorted(callers.items())}


def _assigned_string_dict(tree: ast.Module, name: str) -> set[str]:
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        if isinstance(statement.value, ast.Dict):
            return {
                value
                for node in statement.value.values
                if (value := _literal_string(node)) is not None
            }
    return set()


def _download_registry_attrs() -> set[str]:
    tree = _parse(APP_ROOT / "download_specs.py")
    attrs: set[str] = set()
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if _attribute_parts(call.func)[-1:] != ("DownloadRegistryEntry",):
            continue
        for keyword in call.keywords:
            if keyword.arg == "download_attr" and (value := _literal_string(keyword.value)):
                attrs.add(value)
    return attrs


def collect_dynamic_app_dispatches() -> dict[str, list[str]]:
    """Resolve the two reviewed data-driven ``_app`` facade dispatch tables."""
    generation_methods = _assigned_string_dict(_parse(APP_ROOT / "generate.py"), "_KIND_TO_METHOD")
    download_methods = _download_registry_attrs()
    return {
        "_app/download.py:_bind_download_fn": sorted(
            f"artifacts.{method}" for method in download_methods
        ),
        "_app/generate.py:execute_generation": sorted(
            f"artifacts.{method}" for method in generation_methods
        ),
    }


def collect_unresolved_app_dispatches() -> list[str]:
    """Find dynamic namespace ``getattr`` sites not covered by a derived registry."""
    known = set(collect_dynamic_app_dispatches())
    unresolved: set[str] = set()
    namespace_names = {name.split(".", 1)[0] for name in collect_public_namespace_methods()}

    class Visitor(ast.NodeVisitor):
        def __init__(self, relative: str) -> None:
            self.relative = relative
            self.stack: list[str] = []

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def visit_Call(self, node: ast.Call) -> None:
            if _attribute_parts(node.func)[-1:] == ("getattr",) and len(node.args) >= 2:
                base = _attribute_parts(node.args[0])
                if (
                    any(part in namespace_names for part in base)
                    and _literal_string(node.args[1]) is None
                ):
                    site = f"{self.relative}:{_qualname(self.stack)}"
                    if site not in known:
                        unresolved.add(site)
            self.generic_visit(node)

    for path in sorted(APP_ROOT.glob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        Visitor(relative).visit(_parse(path))
    return sorted(unresolved)


def collect_rpc_references() -> dict[RPCMethod, dict[str, list[str]]]:
    """AST-walk production code and classify current native references."""
    inventory: dict[RPCMethod, dict[str, set[str]]] = {
        method: defaultdict(set) for method in RPCMethod
    }
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative in {"rpc/types.py", "_idempotency_policy.py"}:
            continue
        collector = _ReferenceCollector(relative, set())
        collector.visit(_parse(path))
        if relative.startswith(("_row_adapters/", "_web/codec/")):
            role = "decoders"
        elif relative.startswith("_types/"):
            role = "projectors"
        elif relative.startswith("rpc/"):
            role = "protocol_support"
        else:
            role = "support_references"
        for method_name, owner in collector.rpc_references:
            inventory[RPCMethod[method_name]][role].add(f"{relative}:{owner}")
        if role == "support_references":
            for method_name, _variant, owner in collector.rpc_calls:
                inventory[RPCMethod[method_name]]["execution_authorities"].add(
                    f"{relative}:{owner}"
                )
            for row in collector.binding_rows:
                for method_name, _variant in row.natives:
                    inventory[RPCMethod[method_name]]["execution_authorities"].add(
                        f"{relative}:{row.site}"
                    )
    return {
        method: {role: sorted(sites) for role, sites in sorted(roles.items())}
        for method, roles in inventory.items()
    }


_NATIVE_SITE_EXCLUDED_PREFIXES = ("_row_adapters/", "_types/", "rpc/")
_NATIVE_SITE_EXCLUDED_MODULES = frozenset({"_idempotency_policy.py"})


def _iter_native_site_modules(root: Path) -> typing.Iterator[tuple[str, Path]]:
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if (
            relative.startswith(_NATIVE_SITE_EXCLUDED_PREFIXES)
            or relative in _NATIVE_SITE_EXCLUDED_MODULES
        ):
            continue
        yield relative, path


def collect_native_execution_sites(root: Path = SRC_ROOT) -> dict[NativeKey, list[str]]:
    """Return direct transport-reaching call sites per native method/variant.

    A site is a ``rpc_call``/``_rpc_call`` invocation naming ``RPCMethod.X`` or a
    module-level binding row whose ``NativeCallSpec`` declares ``RPCMethod.X``.
    """
    sites: dict[NativeKey, set[str]] = defaultdict(set)
    for relative, path in _iter_native_site_modules(root):
        collector = _ReferenceCollector(relative, set())
        collector.visit(_parse(path))
        for method_name, variant, owner in collector.rpc_calls:
            site = f"{relative}:{owner}"
            if site not in INERT_P1_WEB_HANDLERS:
                sites[(RPCMethod[method_name], variant)].add(site)
        for row in collector.binding_rows:
            site = f"{relative}:{row.site}"
            if site in INERT_P1_WEB_HANDLERS:
                continue
            for method_name, variant in row.natives:
                sites[(RPCMethod[method_name], variant)].add(site)
    return {
        key: sorted(values)
        for key, values in sorted(sites.items(), key=lambda item: _native_key_text(item[0]))
    }


def collect_binding_rows(root: Path = SRC_ROOT) -> list[BindingRowSite]:
    """Return every module-level binding row with its full ``file.py:NAME`` site."""
    rows: list[BindingRowSite] = []
    for relative, path in _iter_native_site_modules(root):
        collector = _ReferenceCollector(relative, set())
        collector.visit(_parse(path))
        rows.extend(
            BindingRowSite(
                site=f"{relative}:{row.site}",
                operation=row.operation,
                natives=row.natives,
                unresolved=row.unresolved,
            )
            for row in collector.binding_rows
        )
    return rows


def collect_binding_sites(root: Path = SRC_ROOT) -> set[str]:
    """Return binding-row sites, accepted wherever a function site is accepted."""
    return {row.site for row in collect_binding_rows(root)}


def derive_row_authorities(
    root: Path = SRC_ROOT,
) -> dict[tuple[Operation, NativeKey], tuple[str, ...]]:
    """Return the ``(operation, native) -> sites`` allocation every binding row declares.

    A hoist or conversion PR replaces a hand-written ``SHARED_RPC_AUTHORITY_RULES`` entry with
    the derived site for its operation; the audit keeps comparing both directions.
    """
    allocation: dict[tuple[Operation, NativeKey], set[str]] = defaultdict(set)
    for row in collect_binding_rows(root):
        if row.operation is None:
            continue
        for method_name, variant in row.natives:
            allocation[(row.operation, (RPCMethod[method_name], variant))].add(row.site)
    return {
        key: tuple(sorted(sites))
        for key, sites in sorted(
            allocation.items(),
            key=lambda item: (item[0][0].value, _native_key_text(item[0][1])),
        )
    }


def audit_row_bindings(rows: Sequence[BindingRowSite] | None = None) -> list[str]:
    """Fail closed when a binding row's declared natives disagree with the policy ledger."""
    from notebooklm._web.policy import WEB_CALL_POLICY_BINDINGS

    errors: list[str] = []
    seen: dict[Operation, str] = {}
    for row in collect_binding_rows() if rows is None else rows:
        if row.operation is None:
            errors.append(f"binding row {row.site} names no resolvable operation definition")
            continue
        operation = row.operation
        if operation in seen:
            errors.append(
                f"{operation.value} has more than one binding row: {seen[operation]}, {row.site}"
            )
        seen[operation] = row.site
        ledger = WEB_CALL_POLICY_BINDINGS.get(operation)
        if ledger is None:
            errors.append(
                f"binding row {row.site} for {operation.value} has no web call-policy ledger entry"
            )
            continue
        if row.unresolved:
            continue
        declared = {(RPCMethod[method], variant) for method, variant in row.natives}
        expected = {(native.method, native.variant) for native in ledger.native_bindings}
        if declared != expected and ledger.known_divergence is None:
            errors.append(
                f"{operation.value} binding row {row.site} declares natives "
                f"{sorted(_native_key_text(key) for key in declared)} but the policy ledger "
                f"expects {sorted(_native_key_text(key) for key in expected)}"
            )
    return errors


GENERIC_RPC_FORWARDERS = frozenset(
    {
        "_web/backend.py:WebRpcBackend.public_rpc_call",
        "_web/transport.py:WebTransport.call",
        "_notebooks.py:NotebooksAPI._rpc_call",
        "client.py:NotebookLMClient.rpc_call",
    }
)

# P1 constructed the semantic web backend with every handler inert. P2 removes
# each handler from this set in the same slice that delegates its facade. The
# notebook/source read and mutation handlers plus plain-note CRUD are live. The
# shared forwarder remains inert until every registered operation delegates.
INERT_P1_WEB_FORWARDERS = frozenset(
    {
        "_web/transport.py:WebTransport.call",
    }
)
INERT_P1_WEB_HANDLERS: frozenset[str] = frozenset()
INERT_P1_WEB_SITES = INERT_P1_WEB_FORWARDERS | INERT_P1_WEB_HANDLERS

# Each live slice deletes one handler exemption and admits the corresponding
# service/codec/record/projector imports in the same bounded delegation slice.
# These exact imports are the complete production semantic-backend dataflow.
REVIEWED_BACKEND_IMPORTS = frozenset(
    {
        # P9.4b: the source-add family as custom rows over the sources codec.
        ("_web/bindings/sources.py", "_backend", "BackendContractError"),
        ("_web/bindings/sources.py", "_backend", "BackendError"),
        ("_web/bindings/sources.py", "_backend", "BackendErrorReason"),
        ("_web/bindings/sources.py", "_binding", "CodecPayload"),
        ("_web/bindings/sources.py", "_binding", "CustomBinding"),
        ("_web/bindings/sources.py", "_binding", "ErrorMode"),
        ("_web/bindings/sources.py", "_binding", "RowInvoker"),
        ("_web/bindings/sources.py", "_projectors", "project_source"),
        ("_web/bindings/sources.py", "_records", "SOURCE_ADD_DRIVE_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_ADD_FILE_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_ADD_TEXT_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_ADD_URL_BATCH_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_ADD_URL_DEF"),
        ("_web/bindings/sources.py", "_records", "SourceAddCommitState"),
        ("_web/bindings/sources.py", "_records", "SourceAddDriveInput"),
        ("_web/bindings/sources.py", "_records", "SourceAddDriveResult"),
        ("_web/bindings/sources.py", "_records", "SourceAddFileInput"),
        ("_web/bindings/sources.py", "_records", "SourceAddFileResult"),
        ("_web/bindings/sources.py", "_records", "SourceAddTextInput"),
        ("_web/bindings/sources.py", "_records", "SourceAddTextResult"),
        ("_web/bindings/sources.py", "_records", "SourceAddTitleState"),
        ("_web/bindings/sources.py", "_records", "SourceAddUrlBatchInput"),
        ("_web/bindings/sources.py", "_records", "SourceAddUrlBatchResult"),
        ("_web/bindings/sources.py", "_records", "SourceAddUrlInput"),
        ("_web/bindings/sources.py", "_records", "SourceAddUrlReceipt"),
        ("_web/bindings/sources.py", "_records", "SourceAddUrlResult"),
        ("_web/bindings/sources.py", "_records", "SourceFileInputKind"),
        ("_web/bindings/sources.py", "_records", "SourceFileRegistrationRecord"),
        ("_web/bindings/sources.py", "_records", "SourceRecord"),
        ("_web/bindings/sources.py", "_records", "SourceUrlBatchItemRecord"),
        ("_web/bindings/sources.py", "codec", "settings"),
        # P9.4b: notebook and mind-map/catalog composites became custom rows; the
        # legacy note family reaches the transport through the row-scoped caller.
        ("_web/bindings/_invoker_caller.py", "_backend", "BackendContractError"),
        ("_web/bindings/_invoker_caller.py", "_backend", "BackendDeadlineExceededError"),
        ("_web/bindings/_invoker_caller.py", "_binding", "CodecPayload"),
        ("_web/bindings/_invoker_caller.py", "_binding", "RowInvoker"),
        ("_web/bindings/mind_maps.py", "_binding", "CustomBinding"),
        ("_web/bindings/mind_maps.py", "_binding", "RowInvoker"),
        ("_web/bindings/mind_maps.py", "_note_service", "LegacyNoteBackedService"),
        ("_web/bindings/mind_maps.py", "_records", "ARTIFACT_GENERATE_MIND_MAP_DEF"),
        ("_web/bindings/mind_maps.py", "_records", "ARTIFACT_GET_DEF"),
        ("_web/bindings/mind_maps.py", "_records", "ARTIFACT_LIST_DEF"),
        ("_web/bindings/mind_maps.py", "_records", "ArtifactGetInput"),
        ("_web/bindings/mind_maps.py", "_records", "ArtifactGetResult"),
        ("_web/bindings/mind_maps.py", "_records", "ArtifactListInput"),
        ("_web/bindings/mind_maps.py", "_records", "ArtifactListResult"),
        ("_web/bindings/mind_maps.py", "_records", "ArtifactRecord"),
        ("_web/bindings/mind_maps.py", "_records", "MIND_MAP_GENERATE_INTERACTIVE_DEF"),
        ("_web/bindings/mind_maps.py", "_records", "MIND_MAP_GENERATE_NOTE_DEF"),
        ("_web/bindings/mind_maps.py", "_records", "MindMapGenerateInput"),
        ("_web/bindings/mind_maps.py", "_records", "MindMapGenerateInteractiveInput"),
        ("_web/bindings/mind_maps.py", "_records", "MindMapGenerateInteractiveResult"),
        ("_web/bindings/mind_maps.py", "_records", "MindMapGenerateNoteInput"),
        ("_web/bindings/mind_maps.py", "_records", "MindMapGenerateNoteResult"),
        ("_web/bindings/mind_maps.py", "_records", "MindMapGenerateResult"),
        ("_web/bindings/mind_maps.py", "codec", "artifacts"),
        ("_web/bindings/mind_maps.py", "codec", "notebooks"),
        ("_web/bindings/notebooks.py", "_backend", "BackendError"),
        ("_web/bindings/notebooks.py", "_backend", "BackendErrorReason"),
        ("_web/bindings/notebooks.py", "_backend", "mark_backend_outcome_unknown"),
        ("_web/bindings/notebooks.py", "_binding", "CodecPayload"),
        ("_web/bindings/notebooks.py", "_binding", "CustomBinding"),
        ("_web/bindings/notebooks.py", "_binding", "RowInvoker"),
        ("_web/bindings/notebooks.py", "_records", "NOTEBOOK_CREATE_DEF"),
        ("_web/bindings/notebooks.py", "_records", "NOTEBOOK_UPDATE_DEF"),
        ("_web/bindings/notebooks.py", "_records", "NotebookCreateInput"),
        ("_web/bindings/notebooks.py", "_records", "NotebookCreateResult"),
        ("_web/bindings/notebooks.py", "_records", "NotebookRecord"),
        ("_web/bindings/notebooks.py", "_records", "NotebookUpdateInput"),
        ("_web/bindings/notebooks.py", "_records", "NotebookUpdateResult"),
        ("_web/bindings/notebooks.py", "codec", "settings"),
        ("_web/codec/mind_maps.py", "_records", "MindMapGenerateInput"),
        ("_web/codec/mind_maps.py", "_records", "MindMapGenerateInteractiveInput"),
        ("_web/codec/mind_maps.py", "_records", "MindMapGenerateNoteInput"),
        ("_web/codec/mind_maps.py", "_records", "MindMapGenerateNoteResult"),
        ("_web/codec/notebooks.py", "_backend", "BackendError"),
        ("_web/codec/notebooks.py", "_backend", "BackendErrorReason"),
        ("_web/codec/notebooks.py", "_records", "NotebookCreateInput"),
        ("_web/codec/notebooks.py", "_records", "NotebookUpdateInput"),
        ("_web/codec/notebooks.py", "_records", "NotebookUpdateResult"),
        ("_binding.py", "_backend", "BackendContractError"),
        ("_binding.py", "_backend", "BackendError"),
        ("_web/backend.py", "_binding", "Binding"),
        ("_web/backend.py", "_binding", "BindingAuditError"),
        ("_web/backend.py", "_binding", "BindingTable"),
        ("_web/backend.py", "_binding", "OperationDisposition"),
        ("_web/backend.py", "_binding", "ResolvedHandlerBinding"),
        ("_web/backend.py", "_binding", "audit_bindings"),
        ("_web/backend.py", "_binding", "invoke_binding"),
        ("_web/backend.py", "_binding", "row_invoker"),
        ("_web/registry.py", "_binding", "OperationDisposition"),
        ("_web/backend.py", "registry", "WebOperationBinding"),
        ("_web/transport.py", "_backend", "BackendContractError"),
        ("_web/transport.py", "_backend", "BackendDeadlineExceededError"),
        ("_web/transport.py", "_binding", "CodecPayload"),
        ("_web/transport.py", "_binding", "NativeChoice"),
        ("_web/transport.py", "_binding", "StreamPayload"),
        ("_web/transport.py", "_binding", "StreamSpec"),
        ("_artifact/listing.py", "_projectors", "project_artifact"),
        ("_artifacts.py", "_backend", "BackendAdapter"),
        ("_artifacts.py", "_backend", "BackendContractError"),
        ("_artifacts.py", "_backend", "BackendError"),
        ("_artifacts.py", "_backend", "BackendErrorReason"),
        ("_artifacts.py", "_backend_compat", "project_backend_call"),
        ("_artifacts.py", "_backend_compat", "project_backend_error"),
        ("_artifacts.py", "_projectors", "project_artifact"),
        ("_artifacts.py", "_projectors", "project_generation_status"),
        ("_artifacts.py", "_projectors", "project_report_suggestion"),
        ("_artifacts.py", "_records", "InfographicGenerateInput"),
        ("_artifacts.py", "_records", "ArtifactDeleteInput"),
        ("_artifacts.py", "_records", "ArtifactRecord"),
        ("_artifacts.py", "_records", "ArtifactRenameInput"),
        ("_artifacts.py", "_records", "ArtifactRepresentationRecord"),
        ("_artifacts.py", "_records", "ArtifactRetryInput"),
        ("_artifacts.py", "_records", "ArtifactReviseSlideInput"),
        ("_artifacts.py", "_records", "ArtifactSuggestReportsInput"),
        ("_artifacts.py", "_records", "MindMapRepresentationRecord"),
        ("_artifacts.py", "_records", "SlideDeckGenerateInput"),
        ("_artifacts.py", "_records", "DataTableGenerateInput"),
        ("_artifacts.py", "_records", "DriveExportInput"),
        ("_artifacts.py", "_records", "MindMapGenerateInput"),
        ("_artifacts.py", "_note_service", "LegacyNoteBackedService"),
        ("_artifacts.py", "_projectors", "project_artifact"),
        ("_artifacts.py", "_projectors", "project_generation_status"),
        ("_artifacts.py", "_records", "AudioGenerateInput"),
        ("_artifacts.py", "_studio", "AudioFamilyService"),
        ("_artifacts.py", "_studio", "ArtifactLifecycleService"),
        ("_artifacts.py", "_studio", "ArtifactRepresentationService"),
        ("_artifacts.py", "_records", "InteractiveGenerateInput"),
        ("_artifacts.py", "_studio", "InteractiveFamilyService"),
        ("_artifacts.py", "_records", "ReportGenerateInput"),
        ("_artifacts.py", "_records", "VideoGenerateInput"),
        ("_artifacts.py", "_studio", "DocumentOptionError"),
        ("_artifacts.py", "_studio", "ReportFamilyService"),
        ("_artifacts.py", "_studio", "ReportSuggestionService"),
        ("_artifacts.py", "_studio", "StudioManagementService"),
        ("_artifacts.py", "_studio", "StudioCatalog"),
        ("_artifacts.py", "_studio", "VideoFamilyService"),
        ("_artifacts.py", "_studio", "VisualFamilyService"),
        ("_artifacts.py", "_studio", "DataTableFamilyService"),
        ("_artifacts.py", "_studio", "DriveExportService"),
        ("_artifacts.py", "_studio", "NoteBackedMindMapFamilyService"),
        ("_collections.py", "_backend", "BackendAdapter"),
        ("_collections.py", "_backend", "BackendError"),
        ("_collections.py", "_backend_compat", "project_backend_error"),
        ("_collections.py", "_backend_compat", "project_local_not_found"),
        ("_collections.py", "_label_service", "LabelSetService"),
        ("_collections.py", "_label_service", "require_member_ids"),
        ("_collections.py", "_projectors", "project_collection"),
        ("_collections.py", "_records", "LabelKind"),
        ("_backend_compat.py", "_records", "LabelKind"),
        ("_labels.py", "_backend", "BackendAdapter"),
        ("_labels.py", "_backend", "BackendError"),
        ("_labels.py", "_backend_compat", "project_backend_error"),
        ("_labels.py", "_backend_compat", "project_local_not_found"),
        ("_labels.py", "_label_service", "LabelSetService"),
        ("_labels.py", "_label_service", "require_member_ids"),
        ("_labels.py", "_projectors", "project_label"),
        ("_labels.py", "_records", "LabelKind"),
        ("_label_service.py", "_backend", "BackendAdapter"),
        ("_artifact/listing.py", "_backend_compat", "project_local_not_found"),
        ("_projectors.py", "_records", "LabelKind"),
        ("_projectors.py", "_records", "LabelRecord"),
        ("_web/codec/labels.py", "_records", "LabelKind"),
        ("_web/codec/labels.py", "_records", "LabelRecord"),
        ("_label_service.py", "_records", "COLLECTION_CREATE_DEF"),
        ("_label_service.py", "_records", "COLLECTION_DELETE_DEF"),
        ("_label_service.py", "_records", "COLLECTION_GET_DEF"),
        ("_label_service.py", "_records", "COLLECTION_LIST_DEF"),
        ("_label_service.py", "_records", "COLLECTION_UPDATE_DEF"),
        ("_label_service.py", "_records", "LABEL_CREATE_DEF"),
        ("_label_service.py", "_records", "LABEL_DELETE_DEF"),
        ("_label_service.py", "_records", "LABEL_GENERATE_DEF"),
        ("_label_service.py", "_records", "LABEL_GET_DEF"),
        ("_label_service.py", "_records", "LABEL_LIST_DEF"),
        ("_label_service.py", "_records", "LABEL_UPDATE_DEF"),
        ("_label_service.py", "_records", "LabelCreateInput"),
        ("_label_service.py", "_records", "LabelDeleteInput"),
        ("_label_service.py", "_records", "LabelGenerateInput"),
        ("_label_service.py", "_records", "LabelGetInput"),
        ("_label_service.py", "_records", "LabelKind"),
        ("_label_service.py", "_records", "LabelListInput"),
        ("_label_service.py", "_records", "LabelRecord"),
        ("_label_service.py", "_records", "LabelUpdateInput"),
        ("_web/labels.py", "_backend", "BackendContractError"),
        ("_web/labels.py", "_backend", "BackendError"),
        ("_web/labels.py", "_backend", "BackendErrorReason"),
        ("_web/labels.py", "_records", "LabelCreateInput"),
        ("_web/labels.py", "_records", "LabelCreateResult"),
        ("_web/labels.py", "_records", "LabelGetInput"),
        ("_web/labels.py", "_records", "LabelGetResult"),
        ("_web/labels.py", "_records", "LabelKind"),
        ("_web/labels.py", "_records", "LabelListInput"),
        ("_web/labels.py", "_records", "LabelListResult"),
        ("_web/labels.py", "_records", "LabelRecord"),
        ("_web/labels.py", "_records", "LabelUpdateInput"),
        ("_web/labels.py", "_records", "LabelUpdateResult"),
        ("_web/registry.py", "_records", "COLLECTION_CREATE_DEF"),
        ("_web/registry.py", "_records", "COLLECTION_DELETE_DEF"),
        ("_web/registry.py", "_records", "COLLECTION_GET_DEF"),
        ("_web/registry.py", "_records", "COLLECTION_LIST_DEF"),
        ("_web/registry.py", "_records", "COLLECTION_UPDATE_DEF"),
        ("_web/registry.py", "_records", "LABEL_CREATE_DEF"),
        ("_web/registry.py", "_records", "LABEL_DELETE_DEF"),
        ("_web/registry.py", "_records", "LABEL_GENERATE_DEF"),
        ("_web/registry.py", "_records", "LABEL_GET_DEF"),
        ("_web/registry.py", "_records", "LABEL_LIST_DEF"),
        ("_web/registry.py", "_records", "LABEL_UPDATE_DEF"),
        ("_artifacts.py", "_web.backend", "WebRpcBackend"),
        ("_studio/lifecycle.py", "_backend", "BackendAdapter"),
        ("_studio/lifecycle.py", "_records", "ARTIFACT_WAIT_DEF"),
        ("_studio/lifecycle.py", "_records", "ArtifactPollInput"),
        ("_studio/lifecycle.py", "_records", "GenerationStatusRecord"),
        ("_studio/management.py", "_backend", "BackendAdapter"),
        ("_studio/management.py", "_records", "ARTIFACT_DELETE_DEF"),
        ("_studio/management.py", "_records", "ARTIFACT_RENAME_DEF"),
        ("_studio/management.py", "_records", "ARTIFACT_RETRY_DEF"),
        ("_studio/management.py", "_records", "ARTIFACT_REVISE_SLIDE_DEF"),
        ("_studio/management.py", "_records", "ARTIFACT_SUGGEST_REPORTS_DEF"),
        ("_studio/management.py", "_records", "ArtifactDeleteInput"),
        ("_studio/management.py", "_records", "ArtifactRenameInput"),
        ("_studio/management.py", "_records", "ArtifactRenameResult"),
        ("_studio/management.py", "_records", "ArtifactRetryInput"),
        ("_studio/management.py", "_records", "ArtifactRetryResult"),
        ("_studio/management.py", "_records", "ArtifactReviseSlideInput"),
        ("_studio/management.py", "_records", "ArtifactReviseSlideResult"),
        ("_studio/management.py", "_records", "ArtifactSuggestReportsInput"),
        ("_studio/management.py", "_records", "ArtifactSuggestReportsResult"),
        ("_studio/representations.py", "_backend", "BackendAdapter"),
        ("_studio/representations.py", "_records", "ARTIFACT_DOWNLOAD_DEF"),
        ("_studio/representations.py", "_records", "ArtifactDownloadInput"),
        ("_studio/representations.py", "_records", "ArtifactParseFailureKind"),
        ("_studio/representations.py", "_records", "ArtifactParseFailureRecord"),
        ("_studio/representations.py", "_records", "ArtifactRecord"),
        ("_studio/representations.py", "_records", "ArtifactRepresentationRecord"),
        ("_studio/representations.py", "_records", "MindMapRepresentationRecord"),
        ("_backend_compat.py", "_backend", "BackendContractError"),
        ("_backend_compat.py", "_backend", "BackendError"),
        ("_backend_compat.py", "_backend", "BackendErrorReason"),
        ("_backend_compat.py", "_records", "SourceAddFailureKind"),
        ("_backend_compat.py", "_records", "SourceAddFailureRecord"),
        ("_projectors.py", "_records", "GenerationStatusRecord"),
        ("_client_assembly.py", "_note_service", "LegacyNoteBackedService"),
        ("_client_assembly.py", "_note_service", "NoteService"),
        ("_client_assembly.py", "_studio", "MindMapFamilyService"),
        ("_client_assembly.py", "_studio", "StudioCatalog"),
        ("_client_assembly.py", "_web.backend", "WebRpcBackend"),
        ("_mind_maps_api.py", "_backend_compat", "project_backend_call"),
        ("_mind_maps_api.py", "_note_service", "NoteService"),
        ("_mind_maps_api.py", "_studio", "MindMapFamilyService"),
        ("_mind_map.py", "_note_service", "LegacyNoteBackedService"),
        ("_mind_map.py", "_note_service", "NoteRowKind"),
        ("_notebook_mutation_service.py", "_backend", "BackendAdapter"),
        ("_notebook_mutation_service.py", "_projectors", "project_notebook"),
        ("_notebook_mutation_service.py", "_records", "NOTEBOOK_CREATE_DEF"),
        ("_notebook_mutation_service.py", "_records", "NOTEBOOK_DELETE_DEF"),
        ("_notebook_mutation_service.py", "_records", "NOTEBOOK_UPDATE_DEF"),
        ("_notebook_mutation_service.py", "_records", "NotebookCreateInput"),
        ("_notebook_mutation_service.py", "_records", "NotebookDeleteInput"),
        ("_notebook_mutation_service.py", "_records", "NotebookUpdateInput"),
        ("_collections.py", "_projectors", "project_collection"),
        ("_labels.py", "_projectors", "project_label"),
        ("client.py", "_web.backend", "WebRpcBackend"),
        ("client.py", "_note_service", "NoteService"),
        ("_notebooks.py", "_backend", "BackendAdapter"),
        ("_notebooks.py", "_backend", "BackendError"),
        ("_notebooks.py", "_backend_compat", "project_backend_call"),
        ("_notebooks.py", "_backend_compat", "project_backend_error"),
        ("_notebooks.py", "_notebook_mutation_service", "NotebookMutationService"),
        ("_notebooks.py", "_read_services", "NotebookReadService"),
        ("_notebooks.py", "_projectors", "project_notebook_description"),
        ("_mutation_services.py", "_backend", "BackendAdapter"),
        ("_mutation_services.py", "_records", "SOURCE_ADD_URL_DEF"),
        ("_mutation_services.py", "_records", "SourceAddUrlInput"),
        ("_mutation_services.py", "_records", "SourceAddUrlResult"),
        ("_note_service.py", "_backend", "BackendAdapter"),
        ("_note_service.py", "_projectors", "project_mind_map"),
        ("_note_service.py", "_projectors", "project_note"),
        ("_note_service.py", "_records", "MIND_MAP_GENERATE_NOTE_DEF"),
        ("_note_service.py", "_records", "MIND_MAP_LIST_DEF"),
        ("_note_service.py", "_records", "NOTE_CREATE_DEF"),
        ("_note_service.py", "_records", "NOTE_DELETE_DEF"),
        ("_note_service.py", "_records", "NOTE_GET_DEF"),
        ("_note_service.py", "_records", "NOTE_LIST_DEF"),
        ("_note_service.py", "_records", "NOTE_UPDATE_DEF"),
        ("_note_service.py", "_records", "MindMapGenerateNoteInput"),
        ("_note_service.py", "_records", "MindMapListInput"),
        ("_note_service.py", "_records", "MindMapRecord"),
        ("_note_service.py", "_records", "NoteCreateInput"),
        ("_note_service.py", "_records", "NoteDeleteInput"),
        ("_note_service.py", "_records", "NoteGetInput"),
        ("_note_service.py", "_records", "NoteListInput"),
        ("_note_service.py", "_records", "NoteUpdateInput"),
        ("_notes.py", "_backend", "BackendError"),
        ("_notes.py", "_backend_compat", "project_backend_error"),
        ("_notes.py", "_note_service", "NoteService"),
        ("_projectors.py", "_records", "ArtifactRecord"),
        ("_projectors.py", "_records", "ArtifactUserStateRecord"),
        ("_projectors.py", "_records", "CollectionRecord"),
        ("_projectors.py", "_records", "GenerationStatusRecord"),
        ("_projectors.py", "_records", "LabelRecord"),
        ("_projectors.py", "_records", "MindMapRecord"),
        ("_projectors.py", "_records", "NotebookDescriptionRecord"),
        ("_projectors.py", "_records", "NotebookRecord"),
        ("_projectors.py", "_records", "NoteRecord"),
        ("_projectors.py", "_records", "ResearchSourceRecord"),
        ("_projectors.py", "_records", "ResearchTaskRecord"),
        ("_projectors.py", "_records", "ReportSuggestionRecord"),
        ("_projectors.py", "_records", "ShareAccessLevel"),
        ("_projectors.py", "_records", "SharePermissionLevel"),
        ("_projectors.py", "_records", "ShareStatusRecord"),
        ("_projectors.py", "_records", "ShareViewScope"),
        ("_projectors.py", "_records", "SharedUserRecord"),
        ("_projectors.py", "_records", "SourceRecord"),
        ("_read_services.py", "_backend", "BackendAdapter"),
        ("_read_services.py", "_projectors", "project_notebook"),
        ("_read_services.py", "_projectors", "project_source"),
        ("_read_services.py", "_records", "NOTEBOOK_GET_DEF"),
        ("_read_services.py", "_records", "NOTEBOOK_LIST_DEF"),
        ("_read_services.py", "_records", "NotebookGetInput"),
        ("_read_services.py", "_records", "NotebookListInput"),
        ("_read_services.py", "_records", "SOURCE_GET_DEF"),
        ("_read_services.py", "_records", "SOURCE_LIST_DEF"),
        ("_read_services.py", "_records", "SourceGetInput"),
        ("_read_services.py", "_records", "SourceListInput"),
        ("_sharing.py", "_backend", "BackendAdapter"),
        ("_sharing.py", "_backend_compat", "project_backend_call"),
        ("_sharing.py", "_records", "SharePermissionLevel"),
        ("_sharing.py", "_records", "ShareViewScope"),
        ("_sharing.py", "_sharing_service", "SharingService"),
        ("_sharing_service.py", "_backend", "BackendAdapter"),
        ("_sharing_service.py", "_projectors", "project_share_status"),
        ("_sharing_service.py", "_records", "SHARING_GET_DEF"),
        ("_sharing_service.py", "_records", "SHARING_SET_PUBLIC_DEF"),
        ("_sharing_service.py", "_records", "SHARING_SET_VIEW_LEVEL_DEF"),
        ("_sharing_service.py", "_records", "SHARING_UPDATE_USERS_DEF"),
        ("_sharing_service.py", "_records", "SharePermissionLevel"),
        ("_sharing_service.py", "_records", "ShareViewScope"),
        ("_sharing_service.py", "_records", "SharingGetInput"),
        ("_sharing_service.py", "_records", "SharingSetPublicInput"),
        ("_sharing_service.py", "_records", "SharingSetViewLevelInput"),
        ("_sharing_service.py", "_records", "SharingUpdateUsersInput"),
        ("_sharing_service.py", "_records", "SharingUserGrant"),
        ("_research.py", "_backend", "BackendAdapter"),
        ("_research.py", "_research_service", "_INITIAL_INTERVAL_UNSET"),
        ("_research.py", "_research_service", "ResearchService"),
        ("_research_service.py", "_backend", "BackendAdapter"),
        ("_research_service.py", "_backend", "BackendError"),
        ("_research_service.py", "_backend_compat", "project_backend_error"),
        ("_research_service.py", "_projectors", "project_research_task"),
        ("_research_service.py", "_records", "RESEARCH_CANCEL_DEF"),
        ("_research_service.py", "_records", "RESEARCH_IMPORT_DEF"),
        ("_research_service.py", "_records", "RESEARCH_POLL_DEF"),
        ("_research_service.py", "_records", "RESEARCH_START_DEF"),
        ("_research_service.py", "_records", "ResearchCancelInput"),
        ("_research_service.py", "_records", "ResearchImportEntry"),
        ("_research_service.py", "_records", "ResearchImportEntryKind"),
        ("_research_service.py", "_records", "ResearchImportInput"),
        ("_research_service.py", "_records", "ResearchMode"),
        ("_research_service.py", "_records", "ResearchPollInput"),
        ("_research_service.py", "_records", "ResearchSearchSource"),
        ("_research_service.py", "_records", "ResearchStartInput"),
        ("_source/listing.py", "_projectors", "project_source"),
        ("_source/listing.py", "_records", "SourceRecord"),
        ("_source/upload.py", "_records", "SourceFileRegistrationRecord"),
        ("_sources.py", "_backend", "BackendAdapter"),
        ("_sources.py", "_backend", "BackendError"),
        ("_sources.py", "_backend_compat", "project_backend_call"),
        ("_sources.py", "_backend_compat", "project_backend_error"),
        ("_sources.py", "_mutation_services", "SourceUrlMutationService"),
        ("_sources.py", "_projectors", "project_source"),
        ("_sources.py", "_read_services", "SourceReadService"),
        ("_studio/catalog.py", "_backend", "BackendAdapter"),
        ("_studio/catalog.py", "_projectors", "project_artifact"),
        ("_studio/catalog.py", "_records", "ARTIFACT_GET_DEF"),
        ("_studio/catalog.py", "_records", "ARTIFACT_LIST_DEF"),
        ("_studio/catalog.py", "_records", "ArtifactGetInput"),
        ("_studio/catalog.py", "_records", "ArtifactListInput"),
        ("_studio/catalog.py", "_records", "ArtifactRecord"),
        ("_studio/classifiers.py", "_records", "ArtifactRecord"),
        ("_studio/audio.py", "_backend", "BackendAdapter"),
        ("_studio/audio.py", "_records", "ARTIFACT_GENERATE_AUDIO_DEF"),
        ("_studio/audio.py", "_records", "ArtifactRecord"),
        ("_studio/audio.py", "_records", "AudioGenerateInput"),
        ("_studio/audio.py", "_records", "AudioGenerateResult"),
        ("_studio/audio.py", "_records", "AudioMetadataRecord"),
        ("_studio/interactive.py", "_backend", "BackendAdapter"),
        ("_studio/interactive.py", "_records", "ARTIFACT_GENERATE_FLASHCARDS_DEF"),
        ("_studio/interactive.py", "_records", "ARTIFACT_GENERATE_QUIZ_DEF"),
        ("_studio/interactive.py", "_records", "ArtifactRecord"),
        ("_studio/interactive.py", "_records", "InteractiveGenerateInput"),
        ("_studio/interactive.py", "_records", "InteractiveGenerateResult"),
        ("_studio/interactive.py", "_records", "InteractiveMetadataRecord"),
        ("_studio/visuals.py", "_backend", "BackendAdapter"),
        ("_studio/visuals.py", "_records", "ARTIFACT_GENERATE_INFOGRAPHIC_DEF"),
        ("_studio/visuals.py", "_records", "ARTIFACT_GENERATE_SLIDE_DECK_DEF"),
        ("_studio/visuals.py", "_records", "ArtifactRecord"),
        ("_studio/visuals.py", "_records", "InfographicGenerateInput"),
        ("_studio/visuals.py", "_records", "SlideDeckGenerateInput"),
        ("_studio/visuals.py", "_records", "VisualGenerateResult"),
        ("_studio/visuals.py", "_records", "VisualMetadataRecord"),
        ("_studio/documents.py", "_backend", "BackendAdapter"),
        ("_studio/documents.py", "_records", "ARTIFACT_GENERATE_REPORT_DEF"),
        ("_studio/documents.py", "_records", "ARTIFACT_GENERATE_VIDEO_DEF"),
        ("_studio/documents.py", "_records", "ArtifactRecord"),
        ("_studio/documents.py", "_records", "ReportGenerateInput"),
        ("_studio/documents.py", "_records", "ReportGenerateResult"),
        ("_studio/documents.py", "_records", "ReportMetadataRecord"),
        ("_studio/documents.py", "_records", "VideoGenerateInput"),
        ("_studio/documents.py", "_records", "VideoGenerateResult"),
        ("_studio/documents.py", "_records", "VideoMetadataRecord"),
        ("_studio/data_views.py", "_backend", "BackendAdapter"),
        ("_studio/data_views.py", "_records", "ARTIFACT_GENERATE_DATA_TABLE_DEF"),
        ("_studio/data_views.py", "_records", "ARTIFACT_GENERATE_MIND_MAP_DEF"),
        ("_studio/data_views.py", "_records", "ArtifactRecord"),
        ("_studio/data_views.py", "_records", "DataTableGenerateInput"),
        ("_studio/data_views.py", "_records", "DataTableGenerateResult"),
        ("_studio/data_views.py", "_records", "MindMapGenerateInput"),
        ("_studio/data_views.py", "_records", "MindMapGenerateResult"),
        ("_studio/mind_maps.py", "_backend", "BackendAdapter"),
        ("_studio/mind_maps.py", "_projectors", "project_artifact"),
        ("_studio/mind_maps.py", "_records", "ArtifactRecord"),
        ("_studio/mind_maps.py", "_records", "MIND_MAP_DELETE_DEF"),
        ("_studio/mind_maps.py", "_records", "MIND_MAP_GENERATE_INTERACTIVE_DEF"),
        ("_studio/mind_maps.py", "_records", "MIND_MAP_GET_DEF"),
        ("_studio/mind_maps.py", "_records", "MIND_MAP_UPDATE_DEF"),
        ("_studio/mind_maps.py", "_records", "MindMapDeleteInput"),
        ("_studio/mind_maps.py", "_records", "MindMapGenerateInteractiveInput"),
        ("_studio/mind_maps.py", "_records", "MindMapGenerateInteractiveResult"),
        ("_studio/mind_maps.py", "_records", "MindMapGetInput"),
        ("_studio/mind_maps.py", "_records", "MindMapUpdateInput"),
        ("_studio/exports.py", "_backend", "BackendAdapter"),
        ("_studio/exports.py", "_records", "ARTIFACT_EXPORT_DEF"),
        ("_studio/exports.py", "_records", "DriveExportInput"),
        ("_studio/exports.py", "_records", "DriveExportResult"),
        ("_web/__init__.py", "backend", "WebRpcBackend"),
        ("_web/backend.py", "_backend", "BackendCapabilities"),
        ("_web/backend.py", "_backend", "BackendContractError"),
        ("_web/backend.py", "_backend", "BackendDeadlineExceededError"),
        ("_web/backend.py", "_backend", "BackendError"),
        ("_web/backend.py", "_backend", "BackendErrorReason"),
        ("_web/backend.py", "_backend", "BackendKind"),
        ("_web/backend.py", "_backend", "UnsupportedOperationError"),
        ("_web/failure_projection.py", "_backend", "BackendContractError"),
        ("_web/backend.py", "_records", "ArtifactRecord"),
        # P9.3 research codec rows, their row-facing codec helpers, and the shared
        # ``_web/errors.py`` translation the ``RESEARCH_START`` ``map_error`` consumes.
        # P9.3 notebook codec rows and their row-facing codec helpers.
        ("_web/bindings/notebooks.py", "_binding", "Binding"),
        ("_web/bindings/notebooks.py", "_binding", "CodecBinding"),
        ("_web/bindings/notebooks.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/notebooks.py", "_records", "NOTEBOOK_DELETE_DEF"),
        ("_web/bindings/notebooks.py", "_records", "NOTEBOOK_DESCRIBE_DEF"),
        ("_web/bindings/notebooks.py", "_records", "NOTEBOOK_GET_DEF"),
        ("_web/bindings/notebooks.py", "_records", "NOTEBOOK_LIST_DEF"),
        ("_web/bindings/notebooks.py", "_records", "NOTEBOOK_REMOVE_RECENT_DEF"),
        ("_web/bindings/notebooks.py", "_records", "NOTEBOOK_SUMMARIZE_DEF"),
        ("_web/bindings/notebooks.py", "codec", "notebooks"),
        ("_web/codec/notebooks.py", "_binding", "CodecPayload"),
        ("_web/codec/notebooks.py", "_records", "NotebookDeleteInput"),
        ("_web/codec/notebooks.py", "_records", "NotebookDeleteResult"),
        ("_web/codec/notebooks.py", "_records", "NotebookGetInput"),
        ("_web/codec/notebooks.py", "_records", "NotebookGetResult"),
        ("_web/codec/notebooks.py", "_records", "NotebookGuideInput"),
        ("_web/codec/notebooks.py", "_records", "NotebookGuideResult"),
        ("_web/codec/notebooks.py", "_records", "NotebookListInput"),
        ("_web/codec/notebooks.py", "_records", "NotebookListResult"),
        ("_web/codec/notebooks.py", "_records", "NotebookRemoveRecentInput"),
        ("_web/codec/notebooks.py", "_records", "NotebookRemoveRecentResult"),
        ("_web/bindings/research.py", "_backend", "BackendError"),
        ("_web/bindings/research.py", "_backend", "BackendErrorReason"),
        ("_web/bindings/research.py", "_binding", "Binding"),
        ("_web/bindings/research.py", "_binding", "CodecBinding"),
        ("_web/bindings/research.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/research.py", "_binding", "NativeChoice"),
        ("_web/bindings/research.py", "_records", "RESEARCH_CANCEL_DEF"),
        ("_web/bindings/research.py", "_records", "RESEARCH_IMPORT_DEF"),
        ("_web/bindings/research.py", "_records", "RESEARCH_POLL_DEF"),
        ("_web/bindings/research.py", "_records", "RESEARCH_START_DEF"),
        ("_web/bindings/research.py", "_records", "ResearchMode"),
        ("_web/bindings/research.py", "_records", "ResearchStartInput"),
        ("_web/bindings/research.py", "_records", "ResearchStartResult"),
        ("_web/bindings/research.py", "codec", "research"),
        ("_web/codec/research.py", "_binding", "CodecPayload"),
        ("_web/codec/research.py", "_records", "ResearchCancelInput"),
        ("_web/codec/research.py", "_records", "ResearchCancelResult"),
        ("_web/codec/research.py", "_records", "ResearchImportInput"),
        ("_web/codec/research.py", "_records", "ResearchImportResult"),
        ("_web/codec/research.py", "_records", "ResearchPollInput"),
        ("_web/codec/research.py", "_records", "ResearchPollResult"),
        ("_web/codec/research.py", "_records", "ResearchStartInput"),
        ("_web/errors.py", "_backend", "BackendContractError"),
        ("_web/errors.py", "_backend", "BackendError"),
        ("_web/errors.py", "_backend", "BackendErrorReason"),
        ("_web/codec/notes.py", "_records", "NoteRecord"),
        ("_web/codec/notes.py", "_records", "MindMapRecord"),
        ("_web/registry.py", "_records", "MIND_MAP_DELETE_DEF"),
        ("_web/registry.py", "_records", "MIND_MAP_GENERATE_INTERACTIVE_DEF"),
        ("_web/registry.py", "_records", "MIND_MAP_GENERATE_NOTE_DEF"),
        ("_web/registry.py", "_records", "MIND_MAP_GET_DEF"),
        ("_web/registry.py", "_records", "MIND_MAP_LIST_DEF"),
        ("_web/registry.py", "_records", "MIND_MAP_UPDATE_DEF"),
        ("_web/registry.py", "_records", "NOTE_CREATE_DEF"),
        ("_web/registry.py", "_records", "NOTE_DELETE_DEF"),
        ("_web/registry.py", "_records", "NOTE_GET_DEF"),
        ("_web/registry.py", "_records", "NOTE_LIST_DEF"),
        ("_web/registry.py", "_records", "NOTE_UPDATE_DEF"),
        ("_web/backend.py", "_records", "SourceAddFailureRecord"),
        ("_web/failure_projection.py", "_records", "SourceAddFailureKind"),
        ("_web/failure_projection.py", "_records", "SourceAddFailureRecord"),
        ("_web/source_variants.py", "_records", "SourceRecord"),
        ("_web/backend.py", "registry", "WEB_OPERATION_REGISTRY"),
        ("_web/backend.py", "registry", "WEB_SUPPORTED_OPERATIONS"),
        ("_web/codec/artifacts.py", "_records", "ArtifactInfographicRecord"),
        ("_web/codec/artifacts.py", "_records", "ArtifactMediaRecord"),
        ("_web/codec/artifacts.py", "_records", "ArtifactParseFailureKind"),
        ("_web/codec/artifacts.py", "_records", "ArtifactParseFailureRecord"),
        ("_web/codec/artifacts.py", "_records", "ArtifactRecord"),
        ("_web/codec/artifacts.py", "_records", "ArtifactSlideRecord"),
        ("_web/codec/artifacts.py", "_records", "ArtifactUserStateRecord"),
        ("_web/codec/artifacts.py", "_records", "ReportSuggestionRecord"),
        ("_web/codec/artifacts.py", "_records", "sanitize_artifact_parse_text"),
        ("_web/codec/collections.py", "_records", "CollectionRecord"),
        ("_web/codec/labels.py", "_records", "LabelRecord"),
        ("_web/codec/notebooks.py", "_records", "NotebookChatSessionRecord"),
        ("_web/codec/notebooks.py", "_records", "NotebookChatSettingsRecord"),
        ("_web/codec/notebooks.py", "_records", "NotebookDescriptionRecord"),
        ("_web/codec/notebooks.py", "_records", "NotebookPremiumFeaturesRecord"),
        ("_web/codec/notebooks.py", "_records", "NotebookRecord"),
        ("_web/codec/notebooks.py", "_records", "SuggestedTopicRecord"),
        ("_web/codec/sharing.py", "_records", "ShareAccessLevel"),
        ("_web/codec/sharing.py", "_records", "SharePermissionLevel"),
        ("_web/codec/research.py", "_records", "ResearchImportEntry"),
        ("_web/codec/research.py", "_records", "ResearchImportEntryKind"),
        ("_web/codec/research.py", "_records", "ResearchImportedSourceRecord"),
        ("_web/codec/research.py", "_records", "ResearchMode"),
        ("_web/codec/research.py", "_records", "ResearchSearchSource"),
        ("_research_neutral.py", "_records", "ResearchSourceRecord"),
        ("_research_neutral.py", "_records", "ResearchTaskRecord"),
        ("_web/codec/research.py", "_records", "ResearchStartResult"),
        ("_web/codec/research.py", "_records", "ResearchTaskRecord"),
        ("_web/codec/artifacts.py", "_records", "ArtifactRepresentationRecord"),
        ("_web/codec/artifacts.py", "_records", "GenerationStatusRecord"),
        ("_web/codec/artifacts.py", "_records", "MindMapRepresentationRecord"),
        ("_web/codec/sharing.py", "_records", "ShareStatusRecord"),
        ("_web/codec/sharing.py", "_records", "ShareViewScope"),
        ("_web/codec/sharing.py", "_records", "SharedUserRecord"),
        ("_web/codec/sharing.py", "_records", "SharingUserGrant"),
        ("_web/codec/sources.py", "_records", "SourceRecord"),
        ("_web/registry.py", "_records", "ARTIFACT_GET_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_DELETE_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_DOWNLOAD_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_RENAME_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_RETRY_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_REVISE_SLIDE_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_SUGGEST_REPORTS_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_WAIT_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_EXPORT_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_GENERATE_DATA_TABLE_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_GENERATE_MIND_MAP_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_GENERATE_AUDIO_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_GENERATE_FLASHCARDS_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_GENERATE_INFOGRAPHIC_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_GENERATE_QUIZ_DEF"),
        ("_web/codec/__init__.py", "studio_documents", "decode_generation_status"),
        ("_web/codec/__init__.py", "studio_documents", "encode_report_generation"),
        ("_web/codec/__init__.py", "studio_documents", "encode_video_generation"),
        ("_web/registry.py", "_records", "ARTIFACT_GET_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_GENERATE_REPORT_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_GENERATE_SLIDE_DECK_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_GENERATE_VIDEO_DEF"),
        ("_web/registry.py", "_records", "ARTIFACT_LIST_DEF"),
        ("_web/registry.py", "_records", "NOTEBOOK_GET_DEF"),
        ("_web/registry.py", "_records", "NOTEBOOK_LIST_DEF"),
        ("_web/registry.py", "_records", "NOTEBOOK_CREATE_DEF"),
        ("_web/registry.py", "_records", "NOTEBOOK_DELETE_DEF"),
        ("_web/registry.py", "_records", "NOTEBOOK_UPDATE_DEF"),
        ("_web/registry.py", "_records", "SHARING_GET_DEF"),
        ("_web/registry.py", "_records", "SHARING_SET_PUBLIC_DEF"),
        ("_web/registry.py", "_records", "SHARING_SET_VIEW_LEVEL_DEF"),
        ("_web/registry.py", "_records", "SHARING_UPDATE_USERS_DEF"),
        ("_web/registry.py", "_records", "RESEARCH_CANCEL_DEF"),
        ("_web/registry.py", "_records", "RESEARCH_IMPORT_DEF"),
        ("_web/registry.py", "_records", "RESEARCH_POLL_DEF"),
        ("_web/registry.py", "_records", "RESEARCH_START_DEF"),
        ("_web/registry.py", "_records", "SOURCE_ADD_URL_DEF"),
        ("_web/registry.py", "_records", "SOURCE_GET_DEF"),
        ("_web/registry.py", "_records", "SOURCE_LIST_DEF"),
        ("_web/codec/studio_documents.py", "_records", "GenerationStatusRecord"),
        ("_web/codec/studio_documents.py", "_records", "ReportGenerateInput"),
        ("_web/codec/studio_documents.py", "_records", "VideoGenerateInput"),
        (
            "_notebooks.py",
            "_suggestion_service",
            "PROMPT_SUGGESTIONS_DEFAULT_MODE",
        ),
        ("_notebooks.py", "_suggestion_service", "SuggestionService"),
        ("_projectors.py", "_records", "AccountLimitsRecord"),
        ("_projectors.py", "_records", "PromptSuggestionRecord"),
        ("_projectors.py", "_records", "UserSettingsRecord"),
        ("_settings.py", "_backend", "BackendAdapter"),
        ("_settings.py", "_backend_compat", "project_backend_call"),
        ("_settings.py", "_settings_service", "SettingsService"),
        ("_settings_service.py", "_backend", "BackendAdapter"),
        ("_settings_service.py", "_projectors", "project_account_limits"),
        ("_settings_service.py", "_projectors", "project_user_settings"),
        ("_settings_service.py", "_records", "SETTINGS_GET_DEF"),
        ("_settings_service.py", "_records", "SETTINGS_GET_LIMITS_DEF"),
        ("_settings_service.py", "_records", "SETTINGS_SET_LANGUAGE_DEF"),
        ("_settings_service.py", "_records", "SettingsGetInput"),
        ("_settings_service.py", "_records", "SettingsGetLimitsInput"),
        ("_settings_service.py", "_records", "SettingsSetLanguageInput"),
        ("_suggestion_service.py", "_backend", "BackendAdapter"),
        ("_suggestion_service.py", "_projectors", "project_prompt_suggestions"),
        ("_suggestion_service.py", "_projectors", "project_report_suggestions"),
        ("_suggestion_service.py", "_records", "ARTIFACT_SUGGEST_REPORTS_DEF"),
        ("_suggestion_service.py", "_records", "ArtifactSuggestReportsInput"),
        ("_suggestion_service.py", "_records", "NOTEBOOK_SUGGEST_PROMPTS_DEF"),
        ("_suggestion_service.py", "_records", "NotebookSuggestPromptsInput"),
        # P9.3 settings/suggestions codec rows and their row-facing codec helpers.
        ("_web/bindings/__init__.py", "_binding", "Binding"),
        ("_web/bindings/labels.py", "_binding", "Binding"),
        ("_web/bindings/labels.py", "_binding", "CodecBinding"),
        ("_web/bindings/labels.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/labels.py", "_records", "COLLECTION_DELETE_DEF"),
        ("_web/bindings/labels.py", "_records", "COLLECTION_GET_DEF"),
        ("_web/bindings/labels.py", "_records", "COLLECTION_LIST_DEF"),
        ("_web/bindings/labels.py", "_records", "LABEL_DELETE_DEF"),
        ("_web/bindings/labels.py", "_records", "LABEL_GENERATE_DEF"),
        ("_web/bindings/labels.py", "_records", "LABEL_GET_DEF"),
        ("_web/bindings/labels.py", "_records", "LABEL_LIST_DEF"),
        ("_web/bindings/labels.py", "codec", "labels"),
        ("_web/bindings/settings.py", "_binding", "Binding"),
        ("_web/bindings/settings.py", "_binding", "CodecBinding"),
        ("_web/bindings/settings.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/settings.py", "_records", "ARTIFACT_SUGGEST_REPORTS_DEF"),
        ("_web/bindings/settings.py", "_records", "SETTINGS_GET_DEF"),
        ("_web/bindings/settings.py", "_records", "SETTINGS_GET_LIMITS_DEF"),
        ("_web/bindings/settings.py", "_records", "SETTINGS_SET_LANGUAGE_DEF"),
        ("_web/bindings/settings.py", "codec", "settings"),
        ("_web/bindings/settings.py", "codec", "suggestions"),
        ("_web/codec/labels.py", "_backend", "BackendContractError"),
        ("_web/codec/labels.py", "_binding", "CodecPayload"),
        ("_web/codec/labels.py", "_records", "LabelDeleteInput"),
        ("_web/codec/labels.py", "_records", "LabelDeleteResult"),
        ("_web/codec/labels.py", "_records", "LabelGenerateInput"),
        ("_web/codec/labels.py", "_records", "LabelGenerateResult"),
        ("_web/codec/labels.py", "_records", "LabelGetInput"),
        ("_web/codec/labels.py", "_records", "LabelGetResult"),
        ("_web/codec/labels.py", "_records", "LabelListInput"),
        ("_web/codec/labels.py", "_records", "LabelListResult"),
        ("_web/codec/settings.py", "_binding", "CodecPayload"),
        ("_web/codec/settings.py", "_records", "SettingsGetInput"),
        ("_web/codec/settings.py", "_records", "SettingsGetLimitsInput"),
        ("_web/codec/settings.py", "_records", "SettingsSetLanguageInput"),
        ("_web/codec/suggestions.py", "_binding", "CodecPayload"),
        ("_web/codec/suggestions.py", "_records", "ArtifactSuggestReportsInput"),
        # P9.3 notes/mind-map codec rows and their row-facing codec helpers.
        ("_web/bindings/mind_maps.py", "_binding", "Binding"),
        ("_web/bindings/mind_maps.py", "_binding", "CodecBinding"),
        ("_web/bindings/mind_maps.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/mind_maps.py", "_records", "MIND_MAP_DELETE_DEF"),
        ("_web/bindings/mind_maps.py", "_records", "MIND_MAP_GET_DEF"),
        ("_web/bindings/mind_maps.py", "_records", "MIND_MAP_LIST_DEF"),
        ("_web/bindings/mind_maps.py", "_records", "MIND_MAP_UPDATE_DEF"),
        ("_web/bindings/mind_maps.py", "codec", "mind_maps"),
        ("_web/bindings/notes.py", "_binding", "Binding"),
        ("_web/bindings/notes.py", "_binding", "CodecBinding"),
        ("_web/bindings/notes.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/notes.py", "_records", "NOTE_CREATE_DEF"),
        ("_web/bindings/notes.py", "_records", "NOTE_DELETE_DEF"),
        ("_web/bindings/notes.py", "_records", "NOTE_GET_DEF"),
        ("_web/bindings/notes.py", "_records", "NOTE_LIST_DEF"),
        ("_web/bindings/notes.py", "_records", "NOTE_UPDATE_DEF"),
        ("_web/bindings/notes.py", "codec", "notes"),
        ("_web/codec/mind_maps.py", "_binding", "CodecPayload"),
        ("_web/codec/mind_maps.py", "_records", "MindMapDeleteInput"),
        ("_web/codec/mind_maps.py", "_records", "MindMapDeleteResult"),
        ("_web/codec/mind_maps.py", "_records", "MindMapGetInput"),
        ("_web/codec/mind_maps.py", "_records", "MindMapGetResult"),
        ("_web/codec/mind_maps.py", "_records", "MindMapListInput"),
        ("_web/codec/mind_maps.py", "_records", "MindMapListResult"),
        ("_web/codec/mind_maps.py", "_records", "MindMapUpdateInput"),
        ("_web/codec/mind_maps.py", "_records", "MindMapUpdateResult"),
        ("_web/codec/notes.py", "_binding", "CodecPayload"),
        ("_web/codec/notes.py", "_records", "NoteCreateInput"),
        ("_web/codec/notes.py", "_records", "NoteCreateResult"),
        ("_web/codec/notes.py", "_records", "NoteDeleteInput"),
        ("_web/codec/notes.py", "_records", "NoteDeleteResult"),
        ("_web/codec/notes.py", "_records", "NoteGetInput"),
        ("_web/codec/notes.py", "_records", "NoteGetResult"),
        ("_web/codec/notes.py", "_records", "NoteListInput"),
        ("_web/codec/notes.py", "_records", "NoteListResult"),
        ("_web/codec/notes.py", "_records", "NoteUpdateInput"),
        ("_web/codec/notes.py", "_records", "NoteUpdateResult"),
        ("_web/registry.py", "_binding", "Binding"),
        ("_web/codec/settings.py", "_records", "AccountLimitsRecord"),
        ("_web/codec/settings.py", "_records", "SettingsGetLimitsResult"),
        ("_web/codec/settings.py", "_records", "SettingsGetResult"),
        ("_web/codec/settings.py", "_records", "SettingsSetLanguageResult"),
        ("_web/codec/settings.py", "_records", "UserSettingsRecord"),
        ("_web/codec/suggestions.py", "_records", "ArtifactSuggestReportsResult"),
        ("_web/codec/suggestions.py", "_records", "NotebookSuggestPromptsResult"),
        ("_web/codec/suggestions.py", "_records", "PromptSuggestionRecord"),
        ("_web/codec/suggestions.py", "_records", "ReportSuggestionRecord"),
        ("_web/registry.py", "_records", "ARTIFACT_SUGGEST_REPORTS_DEF"),
        ("_web/registry.py", "_records", "NOTEBOOK_SUGGEST_PROMPTS_DEF"),
        ("_web/registry.py", "_records", "SETTINGS_GET_DEF"),
        ("_web/registry.py", "_records", "SETTINGS_GET_LIMITS_DEF"),
        ("_web/registry.py", "_records", "SETTINGS_SET_LANGUAGE_DEF"),
        # P9.3 Studio codec rows and their row-facing codec helpers.
        ("_web/bindings/studio.py", "_backend", "BackendContractError"),
        ("_web/bindings/studio.py", "_binding", "Binding"),
        ("_web/bindings/studio.py", "_binding", "CodecBinding"),
        ("_web/bindings/studio.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/studio.py", "_binding", "NativeChoice"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_DELETE_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_DOWNLOAD_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_EXPORT_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_RETRY_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_REVISE_SLIDE_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_WAIT_DEF"),
        ("_web/bindings/studio.py", "_records", "ArtifactDownloadInput"),
        ("_web/bindings/studio.py", "codec", "artifacts"),
        ("_web/bindings/studio.py", "codec", "studio_documents"),
        ("_web/codec/artifacts.py", "_backend", "BackendContractError"),
        ("_web/codec/artifacts.py", "_binding", "CodecPayload"),
        ("_web/codec/artifacts.py", "_records", "ArtifactDeleteInput"),
        ("_web/codec/artifacts.py", "_records", "ArtifactDeleteResult"),
        ("_web/codec/artifacts.py", "_records", "ArtifactDownloadInput"),
        ("_web/codec/artifacts.py", "_records", "ArtifactDownloadResult"),
        ("_web/codec/artifacts.py", "_records", "ArtifactPollInput"),
        ("_web/codec/artifacts.py", "_records", "ArtifactPollResult"),
        ("_web/codec/artifacts.py", "_records", "DriveExportInput"),
        ("_web/codec/artifacts.py", "_records", "DriveExportResult"),
        ("_web/codec/studio_documents.py", "_backend", "BackendError"),
        ("_web/codec/studio_documents.py", "_backend", "BackendErrorReason"),
        ("_web/codec/studio_documents.py", "_binding", "CodecPayload"),
        ("_web/codec/studio_documents.py", "_records", "ArtifactRetryInput"),
        ("_web/codec/studio_documents.py", "_records", "ArtifactRetryResult"),
        ("_web/codec/studio_documents.py", "_records", "ArtifactReviseSlideInput"),
        ("_web/codec/studio_documents.py", "_records", "ArtifactReviseSlideResult"),
    }
)

REVIEWED_BACKEND_IMPORTS |= frozenset(
    {
        ("_mutation_services.py", "_records", "SourceRecord"),
        ("_projectors.py", "_records", "SourceFulltextRecord"),
        ("_projectors.py", "_records", "SourceGuideRecord"),
        ("_source_service.py", "_backend", "BackendAdapter"),
        ("_source_service.py", "_records", "SOURCE_ADD_DRIVE_DEF"),
        ("_source_service.py", "_records", "SOURCE_ADD_FILE_DEF"),
        ("_source_service.py", "_records", "SOURCE_ADD_TEXT_DEF"),
        ("_source_service.py", "_records", "SOURCE_ADD_URL_BATCH_DEF"),
        ("_source_service.py", "_records", "SOURCE_CHECK_FRESHNESS_DEF"),
        ("_source_service.py", "_records", "SOURCE_DELETE_DEF"),
        ("_source_service.py", "_records", "SOURCE_GET_FULLTEXT_DEF"),
        ("_source_service.py", "_records", "SOURCE_GET_GUIDE_DEF"),
        ("_source_service.py", "_records", "SOURCE_REFRESH_DEF"),
        ("_source_service.py", "_records", "SOURCE_UPDATE_DEF"),
        ("_source_service.py", "_records", "SOURCE_WAIT_DEF"),
        ("_source_service.py", "_records", "SourceAddDriveInput"),
        ("_source_service.py", "_records", "SourceAddDriveResult"),
        ("_source_service.py", "_records", "SourceAddFileInput"),
        ("_source_service.py", "_records", "SourceAddFileResult"),
        ("_source_service.py", "_records", "SourceAddTextInput"),
        ("_source_service.py", "_records", "SourceAddTextResult"),
        ("_source_service.py", "_records", "SourceAddUrlBatchInput"),
        ("_source_service.py", "_records", "SourceAddUrlBatchResult"),
        ("_source_service.py", "_records", "SourceDeleteInput"),
        ("_source_service.py", "_records", "SourceFileInputKind"),
        ("_source_service.py", "_records", "SourceFreshnessInput"),
        ("_source_service.py", "_records", "SourceFulltextInput"),
        ("_source_service.py", "_records", "SourceFulltextResult"),
        ("_source_service.py", "_records", "SourceGuideInput"),
        ("_source_service.py", "_records", "SourceGuideResult"),
        ("_source_service.py", "_records", "SourceProgressCallback"),
        ("_source_service.py", "_records", "SourceRefreshInput"),
        ("_source_service.py", "_records", "SourceRecord"),
        ("_source_service.py", "_records", "SourceUpdateInput"),
        ("_source_service.py", "_records", "SourceUpdateResult"),
        ("_source_service.py", "_records", "SourceWaitSnapshotInput"),
        ("_source_service.py", "_records", "SourceWaitSnapshotResult"),
        ("_sources.py", "_backend_compat", "project_source_add_failure"),
        ("_sources.py", "_projectors", "project_source_fulltext"),
        ("_sources.py", "_projectors", "project_source_guide"),
        ("_sources.py", "_projectors", "record_source"),
        ("_sources.py", "_source_service", "SourceService"),
        ("_web/source_variants.py", "_backend", "BackendError"),
        ("_web/source_variants.py", "_backend", "BackendErrorReason"),
        ("_web/source_variants.py", "_records", "SourceUpdateInput"),
        ("_web/source_variants.py", "_records", "SourceUpdateResult"),
        ("_web/codec/sources.py", "_records", "SourceFileRegistrationRecord"),
        ("_web/codec/sources.py", "_records", "SourceFulltextRecord"),
        ("_web/codec/sources.py", "_records", "SourceGuideRecord"),
        ("_web/codec/sources.py", "_records", "SourceRecord"),
        ("_web/registry.py", "_records", "SOURCE_ADD_DRIVE_DEF"),
        ("_web/registry.py", "_records", "SOURCE_ADD_FILE_DEF"),
        ("_web/registry.py", "_records", "SOURCE_ADD_TEXT_DEF"),
        ("_web/registry.py", "_records", "SOURCE_ADD_URL_BATCH_DEF"),
        ("_web/registry.py", "_records", "SOURCE_CHECK_FRESHNESS_DEF"),
        ("_web/registry.py", "_records", "SOURCE_DELETE_DEF"),
        ("_web/registry.py", "_records", "SOURCE_GET_FULLTEXT_DEF"),
        ("_web/registry.py", "_records", "SOURCE_GET_GUIDE_DEF"),
        ("_web/registry.py", "_records", "SOURCE_REFRESH_DEF"),
        ("_web/registry.py", "_records", "SOURCE_UPDATE_DEF"),
        ("_web/registry.py", "_records", "SOURCE_WAIT_DEF"),
    }
)

# P6.1 adds the transport-neutral Chat service/facade/projector graph and six
# web bindings. The concrete handlers live in a focused mixin to preserve the
# web-backend module-size ratchet.
REVIEWED_BACKEND_IMPORTS |= frozenset(
    {
        ("_chat/api.py", "_backend", "BackendAdapter"),
        ("_chat/api.py", "_backend", "BackendError"),
        ("_chat/api.py", "_backend_compat", "project_backend_error"),
        ("_chat/api.py", "_projectors", "chat_reference_record"),
        ("_chat/api.py", "_projectors", "project_chat_ask_result"),
        ("_chat/api.py", "_projectors", "project_chat_saved_note"),
        ("_chat/api.py", "_projectors", "project_chat_settings"),
        ("_chat/api.py", "_projectors", "project_chat_turns_legacy"),
        ("_chat/api.py", "_records", "ChatAskInput"),
        ("_chat/api.py", "_records", "ChatAskResultRecord"),
        ("_chat/api.py", "_records", "ChatConfigureAction"),
        ("_chat/api.py", "_records", "ChatConfigureInput"),
        ("_chat/api.py", "_records", "ChatGetHistoryResult"),
        ("_chat/api.py", "_records", "ChatHistoryPairRecord"),
        ("_chat/api.py", "_records", "ChatSaveNoteInput"),
        ("_chat/history.py", "_records", "ChatGetHistoryResult"),
        ("_chat/history.py", "_records", "ChatTurnDecodeErrorRecord"),
        ("_chat/notes.py", "_projectors", "chat_reference_record"),
        ("_chat/service.py", "_backend", "BackendAdapter"),
        ("_chat/service.py", "_records", "CHAT_ASK_DEF"),
        ("_chat/service.py", "_records", "CHAT_CONFIGURE_DEF"),
        ("_chat/service.py", "_records", "CHAT_DELETE_HISTORY_DEF"),
        ("_chat/service.py", "_records", "CHAT_GET_CONVERSATION_DEF"),
        ("_chat/service.py", "_records", "CHAT_GET_HISTORY_DEF"),
        ("_chat/service.py", "_records", "CHAT_SAVE_NOTE_DEF"),
        ("_chat/service.py", "_records", "ChatAskInput"),
        ("_chat/service.py", "_records", "ChatAskResultRecord"),
        ("_chat/service.py", "_records", "ChatConfigureInput"),
        ("_chat/service.py", "_records", "ChatConfigureResult"),
        ("_chat/service.py", "_records", "ChatDeleteHistoryInput"),
        ("_chat/service.py", "_records", "ChatGetConversationInput"),
        ("_chat/service.py", "_records", "ChatGetHistoryInput"),
        ("_chat/service.py", "_records", "ChatGetHistoryResult"),
        ("_chat/service.py", "_records", "ChatSaveNoteInput"),
        ("_chat/service.py", "_records", "ChatSaveNoteResult"),
        ("_chat/stream_decode.py", "_records", "ChatNextStepRecord"),
        ("_chat/stream_decode.py", "_records", "ChatReferenceRecord"),
        ("_chat/stream_decode.py", "_records", "ChatStreamAnswerRecord"),
        ("_chat/stream_decode.py", "_records", "ChatTurnKeyRecord"),
        ("_chat/wire.py", "_records", "ChatHistoryPairRecord"),
        ("_projectors.py", "_records", "ChatAskResultRecord"),
        ("_projectors.py", "_records", "ChatGetHistoryResult"),
        ("_projectors.py", "_records", "ChatLegacyMappingRecord"),
        ("_projectors.py", "_records", "ChatLegacySequenceRecord"),
        ("_projectors.py", "_records", "ChatLegacyValue"),
        ("_projectors.py", "_records", "ChatReferenceRecord"),
        ("_projectors.py", "_records", "ChatSavedNoteRecord"),
        ("_projectors.py", "_records", "ChatSettingsRecord"),
        # P9.3 chat codec rows and their row-facing codec helpers.
        ("_web/bindings/chat.py", "_backend", "BackendContractError"),
        ("_web/bindings/chat.py", "_backend", "BackendDeadlineExceededError"),
        ("_web/bindings/chat.py", "_binding", "Binding"),
        ("_web/bindings/chat.py", "_binding", "CodecBinding"),
        ("_web/bindings/chat.py", "_binding", "CustomBinding"),
        ("_web/bindings/chat.py", "_binding", "ErrorMode"),
        ("_web/bindings/chat.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/chat.py", "_binding", "NativeChoice"),
        ("_web/bindings/chat.py", "_binding", "RowInvoker"),
        ("_web/bindings/chat.py", "_binding", "StreamPayload"),
        ("_web/bindings/chat.py", "_binding", "StreamSpec"),
        ("_web/bindings/chat.py", "_records", "CHAT_ASK_DEF"),
        ("_web/bindings/chat.py", "_records", "CHAT_CONFIGURE_DEF"),
        ("_web/bindings/chat.py", "_records", "CHAT_DELETE_HISTORY_DEF"),
        ("_web/bindings/chat.py", "_records", "CHAT_GET_CONVERSATION_DEF"),
        ("_web/bindings/chat.py", "_records", "CHAT_GET_HISTORY_DEF"),
        ("_web/bindings/chat.py", "_records", "CHAT_SAVE_NOTE_DEF"),
        ("_web/bindings/chat.py", "_records", "ChatAskInput"),
        ("_web/bindings/chat.py", "_records", "ChatAskResultRecord"),
        ("_web/bindings/chat.py", "_records", "ChatConfigureAction"),
        ("_web/bindings/chat.py", "_records", "ChatConfigureInput"),
        ("_web/bindings/chat.py", "codec", "chat"),
        ("_web/codec/chat.py", "_binding", "CodecPayload"),
        ("_web/codec/chat.py", "_records", "ChatAskInput"),
        ("_web/codec/chat.py", "_records", "ChatConfigureAction"),
        ("_web/codec/chat.py", "_records", "ChatConfigureInput"),
        ("_web/codec/chat.py", "_records", "ChatConfigureResult"),
        ("_web/codec/chat.py", "_records", "ChatDeleteHistoryInput"),
        ("_web/codec/chat.py", "_records", "ChatDeleteHistoryResult"),
        ("_web/codec/chat.py", "_records", "ChatGetConversationInput"),
        ("_web/codec/chat.py", "_records", "ChatGetConversationResult"),
        ("_web/codec/chat.py", "_records", "ChatGetHistoryInput"),
        ("_web/codec/chat.py", "_records", "ChatSaveNoteInput"),
        ("_web/codec/chat.py", "_records", "ChatSaveNoteResult"),
        ("_web/codec/chat.py", "_records", "ChatConversationTurnRecord"),
        ("_web/codec/chat.py", "_records", "ChatGetHistoryResult"),
        ("_web/codec/chat.py", "_records", "ChatLegacyMappingRecord"),
        ("_web/codec/chat.py", "_records", "ChatLegacySequenceRecord"),
        ("_web/codec/chat.py", "_records", "ChatLegacyValue"),
        ("_web/codec/chat.py", "_records", "ChatReferenceRecord"),
        ("_web/codec/chat.py", "_records", "ChatSavedNoteRecord"),
        ("_web/codec/chat.py", "_records", "ChatSettingsRecord"),
        ("_web/codec/chat.py", "_records", "ChatStreamAnswerRecord"),
        ("_web/codec/chat.py", "_records", "ChatTurnDecodeErrorRecord"),
        ("_web/codec/chat_saved_note.py", "_records", "ChatReferenceRecord"),
        ("_web/error_policy.py", "_backend", "BackendErrorReason"),
        ("_web/registry.py", "_records", "CHAT_ASK_DEF"),
        ("_web/registry.py", "_records", "CHAT_CONFIGURE_DEF"),
        ("_web/registry.py", "_records", "CHAT_DELETE_HISTORY_DEF"),
        ("_web/registry.py", "_records", "CHAT_GET_CONVERSATION_DEF"),
        ("_web/registry.py", "_records", "CHAT_GET_HISTORY_DEF"),
        ("_web/registry.py", "_records", "CHAT_SAVE_NOTE_DEF"),
    }
)

# P9.4b converts the Studio generate families, prompt suggestions and the rename
# composite into custom rows; the emptied chain modules are gone and the row and
# codec modules carry their record/codec imports.
REVIEWED_BACKEND_IMPORTS |= frozenset(
    {
        ("_web/bindings/settings.py", "_binding", "CustomBinding"),
        ("_web/bindings/settings.py", "_binding", "RowInvoker"),
        ("_web/bindings/settings.py", "_records", "NOTEBOOK_SUGGEST_PROMPTS_DEF"),
        ("_web/bindings/settings.py", "_records", "NotebookSuggestPromptsInput"),
        ("_web/bindings/settings.py", "_records", "NotebookSuggestPromptsResult"),
        ("_web/bindings/studio.py", "_binding", "CustomBinding"),
        ("_web/bindings/studio.py", "_binding", "RowInvoker"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_GENERATE_AUDIO_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_GENERATE_DATA_TABLE_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_GENERATE_FLASHCARDS_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_GENERATE_INFOGRAPHIC_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_GENERATE_QUIZ_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_GENERATE_REPORT_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_GENERATE_SLIDE_DECK_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_GENERATE_VIDEO_DEF"),
        ("_web/bindings/studio.py", "_records", "ARTIFACT_RENAME_DEF"),
        ("_web/bindings/studio.py", "_records", "ArtifactRenameInput"),
        ("_web/bindings/studio.py", "_records", "ArtifactRenameResult"),
        ("_web/bindings/studio.py", "_records", "AudioGenerateInput"),
        ("_web/bindings/studio.py", "_records", "AudioGenerateResult"),
        ("_web/bindings/studio.py", "_records", "DataTableGenerateInput"),
        ("_web/bindings/studio.py", "_records", "DataTableGenerateResult"),
        ("_web/bindings/studio.py", "_records", "InfographicGenerateInput"),
        ("_web/bindings/studio.py", "_records", "InteractiveGenerateInput"),
        ("_web/bindings/studio.py", "_records", "InteractiveGenerateResult"),
        ("_web/bindings/studio.py", "_records", "ReportGenerateInput"),
        ("_web/bindings/studio.py", "_records", "ReportGenerateResult"),
        ("_web/bindings/studio.py", "_records", "SlideDeckGenerateInput"),
        ("_web/bindings/studio.py", "_records", "VideoGenerateInput"),
        ("_web/bindings/studio.py", "_records", "VideoGenerateResult"),
        ("_web/bindings/studio.py", "_records", "VisualGenerateResult"),
        ("_web/bindings/studio.py", "codec", "generation"),
        ("_web/codec/artifacts.py", "_backend", "BackendError"),
        ("_web/codec/artifacts.py", "_backend", "BackendErrorReason"),
        ("_web/codec/artifacts.py", "_records", "ArtifactRenameInput"),
        ("_web/codec/generation.py", "_backend", "BackendContractError"),
        ("_web/codec/generation.py", "_binding", "CodecPayload"),
        ("_web/codec/generation.py", "_records", "AudioGenerateInput"),
        ("_web/codec/generation.py", "_records", "DataTableGenerateInput"),
        ("_web/codec/generation.py", "_records", "GenerationStatusRecord"),
        ("_web/codec/generation.py", "_records", "InfographicGenerateInput"),
        ("_web/codec/generation.py", "_records", "InteractiveGenerateInput"),
        ("_web/codec/generation.py", "_records", "ReportGenerateInput"),
        ("_web/codec/generation.py", "_records", "SlideDeckGenerateInput"),
        ("_web/codec/generation.py", "_records", "VideoGenerateInput"),
        ("_web/codec/generation.py", "studio_documents", "artifact_feature_unavailable"),
        ("_web/codec/generation.py", "studio_documents", "decode_generation_status"),
        ("_web/codec/generation.py", "studio_documents", "encode_report_generation"),
        ("_web/codec/generation.py", "studio_documents", "encode_video_generation"),
        ("_web/codec/source_ids.py", "_binding", "CodecPayload"),
        ("_web/codec/suggestions.py", "_records", "NotebookSuggestPromptsInput"),
    }
)

_REVIEWED_BACKEND_IMPORT_MODULES = frozenset(
    {
        "_backend",
        "_binding",
        "_backend_compat",
        "_label_service",
        "_mutation_services",
        "_note_service",
        "_notebook_mutation_service",
        "_projectors",
        "_read_services",
        "_records",
        "_sharing_service",
        "_research_service",
        "_settings_service",
        "_studio",
        "_suggestion_service",
        "_source_service",
        "_web",
        "_web.backend",
        "_web.codec.settings",
        "backend",
        "codec.sharing",
        "codec.research",
        "codec",
        "registry",
        "settings_suggestions",
        "studio_documents",
    }
)

_REVIEWED_BACKEND_IMPORT_PREFIXES = (
    "notebooklm._backend",
    "notebooklm._binding",
    "notebooklm._label_service",
    "notebooklm._mutation_services",
    "notebooklm._notebook_mutation_service",
    "notebooklm._projectors",
    "notebooklm._read_services",
    "notebooklm._records",
    "notebooklm._sharing_service",
    "notebooklm._research_service",
    "notebooklm._settings_service",
    "notebooklm._studio",
    "notebooklm._suggestion_service",
    "notebooklm._source_service",
    "notebooklm._web",
)


def _is_reviewed_backend_import_module(module: str) -> bool:
    return module in _REVIEWED_BACKEND_IMPORT_MODULES or module.startswith(
        _REVIEWED_BACKEND_IMPORT_PREFIXES
    )


ACTIVE_BACKEND_INVOKE_SITES = frozenset(
    {
        "_label_service.py:LabelSetService.create",
        "_label_service.py:LabelSetService.delete",
        "_label_service.py:LabelSetService.generate",
        "_label_service.py:LabelSetService.get",
        "_label_service.py:LabelSetService.list",
        "_label_service.py:LabelSetService.update",
        "_notebook_mutation_service.py:NotebookMutationService.create",
        "_notebook_mutation_service.py:NotebookMutationService.delete",
        "_notebook_mutation_service.py:NotebookMutationService.update",
        "_read_services.py:NotebookReadService.get",
        "_read_services.py:NotebookReadService.list",
        "_read_services.py:SourceReadService.get",
        "_read_services.py:SourceReadService.list",
        "_research_service.py:ResearchService._invoke",
        "_note_service.py:NoteService.create_note",
        "_note_service.py:NoteService.create_note._finalize_then_cleanup",
        "_note_service.py:NoteService.delete_note",
        "_note_service.py:NoteService.get_note_or_none",
        "_note_service.py:NoteService.list_notes",
        "_note_service.py:NoteService._list_mind_map_records",
        "_note_service.py:NoteService.generate_mind_map",
        "_note_service.py:NoteService.update_note",
        "_studio/audio.py:AudioFamilyService.generate",
        "_studio/catalog.py:StudioCatalog.get_record",
        "_studio/catalog.py:StudioCatalog.list_records",
        "_studio/interactive.py:InteractiveFamilyService.generate_flashcards",
        "_studio/interactive.py:InteractiveFamilyService.generate_quiz",
        "_studio/visuals.py:VisualFamilyService.generate_infographic",
        "_studio/visuals.py:VisualFamilyService.generate_slide_deck",
        "_studio/documents.py:ReportFamilyService.generate",
        "_studio/documents.py:VideoFamilyService.generate",
        "_studio/data_views.py:DataTableFamilyService.generate",
        "_studio/data_views.py:NoteBackedMindMapFamilyService.generate",
        "_studio/exports.py:DriveExportService.export",
        "_studio/mind_maps.py:MindMapFamilyService.delete",
        "_studio/mind_maps.py:MindMapFamilyService.generate",
        "_studio/mind_maps.py:MindMapFamilyService.get_tree",
        "_studio/mind_maps.py:MindMapFamilyService.rename",
        "_studio/lifecycle.py:ArtifactLifecycleService.observe",
        "_studio/management.py:ReportSuggestionService.suggest",
        "_studio/management.py:StudioManagementService.delete",
        "_studio/management.py:StudioManagementService.rename",
        "_studio/management.py:StudioManagementService.retry",
        "_studio/management.py:StudioManagementService.revise_slide",
        "_studio/representations.py:ArtifactRepresentationService._get_content",
        "_studio/representations.py:ArtifactRepresentationService._list_mind_maps",
        "_studio/representations.py:ArtifactRepresentationService._list_representations",
        "_mutation_services.py:SourceUrlMutationService.add_url",
        "_sharing_service.py:SharingService.get_status",
        "_sharing_service.py:SharingService.remove_user",
        "_sharing_service.py:SharingService.set_public",
        "_sharing_service.py:SharingService.set_users",
        "_sharing_service.py:SharingService.set_view_level",
        "_settings_service.py:SettingsService.get_account_limits",
        "_settings_service.py:SettingsService.get_output_language",
        "_settings_service.py:SettingsService.get_user_settings",
        "_settings_service.py:SettingsService.set_output_language",
        "_suggestion_service.py:SuggestionService.suggest_prompts",
        "_suggestion_service.py:SuggestionService.suggest_reports",
        "_mutation_services.py:SourceUrlMutationService.finalize_title",
        "_source_service.py:SourceService.add_drive",
        "_source_service.py:SourceService.add_drive_file",
        "_source_service.py:SourceService.add_file",
        "_source_service.py:SourceService.add_text",
        "_source_service.py:SourceService.add_urls_batch",
        "_source_service.py:SourceService.finalize_drive_title",
        "_source_service.py:SourceService.finalize_file_title",
        "_source_service.py:SourceService.check_freshness",
        "_source_service.py:SourceService.delete",
        "_source_service.py:SourceService.get_fulltext",
        "_source_service.py:SourceService.get_guide",
        "_source_service.py:SourceService.refresh",
        "_source_service.py:SourceService.update",
        "_chat/service.py:ChatService.ask",
        "_chat/service.py:ChatService.configure",
        "_chat/service.py:ChatService.delete_history",
        "_chat/service.py:ChatService.get_conversation_id",
        "_chat/service.py:ChatService.get_history",
        "_chat/service.py:ChatService.save_note",
        "_source_service.py:SourceService.wait_snapshot",
    }
)
INERT_P1_BACKEND_INVOKE_SITES: frozenset[str] = frozenset()

# Final notebook semantic slice: guide generation, recent-list removal, and
# source-id extraction now cross the typed backend boundary instead of the
# public facade owning raw positional/RPC execution.
REVIEWED_BACKEND_IMPORTS -= frozenset(
    {("_notebooks.py", "_projectors", "project_notebook_description")}
)
REVIEWED_BACKEND_IMPORTS |= frozenset(
    {
        ("_notebook_guide_service.py", "_backend", "BackendAdapter"),
        ("_notebook_guide_service.py", "_projectors", "project_notebook_description"),
        ("_notebook_guide_service.py", "_records", "NOTEBOOK_DESCRIBE_DEF"),
        ("_notebook_guide_service.py", "_records", "NOTEBOOK_SUMMARIZE_DEF"),
        ("_notebook_guide_service.py", "_records", "NotebookGuideInput"),
        ("_notebook_mutation_service.py", "_records", "NOTEBOOK_REMOVE_RECENT_DEF"),
        ("_notebook_mutation_service.py", "_records", "NotebookRemoveRecentInput"),
        ("_sharing_manager.py", "_backend", "BackendAdapter"),
        ("_sharing_manager.py", "_backend", "BackendError"),
        ("_sharing_manager.py", "_backend_compat", "project_backend_error"),
        ("_sharing_manager.py", "_records", "LEGACY_SHARE_ARTIFACT_DEF"),
        ("_sharing_manager.py", "_records", "LegacyShareArtifactInput"),
        ("_web/registry.py", "_records", "NOTEBOOK_DESCRIBE_DEF"),
        ("_web/registry.py", "_records", "NOTEBOOK_REMOVE_RECENT_DEF"),
        ("_web/registry.py", "_records", "NOTEBOOK_SUMMARIZE_DEF"),
        ("_web/registry.py", "_records", "LEGACY_SHARE_ARTIFACT_DEF"),
        # P9.3 sharing codec rows and their row-facing codec helpers.
        ("_web/bindings/sharing.py", "_binding", "Binding"),
        ("_web/bindings/sharing.py", "_binding", "CodecBinding"),
        ("_web/bindings/sharing.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/sharing.py", "_records", "LEGACY_SHARE_ARTIFACT_DEF"),
        ("_web/bindings/sharing.py", "_records", "SHARING_GET_DEF"),
        ("_web/bindings/sharing.py", "codec", "sharing"),
        ("_web/codec/sharing.py", "_binding", "CodecPayload"),
        ("_web/codec/sharing.py", "_records", "LegacyShareArtifactInput"),
        ("_web/codec/sharing.py", "_records", "LegacyShareArtifactResult"),
        ("_web/codec/sharing.py", "_records", "SharingGetInput"),
        ("_web/codec/sharing.py", "_records", "SharingGetResult"),
        # P9.3 source codec rows and their row-facing codec helpers; the
        # ``source.get_fulltext`` decoder raises the legacy not-found identity.
        ("_web/bindings/sources.py", "_binding", "Binding"),
        ("_web/bindings/sources.py", "_binding", "CodecBinding"),
        ("_web/bindings/sources.py", "_binding", "DeadlineMode"),
        ("_web/bindings/sources.py", "_binding", "NativeCallSpec"),
        ("_web/bindings/sources.py", "_records", "SOURCE_CHECK_FRESHNESS_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_DELETE_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_GET_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_GET_FULLTEXT_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_GET_GUIDE_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_LIST_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_REFRESH_DEF"),
        ("_web/bindings/sources.py", "_records", "SOURCE_WAIT_DEF"),
        ("_web/bindings/sources.py", "codec", "sources"),
        ("_web/codec/sources.py", "_backend", "BackendError"),
        ("_web/codec/sources.py", "_backend", "BackendErrorReason"),
        ("_web/codec/sources.py", "_binding", "CodecPayload"),
        ("_web/codec/sources.py", "_records", "SourceDeleteInput"),
        ("_web/codec/sources.py", "_records", "SourceDeleteResult"),
        ("_web/codec/sources.py", "_records", "SourceFreshnessInput"),
        ("_web/codec/sources.py", "_records", "SourceFreshnessResult"),
        ("_web/codec/sources.py", "_records", "SourceFulltextInput"),
        ("_web/codec/sources.py", "_records", "SourceFulltextResult"),
        ("_web/codec/sources.py", "_records", "SourceGetInput"),
        ("_web/codec/sources.py", "_records", "SourceGetResult"),
        ("_web/codec/sources.py", "_records", "SourceGuideInput"),
        ("_web/codec/sources.py", "_records", "SourceGuideResult"),
        ("_web/codec/sources.py", "_records", "SourceListInput"),
        ("_web/codec/sources.py", "_records", "SourceListResult"),
        ("_web/codec/sources.py", "_records", "SourceRefreshInput"),
        ("_web/codec/sources.py", "_records", "SourceRefreshResult"),
        ("_web/codec/sources.py", "_records", "SourceWaitSnapshotInput"),
        ("_web/codec/sources.py", "_records", "SourceWaitSnapshotResult"),
    }
)
ACTIVE_BACKEND_INVOKE_SITES |= frozenset(
    {
        "_notebook_guide_service.py:NotebookGuideService.get_description",
        "_notebook_guide_service.py:NotebookGuideService.get_summary",
        "_notebook_mutation_service.py:NotebookMutationService.remove_from_recent",
        "_read_services.py:NotebookReadService.get_source_ids",
        "_sharing_manager.py:ShareManager.share",
    }
)

# P4.2 moves artifact kickoff construction/projection into the one private
# caller-budgeted facade workflow; P7 renames the production composition root.
# Keep the exact import inventory fail-closed across both cohesive moves.
REVIEWED_BACKEND_IMPORTS -= frozenset(
    {
        ("_artifacts.py", "_records", "ArtifactReviseSlideInput"),
        ("_artifacts.py", "_records", "AudioGenerateInput"),
        ("_artifacts.py", "_records", "DataTableGenerateInput"),
        ("_artifacts.py", "_records", "InfographicGenerateInput"),
        ("_artifacts.py", "_records", "InteractiveGenerateInput"),
        ("_artifacts.py", "_records", "ReportGenerateInput"),
        ("_artifacts.py", "_records", "SlideDeckGenerateInput"),
        ("_artifacts.py", "_records", "VideoGenerateInput"),
        ("_artifacts.py", "_studio", "DocumentOptionError"),
        ("_client_assembly.py", "_note_service", "LegacyNoteBackedService"),
        ("_client_assembly.py", "_note_service", "NoteService"),
        ("_client_assembly.py", "_studio", "MindMapFamilyService"),
        ("_client_assembly.py", "_studio", "StudioCatalog"),
        ("_client_assembly.py", "_web.backend", "WebRpcBackend"),
    }
)
REVIEWED_BACKEND_IMPORTS |= frozenset(
    {
        ("_artifact/generation_workflow.py", "_backend", "BackendError"),
        ("_artifact/generation_workflow.py", "_backend_compat", "project_backend_call"),
        ("_artifact/generation_workflow.py", "_backend_compat", "project_backend_error"),
        ("_artifact/generation_workflow.py", "_projectors", "project_generation_status"),
        ("_artifact/generation_workflow.py", "_records", "ArtifactReviseSlideInput"),
        ("_artifact/generation_workflow.py", "_records", "AudioGenerateInput"),
        ("_artifact/generation_workflow.py", "_records", "DataTableGenerateInput"),
        ("_artifact/generation_workflow.py", "_records", "InfographicGenerateInput"),
        ("_artifact/generation_workflow.py", "_records", "InteractiveGenerateInput"),
        ("_artifact/generation_workflow.py", "_records", "ReportGenerateInput"),
        ("_artifact/generation_workflow.py", "_records", "SlideDeckGenerateInput"),
        ("_artifact/generation_workflow.py", "_records", "VideoGenerateInput"),
        ("_artifact/generation_workflow.py", "_studio", "ArtifactLifecycleService"),
        ("_artifact/generation_workflow.py", "_studio", "AudioFamilyService"),
        ("_artifact/generation_workflow.py", "_studio", "DataTableFamilyService"),
        ("_artifact/generation_workflow.py", "_studio", "DocumentOptionError"),
        ("_artifact/generation_workflow.py", "_studio", "InteractiveFamilyService"),
        ("_artifact/generation_workflow.py", "_studio", "ReportFamilyService"),
        ("_artifact/generation_workflow.py", "_studio", "StudioManagementService"),
        ("_artifact/generation_workflow.py", "_studio", "VideoFamilyService"),
        ("_artifact/generation_workflow.py", "_studio", "VisualFamilyService"),
        ("_client_composition.py", "_note_service", "LegacyNoteBackedService"),
        ("_client_composition.py", "_note_service", "NoteService"),
        ("_client_composition.py", "_studio", "MindMapFamilyService"),
        ("_client_composition.py", "_studio", "StudioCatalog"),
        ("_client_composition.py", "_web.backend", "WebRpcBackend"),
    }
)
REVIEWED_BACKEND_IMPORTS -= frozenset(
    {
        ("_artifact/generation_workflow.py", "_backend", "BackendError"),
        ("_artifact/generation_workflow.py", "_backend_compat", "project_backend_error"),
    }
)
# P9.2 splits the registry-free probe-then-retry wrapper out of ``_idempotency``;
# it consumes the neutral commit-uncertainty predicate over ``BackendError``.
REVIEWED_BACKEND_IMPORTS |= frozenset(
    {
        ("_idempotency_create.py", "_backend", "BackendError"),
        ("_idempotency_create.py", "_backend", "may_have_committed"),
    }
)
# P9.4a: the head projects a custom row's ``error_mode``; the sharing composites
# are ``CustomBinding`` rows whose handlers and phase payloads name the records.
REVIEWED_BACKEND_IMPORTS |= frozenset(
    {
        ("_web/backend.py", "_binding", "CustomBinding"),
        ("_web/backend.py", "_binding", "ErrorMode"),
        ("_web/bindings/sharing.py", "_binding", "CustomBinding"),
        ("_web/bindings/sharing.py", "_binding", "RowInvoker"),
        ("_web/bindings/sharing.py", "_records", "SHARING_SET_PUBLIC_DEF"),
        ("_web/bindings/sharing.py", "_records", "SHARING_SET_VIEW_LEVEL_DEF"),
        ("_web/bindings/sharing.py", "_records", "SHARING_UPDATE_USERS_DEF"),
        ("_web/bindings/sharing.py", "_records", "SharingSetPublicInput"),
        ("_web/bindings/sharing.py", "_records", "SharingSetPublicResult"),
        ("_web/bindings/sharing.py", "_records", "SharingSetViewLevelInput"),
        ("_web/bindings/sharing.py", "_records", "SharingSetViewLevelResult"),
        ("_web/bindings/sharing.py", "_records", "SharingUpdateUsersInput"),
        ("_web/bindings/sharing.py", "_records", "SharingUpdateUsersResult"),
        ("_web/codec/sharing.py", "_records", "SharingSetPublicInput"),
        ("_web/codec/sharing.py", "_records", "SharingSetViewLevelInput"),
        ("_web/codec/sharing.py", "_records", "SharingUpdateUsersInput"),
    }
)

# Facades that still own RpcCaller paths take the backend as the reviewed
# ``_backend=`` or ``backend=`` keyword beside their executor; a facade whose
# whole wire surface has migrated takes it as its sole positional collaborator.
_KEYWORD_BACKEND_FACADES = frozenset(
    {
        "ArtifactsAPI",
        "ChatAPI",
        "MindMapFamilyService",
        "NoteService",
        "NotebooksAPI",
        "ResearchAPI",
        "ShareManager",
        "SettingsAPI",
        "SharingAPI",
        "SourcesAPI",
        "StudioCatalog",
    }
)
_POSITIONAL_BACKEND_FACADES = frozenset({"CollectionsAPI", "LabelsAPI"})


def audit_inert_p1_backend_dataflow(
    source_overrides: Mapping[str, str] | None = None,
) -> list[str]:
    """Prove backend dataflow is limited to the reviewed migrated service slice."""

    overrides = source_overrides or {}
    observed_imports: set[tuple[str, str, str]] = set()
    invoke_sites: set[str] = set()
    assembly_backend_bindings: list[str] = []
    assembly_backend_escapes: list[int] = []
    assembly_constructor_targets: list[tuple[str, ...]] = []

    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        source = overrides.get(relative, path.read_text(encoding="utf-8"))
        tree = ast.parse(source, filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }

        class InvokeVisitor(ast.NodeVisitor):
            def __init__(self, module: str) -> None:
                self.module = module
                self.stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

            def visit_Call(self, node: ast.Call) -> None:
                if _attribute_parts(node.func)[-1:] == ("invoke",):
                    invoke_sites.add(f"{self.module}:{_qualname(self.stack)}")
                self.generic_visit(node)

        InvokeVisitor(relative).visit(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                observed_imports.update(
                    (relative, alias.name, "*")
                    for alias in node.names
                    if _is_reviewed_backend_import_module(alias.name)
                )
            if isinstance(node, ast.ImportFrom):
                if node.module is not None and (
                    _is_reviewed_backend_import_module(node.module)
                    or (relative.startswith("_web/") and node.module in {"backend", "registry"})
                ):
                    observed_imports.update(
                        (relative, node.module, alias.name) for alias in node.names
                    )
                elif node.module is None:
                    observed_imports.update(
                        (relative, "." * node.level, alias.name)
                        for alias in node.names
                        if alias.name
                        in {
                            "_backend",
                            "_binding",
                            "_backend_compat",
                            "_label_service",
                            "_mutation_services",
                            "_notebook_mutation_service",
                            "_projectors",
                            "_read_services",
                            "_records",
                            "_sharing_service",
                            "_research_service",
                            "_source_service",
                            "_web",
                        }
                    )
            if relative == "_client_composition.py":
                if (
                    isinstance(node, ast.Attribute)
                    and _attribute_parts(node) == ("client", "_backend")
                    and isinstance(node.ctx, ast.Load)
                ):
                    parent = parents.get(node)
                    # A facade takes the backend either as the reviewed private
                    # ``_backend=`` keyword (a facade that still owns RpcCaller
                    # paths) or as its sole positional collaborator (a fully
                    # migrated facade, which no longer accepts an RpcCaller).
                    keyword_parent = parent if isinstance(parent, ast.keyword) else None
                    call = parents.get(keyword_parent) if keyword_parent is not None else parent
                    facade_name = (
                        _attribute_parts(call.func)[-1]
                        if isinstance(call, ast.Call) and _attribute_parts(call.func)
                        else None
                    )
                    keyword_binding = (
                        keyword_parent is not None
                        and keyword_parent.arg in {"_backend", "backend"}
                        and facade_name in _KEYWORD_BACKEND_FACADES
                    )
                    positional_binding = (
                        keyword_parent is None
                        and isinstance(call, ast.Call)
                        and facade_name in _POSITIONAL_BACKEND_FACADES
                        and bool(call.args)
                        and call.args[0] is node
                    )
                    if isinstance(facade_name, str) and (keyword_binding or positional_binding):
                        assembly_backend_bindings.append(facade_name)
                    else:
                        assembly_backend_escapes.append(node.lineno)
                if isinstance(node, ast.Call) and _attribute_parts(node.func)[-1:] == (
                    "WebRpcBackend",
                ):
                    parent = parents.get(node)
                    targets = parent.targets if isinstance(parent, ast.Assign) else []
                    assembly_constructor_targets.extend(
                        _attribute_parts(target) for target in targets
                    )

    errors: list[str] = []
    if observed_imports != REVIEWED_BACKEND_IMPORTS:
        errors.append(
            "reviewed backend imports changed: "
            f"missing={sorted(REVIEWED_BACKEND_IMPORTS - observed_imports)}, "
            f"extra={sorted(observed_imports - REVIEWED_BACKEND_IMPORTS)}"
        )
    expected_invokes = ACTIVE_BACKEND_INVOKE_SITES | INERT_P1_BACKEND_INVOKE_SITES
    if invoke_sites != expected_invokes:
        errors.append(
            "semantic backend invoke sites changed: "
            f"missing={sorted(expected_invokes - invoke_sites)}, "
            f"extra={sorted(invoke_sites - expected_invokes)}"
        )
    if assembly_constructor_targets != [("client", "_backend")]:
        errors.append(
            f"P1 WebRpcBackend construction target changed: {assembly_constructor_targets}"
        )
    expected_bindings = sorted(_KEYWORD_BACKEND_FACADES | _POSITIONAL_BACKEND_FACADES)
    if sorted(assembly_backend_bindings) != expected_bindings:
        errors.append(
            "migrated facade backend bindings changed: "
            f"expected={expected_bindings}, "
            f"actual={sorted(assembly_backend_bindings)}"
        )
    if assembly_backend_escapes:
        errors.append(
            "client._backend escapes the reviewed facade bindings at lines: "
            f"{sorted(assembly_backend_escapes)}"
        )
    return errors


def audit_inert_p1_web_sites(sites: frozenset[str] | None = None) -> list[str]:
    """Fail closed when the remaining bounded P1 no-authority set drifts."""
    actual = INERT_P1_WEB_SITES if sites is None else sites
    expected = INERT_P1_WEB_FORWARDERS | INERT_P1_WEB_HANDLERS
    errors: list[str] = []
    if actual != expected:
        errors.append(
            "inert P1 web site classification changed: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    function_sites = collect_function_sites()
    if missing := sorted(actual - function_sites):
        errors.append(f"inert P1 web sites no longer exist: {missing}")
    return errors


def collect_unresolved_rpc_dispatches() -> list[str]:
    """Return feature RPC calls whose method/variant cannot be statically resolved."""
    unresolved: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        if relative.startswith(("rpc/", "_row_adapters/", "_types/")) or relative in {
            "_idempotency_policy.py",
            "_rpc_executor.py",
            "_web/runtime.py",
        }:
            continue
        collector = _ReferenceCollector(relative, set())
        collector.visit(_parse(path))
        for owner, field in collector.unresolved_rpc_calls:
            site = f"{relative}:{owner}"
            if site not in GENERIC_RPC_FORWARDERS | INERT_P1_WEB_FORWARDERS:
                unresolved.add(f"{site} ({field})")
    return sorted(unresolved)


def collect_function_sites() -> set[str]:
    """Return every production function/method qualname as an exact source site."""
    sites: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self, relative: str) -> None:
            self.relative = relative
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.stack.append(node.name)
            sites.add(f"{self.relative}:{_qualname(self.stack)}")
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        Visitor(relative).visit(_parse(path))
    return sites


_IGNORED_SEMANTIC_AST_FIELDS = frozenset({"ctx", "kind", "type_comment", "type_params"})


def _semantic_ast_value(value: object) -> object:
    if isinstance(value, ast.AST):
        return _semantic_ast_shape(value)
    if isinstance(value, (list, tuple)):
        return tuple(_semantic_ast_value(item) for item in value)
    return value


def _semantic_ast_shape(node: ast.AST) -> tuple[object, ...]:
    """Return a Python-version-neutral semantic representation of an AST node."""

    fields = tuple(
        (name, _semantic_ast_value(value))
        for name, value in ast.iter_fields(node)
        if name not in _IGNORED_SEMANTIC_AST_FIELDS
    )
    return type(node).__name__, fields


def _semantic_ast_fingerprint(node: ast.AST) -> str:
    normalized = repr(_semantic_ast_shape(node)).encode("utf-8")
    return f"sha256:{hashlib.sha256(normalized).hexdigest()}"


def collect_function_ast_fingerprints() -> dict[str, str]:
    """Return stable AST fingerprints for every production function/method."""

    fingerprints: dict[str, str] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self, relative: str) -> None:
            self.relative = relative
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.stack.append(node.name)
            site = f"{self.relative}:{_qualname(self.stack)}"
            fingerprints[site] = _semantic_ast_fingerprint(node)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        Visitor(relative).visit(_parse(path))
    return fingerprints


def collect_function_call_targets() -> dict[str, set[tuple[str, ...]]]:
    """Return call-target attribute paths keyed by exact production function site."""
    calls: dict[str, set[tuple[str, ...]]] = defaultdict(set)

    class Visitor(ast.NodeVisitor):
        def __init__(self, relative: str) -> None:
            self.relative = relative
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def visit_Call(self, node: ast.Call) -> None:
            if self.stack and (target := _attribute_parts(node.func)):
                calls[f"{self.relative}:{_qualname(self.stack)}"].add(target)
            self.generic_visit(node)

    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT).as_posix()
        Visitor(relative).visit(_parse(path))
    return calls


def _declared_module_exports(relative: str) -> set[str]:
    """Return literal names in one production module's ``__all__``."""

    tree = _parse(SRC_ROOT / relative)
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            continue
        if not isinstance(statement.value, (ast.List, ast.Tuple)):
            return set()
        return {
            value for item in statement.value.elts if (value := _literal_string(item)) is not None
        }
    return set()


def collect_app_authority_source_evidence() -> dict[str, dict[str, object]]:
    """Derive helper-body and internal-call-edge evidence for app authorities."""

    call_targets = collect_function_call_targets()
    fingerprints = collect_function_ast_fingerprints()
    evidence: dict[str, dict[str, object]] = {}
    for site, contract in sorted(APP_AUTHORITY_SOURCE_CONTRACTS.items()):
        helper_targets = call_targets.get(site, set())
        observed_required_calls = sorted(
            ".".join(required)
            for required in contract.required_calls
            if any(target[-len(required) :] == required for target in helper_targets)
        )
        internal_callers = sorted(
            caller
            for caller, targets in call_targets.items()
            if any(
                target[-len(contract.caller_target) :] == contract.caller_target
                for target in targets
            )
        )
        evidence[site] = {
            "function_ast_sha256": fingerprints.get(site),
            "observed_required_calls": observed_required_calls,
            "public_export": contract.public_export
            if contract.public_export in _declared_module_exports(site.split(":", 1)[0])
            else None,
            "internal_call_edges": [
                {
                    "caller": caller,
                    "caller_ast_sha256": fingerprints.get(caller),
                    "target": ".".join(contract.caller_target),
                }
                for caller in internal_callers
            ],
        }
    return evidence


def _operation_authorities(
    spec: OperationSpec,
    native_execution_sites: Mapping[NativeKey, list[str]],
    shared_bindings: set[NativeKey],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for binding in spec.native_bindings:
        if binding in shared_bindings:
            rules = SHARED_RPC_AUTHORITY_RULES.get((spec.operation, binding), ())
        else:
            rules = _rules(
                *(
                    (site, "direct semantic binding")
                    for site in native_execution_sites.get(binding, [])
                )
            )
        rows.extend(
            {
                "transport_kind": "rpc",
                "binding": _native_key_text(binding),
                "site": rule.site,
                "discriminator": rule.discriminator,
            }
            for rule in rules
        )
    rows.extend(
        {
            "transport_kind": transport_kind,
            "binding": binding,
            "site": site,
            "discriminator": discriminator,
        }
        for transport_kind, binding, site, discriminator in NON_RPC_AUTHORITY_RULES.get(
            spec.operation, ()
        )
    )
    rows.extend(
        {
            "transport_kind": "orchestrator",
            "binding": rule.binding,
            "site": rule.site,
            "discriminator": rule.discriminator,
        }
        for rule in APP_OPERATION_AUTHORITIES.get(spec.operation, ())
    )
    return sorted(
        rows,
        key=lambda row: (
            row["transport_kind"],
            row["binding"],
            row["site"],
            row["discriminator"],
        ),
    )


def audit_operation_authorities() -> list[str]:
    """Validate exact RPC, non-RPC, and app authority rules bidirectionally."""
    errors: list[str] = []
    native_sites = collect_native_execution_sites()
    specs_by_native: dict[NativeKey, list[OperationSpec]] = defaultdict(list)
    for spec in OPERATION_SPECS:
        for binding in spec.native_bindings:
            specs_by_native[binding].append(spec)
    shared = {binding for binding, specs in specs_by_native.items() if len(specs) > 1}
    expected_rule_keys = {
        (spec.operation, binding) for binding in shared for spec in specs_by_native[binding]
    }
    actual_rule_keys = set(SHARED_RPC_AUTHORITY_RULES)
    if missing := sorted(
        f"{operation.value}/{_native_key_text(binding)}"
        for operation, binding in expected_rule_keys - actual_rule_keys
    ):
        errors.append(f"shared operation bindings lack exact authority rules: {missing}")
    if stale := sorted(
        f"{operation.value}/{_native_key_text(binding)}"
        for operation, binding in actual_rule_keys - expected_rule_keys
    ):
        errors.append(f"stale shared operation authority rules: {stale}")
    for (operation, binding), rules in SHARED_RPC_AUTHORITY_RULES.items():
        valid_sites = set(native_sites.get(binding, []))
        for rule in rules:
            if rule.site not in valid_sites:
                errors.append(
                    f"{operation.value}/{_native_key_text(binding)} authority is not a direct "
                    f"binding site: {rule.site}"
                )
            if not rule.discriminator.strip():
                errors.append(
                    f"{operation.value}/{_native_key_text(binding)} has an empty discriminator"
                )

    function_sites = collect_function_sites() | collect_binding_sites()
    manual_non_rpc_sites = {
        site
        for rules in NON_RPC_AUTHORITY_RULES.values()
        for _kind, _binding, site, _discriminator in rules
    }
    if manual_non_rpc_sites != set(NON_RPC_SOURCE_CONTRACTS):
        errors.append(
            "non-RPC authority/source contracts disagree: "
            f"uncontracted={sorted(manual_non_rpc_sites - set(NON_RPC_SOURCE_CONTRACTS))}, "
            f"unallocated={sorted(set(NON_RPC_SOURCE_CONTRACTS) - manual_non_rpc_sites)}"
        )
    call_targets = collect_function_call_targets()
    for site, required_targets in NON_RPC_SOURCE_CONTRACTS.items():
        actual_targets = call_targets.get(site, set())
        for required in required_targets:
            if not any(target[-len(required) :] == required for target in actual_targets):
                errors.append(
                    f"non-RPC authority {site} no longer reaches transport call "
                    f"{'.'.join(required)}"
                )
    manual_app_sites = {rule.site for rules in APP_OPERATION_AUTHORITIES.values() for rule in rules}
    if unallocated_contracts := sorted(set(APP_AUTHORITY_SOURCE_CONTRACTS) - manual_app_sites):
        errors.append(f"app authority source contracts are unallocated: {unallocated_contracts}")
    public_helper_sites = {
        rule.site
        for rules in APP_OPERATION_AUTHORITIES.values()
        for rule in rules
        if rule.binding == "public_helper"
    }
    if uncontracted_helpers := sorted(public_helper_sites - set(APP_AUTHORITY_SOURCE_CONTRACTS)):
        errors.append(
            f"public-helper app authorities lack source contracts: {uncontracted_helpers}"
        )
    fingerprints = collect_function_ast_fingerprints()
    for site, contract in APP_AUTHORITY_SOURCE_CONTRACTS.items():
        if site not in fingerprints:
            errors.append(f"app authority helper no longer exists: {site}")
            continue
        actual_targets = call_targets.get(site, set())
        for required in contract.required_calls:
            if not any(target[-len(required) :] == required for target in actual_targets):
                errors.append(
                    f"app authority {site} no longer reaches required loop call "
                    f"{'.'.join(required)}"
                )
        module_exports = _declared_module_exports(site.split(":", 1)[0])
        if contract.public_export not in module_exports:
            errors.append(f"app authority {site} lost public export {contract.public_export}")
        internal_callers = {
            caller
            for caller, targets in call_targets.items()
            if any(
                target[-len(contract.caller_target) :] == contract.caller_target
                for target in targets
            )
        }
        if internal_callers != {contract.internal_caller}:
            errors.append(
                f"app authority {site} internal callers changed: "
                f"expected={[contract.internal_caller]}, actual={sorted(internal_callers)}"
            )
    for spec in OPERATION_SPECS:
        expected_app = {rule.site for rule in APP_OPERATION_AUTHORITIES.get(spec.operation, ())}
        actual_app = set(spec.app_authorities)
        if expected_app != actual_app:
            errors.append(
                f"{spec.operation.value} app authorities disagree with reviewed rules: "
                f"spec={sorted(actual_app)}, rules={sorted(expected_app)}"
            )
        expected_paths = {
            binding
            for _kind, binding, _site, _discriminator in NON_RPC_AUTHORITY_RULES.get(
                spec.operation, ()
            )
        }
        if set(spec.web_paths) != expected_paths:
            errors.append(
                f"{spec.operation.value} non-RPC bindings disagree: "
                f"spec={sorted(spec.web_paths)}, rules={sorted(expected_paths)}"
            )
        for row in _operation_authorities(spec, native_sites, shared):
            if row["site"] not in function_sites:
                errors.append(
                    f"{spec.operation.value} authority path no longer exists: {row['site']}"
                )
    for binding, reason in NATIVE_BINDING_DISPOSITIONS.items():
        if native_sites.get(binding):
            errors.append(
                f"{_native_key_text(binding)} is disposed as callsite-free but now executes at "
                f"{native_sites[binding]} ({reason})"
            )
    errors.extend(audit_inert_p1_web_sites())
    errors.extend(audit_inert_p1_backend_dataflow())
    errors.extend(audit_row_bindings())
    if unresolved := collect_unresolved_rpc_dispatches():
        errors.append(f"unresolved feature RPC calls: {unresolved}")
    if unresolved_app := collect_unresolved_app_dispatches():
        errors.append(f"unresolved dynamic _app namespace dispatches: {unresolved_app}")
    return errors


def audit_recency_contracts() -> list[str]:
    """Validate structured GET_NOTEBOOK counts and their source authorities."""
    errors: list[str] = []
    required = {spec.operation for spec in OPERATION_SPECS if spec.recency_effect != "none"}
    if missing := sorted(operation.value for operation in required - set(RECENCY_CONTRACTS)):
        errors.append(f"recency-effect prose lacks a structured contract: {missing}")
    if stale := sorted(operation.value for operation in set(RECENCY_CONTRACTS) - required):
        errors.append(f"structured recency contracts have no reviewed operation row: {stale}")
    specs = {spec.operation: spec for spec in OPERATION_SPECS}
    for operation, rules in RECENCY_CONTRACTS.items():
        spec = specs.get(operation)
        if spec is None:
            continue
        covered_public = {method for rule in rules for method in rule.public_methods}
        if covered_public != set(spec.public_methods):
            errors.append(
                f"{operation.value} recency contracts do not partition every public method: "
                f"missing={sorted(set(spec.public_methods) - covered_public)}, "
                f"unrelated={sorted(covered_public - set(spec.public_methods))}"
            )
        valid_authorities = {
            rule.site
            for rule in SHARED_RPC_AUTHORITY_RULES.get((operation, _b(RPCMethod.GET_NOTEBOOK)), ())
        }
        for rule in rules:
            if rule.minimum_calls < 0 or (
                rule.maximum_calls is not None and rule.maximum_calls < rule.minimum_calls
            ):
                errors.append(f"{operation.value} has an invalid recency call range")
            if not rule.unit or not rule.condition.strip():
                errors.append(f"{operation.value} recency contract lacks unit/condition")
            if not set(rule.public_methods) <= set(spec.public_methods):
                errors.append(f"{operation.value} recency contract names unrelated public methods")
            if rule.maximum_calls != 0 and not rule.authority_sites:
                errors.append(f"{operation.value} recency contract lacks GET_NOTEBOOK authorities")
            if not set(rule.authority_sites) <= valid_authorities:
                errors.append(
                    f"{operation.value} recency authorities disagree with exact RPC rules: "
                    f"{sorted(set(rule.authority_sites) - valid_authorities)}"
                )

    metadata_rules = RECENCY_CONTRACTS.get(Operation.NOTEBOOK_METADATA, ())
    if len(metadata_rules) != 1 or (
        metadata_rules[0].minimum_calls,
        metadata_rules[0].maximum_calls,
        set(metadata_rules[0].authority_sites),
    ) != (2, 2, {_GET_TYPED, _GET_SOURCES}):
        errors.append(
            "notebook.get_metadata must pin exactly two distinct GET_NOTEBOOK authorities"
        )
    update_rules = RECENCY_CONTRACTS.get(Operation.NOTEBOOK_UPDATE, ())
    if len(update_rules) != 1 or (
        update_rules[0].minimum_calls,
        update_rules[0].maximum_calls,
        set(update_rules[0].public_methods),
    ) != (1, 1, set(_p("notebooks", "update", "rename", "set_emoji"))):
        errors.append("notebook.update must pin exactly one GET_NOTEBOOK for every public mutation")
    chat_rules = RECENCY_CONTRACTS.get(Operation.CHAT_CONFIGURE, ())
    chat_ranges = {
        tuple(sorted(rule.public_methods)): (rule.minimum_calls, rule.maximum_calls)
        for rule in chat_rules
    }
    if chat_ranges != {
        ("chat.get_settings",): (1, 1),
        ("chat.configure", "chat.set_mode"): (0, 0),
    }:
        errors.append("chat.configure must split read and mutation recency conditions")
    metadata_tree = _parse(SRC_ROOT / "_notebook_metadata.py")
    metadata_fn = _find_class_method(metadata_tree, "NotebookMetadataService", "get_metadata")
    task_shapes: list[tuple[str, ...]] = []
    gather_shapes: list[tuple[list[tuple[str, ...]], tuple[tuple[str | None, object], ...]]] = []
    cancel_calls = 0
    if metadata_fn is not None:
        for call in (node for node in ast.walk(metadata_fn) if isinstance(node, ast.Call)):
            if (
                _attribute_parts(call.func)[-2:]
                in {("asyncio", "create_task"), ("asyncio", "ensure_future")}
                and len(call.args) == 1
                and isinstance(call.args[0], ast.Call)
            ):
                task_shapes.append(_attribute_parts(call.args[0].func))
            if _attribute_parts(call.func)[-2:] == ("task", "cancel"):
                cancel_calls += 1
            if _attribute_parts(call.func)[-2:] == ("asyncio", "gather"):
                gather_shapes.append(
                    (
                        [
                            _attribute_parts(arg.func)
                            if isinstance(arg, ast.Call)
                            else ((arg.value.id,) if isinstance(arg.value, ast.Name) else ())
                            for arg in call.args
                            if isinstance(arg, (ast.Call, ast.Starred))
                        ],
                        tuple(
                            (
                                keyword.arg,
                                keyword.value.value
                                if isinstance(keyword.value, ast.Constant)
                                else None,
                            )
                            for keyword in call.keywords
                        ),
                    )
                )
    catches_base_exception = metadata_fn is not None and any(
        isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "BaseException"
        for node in ast.walk(metadata_fn)
    )
    if (
        task_shapes
        != [
            ("self", "_get_notebook"),
            ("self", "_source_lister", "list"),
        ]
        or gather_shapes
        != [
            ([("tasks",)], ()),
            ([("tasks",)], (("return_exceptions", True),)),
        ]
        or cancel_calls != 1
        or not catches_base_exception
    ):
        errors.append(
            "NotebookMetadataService.get_metadata must create, gather, cancel, and drain exactly "
            "the notebook lookup + source list tasks"
        )

    notebook_rows_tree = _parse(SRC_ROOT / "_web" / "bindings" / "notebooks.py")
    update_row = _find_module_assignment(notebook_rows_tree, "NOTEBOOK_UPDATE")
    update_fn = _find_module_function(notebook_rows_tree, "_notebook_update")
    # P9.4b: the update composite is a custom row; its handler issues exactly one
    # unconditional ``invoke.call("readback", …)`` and the row declares that
    # spec as GET_NOTEBOOK.
    if (
        update_row is None
        or update_fn is None
        or _native_choice_count(update_row, RPCMethod.GET_NOTEBOOK) != 1
        or _invoker_call_count(notebook_rows_tree, update_fn, "readback") != 1
    ):
        errors.append(
            "NOTEBOOK_UPDATE row must perform exactly one unconditional GET_NOTEBOOK readback"
        )
    elif any(
        argument.arg == "return_object"
        for argument in (*update_fn.args.args, *update_fn.args.kwonlyargs)
    ):
        errors.append("NOTEBOOK_UPDATE recency contract forbids a return_object bypass")

    chat_tree = _parse(SRC_ROOT / "_chat" / "api.py")
    expected_chat_gets = {"configure": 0, "set_mode": 0, "get_settings": 0}
    for method_name, expected_gets in expected_chat_gets.items():
        method_node = _find_class_method(chat_tree, "ChatAPI", method_name)
        actual_gets = (
            _rpc_binding_call_count(method_node, RPCMethod.GET_NOTEBOOK)
            if method_node is not None
            else -1
        )
        if actual_gets != expected_gets:
            errors.append(
                f"ChatAPI.{method_name} must contain exactly {expected_gets} GET_NOTEBOOK binding(s)"
            )
    # Since P9.3 ``chat.configure`` is an input-keyed codec row; its declared
    # native choices are the only GET_NOTEBOOK authority the row can select.
    chat_rows_tree = _parse(SRC_ROOT / "_web" / "bindings" / "chat.py")
    configure_row = _find_module_assignment(chat_rows_tree, "CHAT_CONFIGURE")
    if configure_row is None or _native_choice_count(configure_row, RPCMethod.GET_NOTEBOOK) != 1:
        errors.append(
            "_web/bindings/chat.py:CHAT_CONFIGURE must declare exactly one GET_NOTEBOOK native"
        )
    return errors


def _find_module_assignment(tree: ast.Module, name: str) -> ast.expr | None:
    """Return the value assigned to module-level ``name``, if any."""
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value: ast.expr | None = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        if value is not None and any(
            isinstance(target, ast.Name) and target.id == name for target in targets
        ):
            return value
    return None


def _find_module_function(
    tree: ast.Module, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the module-level function ``name``, if any."""
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ),
        None,
    )


def _invoker_call_count(tree: ast.Module, node: ast.AST, spec_key: str) -> int:
    """Count ``invoke.call(<spec_key>, …)`` sites (the custom-row transport verb).

    The key may be a string literal or a module-level constant bound to one.
    """

    def key_of(argument: ast.AST | None) -> str | None:
        if isinstance(argument, ast.Name):
            argument = _find_module_assignment(tree, argument.id)
        return _literal_string(argument) if argument is not None else None

    return sum(
        1
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and _attribute_parts(call.func)[-2:] == ("invoke", "call")
        and key_of(call.args[0] if call.args else None) == spec_key
    )


def _native_choice_count(node: ast.AST, method: RPCMethod) -> int:
    """Count ``NativeChoice(RPCMethod.X …)``/``NativeCallSpec.constant(RPCMethod.X …)`` literals."""
    count = 0
    for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
        parts = _attribute_parts(call.func)
        if not parts or parts[-1] not in {"NativeChoice", "constant"}:
            continue
        if parts[-1] == "constant" and parts[-2:-1] != ("NativeCallSpec",):
            continue
        if _rpc_method_member(_call_argument(call, 0, "method")) == method.name:
            count += 1
    return count


def _find_class_method(
    tree: ast.Module,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef) or statement.name != class_name:
            continue
        return next(
            (
                node
                for node in statement.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            ),
            None,
        )
    return None


def _call_count(node: ast.AST, suffix: tuple[str, ...]) -> int:
    return sum(
        1
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and _attribute_parts(call.func)[-len(suffix) :] == suffix
    )


def _rpc_binding_call_count(node: ast.AST, method: RPCMethod) -> int:
    count = 0
    for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
        if _attribute_parts(call.func)[-1:] not in {("rpc_call",), ("_rpc_call",)}:
            continue
        method_node = (
            call.args[0]
            if call.args
            else next(
                (keyword.value for keyword in call.keywords if keyword.arg == "method"),
                None,
            )
        )
        if method_node is not None and any(
            isinstance(item, ast.Attribute)
            and _attribute_parts(item)[-2:] == ("RPCMethod", method.name)
            for item in ast.walk(method_node)
        ):
            count += 1
    return count
