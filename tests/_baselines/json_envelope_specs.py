"""Reviewed exact JSON envelope projection declarations.

Channel declarations live in focused modules; this module composes them, adds
stable semantic IDs, and exposes the sink-allocation ID inventory. Runtime and
AST shape derivation lives in ``tests._baselines.json_envelope_contracts``.
"""

from __future__ import annotations

from collections.abc import Mapping

from tests._baselines.json_envelope_cli_specs import CLI_PROJECTION_SPECS
from tests._baselines.json_envelope_mcp_specs import MCP_PROJECTION_SPECS
from tests._baselines.json_envelope_rest_specs import REST_PROJECTION_SPECS

_CHANNEL_PROJECTION_SPECS: dict[str, tuple[dict[str, object], ...]] = {
    "cli --json": CLI_PROJECTION_SPECS,
    "mcp tool result": MCP_PROJECTION_SPECS,
    "rest response": REST_PROJECTION_SPECS,
}


def _literal_dict_derive(path: str, function: str, contains: tuple[str, ...]) -> dict[str, object]:
    return {
        "kind": "ast-dict",
        "path": path,
        "function": function,
        "contains": contains,
    }


# These 37 projection declarations share 28 mechanically exact, literal final-dict sites. Their
# top-level keys are AST-derived; any separately declared nested/conditional semantics remain
# reviewed metadata. Together with 89 declarations that already had runtime/AST derivation, this
# leaves 168 transitive or conditional declarations honestly manual-reviewed. Every manual row is
# still coupled to the smallest semantic AST scope named by its evidence and serialized fingerprint.
_LITERAL_DICT_DERIVATIONS: dict[tuple[str, str, str], dict[str, object]] = {
    (
        "cli --json",
        "notebooklm.types.Artifact",
        "transitive-interactive-mind-map-generation-final-contribution",
    ): _literal_dict_derive(
        "notebooklm/cli/generate_cmd.py",
        "_output_mind_map_result",
        ("mind_map", "note_id", "kind"),
    ),
    (
        "cli --json",
        "notebooklm.types.Artifact",
        "transitive-download-no-artifacts-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/cli/services/download.py",
        "build_download_envelope",
        ("error", "suggestion"),
    ),
    (
        "cli --json",
        "notebooklm.types.Artifact",
        "transitive-download-all-dry-run-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/cli/services/download.py",
        "build_download_envelope",
        ("dry_run", "operation", "count", "output_dir", "artifacts"),
    ),
    (
        "cli --json",
        "notebooklm.types.Artifact",
        "transitive-download-single-dry-run-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/cli/services/download.py",
        "build_download_envelope",
        ("dry_run", "operation", "artifact", "output_path"),
    ),
    (
        "cli --json",
        "notebooklm.types.Artifact",
        "transitive-download-single-downloaded-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/cli/services/download.py",
        "build_download_envelope",
        ("operation", "artifact", "output_path", "status"),
    ),
    (
        "cli --json",
        "notebooklm.types.GenerationStatus",
        "manual-poll-projection",
    ): _literal_dict_derive(
        "notebooklm/cli/artifact_cmd.py",
        "artifact_poll",
        ("task_id", "status", "url", "error", "error_code", "metadata"),
    ),
    (
        "cli --json",
        "notebooklm.types.GenerationStatus",
        "manual-wait-projection",
    ): _literal_dict_derive(
        "notebooklm/cli/artifact_cmd.py",
        "artifact_retry",
        ("artifact_id", "status", "url", "error"),
    ),
    (
        "cli --json",
        "notebooklm.types.GenerationStatus",
        "manual-retry-kickoff-projection",
    ): _literal_dict_derive(
        "notebooklm/cli/artifact_cmd.py",
        "artifact_retry",
        ("task_id", "status", "url", "error", "error_code"),
    ),
    (
        "cli --json",
        "notebooklm.types.GenerationStatus",
        "manual-generation-completed-projection",
    ): _literal_dict_derive(
        "notebooklm/cli/generate_cmd.py",
        "_output_generation_outcome",
        ("task_id", "status", "url"),
    ),
    (
        "cli --json",
        "notebooklm.types.Note",
        "transitive-note-backed-mind-map-generation-final-contribution",
    ): _literal_dict_derive(
        "notebooklm/cli/generate_cmd.py",
        "_output_mind_map_result",
        ("mind_map", "note_id", "kind"),
    ),
    (
        "cli --json",
        "notebooklm.types.Note",
        "manual-create-projection",
    ): _literal_dict_derive(
        "notebooklm/cli/note_cmd.py",
        "note_create",
        ("id", "notebook_id", "title", "created"),
    ),
    (
        "cli --json",
        "notebooklm.types.Source",
        "transitive-wait-processing-error-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/cli/_source_render.py",
        "_render_source_wait_outcome",
        ("source_id", "status", "status_code", "error"),
    ),
    (
        "cli --json",
        "notebooklm.types.Source",
        "transitive-wait-timeout-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/cli/_source_render.py",
        "_render_source_wait_outcome",
        ("source_id", "status", "last_status_code", "timeout_seconds", "error"),
    ),
    (
        "cli --json",
        "notebooklm.types.Source",
        "transitive-research-import-new-source-projection",
    ): _literal_dict_derive("notebooklm/_research.py", "_imported_entry", ("id", "title")),
    (
        "cli --json",
        "notebooklm.types.Source",
        "transitive-research-import-existing-source-projection",
    ): _literal_dict_derive(
        "notebooklm/_research.py",
        "_project_import_verification",
        ("id", "title", "url"),
    ),
    (
        "cli --json",
        "notebooklm.types.SourceFulltext",
        "manual-field-projection",
    ): _literal_dict_derive(
        "notebooklm/cli/services/source_serializers.py",
        "source_fulltext_payload",
        ("source_id", "title", "kind", "content", "url", "char_count"),
    ),
    (
        "cli --json",
        "notebooklm.types.SourceFulltext",
        "manual-file-output-projection",
    ): _literal_dict_derive(
        "notebooklm/cli/_source_render.py",
        "_render_source_fulltext_result",
        ("path", "bytes", "source_id", "title", "kind"),
    ),
    (
        "cli --json",
        "notebooklm.types.SourceGuide",
        "manual-guide-projection",
    ): _literal_dict_derive(
        "notebooklm/cli/_source_render.py",
        "_render_source_guide_result",
        ("source_id", "summary", "keywords"),
    ),
    (
        "mcp tool result",
        "notebooklm.types.Artifact",
        "transitive-studio-rename-final-projection",
    ): _literal_dict_derive(
        "notebooklm/mcp/tools/_studio_payloads.py",
        "_artifact_rename_payload",
        ("status", "notebook_id", "item_id", "type", "new_title", "is_mind_map"),
    ),
    (
        "mcp tool result",
        "notebooklm.types.MindMap",
        "transitive-resolver-rename-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/mcp/tools/_studio_payloads.py",
        "_artifact_rename_payload",
        ("status", "notebook_id", "item_id", "type", "new_title", "is_mind_map"),
    ),
    (
        "mcp tool result",
        "notebooklm.types.Source",
        "transitive-batch-added-item-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/mcp/tools/sources.py",
        "_add_url_batch",
        ("status", "notebook_id", "added", "failed", "results"),
    ),
    (
        "mcp tool result",
        "notebooklm.types.Source",
        "transitive-delete-confirmation-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/mcp/_confirm.py", "needs_confirmation", ("status", "preview")
    ),
    (
        "mcp tool result",
        "notebooklm.types.Source",
        "transitive-research-import-new-source-projection",
    ): _literal_dict_derive("notebooklm/_research.py", "_imported_entry", ("id", "title")),
    (
        "mcp tool result",
        "notebooklm.types.Source",
        "transitive-research-import-existing-source-projection",
    ): _literal_dict_derive(
        "notebooklm/_research.py",
        "_project_import_verification",
        ("id", "title", "url"),
    ),
    (
        "rest response",
        "notebooklm.types.Artifact",
        "transitive-download-no-artifacts-409-error-contribution",
    ): _literal_dict_derive("notebooklm/server/_errors.py", "http_error_response", ("error",)),
    (
        "rest response",
        "notebooklm.types.GenerationStatus",
        "manual-retry-projection",
    ): _literal_dict_derive(
        "notebooklm/server/routes/artifacts.py",
        "retry",
        ("notebook_id", "artifact_id", "task_id", "status"),
    ),
    (
        "rest response",
        "notebooklm.types.GenerationStatus",
        "http-failed-409-error-envelope",
    ): _literal_dict_derive("notebooklm/server/_errors.py", "http_error_response", ("error",)),
    (
        "rest response",
        "notebooklm.types.GenerationStatus",
        "http-removed-410-error-envelope",
    ): _literal_dict_derive("notebooklm/server/_errors.py", "http_error_response", ("error",)),
    (
        "rest response",
        "notebooklm.types.Artifact",
        "transitive-mind-map-rename-membership-final-contribution",
    ): _literal_dict_derive(
        "notebooklm/server/routes/artifacts.py",
        "rename",
        ("status", "notebook_id", "artifact_id", "new_title", "is_mind_map"),
    ),
    (
        "rest response",
        "notebooklm.types.MindMap",
        "transitive-artifact-rename-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/server/routes/artifacts.py",
        "rename",
        ("status", "notebook_id", "artifact_id", "new_title", "is_mind_map"),
    ),
    (
        "rest response",
        "notebooklm.types.ResearchTask",
        "manual-status-projection",
    ): _literal_dict_derive(
        "notebooklm/server/routes/research.py",
        "research_status",
        (
            "notebook_id",
            "run_id",
            "task_id",
            "kind",
            "status",
            "status_code",
            "termination_reason",
            "reason_message",
            "hint",
            "discovery_mode",
            "created_at",
            "updated_at",
            "duration_seconds",
            "query",
            "sources",
            "summary",
            "report",
        ),
    ),
    (
        "rest response",
        "notebooklm.types.ResearchTask",
        "transitive-import-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/server/routes/research.py",
        "import_research",
        ("status", "notebook_id", "run_id", "imported", "sources_found"),
    ),
    (
        "rest response",
        "notebooklm.types.ResearchSource",
        "transitive-import-source-count-contribution",
    ): _literal_dict_derive(
        "notebooklm/server/routes/research.py",
        "import_research",
        ("status", "notebook_id", "run_id", "imported", "sources_found"),
    ),
    (
        "rest response",
        "notebooklm.types.Source",
        "transitive-batch-added-item-final-wrapper",
    ): _literal_dict_derive(
        "notebooklm/server/routes/sources.py",
        "add_batch",
        ("status", "notebook_id", "added", "failed", "results"),
    ),
    (
        "rest response",
        "notebooklm.types.Source",
        "transitive-source-content-readiness-contribution",
    ): _literal_dict_derive(
        "notebooklm/server/routes/sources.py",
        "get_source_content",
        ("notebook_id", "source_id", "content", "char_count", "truncated", "output_format"),
    ),
    (
        "rest response",
        "notebooklm.types.SourceFulltext",
        "manual-content-projection",
    ): _literal_dict_derive(
        "notebooklm/server/routes/sources.py",
        "get_source_content",
        ("notebook_id", "source_id", "content", "char_count", "truncated", "output_format"),
    ),
    (
        "rest response",
        "notebooklm.types.SourceGuide",
        "manual-guide-projection",
    ): _literal_dict_derive(
        "notebooklm/server/routes/sources.py",
        "get_source_content",
        ("notebook_id", "source_id", "summary", "keywords"),
    ),
}


