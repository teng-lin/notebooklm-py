"""Focused tests for operation-catalog derivation and fail-closed behavior."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
from typing import Any

import httpx
import pytest

from notebooklm._app.generate import execute_generation
from notebooklm._idempotency import IdempotencyPolicy, IdempotencyRegistry
from notebooklm._operations import CallPolicy, Operation, OperationDef, OperationTier
from notebooklm.rpc import RPCMethod
from scripts import _operation_catalog_ast as catalog_ast
from scripts import _operation_catalog_authorities as catalog_authorities
from scripts import audit_operation_catalog as catalog
from tests.unit._rpc_executor_support import _executor, _Owner


def test_operation_definition_is_inert_frozen_slotted_vocabulary() -> None:
    definition = OperationDef(Operation.NOTEBOOK_LIST, CallPolicy.READ, str, int)
    leaf = OperationDef(
        Operation.LABEL_MUTATE, CallPolicy.MUTATION, str, int, tier=OperationTier.PRIMITIVE
    )

    assert definition.key is Operation.NOTEBOOK_LIST
    assert definition.input_type is str
    assert definition.output_type is int
    # Only a P9.2 decomposition leaf declares a tier; everything else is product.
    assert {tier.value for tier in OperationTier} == {"product", "primitive"}
    assert definition.tier is OperationTier.PRODUCT
    assert leaf.tier is OperationTier.PRIMITIVE
    assert not hasattr(definition, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        definition.policy = CallPolicy.MUTATION  # type: ignore[misc]


@pytest.mark.repo_lint
def test_operation_and_call_policy_vocabularies_are_total_non_vacuous_and_alias_free() -> None:
    rows = catalog.build_operation_catalog()["operations"]

    assert len(Operation.__members__) == len(Operation) == len(catalog.OPERATION_SPECS) == 98
    assert {row["policy"] for row in rows} == {policy.value for policy in CallPolicy}
    assert next(row for row in rows if row["key"] == "chat.ask")["policy"] == "stream"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "variant"),
    [(method, variant) for method, variant, _entry in catalog.IDEMPOTENCY_REGISTRY.iter_entries()],
    ids=lambda value: value.name if isinstance(value, RPCMethod) else str(value or "default"),
)
async def test_runtime_rpc_override_reaches_url_body_and_decoder_for_every_binding(
    monkeypatch: pytest.MonkeyPatch,
    method: RPCMethod,
    variant: str | None,
) -> None:
    override = f"Override{method.name}"
    monkeypatch.setenv("NOTEBOOKLM_RPC_OVERRIDES", json.dumps({method.name: override}))
    owner = _Owner()
    decoded_ids: list[str] = []

    def decode(
        _raw: str,
        rpc_id: str,
        *,
        allow_null: bool = False,
        raise_on_null_status: bool = False,
    ) -> dict[str, Any]:
        decoded_ids.append(rpc_id)
        return {"rpc_id": rpc_id}

    result = await _executor(owner, decode_response=decode)._execute_once(
        method,
        [],
        "/",
        False,
        False,
        operation_variant=variant,
    )

    assert result == {"rpc_id": override}
    url = httpx.URL(owner.perform_calls[0]["url"])
    body = httpx.QueryParams(owner.perform_calls[0]["body"])
    assert url.params["rpcids"] == override
    assert f'"{override}"' in body["f.req"]
    assert decoded_ids == [override]


@pytest.mark.repo_lint
def test_audit_bites_when_a_new_native_variant_has_no_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = IdempotencyRegistry()
    for method, variant, entry in catalog.IDEMPOTENCY_REGISTRY.iter_entries():
        registry.register(method, entry.policy, variant=variant, notes=entry.notes)
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        variant="future_variant",
        notes="synthetic drift",
    )
    monkeypatch.setattr(catalog, "IDEMPOTENCY_REGISTRY", registry)

    errors = catalog.audit_operation_catalog()

    assert any("LIST_NOTEBOOKS:future_variant" in error for error in errors)


@pytest.mark.repo_lint
def test_audit_bites_when_a_new_public_namespace_method_has_no_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered = catalog.collect_public_namespace_methods()
    discovered["sources.future_method"] = "notebooklm._sources.SourcesAPI"
    monkeypatch.setattr(catalog, "collect_public_namespace_methods", lambda: discovered)

    errors = catalog.audit_operation_catalog()

    assert any("sources.future_method" in error for error in errors)


@pytest.mark.repo_lint
def test_audit_bites_when_a_new_root_client_member_has_no_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovered = catalog.collect_public_client_members()
    discovered["future_lifecycle_method"] = {
        "declared_by": "notebooklm.client.NotebookLMClient",
        "kind": "method",
    }
    monkeypatch.setattr(catalog, "collect_public_client_members", lambda: discovered)

    errors = catalog.audit_operation_catalog()

    assert any("future_lifecycle_method" in error for error in errors)


@pytest.mark.repo_lint
def test_audit_bites_when_an_operation_spec_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        catalog,
        "OPERATION_SPECS",
        tuple(
            spec
            for spec in catalog.OPERATION_SPECS
            if spec.operation is not Operation.NOTEBOOK_LIST
        ),
    )

    errors = catalog.audit_operation_catalog()

    assert any("notebook.list" in error for error in errors)


@pytest.mark.repo_lint
def test_app_ast_walk_records_transport_neutral_orchestrators() -> None:
    callers = catalog.collect_app_callers()

    assert "_app/source_wait.py:execute_source_wait" in callers["sources.wait_until_ready"]
    assert "_app/source_wait.py:wait_all_sources" in callers["sources.wait_all_until_ready"]
    assert "_app/collections.py:execute_collection_list" in callers["collections.list"]
    assert "_app/download.py:_fetch_artifacts_once" in callers["artifacts.list"]
    assert "_app/generate.py:execute_generation" in callers["artifacts.generate_audio"]
    assert "_app/download.py:_bind_download_fn" in callers["artifacts.download_audio"]


def test_namespace_discovery_is_name_agnostic_and_matches_compat_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StudioCatalog:
        def list_items(self) -> None:
            return None

    StudioCatalog.__module__ = "notebooklm._future"
    StudioCatalog.__qualname__ = "StudioCatalog"
    discovered = set(catalog.collect_public_client_namespaces())
    assert discovered == set(catalog_ast.CLIENT_NAMESPACE_ATTRIBUTES)

    monkeypatch.setattr(
        catalog_ast,
        "collect_public_client_namespaces",
        lambda: {"studio": StudioCatalog},
    )
    assert catalog.collect_public_namespace_methods() == {
        "studio.list_items": "notebooklm._future.StudioCatalog"
    }
    assert catalog_ast.audit_public_namespace_contract() == [
        "public client namespaces disagree with CLIENT_NAMESPACE_ATTRIBUTES: "
        "missing=['studio'], stale=['artifacts', 'chat', 'collections', 'labels', 'mind_maps', "
        "'notebooks', 'notes', 'research', 'settings', 'sharing', 'sources']"
    ]


@pytest.mark.repo_lint
def test_call_policy_audit_rejects_an_unused_vocabulary_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        catalog,
        "OPERATION_SPECS",
        tuple(
            # ``stream`` is carried by two specs since P10 R2.2 — the ``chat.ask``
            # workflow and its ``chat.stream_answer`` leaf — so draining the arm
            # means draining every spec that holds it, not one named operation.
            dataclasses.replace(spec, policy=CallPolicy.STATEFUL_START)
            if spec.policy is CallPolicy.STREAM
            else spec
            for spec in catalog.OPERATION_SPECS
        ),
    )

    assert (
        "CallPolicy members unused by operation specs: ['stream']"
        in catalog.audit_operation_catalog()
    )


@pytest.mark.repo_lint
def test_audit_bites_on_unresolved_dynamic_or_non_rpc_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        catalog_ast,
        "collect_unresolved_app_dispatches",
        lambda: ["_app/future.py:execute"],
    )
    assert any("unresolved dynamic _app" in error for error in catalog.audit_operation_catalog())

    monkeypatch.undo()
    chat_spec = next(
        spec for spec in catalog_ast.OPERATION_SPECS if spec.operation is Operation.CHAT_ASK
    )
    monkeypatch.setattr(
        catalog_ast,
        "OPERATION_SPECS",
        tuple(
            dataclasses.replace(spec, web_paths=("future_stream",)) if spec is chat_spec else spec
            for spec in catalog_ast.OPERATION_SPECS
        ),
    )
    assert any(
        "chat.ask non-RPC bindings disagree" in error for error in catalog.audit_operation_catalog()
    )


@pytest.mark.repo_lint
def test_rpc_ast_walk_distinguishes_calls_from_decoder_references() -> None:
    references = catalog.collect_rpc_references()
    sites = catalog.collect_native_execution_sites()

    assert sites[(RPCMethod.GET_NOTEBOOK, None)] == [
        "_notebooks.py:NotebooksAPI.get_raw",
        "_web/bindings/chat.py:CHAT_CONFIGURE",
        "_web/bindings/mind_maps.py:ARTIFACT_GENERATE_MIND_MAP",
        "_web/bindings/mind_maps.py:MIND_MAP_GENERATE_INTERACTIVE",
        "_web/bindings/mind_maps.py:MIND_MAP_GENERATE_NOTE",
        "_web/bindings/notebooks.py:NOTEBOOK_GET",
        "_web/bindings/settings.py:NOTEBOOK_SUGGEST_PROMPTS",
        "_web/bindings/sources.py:SOURCE_ADD_FILE",
        "_web/bindings/sources.py:SOURCE_GET",
        "_web/bindings/sources.py:SOURCE_LIST",
        "_web/bindings/sources.py:SOURCE_WAIT",
        "_web/bindings/studio.py:ARTIFACT_GENERATE_AUDIO",
        "_web/bindings/studio.py:ARTIFACT_GENERATE_DATA_TABLE",
        "_web/bindings/studio.py:ARTIFACT_GENERATE_FLASHCARDS",
        "_web/bindings/studio.py:ARTIFACT_GENERATE_INFOGRAPHIC",
        "_web/bindings/studio.py:ARTIFACT_GENERATE_QUIZ",
        "_web/bindings/studio.py:ARTIFACT_GENERATE_REPORT",
        "_web/bindings/studio.py:ARTIFACT_GENERATE_SLIDE_DECK",
        "_web/bindings/studio.py:ARTIFACT_GENERATE_VIDEO",
    ]
    assert any("_row_adapters/" in site for site in references[RPCMethod.GET_NOTEBOOK]["decoders"])
    assert references[RPCMethod.SUGGEST_PROMPTS]["decoders"] == [
        "_web/codec/suggestions.py:decode_prompt_suggestions"
    ]
    assert references[RPCMethod.SET_USER_SETTINGS]["decoders"] == [
        "_web/codec/settings.py:decode_set_output_language"
    ]
    assert all("_row_adapters/" not in site for site in sites[(RPCMethod.GET_NOTEBOOK, None)])


def test_rpc_ast_walk_resolves_keyword_method_and_local_literal_variant() -> None:
    tree = ast.parse(
        """
