#!/usr/bin/env python3
"""Derive and audit the P0 semantic operation catalog.

The reviewed catalog fields live in focused private modules: semantic specs,
exact transport authorities/recency contracts, AST inventory, and external
evidence.  This module remains the one public build/audit authority and CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from notebooklm._idempotency import IDEMPOTENCY_REGISTRY
from notebooklm._operations import CallPolicy, Operation
from notebooklm._web.policy import (
    WEB_CALL_POLICY_BINDINGS,
    audit_web_call_policy_bindings,
    web_call_policy_report,
)
from notebooklm._web.registry import WEB_OPERATION_REGISTRY

if __package__:
    from ._operation_catalog_ast import (
        _operation_authorities,
        audit_operation_authorities,
        audit_public_namespace_contract,
        audit_recency_contracts,
        audit_row_bindings,
        collect_app_authority_source_evidence,
        collect_app_callers,
        collect_binding_rows,
        collect_native_execution_sites,
        collect_public_client_members,
        collect_public_client_namespaces,
        collect_public_namespace_methods,
        collect_rpc_references,
        collect_unresolved_app_dispatches,
        collect_unresolved_rpc_dispatches,
        derive_row_authorities,
    )
    from ._operation_catalog_authorities import RECENCY_CONTRACTS
    from ._operation_catalog_evidence import (
        _normalize_registry_omissions,
        _override_honored,
        audit_live_registry_against_evidence,
        audit_rpc_registry_evidence,
        collect_golden_evidence,
        collect_variant_golden_evidence,
        load_rpc_registry_evidence,
    )
    from ._operation_catalog_specs import (
        APP_ORCHESTRATOR_DISPOSITIONS,
        APP_PRIVATE_FACADE_DISPOSITIONS,
        CLIENT_PUBLIC_MEMBER_DISPOSITIONS,
        DIVERGENCE_KINDS,
        GREENFIELD_OMISSION_COVERAGE,
        LOCAL_PUBLIC_METHODS,
        NATIVE_BINDING_DISPOSITIONS,
        OPERATION_SPECS,
        NativeKey,
        OperationSpec,
        native_key_text,
    )
else:  # pragma: no cover - direct script execution
    from _operation_catalog_ast import (
        _operation_authorities,
        audit_operation_authorities,
        audit_public_namespace_contract,
        audit_recency_contracts,
        audit_row_bindings,
        collect_app_authority_source_evidence,
        collect_app_callers,
        collect_binding_rows,
        collect_native_execution_sites,
        collect_public_client_members,
        collect_public_client_namespaces,
        collect_public_namespace_methods,
        collect_rpc_references,
        collect_unresolved_app_dispatches,
        collect_unresolved_rpc_dispatches,
        derive_row_authorities,
    )
    from _operation_catalog_authorities import RECENCY_CONTRACTS
    from _operation_catalog_evidence import (
        _normalize_registry_omissions,
        _override_honored,
        audit_live_registry_against_evidence,
        audit_rpc_registry_evidence,
        collect_golden_evidence,
        collect_variant_golden_evidence,
        load_rpc_registry_evidence,
    )
    from _operation_catalog_specs import (
        APP_ORCHESTRATOR_DISPOSITIONS,
        APP_PRIVATE_FACADE_DISPOSITIONS,
        CLIENT_PUBLIC_MEMBER_DISPOSITIONS,
        DIVERGENCE_KINDS,
        GREENFIELD_OMISSION_COVERAGE,
        LOCAL_PUBLIC_METHODS,
        NATIVE_BINDING_DISPOSITIONS,
        OPERATION_SPECS,
        NativeKey,
        OperationSpec,
        native_key_text,
    )

SCHEMA_VERSION = 2
_EXPECTED_OPERATION_COUNT = 95
_native_key_text = native_key_text

__all__ = [
    "CLIENT_PUBLIC_MEMBER_DISPOSITIONS",
    "IDEMPOTENCY_REGISTRY",
    "LOCAL_PUBLIC_METHODS",
    "OPERATION_SPECS",
    "RECENCY_CONTRACTS",
    "audit_live_registry_against_evidence",
    "audit_operation_catalog",
    "audit_row_bindings",
    "audit_rpc_registry_evidence",
    "build_operation_catalog",
    "collect_app_authority_source_evidence",
    "collect_app_callers",
    "collect_binding_rows",
    "collect_golden_evidence",
    "collect_native_execution_sites",
    "collect_public_client_namespaces",
    "collect_public_client_members",
    "collect_public_namespace_methods",
    "collect_rpc_references",
    "collect_unresolved_app_dispatches",
    "collect_unresolved_rpc_dispatches",
    "derive_row_authorities",
    "load_rpc_registry_evidence",
    "main",
]


def audit_operation_catalog() -> list[str]:
    """Return catalog completeness/staleness errors; an empty list is green."""
    errors: list[str] = []
    if len(Operation.__members__) != len(Operation):
        errors.append("Operation aliases are forbidden")
    if len(Operation) != _EXPECTED_OPERATION_COUNT:
        errors.append(
            f"Operation count changed: expected={_EXPECTED_OPERATION_COUNT}, actual={len(Operation)}"
        )
    specs_by_operation: dict[Operation, OperationSpec] = {}
    for spec in OPERATION_SPECS:
        if spec.operation in specs_by_operation:
            errors.append(f"duplicate operation spec: {spec.operation.value}")
        specs_by_operation[spec.operation] = spec

    described_divergences = {
        spec.operation for spec in OPERATION_SPECS if spec.known_divergence is not None
    }
    if described_divergences != set(DIVERGENCE_KINDS):
        errors.append(
            "known divergence descriptions/kinds disagree: "
            f"described={sorted(item.value for item in described_divergences)}, "
            f"kinds={sorted(item.value for item in DIVERGENCE_KINDS)}"
        )

    missing_operations = sorted(
        operation.value for operation in set(Operation) - specs_by_operation.keys()
    )
    stale_operations = sorted(
        operation.value for operation in specs_by_operation.keys() - set(Operation)
    )
    if missing_operations:
        errors.append(f"Operation members missing specs: {missing_operations}")
    if stale_operations:
        errors.append(f"specs for unknown Operation members: {stale_operations}")

    active_definitions = {
        operation: binding.definition
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.is_supported and binding.definition is not None
    }
    errors.extend(audit_web_call_policy_bindings(active_definitions))
    for operation, binding in WEB_CALL_POLICY_BINDINGS.items():
        spec = specs_by_operation.get(operation)
        if spec is None:
            continue
        if spec.policy is not binding.policy:
            errors.append(
                f"{operation.value}: operation catalog policy is {spec.policy.value}, "
                f"active web binding is {binding.policy.value}"
            )
        active_native = {(item.method, item.variant) for item in binding.native_bindings}
        missing_from_catalog = active_native - set(spec.native_bindings)
        if missing_from_catalog:
            errors.append(
                f"{operation.value}: active web native bindings absent from catalog: "
                f"{sorted(_native_key_text(item) for item in missing_from_catalog)}"
            )
        if binding.known_divergence != spec.known_divergence:
            errors.append(
                f"{operation.value}: active web/catalog known-divergence descriptions disagree"
            )

    used_policies = {spec.policy for spec in OPERATION_SPECS}
    if unused_policies := sorted(policy.value for policy in set(CallPolicy) - used_policies):
        errors.append(f"CallPolicy members unused by operation specs: {unused_policies}")

    native_rows = {
        (method, variant) for method, variant, _entry in IDEMPOTENCY_REGISTRY.iter_entries()
    }
    mapped_native: set[NativeKey] = set()
    for spec in OPERATION_SPECS:
        mapped_native.update(spec.native_bindings)
    conflicting_native = mapped_native & set(NATIVE_BINDING_DISPOSITIONS)
    mapped_native.update(NATIVE_BINDING_DISPOSITIONS)
    missing_native = sorted(_native_key_text(row) for row in native_rows - mapped_native)
    stale_native = sorted(_native_key_text(row) for row in mapped_native - native_rows)
    if missing_native:
        errors.append(f"active RPC method/variant rows without disposition: {missing_native}")
    if stale_native:
        errors.append(f"catalog bindings absent from idempotency registry: {stale_native}")
    if conflicting_native:
        errors.append(
            "native rows have both semantic and non-semantic dispositions: "
            f"{sorted(_native_key_text(row) for row in conflicting_native)}"
        )

    discovered_public = set(collect_public_namespace_methods())
    semantic_public: set[str] = set()
    for spec in OPERATION_SPECS:
        semantic_public.update(spec.public_methods)
    mapped_public = set(LOCAL_PUBLIC_METHODS) | semantic_public
    missing_public = sorted(discovered_public - mapped_public)
    stale_public = sorted(mapped_public - discovered_public)
    conflicting_public = sorted(set(LOCAL_PUBLIC_METHODS) & semantic_public)
    if missing_public:
        errors.append(f"public namespace methods without disposition: {missing_public}")
    if stale_public:
        errors.append(f"catalog public mappings no longer exist: {stale_public}")
    if conflicting_public:
        errors.append(
            f"public methods have both semantic and local dispositions: {conflicting_public}"
        )

    discovered_client = set(collect_public_client_members())
    disposed_client = set(CLIENT_PUBLIC_MEMBER_DISPOSITIONS)
    if missing_client := sorted(discovered_client - disposed_client):
        errors.append(f"public NotebookLMClient members without disposition: {missing_client}")
    if stale_client := sorted(disposed_client - discovered_client):
        errors.append(f"root-client dispositions no longer exist: {stale_client}")

    app_callers = collect_app_callers({method.split(".", 1)[0] for method in discovered_public})
    private_app_calls = set(APP_PRIVATE_FACADE_DISPOSITIONS)
    unknown_app_calls = sorted(set(app_callers) - discovered_public - private_app_calls)
    if unknown_app_calls:
        errors.append(
            f"_app calls namespace methods absent from public inventory: {unknown_app_calls}"
        )
    stale_private_app_calls = sorted(private_app_calls - set(app_callers))
    if stale_private_app_calls:
        errors.append(
            f"reviewed private facade app calls no longer exist: {stale_private_app_calls}"
        )

    for method, variant, entry in IDEMPOTENCY_REGISTRY.iter_entries():
        if entry.policy.value == "unclassified":
            errors.append(
                f"native idempotency row remains unclassified: "
                f"{_native_key_text((method, variant))}"
            )

    override_honored, _evidence = _override_honored()
    if not override_honored:
        errors.append("WebExecutionRuntime must call resolve_rpc_id exactly once per binding path")

    for feature, operations in GREENFIELD_OMISSION_COVERAGE.items():
        absent = [
            operation.value for operation in operations if operation not in specs_by_operation
        ]
        if absent:
            errors.append(f"greenfield omission {feature!r} lacks catalog operations: {absent}")
    errors.extend(audit_public_namespace_contract())
    errors.extend(audit_operation_authorities())
    errors.extend(audit_recency_contracts())
    errors.extend(audit_rpc_registry_evidence(load_rpc_registry_evidence()))
    return errors


def build_operation_catalog(
    rpc_registry_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the deterministic ADR-0022 operation-catalog projection."""
    errors = audit_operation_catalog()
    if errors:
        raise ValueError("operation catalog is incomplete:\n- " + "\n- ".join(errors))
    if rpc_registry_snapshot is None:
        rpc_registry_snapshot = load_rpc_registry_evidence()

    public_origins = collect_public_namespace_methods()
    client_members = collect_public_client_members()
    app_callers = collect_app_callers({name.split(".", 1)[0] for name in public_origins})
    app_authority_source_evidence = collect_app_authority_source_evidence()
    rpc_references = collect_rpc_references()
    variant_golden = collect_variant_golden_evidence()
    override_honored, override_evidence = _override_honored()
    native_execution_sites = collect_native_execution_sites()

    specs_by_native: dict[NativeKey, list[OperationSpec]] = defaultdict(list)
    specs_by_public: dict[str, list[OperationSpec]] = defaultdict(list)
    for spec in OPERATION_SPECS:
        for binding in spec.native_bindings:
            specs_by_native[binding].append(spec)
        for method in spec.public_methods:
            specs_by_public[method].append(spec)
    shared_bindings = {binding for binding, specs in specs_by_native.items() if len(specs) > 1}

    operation_rows: list[dict[str, Any]] = []
    for spec in sorted(OPERATION_SPECS, key=lambda item: item.operation.value):
        native_methods = {method for method, _variant in spec.native_bindings}
        authorities = _operation_authorities(spec, native_execution_sites, shared_bindings)
        decoders = {
            site
            for method in native_methods
            for role in ("decoders", "projectors")
            for site in rpc_references[method].get(role, [])
        }
        callers = {
            caller
            for public_method in spec.public_methods
            for caller in app_callers.get(public_method, [])
        }
        operation_rows.append(
            {
                "key": spec.operation.value,
                "owner": spec.owner,
                "policy": spec.policy.value,
                "route_context": spec.route_context,
                "disposition": spec.disposition.value,
                "public_methods": sorted(spec.public_methods),
                "app_callers": sorted(callers),
                "native_bindings": sorted(
                    _native_key_text(binding) for binding in spec.native_bindings
                ),
                "web_paths": sorted(spec.web_paths),
                "response_decoders_projectors": sorted(decoders),
                "execution_authorities": authorities,
                "composite_behavior": spec.composite_behavior,
                "known_divergence": spec.known_divergence,
                "known_divergence_kind": DIVERGENCE_KINDS.get(spec.operation),
                "recency_effect": spec.recency_effect,
                "recency_contract": [
                    {
                        "public_methods": sorted(rule.public_methods),
                        "minimum_calls": rule.minimum_calls,
                        "maximum_calls": rule.maximum_calls,
                        "unit": rule.unit,
                        "condition": rule.condition,
                        "authority_sites": sorted(rule.authority_sites),
                    }
                    for rule in RECENCY_CONTRACTS.get(spec.operation, ())
                ],
            }
        )

    native_rows: list[dict[str, Any]] = []
    entries = sorted(
        IDEMPOTENCY_REGISTRY.iter_entries(),
        key=lambda row: (row[0].name, row[1] is not None, row[1] or ""),
    )
    for method, variant, entry in entries:
        specs = specs_by_native.get((method, variant), [])
        evidence_disposition, evidence, evidence_scope = variant_golden[(method, variant)]
        native_rows.append(
            {
                "key": _native_key_text((method, variant)),
                "rpc_method": method.name,
                "rpc_id": method.value,
                "variant": variant,
                "idempotency_policy": entry.policy.value,
                "idempotency_notes": entry.notes,
                "semantic_operations": sorted(spec.operation.value for spec in specs),
                "disposition": (
                    "semantic" if specs else "compatibility_default_no_active_callsite"
                ),
                "disposition_reason": NATIVE_BINDING_DISPOSITIONS.get((method, variant)),
                "owners": sorted({spec.owner for spec in specs}),
                "route_contexts": sorted({spec.route_context for spec in specs}),
                "execution_authorities": native_execution_sites.get((method, variant), []),
                "response_decoders": rpc_references[method].get("decoders", []),
                "response_projectors": rpc_references[method].get("projectors", []),
                "golden_disposition": evidence_disposition,
                "golden_evidence": evidence,
                "golden_scope": evidence_scope,
                "override_honored": override_honored,
                "override_evidence": override_evidence,
            }
        )

    public_rows: dict[str, dict[str, Any]] = {}
    for method, origin in public_origins.items():
        specs = specs_by_public.get(method, [])
        if specs:
            public_rows[method] = {
                "disposition": "semantic",
                "operations": sorted(spec.operation.value for spec in specs),
                "declared_by": origin,
                "app_callers": app_callers.get(method, []),
            }
        else:
            public_rows[method] = {
                "disposition": "local_only",
                "operations": [],
                "reason": LOCAL_PUBLIC_METHODS[method],
                "declared_by": origin,
                "app_callers": app_callers.get(method, []),
            }

    divergences = [
        {
            "operation": spec.operation.value,
            "disposition": "known_divergence",
            "kind": DIVERGENCE_KINDS[spec.operation],
            "detail": spec.known_divergence,
        }
        for spec in sorted(OPERATION_SPECS, key=lambda item: item.operation.value)
        if spec.known_divergence is not None
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "active_web_policy_bindings": web_call_policy_report(),
        "operations": operation_rows,
        "native_bindings": native_rows,
        "public_methods": public_rows,
        "client_members": {
            name: {
                **details,
                "disposition": CLIENT_PUBLIC_MEMBER_DISPOSITIONS[name][0],
                "reason": CLIENT_PUBLIC_MEMBER_DISPOSITIONS[name][1],
            }
            for name, details in client_members.items()
        },
        "app_callers": app_callers,
        "app_authority_source_evidence": app_authority_source_evidence,
        "app_orchestrator_dispositions": dict(sorted(APP_ORCHESTRATOR_DISPOSITIONS.items())),
        "app_private_facade_dispositions": dict(sorted(APP_PRIVATE_FACADE_DISPOSITIONS.items())),
        "greenfield_omission_coverage": {
            feature: sorted(operation.value for operation in operations)
            for feature, operations in sorted(GREENFIELD_OMISSION_COVERAGE.items())
        },
        "known_divergences": divergences,
        "product_omissions": {
            "source": "scripts/operation_catalog_rpc_registry.json",
            "refresh_command": "uv run python scripts/capture_rpc_registry.py --json",
            "freshness_check": (
                "uv run python scripts/audit_operation_catalog.py "
                "--rpc-registry-json /tmp/rpc-registry.json"
            ),
            "captured_on": rpc_registry_snapshot.get("captured_on"),
            "capture_counts": rpc_registry_snapshot.get("counts", {}),
            "unmapped_live_rpcs": _normalize_registry_omissions(rpc_registry_snapshot),
            "excluded_families": {
                "enterprise": "NotebookLM Enterprise/Agentspace, not consumer-callable",
                "other": "unclassified live service family; investigate before adding",
            },
        },
        "raw_rpc_escape_hatch": {
            "member": "NotebookLMClient.rpc_call",
            "disposition": "explicitly excluded legacy web-only escape hatch",
        },
    }


def _load_snapshot(path: Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("rpc registry snapshot must be a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rpc-registry-json",
        type=Path,
        help="optional output from capture_rpc_registry.py --json for the omissions projection",
    )
    parser.add_argument("--json", action="store_true", help="print the derived catalog as JSON")
    args = parser.parse_args(argv)

    snapshot = _load_snapshot(args.rpc_registry_json)
    errors = audit_operation_catalog()
    if snapshot is not None:
        errors.extend(audit_live_registry_against_evidence(snapshot))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(build_operation_catalog(snapshot), indent=2))
    else:
        catalog = build_operation_catalog(snapshot)
        print(
            "operation catalog: "
            f"{len(catalog['operations'])} semantic operations, "
            f"{len(catalog['native_bindings'])} native rows, "
            f"{len(catalog['public_methods'])} public namespace methods, "
            f"{len(catalog['known_divergences'])} known divergences"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
