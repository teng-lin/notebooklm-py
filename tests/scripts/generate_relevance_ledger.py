#!/usr/bin/env python3
"""Generate the test relevance and routing ledger (Phase 1).

Follows Section 3, Section 4, and Section 6.1 of the test-optimization plan:
- Reviews all guardrail test files and key behavioral candidate families.
- Assigns each node a purpose, oracle description, overlapping tests, runtime cost,
  owner, rationale, review condition, and relevance decision.
- Outputs `tests/fixtures/test_relevance_ledger.json`.
"""

from __future__ import annotations

import ast
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
LEDGER_PATH = FIXTURES_DIR / "test_relevance_ledger.json"

Decision = Literal[
    "pr_routine",
    "pr_contract",
    "extended_qualification",
    "historical_manual",
    "consolidate",
    "deleted_after_replacement",
]


@dataclass(frozen=True)
class LedgerEntry:
    nodeid: str
    file: str
    subsystem: str
    failure_detected: str
    oracle: str
    overlapping_tests: list[str]
    runtime_cost: str
    decision: Decision
    owner: str
    rationale: str
    replacement: str | None
    review_condition: str


# Specific decisions per Section 4 of the optimization plan
NODE_OVERRIDE_RULES: dict[str, dict[str, object]] = {
    # 4.A Completed v0.8.0 migration
    "test_v080_deprecation_coverage.py": {
        "decision": "historical_manual",
        "subsystem": "migration_v080",
        "owner": "migration_maintainers",
        "failure_detected": "v0.8.0 release preparation and deprecation runway enforcement",
        "oracle": "Static table verification; table was drained at v0.8.0 release (V080_BREAKING_CHANGES == ())",
        "rationale": "Drained at 0.8.0 release; no active rows remaining; retained for historical release reconstruction only",
        "review_condition": "historical_reference_until_v1.0",
        "runtime_cost": "fast_static",
    },
    "test_v080_release_gate.py": {
        "decision": "historical_manual",
        "subsystem": "migration_v080",
        "owner": "migration_maintainers",
        "failure_detected": "Unflipped v0.8.0 breaking changes at release bump",
        "oracle": "Bidirectional check around v0.8.0 version flip and synthetic self-tests",
        "rationale": "Package is at v0.8.2; purpose is reconstruction of that completed release program",
        "review_condition": "historical_reference_until_v1.0",
        "runtime_cost": "fast_static",
    },
    # Keep v1.0 gates active!
    "test_v100_release_gate.py": {
        "decision": "pr_contract",
        "subsystem": "future_v100",
        "owner": "api_platform",
        "failure_detected": "Premature unrunwayed break before v1.0.0 or orphaned deprecation",
        "oracle": "Bidirectional version-aware gate enforcing v1.0 deprecation runway while package is 0.8.x",
        "rationale": "Active contract governing upcoming v1.0 breaking changes",
        "review_condition": "active_until_v1.0_release",
        "runtime_cost": "fast_static",
    },
    "test_v100_deprecation_coverage.py": {
        "decision": "pr_contract",
        "subsystem": "future_v100",
        "owner": "api_platform",
        "failure_detected": "Unrunwayed future v1.0 breaking change",
        "oracle": "Validates runway modules and issue links for declared v1.0 breaks",
        "rationale": "Active deprecation runway coverage for future release",
        "review_condition": "active_until_v1.0_release",
        "runtime_cost": "fast_static",
    },
    # 4.B Retired layout/seams
    "test_client_operation_contract_inventory.py": {
        "decision": "historical_manual",
        "subsystem": "retired_layout",
        "owner": "client_architecture",
        "failure_detected": "Drift in client-ownership refactor transition signatures and phase dispositions",
        "oracle": "Exact AST parameter signature matching across frozen migration inventories",
        "rationale": "All client refactor phases are completed; inventories represent past migration states",
        "review_condition": "historical_reference_only",
        "runtime_cost": "ast_scan",
    },
    "test_no_session_compat_bridges.py": {
        "decision": "historical_manual",
        "subsystem": "retired_layout",
        "owner": "client_architecture",
        "failure_detected": "External access to retired concrete Session private attributes",
        "oracle": "AST attribute traversal detecting access to retired session bridge properties",
        "rationale": "Session removal completed; collaborator behavior guarded by live constructor and runtime tests",
        "review_condition": "historical_reference_only",
        "runtime_cost": "ast_scan",
    },
    "test_no_cli_client_patch_surface.py": {
        "decision": "pr_routine",
        "subsystem": "cli_adapters",
        "owner": "cli_adapters",
        "failure_detected": "Reintroduction of patchable NotebookLMClient on *_cmd modules",
        "oracle": "Dynamic import and runtime surface inspection of every discovered Click command module",
        "rationale": "Future command modules must not restore the retired patchable client seam",
        "review_condition": "active_cli_injection",
        "runtime_cost": "fast_runtime_import",
    },
    "test_no_session_cmd_patch_surface.py": {
        "decision": "historical_manual",
        "subsystem": "retired_layout",
        "owner": "cli_adapters",
        "failure_detected": "Reintroduction of retired session_cmd patch names",
        "oracle": "AST inspection of session_cmd module exports and attributes",
        "rationale": "Retired session CLI commands migrated; current auth/session behavior independently tested",
        "review_condition": "historical_reference_only",
        "runtime_cost": "ast_scan",
    },
    "test_no_core_imports.py": {
        "decision": "pr_routine",
        "subsystem": "retired_layout",
        "owner": "architecture_guards",
        "failure_detected": "Runtime imports of deprecated notebooklm._core in load-bearing modules",
        "oracle": "AST import scan across codebase",
        "rationale": "Core removal completed; active neutral and backend import boundary rules cover forbidden edges",
        "review_condition": "active_architecture_guard",
        "runtime_cost": "ast_scan",
    },
    "test_no_session.py": {
        "decision": "pr_routine",
        "subsystem": "retired_layout",
        "owner": "client_architecture",
        "failure_detected": "Deleted concrete session surface re-emerging in source or tests",
        "oracle": "String/AST scan for deleted Session class references",
        "rationale": "Session removal complete; live construction, admission, and lifecycle tests protect client",
        "review_condition": "active_architecture_guard",
        "runtime_cost": "ast_scan",
    },
    "test_no_legacy_rpc_callable_aliases.py": {
        "decision": "pr_routine",
        "subsystem": "retired_layout",
        "owner": "protocol_wire",
        "failure_detected": "Retired RPC callable aliases returning to dependency vocabulary",
        "oracle": "AST inspection of RPC caller attributes",
        "rationale": "RPC callable consolidation complete; live upload callback and RPC dispatch tests verify active contract",
        "review_condition": "active_architecture_guard",
        "runtime_cost": "ast_scan",
    },
    "test_unconfirmed_contract.py": {
        "decision": "pr_routine",
        "subsystem": "protocol_wire",
        "owner": "protocol_wire",
        "failure_detected": "Ambiguous mutation outcome handling between Web batchexecute and Android gRPC backends",
        "oracle": "Simulated ambiguous RPC response and status reconciliation across backends",
        "rationale": "Live behavioral mutation contract; should run in routine PR suite rather than hidden as repo_lint",
        "review_condition": "active_behavioral_contract",
        "runtime_cost": "fast_behavioral",
    },
    "test_wire_contract.py": {
        "decision": "pr_contract",
        "subsystem": "protocol_wire",
        "owner": "protocol_wire",
        "failure_detected": "Positional constant and enum value divergence between SDK adapters and backend wire schemas",
        "oracle": "Positive schema comparison against decoded protocol golden descriptors and enum definitions",
        "rationale": "PR must validate actual positional constants and enum values, not just declaration completeness",
        "review_condition": "active_wire_contract",
        "runtime_cost": "fast_static",
    },
    "test_studio_enum_manifest.py": {
        "decision": "pr_contract",
        "subsystem": "protocol_wire",
        "owner": "protocol_wire",
        "failure_detected": "Wire integer enum drift across generation, research, and artifact types",
        "oracle": "Frozen snapshot comparison against canonical IntEnum descriptors",
        "rationale": "Positive enum value contract run once per PR",
        "review_condition": "active_wire_contract",
        "runtime_cost": "fast_static",
    },
    "test_mcp_lazy_client_error_boundary.py": {
        "decision": "pr_contract",
        "subsystem": "adapter_boundary",
        "owner": "mcp_adapters",
        "failure_detected": "Lost error envelopes during lazy authentication or network failure in MCP tools",
        "oracle": "AST inspection that every MCP tool entry point resolves lazy client within classify_error guard",
        "rationale": "Prevents unhandled error escape in MCP stdio process; critical PR contract",
        "review_condition": "active_adapter_contract",
        "runtime_cost": "ast_scan",
    },
    "test_cassettes_clean.py": {
        "decision": "pr_contract",
        "subsystem": "security_sanitization",
        "owner": "auth_security",
        "failure_detected": "Unscrubbed Google authentication tokens, cookies, or API keys in committed cassettes",
        "oracle": "Subprocess invocation of check_cassettes_clean.py over recorded cassettes and fixtures",
        "rationale": "Run once per PR in contracts/quality; avoids redundant whole-tree execution across compatibility cells",
        "review_condition": "active_security_invariant",
        "runtime_cost": "subprocess",
    },
    "test_public_surface_manifest.py": {
        "decision": "pr_contract",
        "subsystem": "public_surface",
        "owner": "api_platform",
        "failure_detected": "Export surface drift or unauthorized __all__ mutation across public package shims",
        "oracle": "Comparison of live exported symbols against committed baseline JSONs",
        "rationale": "Run once in PR contract lane; caching AST derivations reduces worker contention",
        "review_condition": "active_api_contract",
        "runtime_cost": "baseline_derivation",
    },
}

