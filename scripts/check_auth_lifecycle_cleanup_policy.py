#!/usr/bin/env python3
"""Equality-check authored lifecycle cleanup against live shared-owner calls."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # direct ``python scripts/...`` execution
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.audit_auth_shared_mutations import collect_mutations

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY = REPO_ROOT / "tests/fixtures/policies/auth_lifecycle_cleanup.json"
COOKIE_WARNING_RESET = "_reset_secondary_binding_warning_for_tests"
METHODS = {COOKIE_WARNING_RESET, "_reset_for_tests", "drain", "join", "quiesce", "reset"}
EXACT_COOKIE_WARNING_FIXTURES = {
    ("tests/conftest.py", "_reset_poke_state"),
    ("tests/unit/test_warning_dedupe.py", "_reset_warning_flags"),
}


class LifecyclePolicyError(RuntimeError):
    pass


def live_operations(tests_dir: Path) -> list[dict[str, Any]]:
    yields: dict[tuple[str, str], int] = {}
    exact_cookie_operations: list[dict[str, Any]] = []
    project_root = tests_dir.parent if tests_dir.name == "tests" else REPO_ROOT
    for path in tests_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(project_root).as_posix()

        class Visitor(ast.NodeVisitor):
            def __init__(self, relative_path: str) -> None:
                self.stack: list[str] = []
                self.relative_path = relative_path

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                qualname = ".".join((*self.stack, node.name))
                direct_yields = [
                    child.lineno
                    for statement in node.body
                    for child in ast.walk(statement)
                    if isinstance(child, ast.Yield | ast.YieldFrom)
                ]
                if direct_yields:
                    yields[(self.relative_path, qualname)] = min(direct_yields)

                if (self.relative_path, qualname) in EXACT_COOKIE_WARNING_FIXTURES:
                    scoped_nodes: list[ast.AST] = []

                    def walk_scope(current: ast.AST) -> None:
                        scoped_nodes.append(current)
                        for child in ast.iter_child_nodes(current):
                            if isinstance(
                                child,
                                ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda,
                            ):
                                continue
                            walk_scope(child)

                    for statement in node.body:
                        walk_scope(statement)
                    cookie_aliases = {
                        alias.asname or alias.name
                        for child in scoped_nodes
                        if isinstance(child, ast.ImportFrom) and child.module == "notebooklm._auth"
                        for alias in child.names
                        if alias.name == "cookie_policy"
                    }
                    rebound = {
                        child.id
                        for child in scoped_nodes
                        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
                    }
                    cookie_aliases -= rebound
                    for child in scoped_nodes:
                        if not (
                            isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Attribute)
                            and child.func.attr == COOKIE_WARNING_RESET
                            and isinstance(child.func.value, ast.Name)
                            and child.func.value.id in cookie_aliases
                        ):
                            continue
                        boundary = min(direct_yields) if direct_yields else None
                        exact_cookie_operations.append(
                            {
                                "path": self.relative_path,
                                "owner_qualname": qualname,
                                "owner_kind": "fixture",
                                "production_owner": "notebooklm._auth.cookie_policy",
                                "method": COOKIE_WARNING_RESET,
                                "count": 1,
                                "phase": (
                                    "setup"
                                    if boundary is not None and child.lineno < boundary
                                    else "teardown"
                                ),
                            }
                        )
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_FunctionDef = _function
            visit_AsyncFunctionDef = _function

        Visitor(rel).visit(tree)

    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    phases: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for operation in exact_cookie_operations:
        identity = (
            operation["path"],
            operation["owner_qualname"],
            operation["owner_kind"],
            operation["production_owner"],
            operation["method"],
        )
        row = grouped.setdefault(identity, {**operation, "count": 0})
        row["count"] += 1
        phases[identity].add(operation["phase"])
    for mutation in collect_mutations(tests_dir):
        if (
            mutation.owner_kind not in {"fixture", "helper"}
            or mutation.attribute not in METHODS
            or ".process_default()" not in mutation.owner
        ):
            continue
        identity = (
            mutation.path,
            mutation.owner_qualname,
            mutation.owner_kind,
            mutation.owner,
            mutation.attribute,
        )
        row = grouped.setdefault(
            identity,
            {
                "path": mutation.path,
                "owner_qualname": mutation.owner_qualname,
                "owner_kind": mutation.owner_kind,
                "production_owner": mutation.owner,
                "method": mutation.attribute,
                "count": 0,
            },
        )
        row["count"] += 1
        boundary = yields.get((mutation.path, mutation.owner_qualname))
        if mutation.owner_kind == "fixture" and boundary is not None:
            phases[identity].add("setup" if mutation.lineno < boundary else "teardown")
        else:
            # A non-fixture lifecycle helper is an explicit quiescence barrier,
            # not ambient setup; its dedicated verification node proves use.
            phases[identity].add("teardown")
    for grouped_identity, row in grouped.items():
        row_phases = phases[grouped_identity]
        row["phase"] = (
            "setup_and_teardown" if row_phases == {"setup", "teardown"} else next(iter(row_phases))
        )
    return sorted(grouped.values(), key=lambda row: tuple(str(value) for value in row.values()))


def validate_policy(policy: dict[str, Any], live: list[dict[str, Any]]) -> None:
    if policy.get("version") != 1 or not isinstance(policy.get("operations"), list):
        raise LifecyclePolicyError("lifecycle policy must be version 1 with operations")
    identity = (
        "path",
        "owner_qualname",
        "owner_kind",
        "production_owner",
        "method",
        "count",
        "phase",
    )
    expected: list[dict[str, Any]] = []
    operation_ids: set[tuple[str, ...]] = set()
    for row in policy["operations"]:
        required = {
            *identity,
            "affected_paths",
            "replaced_base_mutations",
            "verification_node_prefixes",
        }
        if set(row) != required:
            raise LifecyclePolicyError(f"lifecycle row has wrong fields: {set(row) ^ required}")
        if row["method"] not in METHODS or row["phase"] not in {
            "setup",
            "teardown",
            "setup_and_teardown",
        }:
            raise LifecyclePolicyError("lifecycle row has an invalid method or phase")
        if not row["affected_paths"] or not row["verification_node_prefixes"]:
            raise LifecyclePolicyError("lifecycle rows require SUT paths and verification nodes")
        if not isinstance(row["replaced_base_mutations"], list):
            raise LifecyclePolicyError("lifecycle replaced_base_mutations must be a list")
        from scripts.check_auth_behavior_scenario_policy import PolicyError, _validate_mutation

        try:
            replacements = [
                _validate_mutation(mutation, "lifecycle replaced_base_mutations")
                for mutation in row["replaced_base_mutations"]
            ]
        except PolicyError as exc:
            raise LifecyclePolicyError(str(exc)) from exc
        if len({json.dumps(row, sort_keys=True) for row in replacements}) != len(replacements):
            raise LifecyclePolicyError("duplicate lifecycle replaced_base_mutations")
        if not all(
            isinstance(path, str)
            and path.startswith("src/notebooklm/")
            and not any(token in path for token in ("*", "?", "["))
            for path in row["affected_paths"]
        ):
            raise LifecyclePolicyError("lifecycle affected_paths must be exact production paths")
        operation_id = tuple(str(row[key]) for key in identity)
        if operation_id in operation_ids:
            raise LifecyclePolicyError("duplicate lifecycle operation identity")
        operation_ids.add(operation_id)
        expected.append({key: row[key] for key in identity})

    def sort_key(row: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(row[key]) for key in identity)

    if sorted(expected, key=sort_key) != sorted(live, key=sort_key):
        raise LifecyclePolicyError(
            "lifecycle policy is not an exact multiset of live drain/quiescence/reset calls"
        )


def validate_verification_nodes(policy: dict[str, Any], collection: dict[str, Any]) -> None:
    items = collection.get("items", [])
    for row in policy["operations"]:
        expanded: set[str] = set()
        for selector in row["verification_node_prefixes"]:
            if any(token in selector for token in ("*", "?", "[!")) or selector.endswith("::"):
                raise LifecyclePolicyError("lifecycle verification selectors must be exact")
            matches = {item["nodeid"] for item in items if item["canonical_node"] == selector}
            if not matches:
                raise LifecyclePolicyError(f"verification node does not collect: {selector}")
            if expanded & matches:
                raise LifecyclePolicyError("duplicate lifecycle verification-node coverage")
            expanded.update(matches)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=POLICY)
    parser.add_argument("--tests-dir", type=Path, default=REPO_ROOT / "tests")
    parser.add_argument("--head-collection", type=Path)
    args = parser.parse_args(argv)
    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        validate_policy(policy, live_operations(args.tests_dir))
        if args.head_collection:
            collection = json.loads(args.head_collection.read_text(encoding="utf-8"))
            validate_verification_nodes(policy, collection)
    except LifecyclePolicyError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
