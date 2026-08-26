"""JSON-envelope compatibility contract baseline tests."""

from __future__ import annotations

import dataclasses
import enum
import importlib
from pathlib import Path

import pytest
import scripts.audit_public_api_compat as public_audit

import notebooklm
from tests._baselines.compatibility_contracts import (
    _evidence_ast_fingerprint,
    _secret_serialization_violations,
    _validate_no_secret_channel_models,
    derive_json_envelope_contract,
)
from tests._baselines.json_envelope_contracts import (
    _ast_projection_shape,
    _normalize_conditional_key_groups,
    _validate_model_contribution_keys,
    _validate_secret_projection,
)
from tests._baselines.json_envelope_specs import (
    _CHANNEL_PROJECTION_SPECS,
    _LITERAL_DICT_DERIVATIONS,
    projection_spec_ids,
)


def _exported_models() -> dict[str, type]:
    package_dir = Path(notebooklm.__file__).resolve().parent
    modules = {public_audit.PUBLIC_PACKAGE}
    for path in package_dir.glob("*.py"):
        if path.stem.startswith("_") or path.stem in public_audit.EXCLUDED_TOP_LEVEL_MODULES:
            continue
        modules.add(f"{public_audit.PUBLIC_PACKAGE}.{path.stem}")
    modules.update(
        f"{public_audit.PUBLIC_PACKAGE}.{name}"
        for name in public_audit.EXTRA_PUBLIC_PACKAGES
        if (package_dir / name / "__init__.py").is_file()
    )

    models: dict[str, type] = {}
    for module_name in sorted(modules):
        module = importlib.import_module(module_name)
        for name in getattr(module, "__all__", ()):
            value = getattr(module, name)
            if isinstance(value, type) and (
                dataclasses.is_dataclass(value) or issubclass(value, enum.Enum)
            ):
                models[f"{value.__module__}.{value.__qualname__}"] = value
    return models


def test_literal_final_dict_projection_shapes_are_ast_derived_and_mutation_proven(
    tmp_path: Path,
) -> None:
    specs = [spec for channel in _CHANNEL_PROJECTION_SPECS.values() for spec in channel]
    literal_specs = [
        spec
        for channel, channel_specs in _CHANNEL_PROJECTION_SPECS.items()
        for spec in channel_specs
        if (channel, str(spec["model"]), str(spec["mode"])) in _LITERAL_DICT_DERIVATIONS
    ]
    literal_sites = {
        (
            str(spec["derive"]["path"]),
            str(spec["derive"]["function"]),
            tuple(spec["derive"]["contains"]),
        )
        for spec in literal_specs
    }
    assert len(_LITERAL_DICT_DERIVATIONS) == len(literal_specs) == 37
    assert len(literal_sites) == 28
    assert sum(spec["derive"] == "manual-reviewed+fingerprint" for spec in specs) == 168

    config = _LITERAL_DICT_DERIVATIONS[
        (
            "cli --json",
            "notebooklm.types.Artifact",
            "transitive-download-no-artifacts-final-wrapper",
        )
    ]
    source_root = Path(notebooklm.__file__).resolve().parents[1]
    assert _ast_projection_shape(source_root, config, object)["keys"] == [
        "error",
        "suggestion",
    ]

    relative_path = Path(str(config["path"]))
    source = (source_root / relative_path).read_text(encoding="utf-8")
    old = 'return {"error": result.error, "suggestion": result.suggestion}'
    new = 'return {"error": result.error, "next_step": result.suggestion}'
    assert old in source
    mutated_path = tmp_path / relative_path
    mutated_path.parent.mkdir(parents=True, exist_ok=True)
    mutated_path.write_text(source.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ValueError, match="expected one build_download_envelope dict"):
        _ast_projection_shape(tmp_path, config, object)