# Node-specific rules within files that have mixed decisions
MIXED_NODE_RULES: dict[str, dict[str, object]] = {
    # 4.C Auth migration files
    "test_auth_cookie_filter_boundary.py::test_malformed_diagnostics_never_render_a_raw_value": {
        "decision": "pr_routine",
        "subsystem": "auth_security",
        "owner": "auth_security",
        "failure_detected": "Malformed cookie diagnostic strings rendering raw token values in exceptions or logs",
        "oracle": "Exercised cookie redaction filter on malformed raw cookie strings asserting no plaintext leakage",
        "rationale": "Active behavioral security invariant; must remain in routine PR suite",
        "review_condition": "permanent_security_invariant",
        "runtime_cost": "fast_behavioral",
    },
    "test_auth_cold_recovery_coordinator_boundary.py::test_callback_fields_scrub_after_success_saved_error_cancellation_and_baseexception": {
        "decision": "pr_routine",
        "subsystem": "auth_security",
        "owner": "auth_security",
        "failure_detected": "Stale credentials or recovery callbacks surviving after completed or failed token recovery",
        "oracle": "Coordinator state assertion after simulated success, error, cancellation, and BaseException",
        "rationale": "Active credential scrubbing regression test",
        "review_condition": "permanent_security_invariant",
        "runtime_cost": "fast_behavioral",
    },
    "test_auth_cold_recovery_coordinator_boundary.py::test_losing_concurrent_and_post_completion_calls_cannot_scrub_or_invoke_callbacks": {
        "decision": "pr_routine",
        "subsystem": "auth_security",
        "owner": "auth_security",
        "failure_detected": "Concurrent loser or post-completion caller inadvertently scrubbing active credentials",
        "oracle": "Asynchronous race harness testing coordinator state transitions under concurrent recovery",
        "rationale": "Active concurrency regression test",
        "review_condition": "permanent_concurrency_invariant",
        "runtime_cost": "concurrency_behavioral",
    },
    "test_auth_master_token_file_boundary.py::test_raw_projection_exists_only_in_the_public_legacy_reader": {
        "decision": "pr_contract",
        "subsystem": "auth_security",
        "owner": "auth_security",
        "failure_detected": "Raw master token file projection leaking outside the designated public legacy reader",
        "oracle": "AST boundary scan ensuring unencrypted token file read site is isolated to legacy reader",
        "rationale": "Capability guard isolating master token access while legacy reader is supported",
        "review_condition": "active_until_legacy_reader_deprecation",
        "runtime_cost": "ast_scan",
    },
    "test_auth_master_token_types_boundary.py::test_runtime_value_shape_and_redaction_are_pinned": {
        "decision": "pr_contract",
        "subsystem": "auth_security",
        "owner": "auth_security",
        "failure_detected": "Master token representation exposing unredacted secret in __repr__ or string conversion",
        "oracle": "Instantiates MasterToken and asserts redaction sentinel in str() and repr()",
        "rationale": "Preserves token confidentiality across logging and error formatting",
        "review_condition": "permanent_security_invariant",
        "runtime_cost": "fast_behavioral",
    },
    "test_auth_profile_document_boundary.py::test_profile_document_size_and_documentation_pins_hold": {
        "decision": "extended_qualification",
        "subsystem": "auth_security",
        "owner": "auth_security",
        "failure_detected": "Prose or byte length drift in profile document structure",
        "oracle": "Exact length and documentation comment assertions on profile storage schema",
        "rationale": "Non-behavioral prose/size pin; separated from document correctness and kept in extended qualification",
        "review_condition": "extended_qualification_only",
        "runtime_cost": "fast_static",
    },
    "test_auth_storage_compatibility.py::test_legacy_account_value_shape_identity_and_pickle_contract": {
        "decision": "pr_routine",
        "subsystem": "auth_security",
        "owner": "auth_security",
        "failure_detected": "Pickle deserialization failure or value shape mutation for stored legacy accounts",
        "oracle": "Round-trip pickle deserialization of legacy account payload verifying field identity",
        "rationale": "Active backwards-compatibility test for existing user profile storage",
        "review_condition": "active_until_v1.0_storage_retirement",
        "runtime_cost": "fast_behavioral",
    },
    "test_auth_storage_compatibility.py::test_first_party_facade_callers_are_frozen_in_both_import_idioms": {
        "decision": "historical_manual",
        "subsystem": "retired_layout",
        "owner": "auth_security",
        "failure_detected": "Call site cardinality changes in first-party facade migration ledger",
        "oracle": "AST caller count verification against frozen migration table",
        "rationale": "Historical caller count ledger from completed auth facade migration",
        "review_condition": "historical_reference_only",
        "runtime_cost": "ast_scan",
    },
    "test_auth_profile_store_boundary.py::test_every_native_replace_caller_has_a_live_b3_behavior_test": {
        "decision": "pr_routine",
        "subsystem": "auth_security",
        "owner": "auth_security",
        "failure_detected": "Atomic profile replacement caller lacking corresponding behavioral lock/replace test",
        "oracle": "Mapping of atomic replacement call sites to behavioral test node IDs",
        "rationale": "Ensures every profile store modification is backed by live transactional test coverage",
        "review_condition": "active_contract",
        "runtime_cost": "fast_static",
    },
    "test_storage_transaction_ratchet.py::test_body_lock_unavailable_error_is_not_misclassified_as_lock_miss": {
        "decision": "pr_routine",
        "subsystem": "auth_security",
        "owner": "auth_security",
        "failure_detected": "Lock acquisition failure misclassified as missing storage file",
        "oracle": "Simulated lock contention asserting BodyLockUnavailableError classification",
        "rationale": "Active behavioral error-handling regression test",
        "review_condition": "permanent_concurrency_invariant",
        "runtime_cost": "fast_behavioral",
    },
    # 4.D Promoted qualification cases from test_client_lifecycle_waves.py
    "test_client_lifecycle_waves.py::test_open_is_transactional_and_concurrent_callers_coalesce": {
        "decision": "pr_routine",
        "subsystem": "client_lifecycle",
        "owner": "client_architecture",
        "failure_detected": "Non-transactional client open allowing duplicate concurrent connection establishment",
        "oracle": "Concurrent asyncio tasks invoking open() simultaneously asserting single transport creation",
        "rationale": "Promoted from refactor_qualification per Section 4.D; critical transactional invariant",
        "review_condition": "permanent_lifecycle_contract",
        "runtime_cost": "async_behavioral",
    },
    "test_client_lifecycle_waves.py::test_open_failure_rolls_back_every_transport_and_preserves_original": {
        "decision": "pr_routine",
        "subsystem": "client_lifecycle",
        "owner": "client_architecture",
        "failure_detected": "Partial open failure leaking transport resources or corrupting existing client state",
        "oracle": "Injects failure during secondary transport open and verifies clean rollback of all transports",
        "rationale": "Promoted from refactor_qualification per Section 4.D; essential rollback safety",
        "review_condition": "permanent_lifecycle_contract",
        "runtime_cost": "async_behavioral",
    },
    "test_client_lifecycle_waves.py::test_cancelling_non_owner_open_does_not_abort_owner": {
        "decision": "pr_routine",
        "subsystem": "client_lifecycle",
        "owner": "client_architecture",
        "failure_detected": "Cancelling a waiter task aborting the owning task responsible for open",
        "oracle": "Asyncio cancellation of concurrent waiter asserting owner continues uninterrupted",
        "rationale": "Promoted from refactor_qualification per Section 4.D; concurrency ownership safety",
        "review_condition": "permanent_lifecycle_contract",
        "runtime_cost": "async_behavioral",
    },
    "test_client_lifecycle_waves.py::test_cancelled_close_aborts_hung_graceful_wait_but_finishes_teardown": {
        "decision": "pr_routine",
        "subsystem": "client_lifecycle",
        "owner": "client_architecture",
        "failure_detected": "Hung close() wait preventing terminal resource release when cancelled",
        "oracle": "Cancels in-progress graceful close wait asserting complete teardown still occurs",
        "rationale": "Promoted from refactor_qualification per Section 4.D; resource cleanup guarantee",
        "review_condition": "permanent_lifecycle_contract",
        "runtime_cost": "async_behavioral",
    },
    "test_client_lifecycle_waves.py::test_close_reopen_allocates_a_new_resource_epoch": {
        "decision": "pr_routine",
        "subsystem": "client_lifecycle",
        "owner": "client_architecture",
        "failure_detected": "Reopened client reusing stale resource handles or previous epoch identifiers",
        "oracle": "Opens, closes, and reopens client asserting fresh epoch allocation",
        "rationale": "Promoted from refactor_qualification per Section 4.D; epoch isolation contract",
        "review_condition": "permanent_lifecycle_contract",
        "runtime_cost": "async_behavioral",
    },
    "test_client_lifecycle_waves.py::test_registered_child_self_close_fails_fast_without_leaking_admission": {
        "decision": "pr_routine",
        "subsystem": "client_lifecycle",
        "owner": "client_architecture",
        "failure_detected": "Child task close recursion leaking admission counters",
        "oracle": "Invokes self-close from child task asserting fail-fast and admission settlement",
        "rationale": "Promoted from refactor_qualification per Section 4.D; admission integrity",
        "review_condition": "permanent_lifecycle_contract",
        "runtime_cost": "async_behavioral",
    },
    "test_client_lifecycle_waves.py::test_poll_callback_self_close_fails_fast_and_poll_settles_once": {
        "decision": "pr_routine",
        "subsystem": "client_lifecycle",
        "owner": "client_architecture",
        "failure_detected": "Poll callback self-close corrupting poll loop or settling multiple times",
        "oracle": "Self-close from within poll event callback asserting exact single settlement",
        "rationale": "Promoted from refactor_qualification per Section 4.D; poll lifecycle contract",
        "review_condition": "permanent_lifecycle_contract",
        "runtime_cost": "async_behavioral",
    },
    # 4.F Behavioral duplication
    "tests/integration/faults/test_web_workflows.py": {
        "decision": "consolidate",
        "subsystem": "fault_resilience",
        "owner": "fault_resilience",
        "failure_detected": "Duplicate execution of all web_workflows scenarios already covered in test_web_faults.py",
        "oracle": "Identical scenario execution to test_web_faults.py",
        "overlapping_tests": ["tests/integration/faults/test_web_faults.py"],
        "rationale": "All workflow scenarios are registered in web_scenarios.SCENARIOS and run in test_web_faults.py; consolidate into dispatcher routing check",
        "replacement": "tests/integration/faults/test_web_faults.py",
        "review_condition": "consolidated_in_phase_4",
        "runtime_cost": "loopback_socket",
    },
}