def _projection_spec_id(channel: str, spec: Mapping[str, object]) -> str:
    """Return a stable, reviewable sink-allocation id for an explicit projection."""
    channel_id = {"cli --json": "cli", "mcp tool result": "mcp", "rest response": "rest"}[channel]
    model_id = str(spec["model"]).rsplit(".", 1)[-1]
    mode_id = "-".join(
        "".join(
            character.lower() if character.isalnum() else " " for character in str(spec["mode"])
        ).split()
    )
    return f"{channel_id}.{model_id}.{mode_id}"


_declared_projection_keys = {
    (channel, str(spec["model"]), str(spec["mode"]))
    for channel, specs in _CHANNEL_PROJECTION_SPECS.items()
    for spec in specs
}
if stale_literal_derivations := sorted(set(_LITERAL_DICT_DERIVATIONS) - _declared_projection_keys):
    raise ValueError(f"stale literal-dict projection derivations: {stale_literal_derivations}")
_explicitly_derived_projection_keys = {
    (channel, str(spec["model"]), str(spec["mode"]))
    for channel, specs in _CHANNEL_PROJECTION_SPECS.items()
    for spec in specs
    if "derive" in spec
}
if conflicting_literal_derivations := sorted(
    set(_LITERAL_DICT_DERIVATIONS) & _explicitly_derived_projection_keys
):
    raise ValueError(
        "literal-dict projection derivations conflict with declaration-local derivation: "
        f"{conflicting_literal_derivations}"
    )

_CHANNEL_PROJECTION_SPECS = {
    channel: tuple(
        {
            **spec,
            "id": spec.get("id", _projection_spec_id(channel, spec)),
            "derive": spec.get(
                "derive",
                _LITERAL_DICT_DERIVATIONS.get(
                    (channel, str(spec["model"]), str(spec["mode"])),
                    "manual-reviewed+fingerprint",
                ),
            ),
        }
        for spec in specs
    )
    for channel, specs in _CHANNEL_PROJECTION_SPECS.items()
}


def projection_spec_ids() -> dict[str, tuple[str, ...]]:
    """Return the stable explicit IDs available to sink-allocation audits."""
    return {
        channel: tuple(sorted(str(spec["id"]) for spec in specs))
        for channel, specs in _CHANNEL_PROJECTION_SPECS.items()
    }


__all__ = ["projection_spec_ids"]