def test_json_envelope_preserves_original_exact_contract_assertions() -> None:
    contract = derive_json_envelope_contract()
    expected = {
        name: cls
        for name, cls in _exported_models().items()
        if dataclasses.is_dataclass(cls) and name != "notebooklm.auth.AuthTokens"
    }
    inventory = contract["exported_dataclass_key_inventory"]

    assert set(inventory) == set(expected)
    assert "notebooklm.auth.AuthTokens" not in inventory
    assert "notebooklm.artifacts.RateLimitRetryEvent" in inventory
    for name, cls in expected.items():
        field_names = [field.name for field in dataclasses.fields(cls)]
        assert inventory[name]["dataclass_fields"] == field_names
        assert inventory[name]["to_jsonable_keys"] == field_names

    assert set(contract["channels"]) == {"cli --json", "mcp tool result", "rest response"}
    for channel_name, reachable in contract["channels"].items():
        assert reachable
        allowed_secret = (
            {"notebooklm.auth.AuthTokens"}
            if channel_name in {"mcp tool result", "rest response"}
            else set()
        )
        assert set(reachable) - allowed_secret <= set(inventory)
        assert (set(reachable) & {"notebooklm.auth.AuthTokens"}) == allowed_secret
        assert "notebooklm.types.AskResult" in reachable
        assert "notebooklm.types.ChatReference" in reachable
        for row in reachable.values():
            assert row["projections"]
            assert all(
                projection["keys"] or projection.get("optional_keys")
                for projection in row["projections"]
            )
            assert all(projection["evidence"] for projection in row["projections"])

    cli = contract["channels"]["cli --json"]
    ask_keys = cli["notebooklm.types.AskResult"]["projections"][0]["keys"]
    assert "raw_response" not in ask_keys
    assert "answer_document" not in ask_keys
    cited = {
        projection["mode"]: projection
        for projection in cli["notebooklm.types.CitedSourceSelection"]["projections"]
    }
    assert cited["manual-completed-wait-projection"]["keys"] == [
        "status",
        "query",
        "sources_found",
        "sources",
        "report",
    ]
    assert cited["manual-completed-wait-projection"]["optional_keys"] == [
        "cited_only",
        "cited_sources_selected",
        "cited_only_fallback",
        "imported",
        "imported_sources",
    ]
    assert cited["manual-direct-import-projection"]["keys"] == [
        "status",
        "run_id",
        "sources_found",
        "sources_selected",
        "imported",
        "imported_sources",
        "already_present",
        "already_present_sources",
    ]
    for projection in cited.values():
        assert projection["model_contribution_keys"] == ["sources", "used_fallback"]
    notebook_projections = cli["notebooklm.types.Notebook"]["projections"]
    create = next(
        projection
        for projection in notebook_projections
        if projection["mode"] == "manual-create-projection"
    )
    assert create["keys"] == ["notebook"]
    assert create["nested_keys"]["notebook"] == [
        "id",
        "title",
        "role",
        "created_at",
        "last_viewed_at",
        "modified_at",
    ]
    assert cli["notebooklm.types.NotebookMetadata"]["projections"][0]["keys"] == [
        "id",
        "title",
        "created_at",
        "last_viewed_at",
        "modified_at",
        "is_owner",
        "role",
        "sources",
    ]
    assert cli["notebooklm.types.NotebookMetadata"]["projections"][0][
        "model_contribution_keys"
    ] == ["notebook", "sources"]
    assert cli["notebooklm.types.SourceSummary"]["projections"][0]["keys"] == [
        "type",
        "title",
        "url",
    ]
    assert cli["notebooklm.types.SourceSummary"]["projections"][0]["model_contribution_keys"] == [
        "kind",
        "title",
        "url",
    ]
    cli_descriptions = cli["notebooklm.types.NotebookDescription"]["projections"]
    assert [projection["keys"] for projection in cli_descriptions] == [
        ["notebook_id", "summary"],
        ["notebook_id", "summary", "suggested_topics"],
    ]
    assert cli_descriptions[0]["projection_condition"] == (
        "execute_notebook_describe returns a NotebookDescription"
    )
    assert "--topics is requested" in cli_descriptions[1]["projection_condition"]
    assert "summary null" in cli_descriptions[0]["contribution_semantics"]
    cli_topic = cli["notebooklm.types.SuggestedTopic"]["projections"][0]
    assert cli_topic["keys"] == ["question"]
    assert cli_topic["model_contribution_keys"] == ["question"]
    assert "at least one SuggestedTopic" in cli_topic["projection_condition"]
    assert "notebooklm.types.ResearchTask" in cli
    assert "notebooklm.types.ResearchSource" in cli
    assert cli["notebooklm.types.SourceGuide"]["projections"][0]["keys"] == [
        "source_id",
        "summary",
        "keywords",
    ]
    assert cli["notebooklm.types.SourceGuide"]["projections"][0]["model_contribution_keys"] == [
        "summary",
        "keywords",
    ]
    cli_fulltext = {
        row["mode"]: row for row in cli["notebooklm.types.SourceFulltext"]["projections"]
    }
    assert cli_fulltext["manual-field-projection"]["model_contribution_keys"] == [
        "source_id",
        "title",
        "content",
        "_type_code",
        "url",
        "char_count",
    ]
    assert cli_fulltext["manual-file-output-projection"]["model_contribution_keys"] == [
        "source_id",
        "title",
        "content",
        "_type_code",
    ]
    cli_generation = {
        projection["mode"]: projection
        for projection in cli["notebooklm.types.GenerationStatus"]["projections"]
    }
    assert cli_generation["manual-poll-projection"]["keys"] == [
        "task_id",
        "status",
        "url",
        "error",
        "error_code",
        "metadata",
    ]
    assert cli_generation["manual-wait-projection"]["keys"] == [
        "artifact_id",
        "status",
        "url",
        "error",
    ]
    retry_timeout = cli_generation["transitive-retry-timeout-task-id-contribution"]
    assert retry_timeout["keys"] == ["artifact_id", "status", "error"]
    assert retry_timeout["model_contribution_keys"] == ["task_id"]
    assert "CLI-owned scalars" in retry_timeout["contribution_semantics"]
    assert cli_generation["manual-retry-kickoff-projection"]["keys"] == [
        "task_id",
        "status",
        "url",
        "error",
        "error_code",
    ]
    assert cli_generation["manual-generation-completed-projection"]["keys"] == [
        "task_id",
        "status",
        "url",
    ]
    assert cli_generation["manual-generation-pending-projection"]["keys"] == [
        "task_id",
        "status",
    ]
    assert cli_generation["manual-generation-failure-envelope"]["keys"] == [
        "error",
        "code",
        "message",
    ]
    assert cli_generation["nested-timeout-transition-projection"]["keys"] == [
        "error",
        "code",
        "message",
        "notebook_id",
        "task_id",
        "timeout_seconds",
        "last_status",
        "status_history",
        "status_transitions",
        "stalled_phase",
    ]
    assert cli_generation["nested-timeout-transition-projection"]["nested_keys"][
        "status_transitions"
    ] == [
        "task_id",
        "status",
        "url",
        "error",
        "error_code",
        "metadata",
    ]
    assert cli_generation["nested-timeout-transition-projection"]["model_contribution_keys"] == [
        "task_id",
        "status",
        "url",
        "error",
        "error_code",
        "metadata",
    ]
    assert (
        "at least one public GenerationStatus"
        in cli_generation["nested-timeout-transition-projection"]["projection_condition"]
    )

    mcp = contract["channels"]["mcp tool result"]
    mcp_metadata = {
        row["mode"]: row for row in mcp["notebooklm.types.NotebookMetadata"]["projections"]
    }
    populated_metadata = mcp_metadata["transitive-notebook-describe-final-with-metadata"]
    null_metadata = mcp_metadata[
        "transitive:notebook-describe-final-with-metadata-null-description"
    ]
    for projection in (populated_metadata, null_metadata):
        assert projection["keys"] == ["notebook_id", "description", "metadata"]
        assert projection["nested_keys"]["metadata"] == ["notebook", "sources"]
        assert projection["nested_keys"]["metadata.sources"] == ["kind", "title", "url"]
        assert projection["model_contribution_keys"] == ["notebook", "sources"]
    assert populated_metadata["nested_keys"]["description"] == ["summary", "suggested_topics"]
    assert "description" not in null_metadata["nested_keys"]
    assert "description is None" in null_metadata["projection_condition"]
    mcp_source_summary = mcp["notebooklm.types.SourceSummary"]["projections"][0]
    assert mcp_source_summary["mode"] == "nested-dataclass"
    assert mcp_source_summary["keys"] == ["kind", "title", "url"]
    assert mcp_source_summary["evidence"] == [
        "nested-via:notebooklm.types.NotebookMetadata.sources"
    ]
    mcp_source = {row["mode"]: row for row in mcp["notebooklm.types.Source"]["projections"]}
    mcp_metadata_source = mcp_source[
        "transitive:notebook-describe-metadata-source-summary-final-wrapper"
    ]
    assert mcp_metadata_source["keys"] == ["notebook_id", "description", "metadata"]
    assert mcp_metadata_source["nested_keys"]["metadata.sources"] == [
        "kind",
        "title",
        "url",
    ]
    assert mcp_metadata_source["model_contribution_keys"] == ["title", "url", "_type_code"]
    assert "include_metadata" in mcp_metadata_source["projection_condition"]
    assert "empty metadata source list" in mcp_metadata_source["contribution_semantics"]
    assert all(
        projection["mode"] != "dataclass-full"
        for projection in mcp["notebooklm.types.Source"]["projections"]
    )
    mcp_description = mcp["notebooklm.types.NotebookDescription"]["projections"][0]
    assert mcp_description["keys"] == ["notebook_id", "description"]
    assert mcp_description["nested_keys"]["description"] == ["summary", "suggested_topics"]
    assert mcp_description["model_contribution_keys"] == ["summary", "suggested_topics"]
    assert mcp_description["projection_condition"] == (
        "NotebookDescribeResult.description is not None"
    )
    mcp_topic = mcp["notebooklm.types.SuggestedTopic"]["projections"][0]
    assert mcp_topic["keys"] == ["question", "prompt"]
    assert mcp_topic["model_contribution_keys"] == ["question", "prompt"]
    assert "at least one SuggestedTopic" in mcp_topic["projection_condition"]
    assert mcp["notebooklm.types.ResearchStart"]["projections"][0]["keys"] == [
        "notebook_id",
        "query",
        "mode",
        "poll_task_id",
    ]
    assert mcp["notebooklm.types.ResearchTask"]["projections"][0]["mode"] == (
        "manual-status-projection"
    )
    mcp_research_source = mcp["notebooklm.types.ResearchSource"]["projections"][0]
    assert mcp_research_source["mode"] == "nested-public-dict-report-omitted"
    assert mcp_research_source["keys"] == ["url", "title", "result_type"]
    assert mcp_research_source["optional_keys"] == [
        "research_task_id",
        "source_ordinal",
        "hint",
    ]
    assert "notebooklm.types.SourceFulltext" in mcp
    assert "notebooklm.types.SourceGuide" in mcp
    assert mcp["notebooklm.types.SourceFulltext"]["projections"][0]["model_contribution_keys"] == [
        "content",
        "char_count",
    ]
    assert mcp["notebooklm.types.SourceGuide"]["projections"][0]["model_contribution_keys"] == [
        "summary",
        "keywords",
    ]
    assert all(
        projection["model_contribution_keys"] == ["notebook", "sources"]
        for projection in mcp["notebooklm.types.NotebookMetadata"]["projections"]
    )
    mcp_source = {
        projection["mode"]: projection
        for projection in mcp["notebooklm.types.Source"]["projections"]
    }
    assert mcp_source["manual-compact-projection"]["keys"] == [
        "id",
        "title",
        "kind",
        "status_label",
        "drive_status_label",
        "created_at",
    ]
    account_success_keys = [
        "email",
        "authuser",
        "available",
        "notebook_limit",
        "source_limit",
        "tier",
        "output_language",
        "output_language_is_default",
    ]
    mcp_settings = mcp["notebooklm.types.UserSettings"]["projections"][0]
    mcp_limits = mcp["notebooklm.types.AccountLimits"]["projections"][0]
    assert mcp_settings["mode"] == "transitive-server-info-account-success-wrapper"
    assert mcp_settings["keys"] == ["server", "version", "auth", "account"]
    assert "optional_keys" not in mcp_settings
    assert mcp_settings["nested_keys"]["account"] == account_success_keys
    assert mcp_settings["model_contribution_keys"] == ["limits", "output_language"]
    assert "get_user_settings succeeds" in mcp_settings["projection_condition"]
    assert mcp_limits["model_contribution_keys"] == [
        "notebook_limit",
        "source_limit",
        "tier",
    ]
    mcp_generation = {
        projection["mode"]: projection
        for projection in mcp["notebooklm.types.GenerationStatus"]["projections"]
    }
    assert mcp_generation["app-status-view-projection"]["keys"] == [
        "notebook_id",
        "task_id",
        "status",
        "url",
        "error",
        "error_code",
        "metadata",
        "is_complete",
        "media_ready",
    ]
    assert mcp_generation["manual-retry-projection"]["keys"] == [
        "notebook_id",
        "artifact_id",
        "task_id",
        "status",
    ]
    assert mcp_generation["manual-generate-projection"]["keys"] == [
        "notebook_id",
        "kind",
        "task_id",
        "status",
        "url",
        "error",
    ]

    rest = contract["channels"]["rest response"]
    assert rest["notebooklm.types.ResearchStart"]["projections"][0]["keys"] == [
        "task_id",
        "report_id",
        "notebook_id",
        "query",
        "mode",
        "poll_id",
    ]
    assert rest["notebooklm.types.ResearchTask"]["projections"][0]["mode"] == (
        "manual-status-projection"
    )
    rest_research_source = rest["notebooklm.types.ResearchSource"]["projections"][0]
    assert rest_research_source["mode"] == "nested-public-dict-projection"
    assert rest_research_source["keys"] == ["url", "title", "result_type"]
    assert rest_research_source["optional_keys"] == [
        "research_task_id",
        "report_markdown",
        "source_ordinal",
        "hint",
    ]
    assert rest_research_source["evidence"] == [
        "notebooklm/_app/research.py:src.to_public_dict()",
        "notebooklm/server/routes/research.py:to_jsonable(result.sources)",
    ]
    assert "notebooklm.types.SourceFulltext" in rest
    assert "notebooklm.types.SourceGuide" in rest
    assert rest["notebooklm.types.SourceFulltext"]["projections"][0]["model_contribution_keys"] == [
        "content",
        "char_count",
    ]
    assert rest["notebooklm.types.SourceGuide"]["projections"][0]["model_contribution_keys"] == [
        "summary",
        "keywords",
    ]
    assert "notebooklm.types.UserSettings" in rest
    assert "notebooklm.types.AccountLimits" in rest
    rest_settings = rest["notebooklm.types.UserSettings"]["projections"][0]
    rest_limits = rest["notebooklm.types.AccountLimits"]["projections"][0]
    assert rest_settings["keys"] == ["server", "version", "auth", "account"]
    assert "optional_keys" not in rest_settings
    assert "nested_optional_keys" not in rest_settings
    assert rest_settings["nested_keys"]["account"] == account_success_keys
    assert rest_settings["model_contribution_keys"] == ["limits", "output_language"]
    assert "get_user_settings succeeds" in rest_settings["projection_condition"]
    assert rest_limits["keys"] == ["server", "version", "auth", "account"]
    assert "nested_optional_keys" not in rest_limits
    assert rest_limits["model_contribution_keys"] == [
        "notebook_limit",
        "source_limit",
        "tier",
    ]
    rest_generation = {
        projection["mode"]: projection
        for projection in rest["notebooklm.types.GenerationStatus"]["projections"]
    }
    assert rest_generation["app-status-view-projection"]["keys"] == [
        "notebook_id",
        "task_id",
        "status",
        "url",
        "error",
        "error_code",
        "metadata",
        "is_complete",
        "media_ready",
    ]
    assert rest_generation["manual-retry-projection"]["keys"] == [
        "notebook_id",
        "artifact_id",
        "task_id",
        "status",
    ]
    assert rest_generation["manual-generate-projection"]["keys"] == [
        "notebook_id",
        "kind",
        "task_id",
        "status",
        "url",
        "error",
    ]
    assert "notebooklm.types.NotebookMetadata" not in rest
    assert "notebooklm.types.SourceSummary" not in rest
    assert (
        "notebooklm.types.CitedSourceSelection"
        in contract["supplemental_import_references"]["cli --json"]
    )
    for channel in ("cli --json", "mcp tool result"):
        for model in ("notebooklm.types.MindMap", "notebooklm.types.MindMapResult"):
            projections = contract["channels"][channel][model]["projections"]
            assert all(projection["mode"] != "dataclass-full" for projection in projections)
            assert all("final" in projection["mode"] for projection in projections)
    assert (
        contract["secret_bearing_exclusions"]["notebooklm.auth.AuthTokens"]["adapter_reachable"]
        is True
    )


