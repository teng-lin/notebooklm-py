#!/usr/bin/env python3
"""Collect and validate authored base/head auth behavior scenario evidence."""

from __future__ import annotations

import argparse
import ast
import contextlib
import inspect
import io
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = REPO_ROOT / "tests/fixtures/policies/auth_behavior_scenarios.json"
NON_PASSING_EVIDENCE_MARKERS = {"repo_lint", "reality", "skip", "skipif", "xfail"}
LIFECYCLE_POLICY = REPO_ROOT / "tests/fixtures/policies/auth_lifecycle_cleanup.json"
CONTRACT_TAGS = {
    "cancellation",
    "compatibility_identity",
    "compatibility_signature",
    "concurrency",
    "exception",
    "ordering",
    "persistence_bytes",
    "redaction_scrubbing",
    "result",
    "traceback_cause",
}
MODULE_MUTATION_FIELDS = {
    "package",
    "module",
    "attribute",
    "idiom",
    "path",
    "owner_qualname",
    "owner_kind",
    "count",
}
SHARED_MUTATION_FIELDS = {
    "package",
    "owner",
    "attribute",
    "idiom",
    "path",
    "owner_qualname",
    "owner_kind",
    "count",
}


class PolicyError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PolicyError(f"{path}: expected an object")
    return value


def _canonical(nodeid: str) -> str:
    return nodeid.split("[", 1)[0]


