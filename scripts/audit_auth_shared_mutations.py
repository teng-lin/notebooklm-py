#!/usr/bin/env python3
"""Audit test mutations of auth/browser classes and shared-lifetime owners.

The module-patch scorecard cannot see a patch moved from ``module._STATE`` to
``Owner._STATE`` or ``Owner.process_default().reset()``.  This companion audit
therefore freezes those shared-object operations, including their lexical
owner.  Unknown/dynamic mutation names against a resolved owner fail closed.
Fresh instances assigned inside a test do not resolve as shared owners.

This is a repository-specific shrink-only ratchet, not a general Python or
pytest analyzer. It supports explicit family imports/aliases, direct
attribute/item/namespace mutation, literal finite names and containers without
unpacking, the suite-used syntactic ``list(...)`` wrapper, and direct local
helper forwarding with explicit finite arguments. A resolved
shared owner is counted, a proven-fresh/non-family value is excluded, and an
unresolved family-related target fails closed. Unsupported syntax cannot
justify a metric decrease; make it statically resolvable or preserve the
existing projected row. Unknown ownership is treated conservatively only when
reached from a statically resolved family/shared owner within this grammar.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:  # direct ``python scripts/...`` execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.audit_auth_patch_sites import (
    _AMBIGUOUS_FAMILY_ALIAS,
    AuditError,
    _alias_context,
    _call_arg,
    _definitely_nonfamily_typed_parameters,
    _dotted_name,
    _forwarded_alias_variants,
    _forwarded_parameter_context,
    _function_definitions,
    _literal_constants_context,
    _literal_mapping_keys,
    _literal_strings,
    _literal_values_context,
    _module_string_constants,
    _owner_metadata,
    _scope_declarations,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FAMILY = {
    "notebooklm._auth": REPO_ROOT / "src/notebooklm/_auth",
    "notebooklm._browser": REPO_ROOT / "src/notebooklm/_browser",
}
MUTATORS = {
    "append",
    "clear",
    "discard",
    "extend",
    "pop",
    "popitem",
    "remove",
    "setdefault",
    "update",
}
READ_ONLY_METHODS = {
    "_active_paths_for_tests",
    "_scheduled_paths_for_tests",
    "_workers_for_tests",
    "await_flight",
    "empty",
    "full",
    "is_set",
    "isdisjoint",
    "issubset",
    "locked",
    "process_default",
    "qsize",
    "read_success_epoch",
}


@dataclass(frozen=True, order=True)
class SharedMutation:
    package: str
    owner: str
    attribute: str
    idiom: str
    path: str
    lineno: int
    owner_qualname: str
    owner_kind: str


@dataclass(frozen=True)
class _Inventory:
    classes: dict[str, set[str]]
    singletons: dict[str, set[str]]
    module_aliases: dict[str, dict[str, str]]
    class_aliases: dict[str, dict[str, tuple[str, str]]]
    shared_aliases: dict[str, dict[str, str]]
    gateways: dict[str, dict[str, tuple[tuple[str, str], ...]]]


def _inventory(source: Path, package: str) -> _Inventory:
    classes: dict[str, set[str]] = {}
    singletons: dict[str, set[str]] = {}
    trees: dict[str, ast.Module] = {}
    module_aliases: dict[str, dict[str, str]] = {}
    class_aliases: dict[str, dict[str, tuple[str, str]]] = {}
    for path in sorted(source.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        trees[path.stem] = tree
        module_classes: set[str] = set()
        module_singletons: set[str] = set()
        imported_modules: dict[str, str] = {}
        imported_classes: dict[str, tuple[str, str]] = {}
        for statement in tree.body:
            if isinstance(statement, ast.ClassDef):
                module_classes.add(statement.name)
            elif isinstance(statement, ast.ImportFrom):
                imported_module: str | None = None
                if statement.level == 1 and statement.module is None:
                    for alias in statement.names:
                        imported_modules[alias.asname or alias.name] = alias.name
                    continue
                if statement.level == 1 and statement.module:
                    imported_module = statement.module.split(".", 1)[0]
                elif statement.module and statement.module.startswith(f"{package}."):
                    imported_module = statement.module[len(package) + 1 :].split(".", 1)[0]
                if imported_module:
                    for alias in statement.names:
                        imported_classes[alias.asname or alias.name] = (
                            imported_module,
                            alias.name,
                        )
            elif isinstance(statement, ast.Assign):
                if isinstance(statement.value, ast.Dict | ast.List | ast.Set | ast.Call):
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            module_singletons.add(target.id)
            elif (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and isinstance(statement.value, ast.Dict | ast.List | ast.Set | ast.Call)
            ):
                module_singletons.add(statement.target.id)
        classes[path.stem] = module_classes
        singletons[path.stem] = module_singletons
        module_aliases[path.stem] = imported_modules
        class_aliases[path.stem] = imported_classes

    def class_owner(module: str, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            if node.id in classes.get(module, set()):
                return f"{package}.{module}.{node.id}"
            imported = class_aliases.get(module, {}).get(node.id)
            if imported and imported[1] in classes.get(imported[0], set()):
                return f"{package}.{imported[0]}.{imported[1]}"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            target_module = module_aliases.get(module, {}).get(node.value.id)
            if target_module and node.attr in classes.get(target_module, set()):
                return f"{package}.{target_module}.{node.attr}"
        return None

    def process_owner(module: str, node: ast.AST) -> str | None:
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "process_default"
        ):
            return None
        owner = class_owner(module, node.func.value)
        return f"{owner}.process_default()" if owner else None

    shared_aliases: dict[str, dict[str, str]] = defaultdict(dict)
    gateways: dict[str, dict[str, tuple[tuple[str, str], ...]]] = defaultdict(dict)
    for module, tree in trees.items():
        for statement in tree.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                value = statement.value
                if isinstance(target, ast.Name) and isinstance(value, ast.Attribute):
                    owner = process_owner(module, value.value)
                    if owner:
                        shared_aliases[module][target.id] = f"{owner}.{value.attr}"
            if not isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            delegated: list[tuple[str, str]] = []
            for call in (node for node in ast.walk(statement) if isinstance(node, ast.Call)):
                if not isinstance(call.func, ast.Attribute):
                    continue
                owner = process_owner(module, call.func.value)
                if owner:
                    delegated.append((owner, call.func.attr))
            if delegated:
                gateways[module][statement.name] = tuple(sorted(set(delegated)))
    return _Inventory(
        classes,
        singletons,
        dict(module_aliases),
        dict(class_aliases),
        {module: dict(values) for module, values in shared_aliases.items()},
        {module: dict(values) for module, values in gateways.items()},
    )


def _absolute_dotted(node: ast.AST, aliases: dict[str, str]) -> str | None:
    dotted = _dotted_name(node)
    if dotted is None:
        return None
    head, separator, tail = dotted.partition(".")
    target = aliases.get(head)
    if target is None:
        return dotted
    if target == _AMBIGUOUS_FAMILY_ALIAS:
        raise AuditError("mutation target is an ambiguous family alias after control-flow merge")
    return target + (separator + tail if separator else "")


def _merge_alias_bindings(destination: dict[str, str], source: dict[str, str]) -> None:
    """Merge family bindings without silently choosing one possible owner."""
    for name, target in source.items():
        if name not in destination:
            destination[name] = target
        elif destination[name] != target:
            destination[name] = _AMBIGUOUS_FAMILY_ALIAS


def _normalize_private_module(
    dotted: str, inventories: dict[str, _Inventory]
) -> tuple[str, str, list[str]] | None:
    for package, inventory in inventories.items():
        prefix = f"{package}."
        if not dotted.startswith(prefix):
            continue
        parts = dotted[len(prefix) :].split(".")
        if not parts:
            continue
        module, tail = parts[0], parts[1:]
        seen: set[tuple[str, str]] = set()
        while tail:
            alias = tail[0]
            target = inventory.module_aliases.get(module, {}).get(alias)
            if target is None or (module, alias) in seen:
                break
            seen.add((module, alias))
            module, tail = target, tail[1:]
        return package, module, tail
    return None


def _resolved_dotted_owner(
    dotted: str,
    inventories: dict[str, _Inventory],
    facade_aliases: dict[str, str],
) -> tuple[str, str] | None:
    facade_prefix = "notebooklm.auth."
    if dotted.startswith(facade_prefix):
        parts = dotted[len(facade_prefix) :].split(".")
        owner = facade_aliases.get(parts[0])
        if owner:
            owner = ".".join((owner, *parts[1:])) if len(parts) > 1 else owner
            package = next(
                (candidate for candidate in inventories if owner.startswith(f"{candidate}.")),
                "notebooklm._auth",
            )
            return package, owner
    normalized = _normalize_private_module(dotted, inventories)
    if normalized is None:
        return None
    package, module, tail = normalized
    if not tail:
        return None
    inventory = inventories[package]
    root, rest = tail[0], tail[1:]
    owner = inventory.shared_aliases.get(module, {}).get(root)
    imported_class = inventory.class_aliases.get(module, {}).get(root)
    if owner is None and imported_class is not None:
        owner = f"{package}.{imported_class[0]}.{imported_class[1]}"
    if owner is None and (
        root in inventory.classes.get(module, set())
        or root in inventory.singletons.get(module, set())
    ):
        owner = f"{package}.{module}.{root}"
    if owner is None:
        return None
    if rest:
        owner = ".".join((owner, *rest))
    return package, owner


def _is_bare_class(owner: tuple[str, str], inventories: dict[str, _Inventory]) -> bool:
    package, dotted = owner
    inventory = inventories[package]
    return any(
        dotted == f"{package}.{module}.{class_name}"
        for module, classes in inventory.classes.items()
        for class_name in classes
    )


def _facade_shared_aliases(facade_file: Path, inventories: dict[str, _Inventory]) -> dict[str, str]:
    if not facade_file.is_file():
        return {}
    tree = ast.parse(facade_file.read_text(encoding="utf-8"), filename=str(facade_file))
    imports: dict[str, str] = {}
    result: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom):
            continue
        module = statement.module or ""
        if statement.level == 1 and module == "_auth":
            for alias in statement.names:
                imports[alias.asname or alias.name] = f"notebooklm._auth.{alias.name}"
        elif statement.level == 1 and module.startswith("_auth."):
            target_module = module[len("_auth.") :].split(".", 1)[0]
            for alias in statement.names:
                dotted = f"notebooklm._auth.{target_module}.{alias.name}"
                resolved = _resolved_dotted_owner(dotted, inventories, {})
                if resolved is not None:
                    result[alias.asname or alias.name] = resolved[1]
        elif module in inventories:
            for alias in statement.names:
                imports[alias.asname or alias.name] = f"{module}.{alias.name}"
    for statement in tree.body:
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            continue
        assignment_dotted = _absolute_dotted(statement.value, imports)
        resolved = (
            _resolved_dotted_owner(assignment_dotted, inventories, {})
            if assignment_dotted is not None
            else None
        )
        if resolved is not None:
            result[statement.targets[0].id] = resolved[1]
    return result


def _gateway_rows(
    call: ast.Call,
    aliases: dict[str, str],
    inventories: dict[str, _Inventory],
) -> list[tuple[str, str, str, str]] | None:
    dotted = _absolute_dotted(call.func, aliases)
    normalized = _normalize_private_module(dotted, inventories) if dotted else None
    if normalized is None:
        return None
    package, module, tail = normalized
    if len(tail) != 1:
        return None
    delegated = inventories[package].gateways.get(module, {}).get(tail[0])
    if delegated is None:
        return None
    rows: list[tuple[str, str, str, str]] = []
    for owner, method in delegated:
        if method in READ_ONLY_METHODS:
            continue
        kind = "gateway-mutator" if method in MUTATORS else "gateway-method-or-unknown"
        rows.append((package, owner, method, kind))
    return rows


def _direct_symbol_context(
    tree: ast.Module,
    package: str,
    owners: dict[int, tuple[str, str]],
) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, set[str]]]:
    """Direct ``from package.module import Class`` bindings by lexical scope."""

    module_aliases: dict[str, str] = {}
    local_aliases: dict[str, dict[str, str]] = defaultdict(dict)
    local_names: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            owner = (
                owners.get(id(next(iter(node.body), node)), ("<module>", "helper"))[0]
                if not isinstance(node, ast.Lambda)
                else "<lambda>"
            )
            local_names[owner] = _scope_declarations(node)[0]
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if not module.startswith(f"{package}."):
            continue
        owner = owners.get(id(node), ("<module>", "helper"))[0]
        destination = module_aliases if owner == "<module>" else local_aliases[owner]
        for alias in node.names:
            destination[alias.asname or alias.name] = f"{module}.{alias.name}"
    return module_aliases, local_aliases, local_names


def _resolve_shared_owner(
    node: ast.AST,
    aliases: dict[str, str],
    inventories: dict[str, _Inventory],
    facade_aliases: dict[str, str],
    accessors: dict[str, tuple[str, str]] | None = None,
) -> tuple[str, str] | None:
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and not node.args
            and not node.keywords
            and accessors is not None
            and node.func.id in accessors
        ):
            return accessors[node.func.id]
        if isinstance(node.func, ast.Attribute) and node.func.attr == "process_default":
            resolved = _resolve_shared_owner(
                node.func.value, aliases, inventories, facade_aliases, accessors
            )
            if resolved is not None:
                package, owner = resolved
                return package, f"{owner}.process_default()"
        return None
    if isinstance(node, ast.Subscript):
        parent = _resolve_shared_owner(node.value, aliases, inventories, facade_aliases, accessors)
        if parent is None:
            return None
        try:
            key = ast.literal_eval(node.slice)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise AuditError("dynamic nested key against a resolved shared owner") from exc
        if not isinstance(key, str | int | float | bool | tuple) and key is not None:
            raise AuditError("unsupported nested key against a resolved shared owner")
        return parent[0], f"{parent[1]}[{key!r}]"
    dotted = _absolute_dotted(node, aliases)
    if dotted is not None:
        resolved = _resolved_dotted_owner(dotted, inventories, facade_aliases)
        if resolved is not None:
            return resolved
    if isinstance(node, ast.Attribute):
        parent = _resolve_shared_owner(node.value, aliases, inventories, facade_aliases, accessors)
        if parent is not None:
            return parent[0], f"{parent[1]}.{node.attr}"
    return None


def _namespace_owner(
    node: ast.AST,
    aliases: dict[str, str],
    inventories: dict[str, _Inventory],
    facade_aliases: dict[str, str],
    accessors: dict[str, tuple[str, str]] | None = None,
) -> tuple[str, str] | None:
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return _resolve_shared_owner(node.value, aliases, inventories, facade_aliases, accessors)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "vars"
        and len(node.args) == 1
    ):
        return _resolve_shared_owner(node.args[0], aliases, inventories, facade_aliases, accessors)
    return _resolve_shared_owner(node, aliases, inventories, facade_aliases, accessors)


def _namespace_method_rows(
    call: ast.Call,
    aliases: dict[str, str],
    inventories: dict[str, _Inventory],
    facade_aliases: dict[str, str],
    constants: dict[str, str],
    context: str,
    expanded: dict[str, tuple[str, ...]] | None,
    accessors: dict[str, tuple[str, str]] | None,
) -> list[tuple[str, str, str, str]] | None:
    if not isinstance(call.func, ast.Attribute):
        return None
    value = call.func.value
    is_namespace = (
        isinstance(value, ast.Attribute)
        and value.attr == "__dict__"
        or isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "vars"
    )
    if not is_namespace:
        return None
    owner = _namespace_owner(value, aliases, inventories, facade_aliases, accessors)
    if owner is None:
        return None
    method = call.func.attr
    if method in {"get", "items", "keys", "values", "copy", "__contains__"}:
        return []
    if method == "update":
        attributes: list[str] = []
        if call.args:
            attributes.extend(
                _literal_mapping_keys(
                    call.args[0],
                    context=context,
                    constants=constants,
                    expanded=expanded,
                )
            )
        for keyword in call.keywords:
            if keyword.arg is None:
                attributes.extend(
                    _literal_mapping_keys(
                        keyword.value,
                        context=context,
                        constants=constants,
                        expanded=expanded,
                    )
                )
            else:
                attributes.append(keyword.arg)
        if not attributes:
            raise AuditError(f"{context}: unexpandable shared namespace update")
        return [(owner[0], owner[1], attribute, "namespace.update") for attribute in attributes]
    if method in {"setdefault", "pop", "__setitem__", "__delitem__"}:
        attributes = _literal_strings(_call_arg(call, 0, "key"), constants, expanded)
        if not attributes:
            raise AuditError(f"{context}: dynamic shared namespace {method}")
        return [(owner[0], owner[1], attribute, f"namespace.{method}") for attribute in attributes]
    raise AuditError(f"{context}: unexpandable shared namespace method {method}")


def _call_rows(
    call: ast.Call,
    aliases: dict[str, str],
    inventories: dict[str, _Inventory],
    facade_aliases: dict[str, str],
    constants: dict[str, str],
    context: str,
    expanded: dict[str, tuple[str, ...]] | None = None,
    accessors: dict[str, tuple[str, str]] | None = None,
) -> list[tuple[str, str, str, str]]:
    name = _dotted_name(call.func) or ""
    tail = name.rsplit(".", 1)[-1]
    receiver = name.rsplit(".", 1)[0] if "." in name else ""
    patch_receiver = receiver.split(".")[-1] == "patch"
    namespace_rows = _namespace_method_rows(
        call, aliases, inventories, facade_aliases, constants, context, expanded, accessors
    )
    if namespace_rows is not None:
        return namespace_rows
    gateway_rows = _gateway_rows(call, aliases, inventories)
    if gateway_rows is not None:
        return gateway_rows

    if tail in {"setattr", "delattr"} or name in {"setattr", "delattr"}:
        target = _call_arg(call, 0, "target", "obj", "object")
        if target is None or isinstance(target, ast.Constant):
            return []
        owner = _resolve_shared_owner(target, aliases, inventories, facade_aliases, accessors)
        if owner is None:
            return []
        attributes = _literal_strings(_call_arg(call, 1, "name", "attribute"), constants, expanded)
        if not attributes:
            raise AuditError(f"{context}: dynamic shared-owner attribute")
        prefix = "builtin" if name in {"setattr", "delattr"} else "monkeypatch"
        return [(owner[0], owner[1], attribute, f"{prefix}.{tail}") for attribute in attributes]

    if patch_receiver and tail == "object":
        target = _call_arg(call, 0, "target")
        owner = (
            _resolve_shared_owner(target, aliases, inventories, facade_aliases, accessors)
            if target is not None
            else None
        )
        if owner is None:
            return []
        attributes = _literal_strings(_call_arg(call, 1, "attribute", "name"), constants, expanded)
        if not attributes:
            raise AuditError(f"{context}: dynamic patch.object shared-owner attribute")
        return [(owner[0], owner[1], attribute, "patch.object") for attribute in attributes]

    if patch_receiver and tail == "multiple":
        target = _call_arg(call, 0, "target")
        owner = (
            _resolve_shared_owner(target, aliases, inventories, facade_aliases, accessors)
            if target is not None
            else None
        )
        if owner is None:
            return []
        multiple_attributes: list[str] = []
        for keyword in call.keywords:
            if keyword.arg is None:
                multiple_attributes.extend(
                    _literal_mapping_keys(
                        keyword.value,
                        context=context,
                        constants=constants,
                        expanded=expanded,
                    )
                )
                continue
            if keyword.arg != "target":
                multiple_attributes.append(keyword.arg)
        if not multiple_attributes:
            raise AuditError(f"{context}: unexpandable patch.multiple shared owner")
        return [
            (owner[0], owner[1], attribute, "patch.multiple") for attribute in multiple_attributes
        ]

    if (patch_receiver and tail == "dict") or tail in {"setitem", "delitem"}:
        target = _call_arg(call, 0, "in_dict", "dic", "mapping")
        owner = (
            _namespace_owner(target, aliases, inventories, facade_aliases, accessors)
            if target is not None
            else None
        )
        if owner is None:
            return []
        if patch_receiver and tail == "dict":
            attributes = _literal_mapping_keys(
                _call_arg(call, 1, "values"),
                context=context,
                constants=constants,
                expanded=expanded,
            )
            for keyword in call.keywords:
                if keyword.arg is None:
                    raise AuditError(f"{context}: dynamic patch.dict shared owner")
                if keyword.arg not in {"in_dict", "values", "clear"}:
                    attributes.append(keyword.arg)
            if not attributes:
                raise AuditError(f"{context}: unexpandable patch.dict shared owner")
            return [(owner[0], owner[1], attribute, "patch.dict") for attribute in attributes]
        attributes = _literal_strings(_call_arg(call, 1, "key", "name"), constants, expanded)
        if not attributes:
            raise AuditError(f"{context}: dynamic shared-owner item key")
        return [(owner[0], owner[1], attribute, f"monkeypatch.{tail}") for attribute in attributes]

    if isinstance(call.func, ast.Attribute):
        owner = _resolve_shared_owner(
            call.func.value, aliases, inventories, facade_aliases, accessors
        )
        if (
            owner is not None
            and not _is_bare_class(owner, inventories)
            and call.func.attr not in READ_ONLY_METHODS
        ):
            kind = "mutator" if call.func.attr in MUTATORS else "method-or-unknown"
            return [(owner[0], owner[1], call.func.attr, kind)]
    return []


def _assignment_rows(
    node: ast.Assign | ast.AnnAssign | ast.AugAssign | ast.Delete,
    aliases: dict[str, str],
    inventories: dict[str, _Inventory],
    facade_aliases: dict[str, str],
    constants: dict[str, str],
    context: str,
    expanded: dict[str, tuple[str, ...]] | None = None,
    accessors: dict[str, tuple[str, str]] | None = None,
) -> list[tuple[str, str, str, str]]:
    if isinstance(node, ast.Assign):
        targets, idiom = node.targets, "assignment"
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target] if node.value is not None else []
        idiom = "annotated-assignment"
    elif isinstance(node, ast.AugAssign):
        targets, idiom = [node.target], "augmented-assignment"
    else:
        targets, idiom = node.targets, "deletion"
    rows: list[tuple[str, str, str, str]] = []
    for target in targets:
        target_idiom = idiom
        owner: tuple[str, str] | None
        attributes: list[str]
        if isinstance(target, ast.Attribute):
            owner = _resolve_shared_owner(
                target.value, aliases, inventories, facade_aliases, accessors
            )
            attributes = [target.attr]
        elif isinstance(target, ast.Subscript):
            owner = _namespace_owner(target.value, aliases, inventories, facade_aliases, accessors)
            attributes = _literal_strings(target.slice, constants, expanded)
            if owner is not None and not attributes:
                raise AuditError(f"{context}: dynamic shared-owner item assignment")
            target_idiom = f"item-{target_idiom}"
        else:
            owner, attributes = None, []
        if owner is not None:
            rows.extend((owner[0], owner[1], attribute, target_idiom) for attribute in attributes)
    return rows


def collect_mutations(
    tests_dir: Path,
    family: dict[str, Path] | None = None,
) -> list[SharedMutation]:
    family = family or FAMILY
    inventories = {package: _inventory(path, package) for package, path in family.items()}
    auth_source = family.get("notebooklm._auth")
    facade_aliases = (
        _facade_shared_aliases(auth_source.parent / "auth.py", inventories)
        if auth_source is not None
        else {}
    )
    project_root = tests_dir.parent if tests_dir.name == "tests" else REPO_ROOT
    result: list[SharedMutation] = []
    for path in sorted(tests_dir.rglob("*.py")):
        source_text = path.read_text(encoding="utf-8")
        tree = ast.parse(source_text, filename=str(path))
        contexts = {
            package: _alias_context(tree, package)
            for package in family
            if package in source_text or package.rsplit(".", 1)[-1] in source_text
        }
        if "notebooklm.auth" in source_text or "import auth" in source_text:
            contexts["notebooklm.auth"] = _alias_context(tree, "notebooklm.auth")
        owners = _owner_metadata(tree, path)
        constants = _module_string_constants(tree)
        constant_context = _literal_constants_context(tree, constants)
        literal_values = _literal_values_context(tree, constant_context)
        symbols = {package: _direct_symbol_context(tree, package, owners) for package in family}
        shared_accessors: dict[str, tuple[str, str]] = {}
        # A small module-level wrapper that returns a proven shared owner is an
        # equivalent process-default accessor. Resolve it separately from
        # ordinary aliases so the function object itself is never mistaken for
        # the returned owner.
        for candidate in tree.body:
            if not isinstance(candidate, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            statements = [
                statement
                for statement in candidate.body
                if not (
                    isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant)
                    and isinstance(statement.value.value, str)
                )
            ]
            return_node = statements[0] if len(statements) == 1 else None
            if not isinstance(return_node, ast.Return) or return_node.value is None:
                continue
            accessor_aliases: dict[str, str] = {}
            for context in contexts.values():
                _merge_alias_bindings(accessor_aliases, context.get(id(return_node), {}))
            accessor_lexical_owner = owners.get(id(return_node), (candidate.name, "helper"))[0]
            accessor_blocked_names: set[str] = set()
            for module_aliases, local_aliases, local_names in symbols.values():
                blocked = local_names.get(accessor_lexical_owner, set())
                accessor_blocked_names.update(blocked)
                _merge_alias_bindings(
                    accessor_aliases,
                    {
                        name: target
                        for name, target in module_aliases.items()
                        if name not in blocked
                    },
                )
                _merge_alias_bindings(
                    accessor_aliases, local_aliases.get(accessor_lexical_owner, {})
                )
            callable_accessors = {
                name: owner
                for name, owner in shared_accessors.items()
                if name not in accessor_blocked_names
            }
            resolved = _resolve_shared_owner(
                return_node.value,
                accessor_aliases,
                inventories,
                facade_aliases,
                callable_accessors,
            )
            if resolved is not None:
                shared_accessors[candidate.name] = resolved
        local_shared: dict[str, dict[str, str]] = defaultdict(dict)
        # Resolve simple ``owner = Class.process_default()`` / ``owner =
        # module.SHARED`` bindings.  They remain scoped to their lexical owner;
        # ordinary fresh construction does not resolve and stays excluded.
        simple_assignments = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ]
        for _ in range(len(simple_assignments) + 1):
            previous_local_shared = {
                owner: dict(bindings) for owner, bindings in local_shared.items()
            }
            for assignment_node in simple_assignments:
                target = assignment_node.targets[0]
                assert isinstance(target, ast.Name)
                assignment_aliases: dict[str, str] = {}
                for context in contexts.values():
                    _merge_alias_bindings(assignment_aliases, context.get(id(assignment_node), {}))
                assignment_owner = owners.get(id(assignment_node), ("<module>", "helper"))[0]
                assignment_blocked_names: set[str] = set()
                for module_aliases, local_aliases, local_names in symbols.values():
                    blocked = local_names.get(assignment_owner, set())
                    assignment_blocked_names.update(blocked)
                    _merge_alias_bindings(
                        assignment_aliases,
                        {
                            name: target
                            for name, target in module_aliases.items()
                            if name not in blocked
                        },
                    )
                    _merge_alias_bindings(
                        assignment_aliases, local_aliases.get(assignment_owner, {})
                    )
                _merge_alias_bindings(assignment_aliases, local_shared.get(assignment_owner, {}))
                assignment_dotted = _dotted_name(assignment_node.value)
                if assignment_dotted is not None:
                    assignment_head = assignment_dotted.partition(".")[0]
                    if assignment_aliases.get(assignment_head) == _AMBIGUOUS_FAMILY_ALIAS:
                        _merge_alias_bindings(
                            local_shared[assignment_owner],
                            {target.id: _AMBIGUOUS_FAMILY_ALIAS},
                        )
                        continue
                assignment_accessors = {
                    name: owner
                    for name, owner in shared_accessors.items()
                    if name not in assignment_blocked_names
                }
                resolved = _resolve_shared_owner(
                    assignment_node.value,
                    assignment_aliases,
                    inventories,
                    facade_aliases,
                    assignment_accessors,
                )
                if resolved is not None:
                    _merge_alias_bindings(local_shared[assignment_owner], {target.id: resolved[1]})
            if local_shared == previous_local_shared:
                break

        def resolution_context(
            node: ast.AST,
            *,
            _contexts: dict[str, dict[int, dict[str, str]]] = contexts,
            _owners: dict[int, tuple[str, str]] = owners,
            _symbols: dict[
                str, tuple[dict[str, str], dict[str, dict[str, str]], dict[str, set[str]]]
            ] = symbols,
            _local_shared: dict[str, dict[str, str]] = local_shared,
            _shared_accessors: dict[str, tuple[str, str]] = shared_accessors,
        ) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
            aliases: dict[str, str] = {}
            for context in _contexts.values():
                _merge_alias_bindings(aliases, context.get(id(node), {}))
            lexical_owner = _owners.get(id(node), ("<module>", "helper"))[0]
            blocked_names: set[str] = set()
            for module_aliases, local_aliases, local_names in _symbols.values():
                blocked = local_names.get(lexical_owner, set())
                blocked_names.update(blocked)
                _merge_alias_bindings(
                    aliases,
                    {
                        name: target
                        for name, target in module_aliases.items()
                        if name not in blocked
                    },
                )
                _merge_alias_bindings(aliases, local_aliases.get(lexical_owner, {}))
            _merge_alias_bindings(aliases, _local_shared.get(lexical_owner, {}))
            accessors = {
                name: owner
                for name, owner in _shared_accessors.items()
                if name not in blocked_names
            }
            return aliases, accessors

        def resolve_forwarded(argument: ast.AST, call: ast.Call) -> str | None:
            aliases, accessors = resolution_context(call)
            resolved = _resolve_shared_owner(
                argument, aliases, inventories, facade_aliases, accessors
            )
            return resolved[1] if resolved is not None else None

        forwarded = _forwarded_parameter_context(
            tree,
            owners,
            resolve_forwarded,
            context=path.as_posix(),
            include_method_receivers=True,
            excluded_parameters=_definitely_nonfamily_typed_parameters(
                tree, _function_definitions(tree), (*family, "notebooklm.auth")
            ),
        )
        try:
            rel = path.relative_to(project_root).as_posix()
        except ValueError:
            rel = path.as_posix()
        for node in ast.walk(tree):
            aliases, node_accessors = resolution_context(node)
            lexical_owner, owner_kind = owners.get(id(node), ("<module>", "helper"))
            location = f"{rel}:{getattr(node, 'lineno', 0)}"
            rows: list[tuple[str, str, str, str]] = []
            for variant in _forwarded_alias_variants(aliases, lexical_owner, node, forwarded):
                if isinstance(node, ast.Call):
                    rows.extend(
                        _call_rows(
                            node,
                            variant,
                            inventories,
                            facade_aliases,
                            constant_context.get(id(node), {}),
                            location,
                            literal_values.get(id(node), {}),
                            node_accessors,
                        )
                    )
                elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.Delete):
                    rows.extend(
                        _assignment_rows(
                            node,
                            variant,
                            inventories,
                            facade_aliases,
                            constant_context.get(id(node), {}),
                            location,
                            literal_values.get(id(node), {}),
                            node_accessors,
                        )
                    )
            for package, shared_owner, attribute, idiom in rows:
                result.append(
                    SharedMutation(
                        package,
                        shared_owner,
                        attribute,
                        idiom,
                        rel,
                        getattr(node, "lineno", 0),
                        lexical_owner,
                        owner_kind,
                    )
                )
    return sorted(result)


def build_projection(mutations: list[SharedMutation]) -> dict[str, object]:
    rows: dict[tuple[str, str, str, str, str, str, str], int] = defaultdict(int)
    for site in mutations:
        rows[
            (
                site.package,
                site.owner,
                site.attribute,
                site.idiom,
                site.path,
                site.owner_qualname,
                site.owner_kind,
            )
        ] += 1
    owned = [site for site in mutations if site.owner_kind != "test"]
    return {
        "version": 1,
        "summary": {
            "total": len(mutations),
            "private": sum(site.attribute.startswith("_") for site in mutations),
            "helper_or_fixture": len(owned),
            "assignments": sum("assignment" in site.idiom for site in mutations),
        },
        "mutations": [
            {
                "package": package,
                "owner": owner,
                "attribute": attribute,
                "idiom": idiom,
                "path": path,
                "owner_qualname": lexical_owner,
                "owner_kind": owner_kind,
                "count": count,
            }
            for (
                package,
                owner,
                attribute,
                idiom,
                path,
                lexical_owner,
                owner_kind,
            ), count in sorted(rows.items())
        ],
    }


def projection_growth(previous: object, current: object) -> list[str]:
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return ["shared-mutation projection is not an object"]
    fields = ("package", "owner", "attribute", "idiom", "path", "owner_qualname", "owner_kind")
    old = {
        tuple(row[field] for field in fields): int(row["count"])
        for row in previous.get("mutations", [])
    }
    new = {
        tuple(row[field] for field in fields): int(row["count"])
        for row in current.get("mutations", [])
    }
    errors = [
        f"shared mutation {identity} grew {old.get(identity, 0)} -> {count}"
        for identity, count in new.items()
        if count > old.get(identity, 0)
    ]
    for name in ("total", "private", "helper_or_fixture", "assignments"):
        before = int(previous.get("summary", {}).get(name, 0))
        after = int(current.get("summary", {}).get(name, 0))
        if after > before:
            errors.append(f"shared mutation {name} grew {before} -> {after}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-dir", type=Path, default=REPO_ROOT / "tests")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.tests_dir.is_dir():
        parser.error(f"not a directory: {args.tests_dir}")
    projection = build_projection(collect_mutations(args.tests_dir))
    if args.json:
        json.dump(projection, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(json.dumps(projection["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