def test_json_envelope_covers_exported_models_and_exact_adapter_variants() -> None:
    contract = derive_json_envelope_contract()
    expected = {
        name: cls
        for name, cls in _exported_models().items()
        if dataclasses.is_dataclass(cls) and name != "notebooklm.auth.AuthTokens"
    }
    inventory = contract["exported_dataclass_key_inventory"]
    assert set(inventory) == set(expected)
    assert "notebooklm.artifacts.RateLimitRetryEvent" in inventory
    for name, cls in expected.items():
        fields = [field.name for field in dataclasses.fields(cls)]
        assert inventory[name]["dataclass_fields"] == fields
        assert inventory[name]["to_jsonable_keys"] == fields

    projection_ids: list[str] = []
    for channel_name, channel in contract["channels"].items():
        assert ("notebooklm.auth.AuthTokens" in channel) is (
            channel_name in {"mcp tool result", "rest response"}
        )
        assert {"notebooklm.types.AskResult", "notebooklm.types.ChatReference"} <= set(channel)
        for row in channel.values():
            for projection in row["projections"]:
                assert projection["id"]
                assert projection["keys"] or projection.get("optional_keys")
                assert projection["evidence"]
                projection_ids.append(projection["id"])
                if not projection["evidence"][0].startswith("nested-via:"):
                    assert projection["evidence_shape_fingerprints"]
                    assert projection["shape_derivation"]
    assert len(projection_ids) == len(set(projection_ids))
    declared_ids = projection_spec_ids()
    assert set(projection_ids) >= {
        projection_id for channel_ids in declared_ids.values() for projection_id in channel_ids
    }

    cli = contract["channels"]["cli --json"]
    cli_ask = cli["notebooklm.types.AskResult"]["projections"][0]
    assert cli_ask["optional_keys"] == ["note", "note_save_error"]
    assert cli_ask["nested_keys"]["note"] == ["id", "title"]
    assert {"raw_response", "answer_document"}.isdisjoint(cli_ask["keys"])
    for model, list_key in (
        ("notebooklm.types.Notebook", "notebooks"),
        ("notebooklm.types.Source", "sources"),
        ("notebooklm.types.Artifact", "artifacts"),
        ("notebooklm.types.Collection", "collections"),
    ):
        projection = next(
            row for row in cli[model]["projections"] if row["mode"] == "manual-list-final-wrapper"
        )
        assert projection["nested_keys"][list_key][0] == "index"
    cli_source = {row["mode"]: row for row in cli["notebooklm.types.Source"]["projections"]}
    assert cli_source["transitive-add-final-wrapper"]["keys"] == ["source"]
    assert cli_source["transitive-add-drive-final-wrapper"]["nested_keys"]["source"][-2:] == [
        "drive_file_id",
        "mime_type",
    ]
    assert "transitive-refresh-projection" not in cli_source
    clean = cli_source["transitive-clean-success-final-wrapper"]
    assert clean["keys"] == [
        "action",
        "notebook_id",
        "status",
        "candidates",
        "deleted_count",
        "failure_count",
    ]
    assert clean["optional_keys"] == ["candidate_count", "failures"]
    assert clean["nested_keys"]["failures"] == ["id", "error"]
    clean_error = cli_source["transitive-clean-confirm-required-error-wrapper"]
    assert clean_error["keys"] == [
        "error",
        "code",
        "message",
        "action",
        "notebook_id",
        "candidate_count",
        "candidates",
    ]
    assert clean_error["nested_keys"]["candidates"] == ["id", "title", "status", "reason"]
    assert {
        "transitive-research-import-new-source-projection",
        "transitive-research-import-existing-source-projection",
    } <= set(cli_source)
    processing_error = cli_source["transitive-wait-processing-error-final-wrapper"]
    assert processing_error["keys"] == ["source_id", "status", "status_code", "error"]
    assert processing_error["model_contribution_keys"] == ["status"]
    timeout_error = cli_source["transitive-wait-timeout-final-wrapper"]
    assert timeout_error["keys"] == [
        "source_id",
        "status",
        "last_status_code",
        "timeout_seconds",
        "error",
    ]
    assert timeout_error["model_contribution_keys"] == ["status"]
    resolver_error = cli_source["transitive-delete-resolver-error-text-wrapper"]
    assert resolver_error["keys"] == ["error", "code", "message"]
    assert resolver_error["model_contribution_keys"] == ["id", "title"]
    confirm_by_id = cli_source["transitive-delete-confirm-by-id-error-wrapper"]
    assert confirm_by_id["keys"] == [
        "error",
        "code",
        "message",
        "action",
        "source_id",
        "notebook_id",
    ]
    assert confirm_by_id["conditional_key_groups"] == [
        {
            "condition": "partial-id expansion differs from the requested source id",
            "keys": ["status_message"],
        }
    ]
    confirm_by_title = cli_source["transitive-delete-confirm-by-title-error-wrapper"]
    assert confirm_by_title["keys"][-3:] == ["source_id", "title", "notebook_id"]
    direct_import = cli_source["transitive-research-import-direct-final-wrapper"]
    assert direct_import["nested_keys"] == {
        "imported_sources": ["id", "title"],
        "already_present_sources": ["id", "title", "url"],
    }
    assert {
        "transitive-research-wait-imported-sources-final-wrapper",
        "transitive-source-add-research-imported-sources-final-wrapper",
    } <= set(cli_source)
    cli_label = {row["mode"]: row for row in cli["notebooklm.types.Label"]["projections"]}
    assert cli_label["manual-list-final-wrapper"]["keys"] == ["labels", "count"]
    assert cli_label["manual-list-final-wrapper"]["nested_keys"]["labels[].sources"] == [
        "id",
        "title",
    ]
    assert cli_label["resolver-ambiguous-id-error-wrapper"]["nested_keys"]["candidates"] == [
        "id",
        "emoji",
        "source_count",
    ]
    assert cli_label["resolver-near-miss-error-wrapper"]["nested_keys"]["candidates"] == [
        "id",
        "title",
    ]
    cli_collection = {row["mode"]: row for row in cli["notebooklm.types.Collection"]["projections"]}
    assert cli_collection["resolver-ambiguous-name-error-wrapper"]["nested_keys"]["candidates"] == [
        "id",
        "emoji",
        "notebook_count",
    ]
    assert cli_collection["resolver-near-miss-error-wrapper"]["model_contribution_keys"] == [
        "id",
        "name",
    ]
    cli_note = {row["mode"]: row for row in cli["notebooklm.types.Note"]["projections"]}
    assert cli_note["manual-list-final-wrapper"]["keys"] == [
        "notebook_id",
        "notes",
        "count",
    ]
    assert cli_note["transitive-history-save-note-final-wrapper"]["nested_keys"]["note"] == [
        "id",
        "title",
    ]
    cli_note_mind_map = cli_note["transitive-note-backed-mind-map-generation-final-contribution"]
    assert cli_note_mind_map["keys"] == ["mind_map", "note_id", "kind"]
    assert cli_note_mind_map["model_contribution_keys"] == ["id"]
    assert "successfully creates a public Note" in cli_note_mind_map["projection_condition"]
    cli_research = {row["mode"]: row for row in cli["notebooklm.types.ResearchTask"]["projections"]}
    cli_research_start = {
        row["mode"]: row for row in cli["notebooklm.types.ResearchStart"]["projections"]
    }
    for mode in (
        "transitive-source-add-research-no-wait-projection",
        "transitive-source-add-research-completed-projection",
    ):
        assert cli_research_start[mode]["model_contribution_keys"] == ["task_id", "report_id"]
    assert cli_research["manual-status-projection"]["nested_keys"]["tasks"] == [
        "task_id",
        "status",
        "query",
        "sources",
        "summary",
        "report",
    ]
    assert cli_research["manual-status-projection"]["nested_keys"]["tasks[].sources"] == [
        "url",
        "title",
        "result_type",
    ]
    assert cli_research["transitive-wait-completed-final-projection"]["nested_keys"] == {
        "imported_sources": ["id", "title"]
    }
    assert cli_research["transitive-wait-completed-final-projection"][
        "model_contribution_keys"
    ] == ["task_id", "status", "query", "sources", "report"]
    assert cli_research["transitive-wait-failed-final-projection"]["model_contribution_keys"] == [
        "status",
        "status_code",
        "source_type",
        "query",
        "sources",
        "report",
    ]
    assert cli_research["transitive-source-add-research-completed-final-projection"][
        "model_contribution_keys"
    ] == ["status", "sources", "report"]
    assert cli_research["transitive-source-add-research-failure-final-projection"][
        "model_contribution_keys"
    ] == ["status", "status_code", "source_type", "query", "sources"]
    no_research = cli_research["transitive-wait-no-research-branch-contribution"]
    assert no_research["keys"] == ["status", "error"]
    assert no_research["model_contribution_keys"] == ["status"]
    assert "timeout branch has no ResearchTask" in no_research["contribution_semantics"]
    cli_import_refusal = cli_research["transitive-import-refusal-error-contribution"]
    assert cli_import_refusal["keys"] == ["error", "code", "message"]
    assert cli_import_refusal["model_contribution_keys"] == [
        "task_id",
        "status",
        "status_code",
        "source_type",
        "query",
        "sources",
    ]
    assert "no ResearchSource field" in cli_import_refusal["contribution_semantics"]
    assert "transitive-source-add-research-completed-final-projection" in cli_research
    cli_source = {row["mode"]: row for row in cli["notebooklm.types.Source"]["projections"]}
    cli_metadata_source = cli_source["transitive:notebook-metadata-source-summary-final-wrapper"]
    assert cli_metadata_source["nested_keys"]["sources"] == ["type", "title", "url"]
    assert cli_metadata_source["model_contribution_keys"] == ["title", "url", "_type_code"]
    assert "at least one listed public Source" in cli_metadata_source["projection_condition"]
    assert "zero-source envelope" in cli_metadata_source["contribution_semantics"]
    cli_artifact = {row["mode"]: row for row in cli["notebooklm.types.Artifact"]["projections"]}
    cli_artifact_modes = set(cli_artifact)
    assert {
        "transitive-download-no-artifacts-final-wrapper",
        "transitive-download-error-final-wrapper",
        "transitive-download-all-dry-run-final-wrapper",
        "transitive-download-all-executed-final-wrapper",
        "transitive-download-single-dry-run-final-wrapper",
        "transitive-download-single-downloaded-final-wrapper",
    } <= cli_artifact_modes
    no_artifacts = cli_artifact["transitive-download-no-artifacts-final-wrapper"]
    assert no_artifacts["model_contribution_keys"] == ["_artifact_type", "status", "_variant"]
    assert "listing is non-empty" in no_artifacts["projection_condition"]
    assert "truly empty listing" in no_artifacts["contribution_semantics"]
    cli_interactive = cli_artifact["transitive-interactive-mind-map-generation-final-contribution"]
    assert cli_interactive["keys"] == ["mind_map", "note_id", "kind"]
    assert cli_interactive["model_contribution_keys"] == ["id"]
    assert "raw create-id fallback" in cli_interactive["contribution_semantics"]
    cli_mind_map_modes = {row["mode"] for row in cli["notebooklm.types.MindMap"]["projections"]}
    assert "transitive-artifact-delete-final-carveout" in cli_mind_map_modes
    cli_mind_map = {row["mode"]: row for row in cli["notebooklm.types.MindMap"]["projections"]}
    assert cli_mind_map["transitive-artifact-delete-final-carveout"]["conditional_key_groups"] == [
        {"condition": "note-backed mind map", "keys": ["kind", "note"]}
    ]
    assert cli_mind_map["transitive-artifact-delete-final-carveout"]["model_contribution_keys"] == [
        "id"
    ]
    assert (
        "regular or missing full-id artifact"
        in cli_mind_map["transitive-artifact-delete-final-carveout"]["contribution_semantics"]
    )
    cli_notebook = {row["mode"]: row for row in cli["notebooklm.types.Notebook"]["projections"]}
    assert cli_notebook["artifact-list-title-contribution"]["keys"] == ["title"]
    assert cli_notebook["source-list-title-contribution"]["keys"] == ["title"]

    mcp = contract["channels"]["mcp tool result"]
    assert "notebooklm.types.CitedSourceSelection" in mcp
    mcp_research_sources = {
        row["mode"]: row for row in mcp["notebooklm.types.ResearchSource"]["projections"]
    }
    assert {
        "nested-public-dict-report-omitted",
        "nested-public-dict-report-included-truncated",
        "transitive-import-source-count-contribution",
    } <= set(mcp_research_sources)
    assert (
        "report_markdown"
        not in mcp_research_sources["nested-public-dict-report-omitted"]["optional_keys"]
    )
    mcp_research_tasks = {
        row["mode"]: row for row in mcp["notebooklm.types.ResearchTask"]["projections"]
    }
    assert mcp_research_tasks["manual-status-projection"]["model_contribution_keys"] == [
        "task_id",
        "status",
        "query",
        "sources",
        "summary",
        "report",
        "status_code",
        "source_type",
        "discovery_mode",
        "created_at",
        "updated_at",
    ]
    assert mcp_research_tasks["transitive-cancel-terminal-final-wrapper"]["keys"] == [
        "status",
        "notebook_id",
        "poll_task_id",
        "run_id",
        "cancel_requested",
    ]
    assert mcp_research_tasks["transitive-cancel-nonterminal-final-wrapper"]["keys"][-1] == (
        "run_status_before"
    )
    assert mcp_research_tasks["transitive-import-final-wrapper"]["model_contribution_keys"] == [
        "status",
        "sources",
        "report",
    ]
    mcp_import_refusal = mcp_research_tasks["transitive-import-refusal-error-contribution"]
    assert mcp_import_refusal["keys"] == ["message"]
    assert mcp_import_refusal["model_contribution_keys"] == [
        "status",
        "status_code",
        "source_type",
        "query",
        "sources",
    ]
    assert "optional_keys" not in mcp_import_refusal
    mcp_research_start = {
        row["mode"]: row for row in mcp["notebooklm.types.ResearchStart"]["projections"]
    }
    assert mcp_research_start["manual-start-projection"]["model_contribution_keys"] == [
        "task_id",
        "report_id",
        "notebook_id",
        "query",
        "mode",
    ]
    missing_report = mcp_research_start["transitive-start-missing-report-id-error-contribution"]
    assert missing_report["keys"] == ["message"]
    assert missing_report["model_contribution_keys"] == ["task_id", "report_id"]
    count_contribution = mcp_research_sources["transitive-import-source-count-contribution"]
    assert count_contribution["keys"] == [
        "status",
        "notebook_id",
        "poll_task_id",
        "task_id",
        "imported",
        "newly_imported",
        "newly_imported_count",
        "already_present",
        "already_present_count",
        "sources_found",
        "sources_selected",
    ]
    assert count_contribution["model_contribution_keys"] == [
        "url",
        "result_type",
        "report_markdown",
    ]
    assert "no ResearchSource field envelope" in count_contribution["contribution_semantics"]
    mcp_ask = {row["mode"]: row for row in mcp["notebooklm.types.AskResult"]["projections"]}
    lite = mcp_ask["app-view:mcp-final-lite-references"]
    assert lite["nested_keys"]["references"] == []
    assert lite["nested_optional_keys"]["references"] == [
        "source_id",
        "citation_number",
        "cited_text",
    ]
    lite_reference = next(
        row
        for row in mcp["notebooklm.types.ChatReference"]["projections"]
        if row["mode"] == "nested-lite-projection"
    )
    assert lite_reference["keys"] == []
    assert lite_reference["optional_keys"] == ["source_id", "citation_number", "cited_text"]
    assert not any(
        row["id"].startswith("mcp.AskResult.app-view-mcp-final-lite-references.nested-references")
        for row in mcp["notebooklm.types.ChatReference"]["projections"]
    )
    full_reference_keys = [
        "source_id",
        "citation_number",
        "cited_text",
        "start_char",
        "end_char",
        "chunk_id",
        "passage_id",
        "answer_start_char",
        "answer_end_char",
        "score",
        "fragment_start_char",
        "fragment_end_char",
        "answer_anchor_start",
        "answer_anchor_end",
    ]
    for channel_name, ask_mode in (
        ("cli --json", "app-view-cli-final-with-note-outcome"),
        ("mcp tool result", "app-view-mcp-final-full-references"),
        ("rest response", "app-view-ask-result-view"),
    ):
        channel_rows = contract["channels"][channel_name]
        prefix = f"{channel_name.split()[0]}.AskResult.{ask_mode}.nested-"
        nested_reference = next(
            row
            for row in channel_rows["notebooklm.types.ChatReference"]["projections"]
            if row["id"] == f"{prefix}references-ChatReference"
        )
        assert nested_reference["keys"] == full_reference_keys
        nested_turn_key = next(
            row
            for row in channel_rows["notebooklm.types.ConversationTurnKey"]["projections"]
            if row["id"] == f"{prefix}turn_key-ConversationTurnKey"
        )
        assert nested_turn_key["keys"] == ["session_id", "turn_id", "turn_code"]
        nested_next_step = next(
            row
            for row in channel_rows["notebooklm.types.NextStepSuggestion"]["projections"]
            if row["id"] == f"{prefix}next_steps-NextStepSuggestion"
        )
        assert nested_next_step["keys"] == ["question", "type_code"]
    document_model_keys = {
        "notebooklm._types.documents.StructuredDocument": ["blocks", "annotations"],
        "notebooklm._types.documents.DocumentAnnotation": [
            "object_id",
            "start_index",
            "end_index",
        ],
        "notebooklm._types.documents.DocumentBlock": ["start_index", "end_index", "spans"],
        "notebooklm._types.documents.TextSpan": ["start_index", "end_index", "text"],
    }
    for channel_name, expected_root in (
        (
            "cli --json",
            [
                "answer",
                "conversation_id",
                "turn_number",
                "is_follow_up",
                "references",
                "turn_key",
                "next_steps",
            ],
        ),
        (
            "mcp tool result",
            [
                "notebook_id",
                "answer",
                "conversation_id",
                "turn_number",
                "is_follow_up",
                "references",
                "turn_key",
                "next_steps",
            ],
        ),
        (
            "rest response",
            [
                "answer",
                "conversation_id",
                "turn_number",
                "is_follow_up",
                "references",
                "turn_key",
                "next_steps",
            ],
        ),
    ):
        channel_rows = contract["channels"][channel_name]
        for model, contribution_keys in document_model_keys.items():
            full = next(
                row
                for row in channel_rows[model]["projections"]
                if row["mode"] == "transitive-chat-reference-full-contribution"
            )
            assert full["keys"] == expected_root
            assert full["nested_keys"]["references"] == full_reference_keys
            assert full["model_contribution_keys"] == contribution_keys
            assert full["projection_condition"]
    for model in (
        "notebooklm._types.documents.DocumentBlock",
        "notebooklm._types.documents.TextSpan",
    ):
        lite_document = next(
            row
            for row in mcp[model]["projections"]
            if row["mode"] == "transitive-chat-reference-lite-fragment-contribution"
        )
        assert lite_document["nested_keys"]["references"] == []
        assert lite_document["nested_optional_keys"]["references"] == [
            "source_id",
            "citation_number",
            "cited_text",
        ]
        assert "references=lite" in lite_document["projection_condition"]
    for channel_rows in contract["channels"].values():
        assert "notebooklm._types.documents.ListInfo" not in channel_rows
        assert "notebooklm._types.documents.TableCell" not in channel_rows
    mcp_source = {row["mode"]: row for row in mcp["notebooklm.types.Source"]["projections"]}
    assert mcp_source["nested-dataclass-source-rename-result"]["nested_keys"]["source"]
    assert mcp_source["app-view:source-add-drive-final-wrapper"]["keys"] == [
        "source",
        "file_id",
        "mime_type",
        "notebook_id",
        "status",
    ]
    compact_list = mcp_source["manual-compact-list-final-wrapper"]
    assert compact_list["keys"] == ["notebook_id", "sources", "total", "offset", "has_more"]
    assert compact_list["nested_keys"]["sources"] == [
        "id",
        "title",
        "kind",
        "status_label",
        "drive_status_label",
        "created_at",
    ]
    assert mcp_source["app-view:source-read-full-final-wrapper"]["keys"] == [
        "notebook_id",
        "source_id",
        "source",
        "content",
        "char_count",
        "truncated",
        "output_format",
    ]
    assert mcp_source["app-view:source-add-drive-file-final-wrapper"]["keys"] == [
        "source",
        "document_id",
        "notebook_id",
        "status",
    ]
    assert "optional_keys" not in mcp_source["app-view:source-add-drive-file-final-wrapper"]
    batch = mcp_source["transitive-batch-added-item-final-wrapper"]
    assert "transitive-batch-error-item-final-wrapper" not in mcp_source
    assert batch["nested_union_keys"]["results"]["added"] == [
        "input",
        "status",
        "source_id",
        "title",
        "status_label",
    ]
    assert batch["nested_union_keys"]["results"]["error"] == ["input", "status", "error"]
    assert batch["nested_keys"]["results[].error"] == [
        "code",
        "message",
        "retriable",
    ]
    assert batch["nested_optional_keys"]["results[].error"] == [
        "unconfirmed",
        "candidates",
        "hint",
    ]
    assert batch["model_contribution_keys"] == ["id", "title", "status"]
    assert "all-error batch is non-public" in batch["contribution_semantics"]
    wait = mcp_source["app-view:source-wait-final-wrapper"]
    for bucket in ("timed_out", "failed", "not_found"):
        assert wait["nested_keys"][bucket] == ["source_id", "error"]
    assert wait["nested_optional_keys"]["ready"] == ["warning"]
    assert wait["model_contribution_keys"] == [
        "id",
        "title",
        "url",
        "_type_code",
        "created_at",
        "status",
        "drive_document_id",
        "drive_status",
    ]
    assert "explicit canonical-id wait" in wait["contribution_semantics"]
    assert "source_add wait" in wait["projection_condition"]
    assert {
        "transitive-research-import-new-source-projection",
        "transitive-research-import-existing-source-projection",
    } <= set(mcp_source)
    remote_upload = mcp_source["transitive-remote-upload-await-final-wrapper"]
    assert remote_upload["keys"] == ["status", "source_id", "file"]
    assert remote_upload["nested_keys"]["file"] == [
        "source_id",
        "name",
        "size",
        "mime",
        "sha256",
    ]
    assert mcp_source["transitive-remote-upload-http-final-wrapper"]["keys"] == [
        "status",
        "source_id",
    ]
    mcp_note = {row["mode"]: row for row in mcp["notebooklm.types.Note"]["projections"]}
    assert "created_at" not in mcp_note["manual-studio-summary-projection"]["keys"]
    assert "transitive-studio-delete-confirmation-wrapper" in mcp_note
    mcp_note_mind_map = mcp_note["transitive-note-backed-mind-map-generation-final-contribution"]
    assert mcp_note_mind_map["keys"] == ["notebook_id", "kind", "mind_map", "mind_map_id"]
    assert mcp_note_mind_map["model_contribution_keys"] == ["id"]
    for model in ("notebooklm.types.Note", "notebooklm.types.Artifact"):
        studio_rows = {row["mode"]: row for row in mcp[model]["projections"]}
        assert studio_rows["manual-studio-full-list-final-wrapper"]["nested_union_keys"] == {
            "items": {
                "note": ["id", "title", "type", "content"],
                "artifact": ["id", "title", "type", "status_label", "url"],
            }
        }
        summary_union = studio_rows["manual-studio-summary-list-final-wrapper"][
            "nested_union_keys"
        ]["items"]
        assert summary_union["note"] == [
            "id",
            "title",
            "type",
            "content_preview",
            "char_count",
        ]
        assert summary_union["artifact"][-2:] == ["created_at", "generation_prompt"]
        by_item = studio_rows["manual-studio-by-item-final-wrapper"]["nested_union_keys"]["items"]
        assert by_item["note"][-1] == "content"
        assert by_item["artifact"][-1] == "generation_prompt"
    mcp_artifact_modes = {row["mode"] for row in mcp["notebooklm.types.Artifact"]["projections"]}
    assert {
        "transitive-mcp-stdio-download-selected-wrapper",
        "transitive-mcp-stdio-download-error-wrapper",
        "transitive-mcp-stdio-download-dry-all-wrapper",
        "transitive-mcp-stdio-download-executed-wrapper",
        "transitive-mcp-remote-download-broker-wrapper",
    } <= mcp_artifact_modes
    mcp_artifact = {row["mode"]: row for row in mcp["notebooklm.types.Artifact"]["projections"]}
    assert mcp_artifact["transitive-studio-rename-final-projection"]["model_contribution_keys"] == [
        "id",
        "_artifact_type",
        "_variant",
    ]
    assert (
        "full-UUID resolver-miss"
        in mcp_artifact["transitive-studio-rename-final-projection"]["contribution_semantics"]
    )
    mcp_rename_membership = mcp_artifact["transitive-mind-map-rename-membership-final-contribution"]
    assert mcp_rename_membership["model_contribution_keys"] == [
        "id",
        "_artifact_type",
        "_variant",
    ]
    assert "full-UUID resolver-miss" in mcp_rename_membership["contribution_semantics"]
    for mode in (
        "transitive-download-incomplete-status-error-text-contribution",
        "transitive-retry-wrong-state-status-error-text-contribution",
    ):
        assert mcp_artifact[mode]["keys"] == ["message"]
        assert mcp_artifact[mode]["model_contribution_keys"] == ["status"]
        assert "optional_keys" not in mcp_artifact[mode]
    stdio_error = mcp_artifact["transitive-mcp-stdio-download-error-wrapper"]
    assert stdio_error["model_contribution_keys"] == [
        "id",
        "title",
        "_artifact_type",
        "status",
        "created_at",
        "_variant",
    ]
    assert "at least one public Artifact" in stdio_error["projection_condition"]
    assert "truly empty listing" in stdio_error["contribution_semantics"]
    mcp_interactive = mcp_artifact["transitive-interactive-mind-map-generation-final-contribution"]
    assert mcp_interactive["keys"] == ["notebook_id", "kind", "mind_map", "mind_map_id"]
    assert mcp_interactive["model_contribution_keys"] == ["id"]
    broker = next(
        row
        for row in mcp["notebooklm.types.Artifact"]["projections"]
        if row["mode"] == "transitive-mcp-remote-download-broker-wrapper"
    )
    assert broker["optional_keys"] == ["artifact_id"]
    assert broker["model_contribution_keys"] == stdio_error["model_contribution_keys"]
    assert "artifact-ref branch" in broker["projection_condition"]
    assert "non-inline kind" in broker["contribution_semantics"]
    assert broker["conditional_key_groups"] == [
        {
            "condition": "inline textual artifact content",
            "keys": ["content", "char_count", "truncated"],
        }
    ]
    mcp_notebook = {row["mode"]: row for row in mcp["notebooklm.types.Notebook"]["projections"]}
    delete_preview = mcp_notebook["always-listed-delete-confirmation-title-wrapper"]
    assert delete_preview["nested_keys"]["preview"] == ["action", "notebook_id", "title"]
    assert delete_preview["model_contribution_keys"] == ["title"]
    suggested_topic = mcp["notebooklm.types.SuggestedTopic"]["projections"]
    assert suggested_topic == [
        {
            "id": (
                "mcp.NotebookDescription.nested-notebook-describe-final."
                "nested-suggested_topics-SuggestedTopic"
            ),
            "mode": "nested-dataclass",
            "keys": ["question", "prompt"],
            "evidence": ["nested-via:notebooklm.types.NotebookDescription.suggested_topics"],
            "model_contribution_keys": ["question", "prompt"],
            "projection_condition": (
                "NotebookDescribeResult.description is not None and contains at least "
                "one SuggestedTopic"
            ),
            "contribution_semantics": (
                "Each SuggestedTopic is recursively serialized in description."
                "suggested_topics; an empty collection has no SuggestedTopic instance"
            ),
        }
    ]
    source_summary = mcp["notebooklm.types.SourceSummary"]["projections"]
    assert source_summary == [
        {
            "id": (
                "mcp.NotebookMetadata.transitive-notebook-describe-final-with-metadata."
                "nested-sources-SourceSummary"
            ),
            "mode": "nested-dataclass",
            "keys": ["kind", "title", "url"],
            "evidence": ["nested-via:notebooklm.types.NotebookMetadata.sources"],
        }
    ]
    prompt_rows = {
        row["mode"]: row for row in mcp["notebooklm.types.PromptSuggestion"]["projections"]
    }
    suggestion_only = prompt_rows["manual:chat-suggestion-only-final-wrapper"]
    assert suggestion_only["keys"] == ["notebook_id", "suggested_prompts"]
    assert suggestion_only["optional_keys"] == ["history", "conversation_id", "source_ids"]
    share_rows = {row["mode"]: row for row in mcp["notebooklm.types.ShareStatus"]["projections"]}
    assert "app-view:share_status_view+view_level" not in share_rows
    assert "app-view:mutation-final-updated+view_level" in share_rows
    share_preview = share_rows["conditional-public-widening-confirmation-wrapper"]
    assert share_preview["model_contribution_keys"] == ["is_public"]
    assert share_preview["projection_condition"] == "current ShareStatus.is_public is false"
    for model in (
        "notebooklm.types.Notebook",
        "notebooklm.types.Source",
        "notebooklm.types.Note",
        "notebooklm.types.Artifact",
    ):
        error_row = next(
            row
            for row in mcp[model]["projections"]
            if row["mode"] == "conditional-resolver-error-text-contribution"
        )
        assert error_row["keys"] == ["message"]
        assert error_row["optional_keys"] == ["hint"]
        assert error_row["model_contribution_keys"] == ["id", "title"]
    label_error = next(
        row
        for row in mcp["notebooklm.types.Label"]["projections"]
        if row["mode"] == "always-listed-resolver-error-text-contribution"
    )
    assert label_error["model_contribution_keys"] == ["id", "name"]

    expected_resolver_ids = {
        "cli": {
            "cli.Notebook.conditional-noncanonical-resolver-id-contribution",
            "cli.Notebook.always-listed-collection-membership-resolver-id-contribution",
            "cli.Source.conditional-noncanonical-resolver-id-contribution",
            "cli.Artifact.conditional-noncanonical-resolver-id-contribution",
            "cli.Artifact.always-listed-download-resolver-id-contribution",
            "cli.Note.conditional-noncanonical-resolver-id-contribution",
            "cli.Collection.always-listed-resolver-id-contribution",
            "cli.Label.always-listed-resolver-id-contribution",
        },
        "mcp": {
            "mcp.Notebook.conditional-noncanonical-resolver-id-contribution",
            "mcp.Source.conditional-noncanonical-resolver-id-contribution",
            "mcp.Note.conditional-noncanonical-resolver-id-contribution",
            "mcp.Artifact.conditional-noncanonical-resolver-id-contribution",
            "mcp.Note.always-listed-studio-item-resolver-id-contribution",
            "mcp.Artifact.always-listed-studio-item-resolver-id-contribution",
            "mcp.Artifact.always-listed-studio-download-resolver-id-contribution",
        },
    }
    assert expected_resolver_ids["cli"] <= set(declared_ids["cli --json"])
    assert expected_resolver_ids["mcp"] <= set(declared_ids["mcp tool result"])
    for expected_ids in expected_resolver_ids.values():
        for projection_id in expected_ids:
            projection = next(
                item
                for channel in contract["channels"].values()
                for model in channel.values()
                for item in model["projections"]
                if item["id"] == projection_id
            )
            assert projection["keys"] == ["id"]

    rest = contract["channels"]["rest response"]
    assert "notebooklm.types.NotebookMetadata" not in rest
    rest_note = {row["mode"]: row for row in rest["notebooklm.types.Note"]["projections"]}
    rest_note_mind_map = rest_note["transitive-note-backed-mind-map-generation-final-contribution"]
    assert rest_note_mind_map["nested_keys"]["mind_map"] == [
        "mind_map",
        "note_id",
        "created_at",
    ]
    assert rest_note_mind_map["model_contribution_keys"] == ["id", "created_at"]
    for model, collection_key in (
        ("notebooklm.types.Notebook", "notebooks"),
        ("notebooklm.types.Source", "sources"),
        ("notebooklm.types.Artifact", "artifacts"),
        ("notebooklm.types.Note", "notes"),
    ):
        paged = next(
            row for row in rest[model]["projections"] if "list-paged-final-wrapper" in row["mode"]
        )
        assert collection_key in paged["keys"]
        assert paged["nested_keys"]["meta"] == ["total", "has_more", "limit", "offset"]
    rest_generation = {
        row["mode"]: row for row in rest["notebooklm.types.GenerationStatus"]["projections"]
    }
    for mode in ("http-failed-409-error-envelope", "http-removed-410-error-envelope"):
        assert rest_generation[mode]["nested_keys"]["error"] == ["category", "message"]
    rest_source = {row["mode"]: row for row in rest["notebooklm.types.Source"]["projections"]}
    content_gate = rest_source["transitive-source-content-readiness-contribution"]
    assert content_gate["keys"] == [
        "notebook_id",
        "source_id",
        "content",
        "char_count",
        "truncated",
        "output_format",
    ]
    assert content_gate["model_contribution_keys"] == ["status"]
    assert "null, zero, false" in content_gate["contribution_semantics"]
    rest_batch = rest_source["transitive-batch-added-item-final-wrapper"]
    assert "transitive-batch-error-item-final-wrapper" not in rest_source
    assert rest_batch["nested_union_keys"]["results"]["added"][-3:] == [
        "source_id",
        "title",
        "status_label",
    ]
    assert rest_batch["nested_union_keys"]["results"]["error"] == [
        "input",
        "status",
        "error",
    ]
    assert rest_batch["nested_keys"]["results[].error"] == [
        "category",
        "message",
        "retriable",
    ]
    assert rest_batch["model_contribution_keys"] == ["id", "title", "status"]
    rest_wait = rest_source["app-view:source-wait-final-wrapper"]
    for bucket in ("timed_out", "failed", "not_found"):
        assert rest_wait["nested_keys"][bucket] == ["source_id", "error"]
    assert "nested_optional_keys" not in rest_wait
    assert rest_wait["model_contribution_keys"] == wait["model_contribution_keys"]
    assert "empty wait-all result" in rest_wait["contribution_semantics"]
    assert "wait-all lists a Source" in rest_wait["projection_condition"]
    rest_research_task = {
        row["mode"]: row for row in rest["notebooklm.types.ResearchTask"]["projections"]
    }
    assert rest_research_task["manual-status-projection"]["model_contribution_keys"] == [
        "task_id",
        "status",
        "query",
        "sources",
        "summary",
        "report",
        "status_code",
        "source_type",
        "discovery_mode",
        "created_at",
        "updated_at",
    ]
    assert rest_research_task["transitive-import-final-wrapper"]["keys"] == [
        "status",
        "notebook_id",
        "run_id",
        "imported",
        "sources_found",
    ]
    rest_import_refusal = rest_research_task["transitive-import-refusal-error-contribution"]
    assert rest_import_refusal["nested_keys"]["error"] == [
        "category",
        "message",
        "retriable",
    ]
    assert "no ResearchSource field" in rest_import_refusal["contribution_semantics"]
    rest_research_start = {
        row["mode"]: row for row in rest["notebooklm.types.ResearchStart"]["projections"]
    }
    assert rest_research_start["dataclass-full-with-poll-id"]["model_contribution_keys"] == [
        "task_id",
        "report_id",
        "query",
        "mode",
    ]
    missing_poll = rest_research_start["transitive-start-missing-poll-id-error-contribution"]
    assert missing_poll["keys"] == ["error"]
    assert missing_poll["nested_keys"]["error"] == ["category", "message", "retriable"]
    assert missing_poll["model_contribution_keys"] == ["task_id", "report_id"]
    rest_artifact = {row["mode"]: row for row in rest["notebooklm.types.Artifact"]["projections"]}
    rest_download_error = rest_artifact["transitive-download-no-artifacts-409-error-contribution"]
    assert rest_download_error["keys"] == ["error"]
    assert rest_download_error["nested_keys"]["error"] == ["category", "message"]
    assert rest_download_error["model_contribution_keys"] == [
        "_artifact_type",
        "status",
        "_variant",
    ]
    assert "at least one public Artifact" in rest_download_error["projection_condition"]
    assert "truly empty list is non-public" in rest_download_error["contribution_semantics"]
    assert "binary FileResponse" in rest_download_error["contribution_semantics"]
    rest_interactive = rest_artifact[
        "transitive-interactive-mind-map-generation-final-contribution"
    ]
    assert rest_interactive["nested_keys"]["mind_map"] == [
        "id",
        "notebook_id",
        "title",
        "kind",
        "created_at",
        "tree",
    ]
    assert rest_interactive["model_contribution_keys"] == [
        "id",
        "title",
        "_artifact_type",
        "created_at",
        "_variant",
    ]
    rest_rename_artifact = rest_artifact["transitive-mind-map-rename-membership-final-contribution"]
    assert rest_rename_artifact["model_contribution_keys"] == [
        "id",
        "_artifact_type",
        "_variant",
    ]
    assert "unmatched regular artifacts" in rest_rename_artifact["contribution_semantics"]
    for model in ("notebooklm.types.MindMap", "notebooklm.types.MindMapResult"):
        mind_map = rest[model]["projections"][0]
        assert mind_map["keys"] == ["notebook_id", "kind", "mind_map"]
        assert mind_map["nested_keys"]["mind_map"]
    rest_mind_map_modes = {row["mode"] for row in rest["notebooklm.types.MindMap"]["projections"]}
    assert "transitive-artifact-rename-final-wrapper" in rest_mind_map_modes
    rest_mind_map = {row["mode"]: row for row in rest["notebooklm.types.MindMap"]["projections"]}
    assert rest_mind_map["transitive-artifact-rename-final-wrapper"]["model_contribution_keys"] == [
        "id"
    ]
    assert (
        "unmatched artifact"
        in rest_mind_map["transitive-artifact-rename-final-wrapper"]["contribution_semantics"]
    )
    mcp_mind_map = {row["mode"]: row for row in mcp["notebooklm.types.MindMap"]["projections"]}
    for mode in (
        "transitive-resolver-rename-final-wrapper",
        "transitive-resolver-delete-final-wrapper",
    ):
        assert mcp_mind_map[mode]["model_contribution_keys"] == ["id"]
        assert mcp_mind_map[mode]["projection_condition"]

    assert (
        contract["secret_bearing_exclusions"]["notebooklm.auth.AuthTokens"]["adapter_reachable"]
        is True
    )
    auth_tokens_projection = mcp["notebooklm.auth.AuthTokens"]["projections"]
    assert auth_tokens_projection == [
        {
            "id": "mcp.AuthTokens.redacted-server-info-account-identity-contribution",
            "mode": "redacted-server-info-account-identity-contribution",
            "keys": ["server", "version", "auth", "account"],
            "evidence": [
                "notebooklm/client.py:return self.auth.authuser",
                "notebooklm/client.py:return self._provider.auth",
                "notebooklm/_auth/account_email.py:def _session_key",
                "notebooklm/_auth/account_email.py:storage_path = auth.storage_path",
                "notebooklm/mcp/tools/meta.py:async def _account_block",
                'notebooklm/mcp/tools/meta.py:info["account"] = await _account_block',
            ],
            "evidence_shape_fingerprints": auth_tokens_projection[0]["evidence_shape_fingerprints"],
            "shape_derivation": "manual-reviewed+fingerprint",
            "nested_keys": {
                "auth": [
                    "authenticated",
                    "storage_exists",
                    "json_valid",
                    "cookies_present",
                    "sid_cookie",
                    "profile",
                ]
            },
            "nested_union_keys": {
                "account": {
                    "success": [
                        "email",
                        "authuser",
                        "available",
                        "notebook_limit",
                        "source_limit",
                        "tier",
                        "output_language",
                        "output_language_is_default",
                    ],
                    "unavailable": ["email", "authuser", "available", "reason"],
                }
            },
            "model_contribution_keys": [
                "authuser",
                "account_email",
                "storage_path",
                "_profile_session_generation",
            ],
            "emitted_model_contribution_keys": ["authuser", "account_email"],
            "control_model_contribution_keys": [
                "storage_path",
                "_profile_session_generation",
            ],
            "projection_condition": (
                "server_info include_account is true; authuser always comes from the live mutable "
                "in-memory AuthTokens returned by client.auth, while account_email contributes "
                "only when that in-memory/cached identity wins before persisted or live fallback"
            ),
            "adapter_surface": "MCP explicitly redacted safe-field identity contribution",
            "contribution_semantics": (
                "Only the live mutable AuthTokens.authuser and account_email may supply emitted "
                "account values; storage_path and _profile_session_generation only select "
                "persisted/cache/live fallback behavior, and the account unions expose no "
                "credential-bearing fields"
            ),
            "redacted_projection": "safe-field-contribution",
        }
    ]
    rest_auth_tokens_projection = contract["channels"]["rest response"][
        "notebooklm.auth.AuthTokens"
    ]["projections"]
    assert len(rest_auth_tokens_projection) == 1
    rest_auth = rest_auth_tokens_projection[0]
    assert rest_auth["id"] == "rest.AuthTokens.redacted-server-info-account-identity-contribution"
    assert rest_auth["keys"] == ["server", "version", "auth", "account"]
    assert "nested_optional_keys" not in rest_auth
    assert set(rest_auth["nested_union_keys"]["account"]) == {"success", "unavailable"}
    assert rest_auth["model_contribution_keys"] == [
        "authuser",
        "account_email",
        "storage_path",
        "_profile_session_generation",
    ]
    assert rest_auth["redacted_projection"] == "safe-field-contribution"
    assert rest_auth["emitted_model_contribution_keys"] == ["authuser", "account_email"]
    assert rest_auth["control_model_contribution_keys"] == [
        "storage_path",
        "_profile_session_generation",
    ]
    assert "bound client exists with no startup error" in rest_auth["projection_condition"]
    assert "persisted-identity branch has no AuthTokens" in rest_auth["contribution_semantics"]
    auth_tokens_policy = contract["secret_bearing_exclusions"]["notebooklm.auth.AuthTokens"]
    assert auth_tokens_policy["allowed_projections"] == [
        {
            "channel": "mcp tool result",
            "projection_id": "mcp.AuthTokens.redacted-server-info-account-identity-contribution",
        },
        {
            "channel": "rest response",
            "projection_id": "rest.AuthTokens.redacted-server-info-account-identity-contribution",
        },
    ]
    assert auth_tokens_policy["allowed_model_contribution_keys"] == [
        "authuser",
        "account_email",
        "storage_path",
        "_profile_session_generation",
    ]
    assert auth_tokens_policy["allowed_emitted_value_fields"] == ["account_email", "authuser"]
    assert auth_tokens_policy["recursive_serialization_allowed"] is False


