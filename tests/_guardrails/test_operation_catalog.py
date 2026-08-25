"""Fail-closed completeness guard for the semantic operation catalog."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from scripts._operation_catalog_ast import (
    INERT_P1_WEB_SITES,
    audit_inert_p1_backend_dataflow,
    audit_inert_p1_web_sites,
)
from scripts.audit_operation_catalog import (
    CLIENT_PUBLIC_MEMBER_DISPOSITIONS,
    LOCAL_PUBLIC_METHODS,
    audit_operation_catalog,
    build_operation_catalog,
    collect_public_client_members,
    collect_public_client_namespaces,
    collect_public_namespace_methods,
)
from scripts.audit_public_api_compat import CLIENT_NAMESPACE_ATTRIBUTES

from notebooklm._app.generate import execute_generation
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY
from notebooklm._operations import CallPolicy, Operation
from notebooklm.rpc import RPCMethod

pytestmark = pytest.mark.repo_lint


def test_operation_catalog_is_total_and_current() -> None:
    """Every enum member, native row, and namespace method has a disposition."""
    assert audit_operation_catalog() == []


def test_p1_inert_web_sites_are_exact_and_mutation_sensitive() -> None:
    """Only web handlers without a P2 facade delegation remain inert."""
    assert {
        "_web/transport.py:WebTransport.call",
    } == INERT_P1_WEB_SITES
    assert audit_inert_p1_web_sites() == []

    missing_one = INERT_P1_WEB_SITES - {"_web/transport.py:WebTransport.call"}
    assert audit_inert_p1_web_sites(frozenset(missing_one)) == [
        "inert P1 web site classification changed: "
        "missing=['_web/transport.py:WebTransport.call'], extra=[]"
    ]


def test_backend_dataflow_is_bounded_to_migrated_services() -> None:
    assert audit_inert_p1_backend_dataflow() == []

    root = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
    notebooks = (root / "_notebooks.py").read_text(encoding="utf-8")
    alias_mutation = notebooks + (
        "\nasync def _p1_alias_mutation(semantic):\n"
        "    return await semantic.invoke(None, None, deadline=None)\n"
    )
    errors = audit_inert_p1_backend_dataflow({"_notebooks.py": alias_mutation})
    assert len(errors) == 1
    assert errors[0].startswith("semantic backend invoke sites changed: ")
    assert "_notebooks.py" in errors[0]

    assembly = (root / "_client_composition.py").read_text(encoding="utf-8")
    escape_mutation = assembly + (
        "\ndef _p1_escape_mutation(client):\n    return NotebooksAPI(client._backend)\n"
    )
    errors = audit_inert_p1_backend_dataflow({"_client_composition.py": escape_mutation})
    assert len(errors) == 1
    assert errors[0].startswith("client._backend escapes the reviewed facade bindings at lines: ")

    for statement in (
        "from .._web import WebRpcBackend",
        "from . import _backend",
        "import notebooklm._backend",
    ):
        import_mutation = notebooks + f"\n{statement}\n"
        errors = audit_inert_p1_backend_dataflow({"_notebooks.py": import_mutation})
        assert len(errors) == 1
        assert errors[0].startswith("reviewed backend imports changed: ")


def test_catalog_projection_covers_the_live_authorities() -> None:
    catalog = build_operation_catalog()

    assert {row["key"] for row in catalog["operations"]} == {
        operation.value for operation in Operation
    }
    assert {row["key"] for row in catalog["native_bindings"]} == {
        f"{method.name}:{variant if variant is not None else '<default>'}"
        for method, variant, _entry in IDEMPOTENCY_REGISTRY.iter_entries()
    }
    assert set(catalog["public_methods"]) == set(collect_public_namespace_methods())
    assert len(Operation.__members__) == len(Operation) == len(catalog["operations"]) == 92
    assert {row["policy"] for row in catalog["operations"]} == {
        policy.value for policy in CallPolicy
    }


def test_catalog_and_public_compat_audit_agree_on_client_namespaces() -> None:
    assert set(collect_public_client_namespaces()) == set(CLIENT_NAMESPACE_ATTRIBUTES)


def test_catalog_names_every_inherited_and_local_only_public_helper() -> None:
    public_methods = build_operation_catalog()["public_methods"]

    assert public_methods["chat.set_bound_loop"]["disposition"] == "local_only"
    assert public_methods["chat.reset_after_open"]["disposition"] == "local_only"
    assert set(LOCAL_PUBLIC_METHODS) <= set(public_methods)


def test_catalog_names_every_root_client_member_disposition() -> None:
    client_members = build_operation_catalog()["client_members"]

    assert set(client_members) == set(collect_public_client_members())
    assert set(client_members) == set(CLIENT_PUBLIC_MEMBER_DISPOSITIONS)
    assert client_members["close"]["disposition"] == "lifecycle"
    assert client_members["refresh_auth"]["disposition"] == "auth"
    assert client_members["metrics_snapshot"]["disposition"] == "observability"
    assert client_members["rpc_call"]["disposition"] == "raw"


def test_rule_two_distinguishes_app_callers_from_execution_authorities() -> None:
    catalog = build_operation_catalog()
    rows = {row["key"]: row for row in catalog["operations"]}

    assert catalog["app_callers"]["sources.wait_until_ready"] == [
        "_app/source_wait.py:execute_source_wait"
    ]
    assert catalog["app_callers"]["sources.wait_all_until_ready"] == [
        "_app/source_wait.py:wait_all_sources"
    ]
    for operation in ("source.wait", "artifact.wait"):
        assert rows[operation]["known_divergence"] is None
        assert not any(
            authority["transport_kind"] == "orchestrator"
            for authority in rows[operation]["execution_authorities"]
        )


def test_generation_workflow_is_local_and_not_a_second_execution_authority() -> None:
    catalog = build_operation_catalog()
    rows = {row["key"]: row for row in catalog["operations"]}
    source = inspect.getsource(execute_generation)

    assert catalog["app_authority_source_evidence"] == {}
    assert "_run_generation_workflow" in source
    assert "_run_rate_limit_retry" not in source
    assert "generate_with_retry(" not in source
    assert "handle_generation_result(" not in source
    assert all(
        all(
            authority["transport_kind"] != "orchestrator"
            for authority in rows[key]["execution_authorities"]
        )
        for key in (
            "artifact.generate_audio",
            "artifact.generate_video",
            "artifact.generate_report",
            "artifact.generate_quiz",
            "artifact.generate_flashcards",
            "artifact.generate_infographic",
            "artifact.generate_slide_deck",
            "artifact.generate_data_table",
            "artifact.revise_slide",
        )
    )


def test_every_active_binding_honors_runtime_rpc_overrides() -> None:
    rows = build_operation_catalog()["native_bindings"]

    assert len(rows) == sum(1 for _entry in IDEMPOTENCY_REGISTRY.iter_entries())
    assert all(row["override_honored"] for row in rows)
    evidence = rows[0]["override_evidence"]
    assert all(row["override_evidence"] == evidence for row in rows)
    assert evidence["source_contract"] == (
        "_web/runtime.py:WebExecutionRuntime._execute_once -> "
        "_web_request_auth.py:build_web_rpc_request"
    )
    assert all(evidence["dataflow"].values())
    assert "test_runtime_rpc_override" in evidence["behavior_test"]


def test_polymorphic_native_surfaces_keep_all_reviewed_dispositions() -> None:
    rows = {row["key"]: row for row in build_operation_catalog()["native_bindings"]}

    # P9.2: the LABEL_MUTATE primitive shares every UPDATE_LABEL variant.
    assert rows["UPDATE_LABEL:add_sources"]["semantic_operations"] == [
        "label.mutate",
        "label.update",
    ]
    assert rows["UPDATE_LABEL:add_notebooks"]["semantic_operations"] == [
        "collection.update",
        "label.mutate",
    ]
    assert rows["LIST_LABELS:<default>"]["semantic_operations"] == [
        "collection.create",
        "collection.get",
        "collection.list",
        "collection.notebooks",
        "collection.update",
        "label.create",
        "label.get",
        "label.list",
        "label.sources",
        "label.update",
    ]
    assert rows["SHARE_ARTIFACT:<default>"]["semantic_operations"] == [
        "sharing.legacy_share_artifact"
    ]


def test_plan_named_greenfield_omissions_remain_covered() -> None:
    coverage = build_operation_catalog()["greenfield_omission_coverage"]

    assert {
        "source listing",
        "settings and account limits",
        "individual sharing",
        "prompt suggestions",
        "report suggestions",
        "generic artifact actions",
        "artifact retry",
        "mind maps",
        "data tables",
        "exports and download formats",
    } == set(coverage)


def test_committed_live_registry_omissions_are_non_vacuous_and_disposed() -> None:
    omissions = build_operation_catalog()["product_omissions"]

    assert omissions["source"] == "scripts/operation_catalog_rpc_registry.json"
    assert omissions["capture_counts"]["unmapped"] == len(omissions["unmapped_live_rpcs"])
    assert omissions["unmapped_live_rpcs"]
    assert all(row["disposition"] for row in omissions["unmapped_live_rpcs"])
    assert any(row["family"] == "current" for row in omissions["unmapped_live_rpcs"])


def test_no_native_idempotency_row_is_left_unclassified() -> None:
    assert all(
        entry.policy.value != "unclassified"
        for _method, _variant, entry in IDEMPOTENCY_REGISTRY.iter_entries()
    )
    assert {row["rpc_method"] for row in build_operation_catalog()["native_bindings"]} == {
        method.name for method in RPCMethod
    }