def build_ledger() -> dict[str, object]:
    """Inspect all guardrail and historical files and build comprehensive relevance ledger."""
    guardrails_dir = REPO_ROOT / "tests" / "_guardrails"
    historical_dir = REPO_ROOT / "tests" / "qualification" / "historical"
    files = sorted(list(guardrails_dir.glob("test_*.py")) + list(historical_dir.glob("test_*.py")))

    entries: list[dict[str, object]] = []

    for f in files:
        rel_path = f.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(f.read_text("utf-8"))
        doc = ast.get_docstring(tree) or ""
        first_line = doc.splitlines()[0] if doc else f"Guardrail test in {f.name}"

        # Collect function names
        funcs = [
            n.name
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
        ]

        file_rule = NODE_OVERRIDE_RULES.get(f.name, {})

        for func in funcs:
            nodeid = f"{rel_path}::{func}"
            short_nodeid = f"{f.name}::{func}"

            # Check if specific node rule exists
            node_rule = MIXED_NODE_RULES.get(short_nodeid, MIXED_NODE_RULES.get(nodeid))
            if node_rule:
                rule = node_rule
            elif file_rule:
                rule = file_rule
            else:
                # Default guardrail classification
                is_contract = any(
                    k in f.name
                    for k in (
                        "boundary",
                        "manifest",
                        "contract",
                        "parity",
                        "secret",
                        "security",
                        "error",
                        "wire",
                    )
                )
                rule = {
                    "decision": "pr_contract" if is_contract else "extended_qualification",
                    "subsystem": "guardrails",
                    "owner": "platform_quality",
                    "failure_detected": first_line,
                    "oracle": f"Static or AST guard checking {f.name}",
                    "rationale": (
                        "PR critical contract run once on primary platform"
                        if is_contract
                        else "Extended repository audit run in extended qualification"
                    ),
                    "review_condition": "active_guardrail",
                    "runtime_cost": "ast_scan",
                }

            entry = LedgerEntry(
                nodeid=nodeid,
                file=rel_path,
                subsystem=str(rule.get("subsystem", "guardrails")),
                failure_detected=str(rule.get("failure_detected", first_line)),
                oracle=str(rule.get("oracle", "AST or code-structure inspection")),
                overlapping_tests=list(rule.get("overlapping_tests", [])),
                runtime_cost=str(rule.get("runtime_cost", "ast_scan")),
                decision=rule["decision"],  # type: ignore[arg-type]
                owner=str(rule.get("owner", "platform_quality")),
                rationale=str(rule.get("rationale", "")),
                replacement=str(rule["replacement"]) if "replacement" in rule else None,
                review_condition=str(rule.get("review_condition", "active_contract")),
            )
            entries.append(asdict(entry))

    # Add promoted cases from test_client_lifecycle_waves.py
    lifecycle_file = "tests/unit/test_client_lifecycle_waves.py"
    for key, rule in MIXED_NODE_RULES.items():
        if key.startswith("test_client_lifecycle_waves.py::"):
            func = key.split("::", 1)[1]
            nodeid = f"{lifecycle_file}::{func}"
            entry = LedgerEntry(
                nodeid=nodeid,
                file=lifecycle_file,
                subsystem=str(rule.get("subsystem", "client_lifecycle")),
                failure_detected=str(rule.get("failure_detected", "")),
                oracle=str(rule.get("oracle", "")),
                overlapping_tests=list(rule.get("overlapping_tests", [])),
                runtime_cost=str(rule.get("runtime_cost", "async_behavioral")),
                decision=rule["decision"],  # type: ignore[arg-type]
                owner=str(rule.get("owner", "client_architecture")),
                rationale=str(rule.get("rationale", "")),
                replacement=None,
                review_condition=str(rule.get("review_condition", "permanent_lifecycle_contract")),
            )
            entries.append(asdict(entry))

    # Add workflow consolidation entry
    workflow_rule = MIXED_NODE_RULES["tests/integration/faults/test_web_workflows.py"]
    entries.append(
        asdict(
            LedgerEntry(
                nodeid="tests/integration/faults/test_web_workflows.py::test_web_workflow_fault_scenario",
                file="tests/integration/faults/test_web_workflows.py",
                subsystem=str(workflow_rule["subsystem"]),
                failure_detected=str(workflow_rule["failure_detected"]),
                oracle=str(workflow_rule["oracle"]),
                overlapping_tests=list(workflow_rule["overlapping_tests"]),
                runtime_cost=str(workflow_rule["runtime_cost"]),
                decision=workflow_rule["decision"],  # type: ignore[arg-type]
                owner=str(workflow_rule["owner"]),
                rationale=str(workflow_rule["rationale"]),
                replacement=str(workflow_rule["replacement"]),
                review_condition=str(workflow_rule["review_condition"]),
            )
        )
    )
    entries.append(
        asdict(
            LedgerEntry(
                nodeid="tests/integration/faults/test_web_workflows.py::test_web_workflow_scenarios_registered_in_aggregate_registry",
                file="tests/integration/faults/test_web_workflows.py",
                subsystem="fault_injection",
                failure_detected="Workflow scenarios missing from aggregate fault runner",
                oracle="Registry subset verification",
                overlapping_tests=["tests/integration/faults/test_web_faults.py"],
                runtime_cost="fast_static",
                decision="pr_routine",
                owner="transport_resilience",
                rationale="Fast static check ensuring all workflow scenarios remain present in the aggregate test_web_faults runner",
                replacement=None,
                review_condition="permanent_scenario_registry",
            )
        )
    )
    entries.append(
        asdict(
            LedgerEntry(
                nodeid="tests/integration/faults/test_web_workflows.py::test_web_workflow_dispatcher_routing",
                file="tests/integration/faults/test_web_workflows.py",
                subsystem="fault_injection",
                failure_detected="Dispatcher routing failure for workflow scenario cohort",
                oracle="Scenario runner plan and checks execution",
                overlapping_tests=["tests/integration/faults/test_web_faults.py"],
                runtime_cost="loopback_socket",
                decision="pr_routine",
                owner="transport_resilience",
                rationale="Focused routing test verifying scenario dispatcher dispatches workflow scenarios",
                replacement=None,
                review_condition="permanent_scenario_registry",
            )
        )
    )

    # Sort deterministically
    entries.sort(key=lambda e: e["nodeid"])

    # Compute summary statistics
    by_decision: dict[str, int] = {}
    for e in entries:
        d = e["decision"]
        by_decision[d] = by_decision.get(d, 0) + 1

    return {
        "version": "1.0.0",
        "schema": {
            "nodeid": "Full pytest node ID",
            "file": "Path relative to repository root",
            "subsystem": "Architecture or functional area",
            "failure_detected": "The concrete defect or invariant this test detects",
            "oracle": "The inspection mechanism (AST, runtime assertion, schema compare, etc.)",
            "overlapping_tests": "List of independent tests with overlapping coverage",
            "runtime_cost": "Execution profile (fast_static, ast_scan, async_behavioral, etc.)",
            "decision": "One of: pr_routine, pr_contract, extended_qualification, historical_manual, consolidate, deleted_after_replacement",
            "owner": "Subsystem owner responsible for this test",
            "rationale": "Engineering justification for the routing decision",
            "replacement": "Replacement node ID if consolidated or superseded",
            "review_condition": "Lifecycle or milestone when this decision expires or is reviewed",
        },
        "summary": {
            "total_entries": len(entries),
            "by_decision": by_decision,
        },
        "entries": entries,
    }


def main() -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    data = build_ledger()
    with LEDGER_PATH.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"Generated {LEDGER_PATH} with {data['summary']['total_entries']} entries.")
    print(f"Decisions breakdown: {data['summary']['by_decision']}")


if __name__ == "__main__":
    main()