def test_json_envelope_rejects_secret_bearing_channel_reachability() -> None:
    with pytest.raises(ValueError, match="require a redacted adapter projection"):
        _validate_no_secret_channel_models(
            {"cli --json": {"notebooklm.types.Notebook", "notebooklm.auth.AuthTokens"}}
        )

    safe_projection: dict[str, object] = {
        "id": "mcp.AuthTokens.redacted-server-info-account-identity-contribution",
        "redacted_projection": "safe-field-contribution",
        "keys": ("server", "account"),
        "nested_keys": {"account": ("email", "authuser")},
        "model_contribution_keys": (
            "authuser",
            "account_email",
            "storage_path",
            "_profile_session_generation",
        ),
        "emitted_model_contribution_keys": ("authuser", "account_email"),
        "control_model_contribution_keys": (
            "storage_path",
            "_profile_session_generation",
        ),
    }
    _validate_secret_projection("notebooklm.auth.AuthTokens", safe_projection)
    with pytest.raises(ValueError, match="require a redacted adapter projection"):
        _validate_secret_projection(
            "notebooklm.auth.AuthTokens",
            {key: value for key, value in safe_projection.items() if key != "redacted_projection"},
        )
    for credential_key in (
        "cookies",
        "cookie_jar",
        "csrf_token",
        "authorization_header",
        "bearer_token",
        "storage_path",
        "_profile_session_generation",
    ):
        mutated = {
            **safe_projection,
            "nested_keys": {"account": ("email", "authuser", credential_key)},
        }
        with pytest.raises(ValueError, match="credential-bearing keys"):
            _validate_secret_projection("notebooklm.auth.AuthTokens", mutated)

    with pytest.raises(ValueError, match="reviewed projection ids"):
        _validate_secret_projection(
            "notebooklm.auth.AuthTokens",
            {**safe_projection, "id": "mcp.AuthTokens.second-safe-looking-projection"},
        )
    with pytest.raises(ValueError, match="safe emitted and control-only origins"):
        _validate_secret_projection(
            "notebooklm.auth.AuthTokens",
            {
                **safe_projection,
                "emitted_model_contribution_keys": ("authuser", "storage_path"),
                "control_model_contribution_keys": (
                    "account_email",
                    "_profile_session_generation",
                ),
            },
        )

    safe_channel_model = {"notebooklm.auth.AuthTokens": {"projections": [safe_projection]}}
    with pytest.raises(ValueError, match="reviewed channel/id pairs"):
        _validate_no_secret_channel_models({"cli --json": safe_channel_model})
    rest_safe_projection = {
        **safe_projection,
        "id": "rest.AuthTokens.redacted-server-info-account-identity-contribution",
    }
    with pytest.raises(ValueError, match="exactly the two reviewed adapter projections"):
        _validate_no_secret_channel_models(
            {
                "mcp tool result": {
                    "notebooklm.auth.AuthTokens": {
                        "projections": [safe_projection, dict(safe_projection)]
                    }
                },
                "rest response": {
                    "notebooklm.auth.AuthTokens": {"projections": [rest_safe_projection]}
                },
            }
        )

    source = """
from dataclasses import asdict
from notebooklm.auth import AuthTokens

def leak(client, explicit: AuthTokens):
    copied = client.auth
    to_jsonable(copied)
    asdict(explicit)
    to_jsonable({"credentials": [client.auth]})
"""
    assert _secret_serialization_violations(source, filename="mutation.py") == [
        "mutation.py:7",
        "mutation.py:8",
        "mutation.py:9",
    ]

    field_source = """
from notebooklm.auth import AuthTokens

def leak(tokens: AuthTokens):
    to_jsonable(tokens.cookies)
    to_jsonable({"email": tokens.storage_path})
    copied = {"nested": [tokens["csrf_token"]]}
    to_jsonable(copied)
    relabelled = tokens.cookie_jar
    return {"harmless_name": [relabelled]}
    to_jsonable({"email": tokens.account_email, "authuser": tokens.authuser})
"""
    assert _secret_serialization_violations(field_source, filename="fields.py") == [
        "fields.py:10",
        "fields.py:5",
        "fields.py:6",
        "fields.py:8",
    ]

    assigned_source = """
from notebooklm.auth import AuthTokens

def leak(tokens: AuthTokens):
    holder = object()
    holder.value = tokens.bearer_token
    values = []
    values.append(tokens.authorization_header)
    return {"renamed_holder": holder.value, "renamed_values": values}
"""
    assert _secret_serialization_violations(assigned_source, filename="assigned.py") == [
        "assigned.py:9"
    ]

    generic_return_source = """
from notebooklm.auth import AuthTokens

def identity(value):
    return value

def leak(tokens: AuthTokens):
    return identity(tokens.cookies)
"""
    assert _secret_serialization_violations(
        generic_return_source, filename="generic-return.py"
    ) == ["generic-return.py:8"]

    closure_source = """
from notebooklm.auth import AuthTokens

def register(tokens: AuthTokens):
    def handler():
        return {"renamed": tokens.cookies}
    return handler
"""
    assert _secret_serialization_violations(closure_source, filename="closure.py") == [
        "closure.py:6"
    ]

    mutator_source = """
from notebooklm.auth import AuthTokens

def leak(tokens: AuthTokens):
    first = {}
    first.setdefault("renamed", tokens.cookies)
    second = {}
    second.__setitem__("renamed", tokens.csrf_token)
    holder = object()
    setattr(holder, "renamed", tokens.session_id)
    return {"first": first, "second": second, "holder": holder.renamed}
"""
    assert _secret_serialization_violations(mutator_source, filename="mutators.py") == [
        "mutators.py:11"
    ]