async def invoke(rpc):
    method = RPCMethod.ADD_SOURCE
    variant = "drive"
    await rpc.rpc_call(method=method, params=[], operation_variant=variant)
"""
    )
    collector = catalog_ast._ReferenceCollector("synthetic.py", set())
    collector.visit(tree)

    assert collector.rpc_calls == [("ADD_SOURCE", "drive", "invoke")]
    assert collector.unresolved_rpc_calls == []


def test_rpc_ast_walk_marks_dynamic_method_and_variant_unresolved() -> None:
    tree = ast.parse(
        """
async def invoke(rpc):
    await rpc.rpc_call(method=choose_method(), params=[], operation_variant=choose_variant())
"""
    )
    collector = catalog_ast._ReferenceCollector("synthetic.py", set())
    collector.visit(tree)

    assert collector.rpc_calls == []
    assert collector.unresolved_rpc_calls == [("invoke", "method")]


@pytest.mark.repo_lint
def test_golden_evidence_is_read_from_the_existing_guardrail() -> None:
    covered, exempt = catalog.collect_golden_evidence()

    assert covered[RPCMethod.GET_NOTEBOOK]
    assert covered[RPCMethod.ADD_SOURCE]
    assert "returns None" in exempt[RPCMethod.DELETE_NOTEBOOK]


@pytest.mark.repo_lint
def test_golden_evidence_is_variant_specific() -> None:
    rows = {row["key"]: row for row in catalog.build_operation_catalog()["native_bindings"]}

    assert rows["ADD_SOURCE:url"]["golden_disposition"] == "golden_covered"
    assert rows["ADD_SOURCE:text"]["golden_disposition"] == "golden_covered"
    assert rows["ADD_SOURCE:drive"]["golden_disposition"] == "not_recorded"
    assert rows["CREATE_NOTE:plain"]["golden_disposition"] == "golden_covered"
    assert rows["CREATE_NOTE:saved_from_chat"]["golden_disposition"] == "not_recorded"
    assert rows["ADD_SOURCE:drive"]["golden_scope"] == "variant"


@pytest.mark.repo_lint
def test_capture_rpc_registry_snapshot_supplies_product_omissions() -> None:
    projection = catalog.build_operation_catalog(
        {"unmapped": {"NewOne": {"method": "/LabsTailwindService.NewThing", "family": "current"}}}
    )

    assert projection["product_omissions"]["unmapped_live_rpcs"] == [
        {
            "rpc_id": "NewOne",
            "method": "/LabsTailwindService.NewThing",
            "family": "current",
            "disposition": "unsupported_current_product_surface",
        }
    ]


@pytest.mark.repo_lint
def test_committed_rpc_evidence_audit_rejects_a_vacuous_snapshot() -> None:
    confirmed = {
        method.value: {"name": method.name, "method": f"/Synthetic.{method.name}"}
        for method in RPCMethod
    }
    snapshot = {
        "schema_version": 1,
        "captured_on": "2026-08-23",
        "source_command": "uv run python scripts/capture_rpc_registry.py --json",
        "capture_kind": "public_web_bundle",
        "scrubbed": True,
        "registry_total": len(RPCMethod),
        "counts": {
            "confirmed": len(RPCMethod),
            "absent": 0,
            "present_unparsed": 0,
            "unmapped": 0,
        },
        "confirmed": confirmed,
        "absent": {},
        "present_unparsed": {},
        "unmapped": {},
    }

    assert catalog.audit_rpc_registry_evidence(snapshot) == [
        "RPC registry omissions evidence must not be empty/vacuous",
        "RPC registry evidence must inventory current-family product omissions",
    ]


@pytest.mark.repo_lint
def test_committed_rpc_evidence_rejects_unscrubbed_schema_and_rows() -> None:
    snapshot = json.loads(json.dumps(catalog.load_rpc_registry_evidence()))
    snapshot["enums"] = {"raw": []}
    assert any(
        "top-level schema differs" in error
        for error in catalog.audit_rpc_registry_evidence(snapshot)
    )

    snapshot = json.loads(json.dumps(catalog.load_rpc_registry_evidence()))
    rpc_id = next(iter(snapshot["unmapped"]))
    snapshot["unmapped"][rpc_id]["raw_capture"] = "must not be committed"
    assert any(
        f"unmapped row {rpc_id!r} has unexpected fields" in error
        for error in catalog.audit_rpc_registry_evidence(snapshot)
    )

    snapshot = json.loads(json.dumps(catalog.load_rpc_registry_evidence()))
    rpc_id = next(iter(snapshot["unmapped"]))
    snapshot["unmapped"][rpc_id]["method"] = "truncated"
    assert any(
        f"unmapped row {rpc_id!r} lacks a full /Service.Method path" in error
        for error in catalog.audit_rpc_registry_evidence(snapshot)
    )


@pytest.mark.repo_lint
def test_fresh_capture_check_bites_when_live_omissions_drift() -> None:
    snapshot = _fresh_registry_snapshot()
    unmapped = dict(snapshot["unmapped"])
    unmapped["NewOne"] = {
        "method": "/LabsTailwindOrchestrationService.NewThing",
        "family": "current",
    }
    snapshot["unmapped"] = unmapped
    snapshot["counts"]["unmapped"] = len(unmapped)

    errors = catalog.audit_live_registry_against_evidence(snapshot)

    assert len(errors) == 1
    assert "added=['NewOne']" in errors[0]


def _fresh_registry_snapshot() -> dict[str, Any]:
    committed = catalog.load_rpc_registry_evidence()
    counts = dict(committed["counts"])
    counts.update(
        {
            "ours": len(RPCMethod),
            "enum_changed": 0,
            "enum_stale": 0,
            "enum_new": 0,
            "enum_unparsed": 0,
        }
    )
    return {
        "confirmed": dict(committed["confirmed"]),
        "absent": dict(committed["absent"]),
        "present_unparsed": dict(committed["present_unparsed"]),
        "unmapped": dict(committed["unmapped"]),
        "enums": {"changed": [], "stale": [], "new": [], "unparsed": []},
        "quota_codes": {"1": "synthetic quota"},
        "proto_assertions": ["Synthetic.message"],
        "counts": counts,
    }


@pytest.mark.repo_lint
def test_fresh_capture_check_rejects_schema_gaps_and_truncation() -> None:
    missing_section = _fresh_registry_snapshot()
    missing_section.pop("enums")
    assert any(
        "top-level schema differs" in error
        for error in catalog.audit_live_registry_against_evidence(missing_section)
    )

    truncated = _fresh_registry_snapshot()
    truncated["unmapped"] = {}
    truncated["counts"]["unmapped"] = 0
    errors = catalog.audit_live_registry_against_evidence(truncated)
    assert any("capture is truncated" in error for error in errors)
    assert any("omissions differ" in error for error in errors)

    incomplete = _fresh_registry_snapshot()
    incomplete["quota_codes"] = {}
    incomplete["proto_assertions"] = []
    incomplete["counts"].pop("enum_new")
    errors = catalog.audit_live_registry_against_evidence(incomplete)
    assert any("quota_codes must be non-vacuous" in error for error in errors)
    assert any("proto_assertions must be non-vacuous" in error for error in errors)
    assert any("counts has unexpected or missing fields" in error for error in errors)

    raw_row = _fresh_registry_snapshot()
    rpc_id = next(iter(raw_row["unmapped"]))
    raw_row["unmapped"][rpc_id]["raw_capture"] = "must remain outside scrubbed projection"
    errors = catalog.audit_live_registry_against_evidence(raw_row)
    assert any(f"unmapped row {rpc_id!r} has unexpected fields" in error for error in errors)

    bad_confirmed = _fresh_registry_snapshot()
    rpc_id = next(iter(bad_confirmed["confirmed"]))
    bad_confirmed["confirmed"][rpc_id]["method"] = "truncated"
    errors = catalog.audit_live_registry_against_evidence(bad_confirmed)
    assert any(
        f"confirmed row {rpc_id!r} lacks a full /Service.Method path" in error for error in errors
    )


@pytest.mark.repo_lint
def test_non_rpc_authority_source_contract_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contracts = dict(catalog_ast.NON_RPC_SOURCE_CONTRACTS)
    contracts["_source/drive_import.py:DriveFetcher._request"] = (("future_transport",),)
    monkeypatch.setattr(catalog_ast, "NON_RPC_SOURCE_CONTRACTS", contracts)

    assert any(
        "DriveFetcher._request no longer reaches transport call future_transport" in error
        for error in catalog.audit_operation_catalog()
    )


@pytest.mark.repo_lint
def test_internal_generation_workflow_is_not_a_second_execution_authority() -> None:
    projection = catalog.build_operation_catalog()
    source = inspect.getsource(execute_generation)

    assert projection["app_authority_source_evidence"] == {}
    assert "_run_generation_workflow" in source
    assert "_run_rate_limit_retry" not in source
    assert "generate_with_retry(" not in source
    assert "handle_generation_result(" not in source


def test_semantic_ast_fingerprint_ignores_cross_version_shape_noise() -> None:
    class VersionedNode(ast.AST):
        _fields = ("payload", "ctx", "kind", "type_comment", "type_params")

        def __init__(
            self,
            *,
            payload: list[ast.AST] | tuple[ast.AST, ...],
            ctx: ast.expr_context,
            kind: str | None,
            type_comment: str | None,
            type_params: list[ast.AST],
        ) -> None:
            self.payload = payload
            self.ctx = ctx
            self.kind = kind
            self.type_comment = type_comment
            self.type_params = type_params

    older_shape = VersionedNode(
        payload=[ast.Constant(value="value")],
        ctx=ast.Load(),
        kind="u",
        type_comment="str",
        type_params=[],
    )
    newer_shape = VersionedNode(
        payload=(ast.Constant(value="value"),),
        ctx=ast.Store(),
        kind=None,
        type_comment=None,
        type_params=[ast.Name(id="T")],
    )

    assert catalog_ast._semantic_ast_shape(older_shape) == catalog_ast._semantic_ast_shape(
        newer_shape
    )
    fingerprint = catalog_ast._semantic_ast_fingerprint(older_shape)
    assert fingerprint == catalog_ast._semantic_ast_fingerprint(newer_shape)
    assert fingerprint.startswith("sha256:")
    assert len(fingerprint) == 71


@pytest.mark.repo_lint
def test_known_divergences_remain_reported_but_do_not_fail_audit() -> None:
    assert catalog.audit_operation_catalog() == []
    divergences = catalog.build_operation_catalog()["known_divergences"]

    assert {row["operation"] for row in divergences} == {
        "artifact.download",
        "chat.ask",
        "source.refresh",
    }


@pytest.mark.repo_lint
def test_operation_authorities_are_exact_discriminated_and_include_non_rpc_paths() -> None:
    projection = catalog.build_operation_catalog()
    rows = {row["key"]: row for row in projection["operations"]}

    audio = rows["artifact.generate_audio"]["execution_authorities"]
    assert {row["site"] for row in audio} == {
        "_web/bindings/studio.py:ARTIFACT_GENERATE_AUDIO",
    }
    assert all(row["discriminator"] for row in audio)
    assert not any("MindMapsAPI.generate" in row["site"] for row in audio)

    for operation, row_name in (
        ("artifact.generate_quiz", "ARTIFACT_GENERATE_QUIZ"),
        ("artifact.generate_flashcards", "ARTIFACT_GENERATE_FLASHCARDS"),
    ):
        authorities = rows[operation]["execution_authorities"]
        assert {row["site"] for row in authorities} == {
            f"_web/bindings/studio.py:{row_name}",
        }
        assert all(row["discriminator"] for row in authorities)

    note_backed = rows["artifact.generate_mind_map"]
    assert note_backed["native_bindings"] == [
        "CREATE_NOTE:plain",
        "DELETE_NOTE:<default>",
        "GENERATE_MIND_MAP:<default>",
        "GET_NOTEBOOK:<default>",
        "UPDATE_NOTE:<default>",
    ]
    # The shared natives (GET_NOTEBOOK, GENERATE_MIND_MAP, CREATE_NOTE/plain) are
    # allocated to the row; UPDATE_NOTE/DELETE_NOTE are single-consumer natives,
    # so their direct sites — the legacy note family the row drives and the row
    # itself — are derived.
    assert {row["site"] for row in note_backed["execution_authorities"]} == {
        "_note_service.py:LegacyNoteBackedService.delete_note",
        "_note_service.py:LegacyNoteBackedService.update_note",
        "_web/bindings/mind_maps.py:ARTIFACT_GENERATE_MIND_MAP",
    }
    assert "CREATE_ARTIFACT:<default>" not in note_backed["native_bindings"]
    assert {row["transport_kind"] for row in rows["chat.ask"]["execution_authorities"]} >= {
        "rpc",
        "stream",
    }
    assert {row["transport_kind"] for row in rows["source.add_file"]["execution_authorities"]} >= {
        "rpc",
        "upload",
        "download",
    }
    assert any(
        row["site"] == "_source/drive_import.py:DriveFetcher._request"
        for row in rows["source.add_file"]["execution_authorities"]
    )
    public = projection["public_methods"]
    assert public["sources.add_drive"]["operations"] == ["source.add_drive", "source.wait"]
    assert public["sources.add_drive_file"]["operations"] == [
        "source.add_file",
        "source.wait",
    ]
    assert {
        row["transport_kind"] for row in rows["artifact.download"]["execution_authorities"]
    } >= {
        "rpc",
        "download",
        "orchestrator",
    }
    assert all(
        row["site"] != "artifacts.py:with_rate_limit_retry"
        for row in rows["artifact.retry"]["execution_authorities"]
    )
    assert rows["source.wait"]["known_divergence"] is None
    assert rows["artifact.wait"]["known_divergence"] is None
    assert not any(
        row["transport_kind"] == "orchestrator"
        for operation in ("source.wait", "artifact.wait")
        for row in rows[operation]["execution_authorities"]
    )


@pytest.mark.repo_lint
@pytest.mark.parametrize("operation", list(catalog.RECENCY_CONTRACTS), ids=lambda op: op.value)
def test_recency_contracts_are_structured_source_contracts(operation: Operation) -> None:
    rows = {row["key"]: row for row in catalog.build_operation_catalog()["operations"]}
    contracts = rows[operation.value]["recency_contract"]

    assert contracts
    assert all(rule["unit"] and rule["condition"] for rule in contracts)
    assert all(
        rule["maximum_calls"] is None or rule["maximum_calls"] >= rule["minimum_calls"]
        for rule in contracts
    )


@pytest.mark.repo_lint
def test_get_metadata_recency_contract_pins_two_distinct_reads() -> None:
    row = next(
        row
        for row in catalog.build_operation_catalog()["operations"]
        if row["key"] == "notebook.metadata"
    )
    assert row["recency_contract"] == [
        {
            "public_methods": ["notebooks.get_metadata"],
            "minimum_calls": 2,
            "maximum_calls": 2,
            "unit": "public_call",
            "condition": "always: concurrent notebook.get plus source listing",
            "authority_sites": [
                "_web/bindings/notebooks.py:NOTEBOOK_GET",
                "_web/bindings/sources.py:SOURCE_LIST",
            ],
        }
    ]


def test_notebook_get_recency_contract_separates_typed_and_raw_authorities() -> None:
    typed, raw = catalog.RECENCY_CONTRACTS[Operation.NOTEBOOK_GET]

    assert typed.public_methods == (
        "notebooks.get",
        "notebooks.get_or_none",
        "notebooks.get_source_ids",
    )
    assert (typed.minimum_calls, typed.maximum_calls, typed.authority_sites) == (
        1,
        1,
        ("_web/bindings/notebooks.py:NOTEBOOK_GET",),
    )
    assert raw.public_methods == ("notebooks.get_raw",)
    assert (raw.minimum_calls, raw.maximum_calls, raw.authority_sites) == (
        1,
        1,
        ("_notebooks.py:NotebooksAPI.get_raw",),
    )

    notebook_tree = catalog_ast._parse(catalog_ast.SRC_ROOT / "_notebooks.py")
    raw_get = catalog_ast._find_class_method(notebook_tree, "NotebooksAPI", "get_raw")
    assert raw_get is not None
    assert catalog_ast._rpc_binding_call_count(raw_get, RPCMethod.GET_NOTEBOOK) == 1
    # P9.3: the typed authority is a codec row whose spec declares exactly one native.
    typed_row = next(
        row
        for row in catalog_ast.collect_binding_rows()
        if row.site == "_web/bindings/notebooks.py:NOTEBOOK_GET"
    )
    assert typed_row.operation is Operation.NOTEBOOK_GET
    assert typed_row.natives == (("GET_NOTEBOOK", None),)
    assert not typed_row.unresolved


def test_notebook_create_catalog_has_no_phantom_get_notebook_recency() -> None:
    create = next(
        spec for spec in catalog.OPERATION_SPECS if spec.operation is Operation.NOTEBOOK_CREATE
    )

    assert Operation.NOTEBOOK_CREATE not in catalog.RECENCY_CONTRACTS
    assert (RPCMethod.GET_NOTEBOOK, None) not in create.native_bindings
    assert (
        Operation.NOTEBOOK_CREATE,
        (RPCMethod.GET_NOTEBOOK, None),
    ) not in catalog_authorities.SHARED_RPC_AUTHORITY_RULES

    backend_tree = catalog_ast._parse(catalog_ast.SRC_ROOT / "_web" / "backend.py")
    assert catalog_ast._find_class_method(backend_tree, "WebRpcBackend", "_notebook_create") is None
    service_tree = catalog_ast._parse(catalog_ast.SRC_ROOT / "_notebook_mutation_service.py")
    create_workflow = catalog_ast._find_class_method(
        service_tree,
        "NotebookMutationService",
        "create",
    )
    assert create_workflow is not None
    assert catalog_ast._definition_invoke_call_count(create_workflow, "NOTEBOOK_GET_DEF") == 0


@pytest.mark.repo_lint
def test_update_and_chat_recency_conditions_are_explicit() -> None:
    rows = {row["key"]: row for row in catalog.build_operation_catalog()["operations"]}

    assert rows["notebook.update"]["recency_contract"] == [
        {
            "public_methods": [
                "notebooks.rename",
                "notebooks.set_emoji",
                "notebooks.update",
            ],
            "minimum_calls": 1,
            "maximum_calls": 1,
            "unit": "public_call",
            "condition": "always after a successful mutation",
            "authority_sites": ["_web/bindings/notebooks.py:NOTEBOOK_GET"],
        }
    ]
    chat_contracts = rows["chat.configure"]["recency_contract"]
    assert {
        tuple(rule["public_methods"]): (rule["minimum_calls"], rule["maximum_calls"])
        for rule in chat_contracts
    } == {
        ("chat.get_settings",): (1, 1),
        ("chat.configure", "chat.set_mode"): (0, 0),
    }