def _function_scope_nodes(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    """Walk one function body without attributing nested bodies to it."""
    result: list[ast.AST] = []

    def walk(current: ast.AST) -> None:
        result.append(current)
        for child in ast.iter_child_nodes(current):
            if isinstance(
                child,
                ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda,
            ):
                result.append(child)
            else:
                walk(child)

    for statement in node.body:
        walk(statement)
    return result


def _static_helper_consumers(
    workspace: Path, items: list[dict[str, Any]]
) -> tuple[dict[str, list[str]], list[str]]:
    consumers: dict[str, set[str]] = {}
    unresolved: set[str] = set()
    concrete_by_canonical: dict[str, set[str]] = {}
    for item in items:
        concrete_by_canonical.setdefault(item["canonical_node"], set()).add(item["nodeid"])
    for path in sorted((workspace / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
                self.stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                qualname = ".".join((*self.stack, node.name))
                self.functions[qualname] = node
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                qualname = ".".join((*self.stack, node.name))
                self.functions[qualname] = node
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

        visitor = Visitor()
        visitor.visit(tree)
        functions = visitor.functions
        calls: dict[str, set[str]] = {}
        file_unresolved: set[str] = set()
        by_leaf: dict[str, set[str]] = {}
        for name in functions:
            by_leaf.setdefault(name.rsplit(".", 1)[-1], set()).add(name)
        helper_names = {
            name for name in functions if not name.rsplit(".", 1)[-1].startswith("test_")
        }
        for name, node in functions.items():
            scoped_nodes = _function_scope_nodes(node)
            scoped_ids = {id(child) for child in scoped_nodes}
            parents = {
                id(child): parent
                for parent in scoped_nodes
                for child in ast.iter_child_nodes(parent)
                if id(child) in scoped_ids
            }
            calls[name] = set()
            for child in scoped_nodes:
                if isinstance(child, ast.Call):
                    candidates: set[str] = set()
                    opaque_candidates: set[str] = set()
                    if isinstance(child.func, ast.Subscript):
                        # ``globals()[name]()``, registries, and other dynamic
                        # dispatch can name any helper in the file.
                        opaque_candidates = helper_names
                    elif (
                        isinstance(child.func, ast.Call)
                        and isinstance(child.func.func, ast.Name)
                        and child.func.func.id == "getattr"
                    ):
                        attribute = (
                            child.func.args[1]
                            if len(child.func.args) > 1
                            else next(
                                (
                                    keyword.value
                                    for keyword in child.func.keywords
                                    if keyword.arg == "name"
                                ),
                                None,
                            )
                        )
                        if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
                            opaque_candidates = by_leaf.get(attribute.value, set())
                        else:
                            opaque_candidates = helper_names
                    file_unresolved.update(opaque_candidates)
                    if isinstance(child.func, ast.Name):
                        candidates = by_leaf.get(child.func.id, set())
                    elif (
                        isinstance(child.func, ast.Attribute)
                        and isinstance(child.func.value, ast.Name)
                        and child.func.value.id in {"self", "cls"}
                    ):
                        class_name = name.rpartition(".")[0]
                        candidate = f"{class_name}.{child.func.attr}" if class_name else ""
                        candidates = {candidate} if candidate in functions else set()
                    if len(candidates) == 1:
                        calls[name].update(candidates)
                    elif candidates:
                        file_unresolved.update(candidates)
                if not isinstance(child, ast.Name) or not isinstance(child.ctx, ast.Load):
                    continue
                candidates = by_leaf.get(child.id, set())
                parent = parents.get(id(child))
                direct_call = isinstance(parent, ast.Call) and parent.func is child
                if candidates and (not direct_call or len(candidates) != 1):
                    file_unresolved.update(candidates)
        rel = path.relative_to(workspace).as_posix()
        test_roots = [name for name in functions if name.rsplit(".", 1)[-1].startswith("test_")]
        for helper in functions:
            reached: set[str] = set()
            for test_name in test_roots:
                pending = [test_name]
                seen: set[str] = set()
                while pending:
                    current = pending.pop()
                    if current in seen:
                        continue
                    seen.add(current)
                    if helper in calls.get(current, set()):
                        reached.add(test_name)
                    pending.extend(calls.get(current, set()))
            nodes: set[str] = set()
            for test_name in reached:
                canonical = f"{rel}::" + test_name.replace(".", "::")
                nodes.update(concrete_by_canonical.get(canonical, set()))
            consumers[f"{rel}::{helper}"] = nodes
        unresolved.update(f"{rel}::{name}" for name in file_unresolved)
    return (
        {key: sorted(value) for key, value in sorted(consumers.items())},
        sorted(unresolved),
    )


def collect_workspace(workspace: Path) -> dict[str, Any]:
    """Collect concrete/canonical nodes and full fixture closure with pytest."""

    workspace = workspace.resolve()
    items: list[dict[str, Any]] = []

    class Collector:
        @staticmethod
        def pytest_collection_finish(session: Any) -> None:
            for item in session.items:
                original_name = getattr(item, "originalname", None)
                canonical_node = (
                    f"{item.parent.nodeid}::{original_name}"
                    if isinstance(original_name, str) and original_name
                    else _canonical(item.nodeid)
                )
                fixtures: list[str] = []
                for name in set(item.fixturenames):
                    definitions = item._fixtureinfo.name2fixturedefs.get(name) or ()
                    if not definitions:
                        continue
                    function = definitions[-1].func
                    source = inspect.getsourcefile(function)
                    if source is None:
                        continue
                    try:
                        rel = Path(source).resolve().relative_to(workspace).as_posix()
                    except ValueError:
                        continue
                    qualname = function.__qualname__.replace(".<locals>.", ".")
                    fixtures.append(f"{rel}::{qualname}")
                items.append(
                    {
                        "nodeid": item.nodeid,
                        "canonical_node": canonical_node,
                        "fixtures": sorted(fixtures),
                        "non_passing_markers": sorted(
                            {
                                marker.name
                                for marker in item.iter_markers()
                                if marker.name in NON_PASSING_EVIDENCE_MARKERS
                            }
                        ),
                    }
                )

    before = Path.cwd()
    try:
        os.chdir(workspace)
        import pytest

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            status = pytest.main(["--collect-only", "-q", "tests"], plugins=[Collector()])
    finally:
        os.chdir(before)
    if status != 0:
        tail = captured.getvalue()[-4000:]
        raise PolicyError(f"pytest collection failed in {workspace}: exit {status}\n{tail}")
    fixture_consumers: dict[str, list[str]] = {}
    for item in items:
        for fixture in item["fixtures"]:
            fixture_consumers.setdefault(fixture, []).append(item["nodeid"])
    helper_consumers, unresolved_helpers = _static_helper_consumers(workspace, items)
    return {
        "version": 1,
        "workspace": str(workspace),
        "items": sorted(items, key=lambda item: item["nodeid"]),
        "fixture_consumers": {
            name: sorted(nodes) for name, nodes in sorted(fixture_consumers.items())
        },
        "helper_consumers": helper_consumers,
        "unresolved_helpers": unresolved_helpers,
    }


def _expand(selectors: list[str], collection: dict[str, Any], label: str) -> set[str]:
    items = collection.get("items", [])
    expanded: set[str] = set()
    for selector in selectors:
        if any(token in selector for token in ("*", "?", "[!")) or selector.endswith("::"):
            raise PolicyError(f"{label}: wildcards/module selectors are forbidden: {selector}")
        matches = {
            item["nodeid"]
            for item in items
            if item["nodeid"] == selector or item["canonical_node"] == selector
        }
        if not matches:
            raise PolicyError(f"{label}: selector collected no nodes: {selector}")
        if expanded & matches:
            raise PolicyError(f"{label}: duplicate selector coverage: {selector}")
        expanded.update(matches)
    return expanded


def _is_normalized_affected_path(value: Any) -> bool:
    if not isinstance(value, str) or any(
        token in value for token in ("*", "?", "[", "]", "{", "}", "\\")
    ):
        return False
    path = PurePosixPath(value)
    return (
        value == path.as_posix()
        and path.parts[:2] == ("src", "notebooklm")
        and len(path.parts) >= 3
        and path.suffix == ".py"
        and "." not in path.parts
        and ".." not in path.parts
    )


def _validate_mutation(row: Any, label: str) -> dict[str, Any]:
    if not isinstance(row, dict) or set(row) not in {
        frozenset(MODULE_MUTATION_FIELDS),
        frozenset(SHARED_MUTATION_FIELDS),
    }:
        raise PolicyError(f"{label}: base mutation is not an exact joint projection row")
    if row["owner_kind"] not in {"test", "fixture", "helper"}:
        raise PolicyError(f"{label}: invalid mutation owner kind")
    if not isinstance(row["count"], int) or isinstance(row["count"], bool) or row["count"] < 1:
        raise PolicyError(f"{label}: mutation count must be a positive integer")
    return row


def _mutation_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    fields = MODULE_MUTATION_FIELDS if "module" in row else SHARED_MUTATION_FIELDS
    return tuple(row[field] for field in sorted(fields - {"count"}))


def _unresolved_helper_mutation_key(row: dict[str, Any]) -> tuple[Any, ...]:
    target_field = "module" if "module" in row else "owner"
    return (
        row["package"],
        target_field,
        row[target_field],
        row["attribute"],
        row["idiom"],
        row["path"],
        row["owner_qualname"],
        row["owner_kind"],
        row["count"],
    )


# These are the complete unresolved helper rows in the pinned integration-base
# decrease ledger. Their scenario selectors and concrete mappings remain exact,
# and the global mutation counter still requires each full row and count. This
# exception does not authorize any other unresolved helper identity.
_PINNED_UNRESOLVED_HELPER_MUTATIONS: frozenset[tuple[Any, ...]] = frozenset(
    {
        (
            "notebooklm._auth",
            "module",
            "profile_store",
            "_STORAGE_LOCKS",
            "monkeypatch.setattr",
            "tests/unit/test_storage_writer.py",
            "_patch_lock_unavailable",
            "helper",
            1,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.profile_store.ProfileStore",
            "read_master_token",
            "patch.object",
            "tests/unit/test_auth_master_token_bootstrap.py",
            "test_public_malformed_projection_preserves_ordinary_context.invoke",
            "helper",
            1,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.single_flight.SingleFlight.process_default()",
            "claim",
            "gateway-method-or-unknown",
            "tests/unit/test_refresh_lock_registry.py",
            "TestDoubleCancelDoesNotDetonateBridge.test_second_cancel_while_pending_preserves_bridge_and_siblings._run",
            "helper",
            1,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.single_flight.SingleFlight.process_default()",
            "claim",
            "gateway-method-or-unknown",
            "tests/unit/test_refresh_lock_registry.py",
            "TestPromptPopRetention.test_registry_does_not_accumulate_across_cycles._run",
            "helper",
            1,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.single_flight.SingleFlight.process_default()",
            "claim",
            "gateway-method-or-unknown",
            "tests/unit/test_auth_headless_reauth.py",
            "test_single_flight_is_unreachable_from_the_sync_drive_entry._drive_in_worker_thread._claim_from_worker",
            "helper",
            1,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.single_flight.SingleFlight.process_default()",
            "claim",
            "gateway-method-or-unknown",
            "tests/unit/test_refresh_lock_registry.py",
            "TestFlightClaimIdentity.test_distinct_keys_get_distinct_flights._run",
            "helper",
            2,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.single_flight.SingleFlight.process_default()",
            "claim",
            "gateway-method-or-unknown",
            "tests/unit/test_refresh_lock_registry.py",
            "TestFlightClaimIdentity.test_second_claim_same_key_follows_leader._run",
            "helper",
            2,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.single_flight.SingleFlight.process_default()",
            "claim",
            "gateway-method-or-unknown",
            "tests/unit/test_refresh_lock_registry.py",
            "TestFlightClaimIdentity.test_settled_flight_is_overwritable_by_next_leader._run",
            "helper",
            2,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.single_flight.SingleFlight.process_default()",
            "claim_if_epoch_current",
            "gateway-method-or-unknown",
            "tests/unit/test_refresh_lock_registry.py",
            "TestClaimIfEpochCurrent.test_claims_when_epoch_unchanged._run",
            "helper",
            1,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.single_flight.SingleFlight.process_default()",
            "claim_if_epoch_current",
            "gateway-method-or-unknown",
            "tests/unit/test_refresh_lock_registry.py",
            "TestClaimIfEpochCurrent.test_skips_when_epoch_already_advanced._run",
            "helper",
            1,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.single_flight.SingleFlight.process_default()",
            "note_success",
            "gateway-method-or-unknown",
            "tests/unit/test_refresh_lock_registry.py",
            "TestClaimIfEpochCurrent.test_skips_when_epoch_already_advanced._run",
            "helper",
            1,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.storage_lock.StorageLockManager",
            "process_default",
            "monkeypatch.setattr",
            "tests/unit/test_storage_writer.py",
            "_patch_master_token_lock_unavailable",
            "helper",
            1,
        ),
        (
            "notebooklm._auth",
            "owner",
            "notebooklm._auth.profile_migration.LegacyPromotionScheduler.process_default()",
            "schedule",
            "gateway-method-or-unknown",
            "tests/unit/test_auth_account_promotion.py",
            "TestRetryableSingleFlight.test_concurrent_reads_of_one_profile_promote_exactly_once._read",
            "helper",
            1,
        ),
    }
)


_PINNED_UNRESOLVED_HELPER_CONSUMERS: dict[tuple[str, str], frozenset[str]] = {
    ("tests/unit/test_storage_writer.py", "_patch_lock_unavailable"): frozenset(
        {
            "tests/unit/test_storage_writer.py::test_clear_in_band_account_swallows_lock_unavailable",
            "tests/unit/test_storage_writer.py::test_persist_minted_jar_fails_closed_on_lock_unavailable",
            "tests/unit/test_storage_writer.py::test_replace_from_login_failed_write_leaves_legacy_account_untouched",
            "tests/unit/test_storage_writer.py::test_replace_from_login_fails_closed_on_lock_unavailable",
            "tests/unit/test_storage_writer.py::test_replace_from_remint_takes_storage_lock",
            "tests/unit/test_storage_writer.py::test_update_account_metadata_fails_closed_on_lock_unavailable",
        }
    ),
    (
        "tests/unit/test_auth_master_token_bootstrap.py",
        "test_public_malformed_projection_preserves_ordinary_context.invoke",
    ): frozenset(
        {
            "tests/unit/test_auth_master_token_bootstrap.py::test_public_malformed_projection_preserves_ordinary_context[False]",
            "tests/unit/test_auth_master_token_bootstrap.py::test_public_malformed_projection_preserves_ordinary_context[True]",
        }
    ),
    (
        "tests/unit/test_refresh_lock_registry.py",
        "TestDoubleCancelDoesNotDetonateBridge.test_second_cancel_while_pending_preserves_bridge_and_siblings._run",
    ): frozenset(
        {
            "tests/unit/test_refresh_lock_registry.py::TestDoubleCancelDoesNotDetonateBridge::test_second_cancel_while_pending_preserves_bridge_and_siblings"
        }
    ),
    (
        "tests/unit/test_refresh_lock_registry.py",
        "TestPromptPopRetention.test_registry_does_not_accumulate_across_cycles._run",
    ): frozenset(
        {
            "tests/unit/test_refresh_lock_registry.py::TestPromptPopRetention::test_registry_does_not_accumulate_across_cycles"
        }
    ),
    (
        "tests/unit/test_auth_headless_reauth.py",
        "test_single_flight_is_unreachable_from_the_sync_drive_entry._drive_in_worker_thread._claim_from_worker",
    ): frozenset(
        {
            "tests/unit/test_auth_headless_reauth.py::test_single_flight_is_unreachable_from_the_sync_drive_entry"
        }
    ),
    (
        "tests/unit/test_refresh_lock_registry.py",
        "TestFlightClaimIdentity.test_distinct_keys_get_distinct_flights._run",
    ): frozenset(
        {
            "tests/unit/test_refresh_lock_registry.py::TestFlightClaimIdentity::test_distinct_keys_get_distinct_flights"
        }
    ),
    (
        "tests/unit/test_refresh_lock_registry.py",
        "TestFlightClaimIdentity.test_second_claim_same_key_follows_leader._run",
    ): frozenset(
        {
            "tests/unit/test_refresh_lock_registry.py::TestFlightClaimIdentity::test_second_claim_same_key_follows_leader"
        }
    ),
    (
        "tests/unit/test_refresh_lock_registry.py",
        "TestFlightClaimIdentity.test_settled_flight_is_overwritable_by_next_leader._run",
    ): frozenset(
        {
            "tests/unit/test_refresh_lock_registry.py::TestFlightClaimIdentity::test_settled_flight_is_overwritable_by_next_leader"
        }
    ),
    (
        "tests/unit/test_refresh_lock_registry.py",
        "TestClaimIfEpochCurrent.test_claims_when_epoch_unchanged._run",
    ): frozenset(
        {
            "tests/unit/test_refresh_lock_registry.py::TestClaimIfEpochCurrent::test_claims_when_epoch_unchanged"
        }
    ),
    (
        "tests/unit/test_refresh_lock_registry.py",
        "TestClaimIfEpochCurrent.test_skips_when_epoch_already_advanced._run",
    ): frozenset(
        {
            "tests/unit/test_refresh_lock_registry.py::TestClaimIfEpochCurrent::test_skips_when_epoch_already_advanced"
        }
    ),
    ("tests/unit/test_storage_writer.py", "_patch_master_token_lock_unavailable"): frozenset(
        {
            "tests/unit/test_storage_writer.py::test_write_master_token_fails_closed_on_lock_unavailable"
        }
    ),
    (
        "tests/unit/test_auth_account_promotion.py",
        "TestRetryableSingleFlight.test_concurrent_reads_of_one_profile_promote_exactly_once._read",
    ): frozenset(
        {
            "tests/unit/test_auth_account_promotion.py::TestRetryableSingleFlight::test_concurrent_reads_of_one_profile_promote_exactly_once"
        }
    ),
}


def _mutation_counter(rows: list[dict[str, Any]]) -> Counter[tuple[Any, ...]]:
    result: Counter[tuple[Any, ...]] = Counter()
    for row in rows:
        result[_mutation_identity(row)] += int(row["count"])
    return result


def _mutation_row_counter(rows: list[dict[str, Any]]) -> Counter[tuple[Any, ...]]:
    result: Counter[tuple[Any, ...]] = Counter()
    for row in rows:
        fields = MODULE_MUTATION_FIELDS if "module" in row else SHARED_MUTATION_FIELDS
        result[tuple(row[field] for field in sorted(fields))] += 1
    return result


def collect_mutation_projection(workspace: Path) -> list[dict[str, Any]]:
    """Run the head-version collectors against an arbitrary workspace."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.audit_auth_patch_sites import build_projection, collect_sites
    from scripts.audit_auth_shared_mutations import build_projection as build_shared
    from scripts.audit_auth_shared_mutations import collect_mutations

    tests = workspace / "tests"
    projections = [
        build_projection(collect_sites(tests, workspace / "src/notebooklm/_auth")),
        build_projection(
            collect_sites(
                tests,
                workspace / "src/notebooklm/_browser",
                package_dotted="notebooklm._browser",
            )
        ),
        build_projection(
            collect_sites(
                tests,
                workspace / "src/notebooklm/auth.py",
                package_dotted="notebooklm.auth",
            )
        ),
    ]
    module_rows = [
        dict(row)
        for projection in projections
        for row in cast(list[dict[str, Any]], projection.get("joint_sites", []))
    ]
    shared = build_shared(
        collect_mutations(
            tests,
            {
                "notebooklm._auth": workspace / "src/notebooklm/_auth",
                "notebooklm._browser": workspace / "src/notebooklm/_browser",
            },
        )
    )
    return [
        *module_rows,
        *(dict(row) for row in cast(list[dict[str, Any]], shared.get("mutations", []))),
    ]


def decreased_mutations(
    base_rows: list[dict[str, Any]], head_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return each decreased full joint row with only the removed count."""
    base_by_id = {_mutation_identity(row): row for row in base_rows}
    old = _mutation_counter(base_rows)
    new = _mutation_counter(head_rows)
    result: list[dict[str, Any]] = []
    for identity, old_count in old.items():
        if old_count <= new[identity]:
            continue
        row = dict(base_by_id[identity])
        row["count"] = old_count - new[identity]
        result.append(row)
    return sorted(result, key=lambda row: tuple(str(item) for item in _mutation_identity(row)))


def _expected_consumers(
    mutations: list[dict[str, Any]], collection: dict[str, Any], label: str
) -> set[str]:
    expected: set[str] = set()
    items = collection.get("items", [])
    for mutation in mutations:
        path = mutation["path"]
        qualname = mutation["owner_qualname"]
        kind = mutation["owner_kind"]
        key = f"{path}::{qualname}"
        if kind == "test":
            canonical = f"{path}::{qualname.replace('.', '::')}"
            consumers = {
                item["nodeid"] for item in items if item.get("canonical_node") == canonical
            }
        elif kind == "fixture":
            consumers = set(collection.get("fixture_consumers", {}).get(key, []))
        else:
            if key in collection.get("unresolved_helpers", []):
                raise PolicyError(f"{label}: unresolved dynamic callers for {key}")
            consumers = set(collection.get("helper_consumers", {}).get(key, []))
        if not consumers:
            raise PolicyError(f"{label}: cannot resolve every {kind} consumer for {key}")
        expected.update(consumers)
    return expected


def validate_policy(
    policy: dict[str, Any],
    base: dict[str, Any],
    head: dict[str, Any],
    *,
    base_policy: dict[str, Any] | None = None,
    required_base_mutations: list[dict[str, Any]] | None = None,
    lifecycle_policy: dict[str, Any] | None = None,
    base_lifecycle_policy: dict[str, Any] | None = None,
) -> None:
    if policy.get("version") != 1 or not isinstance(policy.get("scenarios"), list):
        raise PolicyError("scenario policy must be version 1 with a scenarios list")
    if base_policy is not None and (
        base_policy.get("version") != 1 or not isinstance(base_policy.get("scenarios"), list)
    ):
        raise PolicyError("base scenario policy must be version 1 with a scenarios list")
    previous = {row["id"]: row for row in (base_policy or {}).get("scenarios", [])}
    ids: set[str] = set()
    replacement_users: set[str] = set()
    new_evidence: list[dict[str, Any]] = []
    for row in policy["scenarios"]:
        required = {
            "id",
            "base_selectors",
            "replacement_selectors",
            "node_mapping",
            "affected_paths",
            "contracts",
            "base_mutations",
        }
        if set(row) != required:
            raise PolicyError(f"scenario row has wrong fields: {set(row) ^ required}")
        if row["id"] in ids:
            raise PolicyError(f"duplicate scenario id: {row['id']}")
        ids.add(row["id"])
        is_new = row["id"] not in previous
        if not is_new and row != previous[row["id"]]:
            raise PolicyError(f"{row['id']}: existing scenario rows are immutable")
        if (
            not isinstance(row["affected_paths"], list)
            or not row["affected_paths"]
            or not all(_is_normalized_affected_path(path) for path in row["affected_paths"])
            or len(row["affected_paths"]) != len(set(row["affected_paths"]))
        ):
            raise PolicyError(
                f"{row['id']}: affected_paths must name exact normalized production files"
            )
        if not row["contracts"] or not set(row["contracts"]) <= CONTRACT_TAGS:
            raise PolicyError(f"{row['id']}: unknown or empty contract tags")
        mutations = [
            _validate_mutation(mutation, f"{row['id']} base_mutations")
            for mutation in row["base_mutations"]
        ]
        if not mutations:
            raise PolicyError(f"{row['id']}: base_mutations cannot be empty")
        old_nodes = _expand(row["base_selectors"], base, f"{row['id']} base") if is_new else set()
        new_nodes = _expand(row["replacement_selectors"], head, f"{row['id']} head")
        non_passing = {
            item["nodeid"]: item.get("non_passing_markers", [])
            for item in head.get("items", [])
            if item["nodeid"] in new_nodes and item.get("non_passing_markers")
        }
        if non_passing:
            raise PolicyError(
                f"{row['id']}: replacement nodes are not passing CI evidence: {non_passing}"
            )
        if not row["node_mapping"] or any(
            not isinstance(entry, dict)
            or set(entry) != {"base", "head"}
            or not isinstance(entry["head"], list)
            or not entry["head"]
            for entry in row["node_mapping"]
        ):
            raise PolicyError(f"{row['id']}: node mapping has invalid rows")
        mapping_domain = [entry["base"] for entry in row["node_mapping"]]
        mapping_codomain = [node for entry in row["node_mapping"] for node in entry["head"]]
        if len(mapping_domain) != len(set(mapping_domain)) or len(mapping_codomain) != len(
            set(mapping_codomain)
        ):
            raise PolicyError(f"{row['id']}: node mapping contains duplicate nodes")
        if (is_new and set(mapping_domain) != old_nodes) or set(mapping_codomain) != new_nodes:
            raise PolicyError(f"{row['id']}: node mapping is not exact")
        if replacement_users & new_nodes:
            raise PolicyError(
                f"{row['id']}: replacement node reused; encode a split/merge as one group row"
            )
        replacement_users.update(new_nodes)
        if is_new:
            unresolved_keys = set(base.get("unresolved_helpers", []))
            unresolved = [
                mutation
                for mutation in mutations
                if mutation["owner_kind"] == "helper"
                and f"{mutation['path']}::{mutation['owner_qualname']}" in unresolved_keys
            ]
            unexpected = [
                mutation
                for mutation in unresolved
                if _unresolved_helper_mutation_key(mutation)
                not in _PINNED_UNRESOLVED_HELPER_MUTATIONS
            ]
            if unexpected:
                mutation = unexpected[0]
                key = f"{mutation['path']}::{mutation['owner_qualname']}"
                raise PolicyError(f"{row['id']}: unresolved dynamic callers for {key}")
            resolvable = [mutation for mutation in mutations if mutation not in unresolved]
            expected = _expected_consumers(resolvable, base, row["id"])
            for mutation in unresolved:
                expected.update(
                    _PINNED_UNRESOLVED_HELPER_CONSUMERS[
                        (mutation["path"], mutation["owner_qualname"])
                    ]
                )
            if old_nodes != expected:
                raise PolicyError(
                    f"{row['id']}: selectors do not exactly cover helper/fixture consumers"
                )
        if is_new:
            new_evidence.extend(mutations)
    removed_ids = set(previous) - ids
    if removed_ids:
        raise PolicyError(f"scenario rows cannot be deleted: {sorted(removed_ids)}")
    if required_base_mutations is not None:

        def normalize_lifecycle_row(row: dict[str, Any]) -> str:
            normalized = dict(row)
            legacy = normalized.pop("replaced_base_mutation", None)
            if "replaced_base_mutations" not in normalized:
                normalized["replaced_base_mutations"] = [] if legacy is None else [legacy]
            return json.dumps(normalized, sort_keys=True)

        old_lifecycle_rows = {
            normalize_lifecycle_row(row)
            for row in (base_lifecycle_policy or {}).get("operations", [])
        }
        lifecycle_evidence = [
            _validate_mutation(mutation, "lifecycle replacement")
            for row in (lifecycle_policy or {}).get("operations", [])
            if normalize_lifecycle_row(row) not in old_lifecycle_rows
            for mutation in row.get("replaced_base_mutations", [])
        ]
        if _mutation_row_counter([*new_evidence, *lifecycle_evidence]) != _mutation_row_counter(
            required_base_mutations
        ):
            raise PolicyError(
                "new scenario/lifecycle evidence does not exactly match decreased joint rows"
            )


def _policy_at_revision(revision: str, path: Path) -> dict[str, Any]:
    rel = path.resolve().relative_to(REPO_ROOT).as_posix()
    result = subprocess.run(
        ["git", "show", f"{revision}:{rel}"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return {"version": 1, "scenarios": []}
    return json.loads(result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--workspace", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--base-collection", type=Path, required=True)
    validate.add_argument("--head-collection", type=Path, required=True)
    validate.add_argument("--base-revision", required=True)
    validate.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    validate_head = sub.add_parser("validate-head")
    validate_head.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            args.output.write_text(
                json.dumps(collect_workspace(args.workspace), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif args.command == "validate-head":
            head = collect_workspace(REPO_ROOT)
            policy = load_json(args.policy)
            validate_policy(policy, head, head, base_policy=policy)
        else:
            base = load_json(args.base_collection)
            head = load_json(args.head_collection)
            policy = load_json(args.policy)
            base_policy = _policy_at_revision(args.base_revision, args.policy)
            base_lifecycle = _policy_at_revision(args.base_revision, LIFECYCLE_POLICY)
            lifecycle = load_json(LIFECYCLE_POLICY)
            try:
                base_workspace = Path(base["workspace"])
                head_workspace = Path(head["workspace"])
            except (KeyError, TypeError) as exc:
                raise PolicyError("collection artifacts must record their workspace") from exc
            required = decreased_mutations(
                collect_mutation_projection(base_workspace),
                collect_mutation_projection(head_workspace),
            )
            validate_policy(
                policy,
                base,
                head,
                base_policy=base_policy,
                required_base_mutations=required,
                lifecycle_policy=lifecycle,
                base_lifecycle_policy=base_lifecycle,
            )
    except PolicyError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