def test_json_envelope_rejects_invalid_model_contribution_keys() -> None:
    from notebooklm.types import Artifact

    with pytest.raises(ValueError, match="non-empty string list"):
        _validate_model_contribution_keys(
            "notebooklm.types.Artifact", Artifact, (), identity="empty"
        )
    with pytest.raises(ValueError, match="duplicate model_contribution_keys"):
        _validate_model_contribution_keys(
            "notebooklm.types.Artifact", Artifact, ("id", "id"), identity="duplicate"
        )
    with pytest.raises(ValueError, match="unknown model_contribution_keys"):
        _validate_model_contribution_keys(
            "notebooklm.types.Artifact",
            Artifact,
            ("definitely_not_a_model_field",),
            identity="unknown",
        )

    @dataclasses.dataclass
    class FutureModel:
        value: str

        @property
        def unreviewed_alias(self) -> str:
            return self.value

    with pytest.raises(ValueError, match="unknown model_contribution_keys"):
        _validate_model_contribution_keys(
            "future.FutureModel",
            FutureModel,
            ("unreviewed_alias",),
            identity="unreviewed-property",
        )


def test_json_envelope_evidence_fingerprint_detects_adapter_shape_mutation() -> None:
    original = """
def sink(result):
    payload = {"id": result.id, "title": result.title}
    return payload
"""
    mutated = original.replace('"title": result.title', '"name": result.title')

    assert _evidence_ast_fingerprint(original, "payload =") != _evidence_ast_fingerprint(
        mutated, "payload ="
    )


def test_json_envelope_conditional_key_groups_preserve_cooccurrence_mutations() -> None:
    before = _normalize_conditional_key_groups(
        ({"condition": "inline", "keys": ("content", "char_count", "truncated")},)
    )
    after = _normalize_conditional_key_groups(
        ({"condition": "inline", "keys": ("content", "char_count")},)
    )

    assert before != after
    with pytest.raises(ValueError, match="duplicate conditional key group"):
        _normalize_conditional_key_groups(
            (
                {"condition": "inline", "keys": ("content",)},
                {"condition": "inline", "keys": ("char_count",)},
            )
        )


def test_json_envelope_allocates_every_live_projection_to_an_exact_terminal() -> None:
    contract = derive_json_envelope_contract()
    live_projection_ids = {
        projection["id"]
        for channel in contract["channels"].values()
        for model_row in channel.values()
        for projection in model_row["projections"]
    }
    reachability = contract["adapter_sink_reachability"]
    allocated_projection_ids = {
        projection_id
        for site in reachability["sites"]
        for projection_id in site["allocation"].get("projection_ids", [])
    }

    assert allocated_projection_ids == live_projection_ids
    assert reachability["site_count"] == 350
    private_paths = reachability["private_dataclass_projection_paths"]
    assert len(private_paths) == 34
    provider_auth_paths = {
        (path["private_model"], path["field_path"], path["public_model"])
        for path in private_paths
        if str(path["private_model"]).startswith("notebooklm._auth.web_provider_")
    }
    assert provider_auth_paths == {
        (
            "notebooklm._auth.web_provider_refresh.WebProviderRefresh",
            "auth",
            "notebooklm.auth.AuthTokens",
        ),
        (
            "notebooklm._auth.web_provider_storage.WebProviderBootstrap",
            "auth",
            "notebooklm.auth.AuthTokens",
        ),
    }
    assert all(
        path["allocation"]["unreachable_category"] == "internal-runtime-auth-capability"
        and "projection_ids" not in path["allocation"]
        and "terminal_locators" not in path["allocation"]
        for path in private_paths
        if str(path["private_model"]).startswith("notebooklm._auth.web_provider_")
    )
