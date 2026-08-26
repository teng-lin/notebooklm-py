"""Reviewed CLI ``--json`` public-model projection declarations."""

from __future__ import annotations

_ASK_ROOT_KEYS = (
    "answer",
    "conversation_id",
    "turn_number",
    "is_follow_up",
    "references",
    "turn_key",
    "next_steps",
)
_FULL_REFERENCE_KEYS = (
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
)

CLI_PROJECTION_SPECS: tuple[dict[str, object], ...] = (
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-list-row-projection",
        "keys": (
            "index",
            "id",
            "title",
            "type",
            "type_id",
            "status",
            "status_id",
            "created_at",
        ),
        "evidence": ("notebooklm/cli/artifact_cmd.py:artifact_list",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-list-final-wrapper",
        "keys": ("notebook_id", "notebook_title", "artifacts", "count"),
        "nested_keys": {
            "artifacts": (
                "index",
                "id",
                "title",
                "type",
                "type_id",
                "status",
                "status_id",
                "created_at",
            )
        },
        "evidence": (
            'notebooklm/cli/artifact_cmd.py:items_key="artifacts"',
            "notebooklm/cli/services/listing.py:def prepare_list",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-get-projection",
        "keys": (
            "notebook_id",
            "id",
            "title",
            "type",
            "type_id",
            "status",
            "status_id",
            "created_at",
            "found",
        ),
        "evidence": ("notebooklm/cli/artifact_cmd.py:artifact_get",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-interactive-mind-map-generation-final-contribution",
        "keys": ("mind_map", "note_id", "kind"),
        "model_contribution_keys": ("id",),
        "projection_condition": (
            "interactive mind-map generation finds the newly created public Artifact"
        ),
        "contribution_semantics": (
            "Artifact.id constructs MindMap.id and is emitted as note_id; the raw create-id "
            "fallback emits the same wrapper without a public Artifact contribution"
        ),
        "evidence": (
            "notebooklm/_studio/mind_maps.py:artifact = project_artifact(record)",
            "notebooklm/_studio/mind_maps.py:return MindMap(",
            "notebooklm/cli/generate_cmd.py:_output_mind_map_result",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-selected-artifact-projection",
        "evidence": ("notebooklm/_app/download.py:selected_envelope",),
        "derive": "runtime:artifact-download-selected",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-error-artifact-projection",
        "evidence": ("notebooklm/_app/download.py:artifact=dict(selected)",),
        "derive": "runtime:artifact-download-error",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-dry-all-item-projection",
        "evidence": ("notebooklm/_app/download.py:outcome=DownloadOutcome.ALL_DRY_RUN",),
        "derive": "runtime:artifact-download-dry-all",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-executed-item-projection",
        "evidence": ("notebooklm/_app/download.py:artifacts_results.append",),
        "derive": "runtime:artifact-download-executed",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-no-artifacts-final-wrapper",
        "keys": ("error", "suggestion"),
        "model_contribution_keys": ("_artifact_type", "status", "_variant"),
        "projection_condition": (
            "the Artifact listing is non-empty but every public Artifact is excluded by "
            "the requested kind or completed-status filters"
        ),
        "contribution_semantics": (
            "Artifact.kind (backed by _artifact_type and _variant) and Artifact.status "
            "(through is_completed) select the NO_ARTIFACTS envelope; a truly empty "
            "listing reaches the same envelope without any public Artifact instance and "
            "is non-public"
        ),
        "evidence": ("notebooklm/cli/services/download.py:DownloadOutcome.NO_ARTIFACTS",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-error-final-wrapper",
        "keys": ("error",),
        "optional_keys": ("artifact", "suggestion"),
        "nested_keys": {"artifact": ("id", "title", "created_at")},
        "evidence": ("notebooklm/cli/services/download.py:DownloadOutcome.ERROR",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-all-dry-run-final-wrapper",
        "keys": ("dry_run", "operation", "count", "output_dir", "artifacts"),
        "nested_keys": {"artifacts": ("id", "title", "filename")},
        "evidence": ("notebooklm/cli/services/download.py:DownloadOutcome.ALL_DRY_RUN",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-all-executed-final-wrapper",
        "keys": (
            "operation",
            "output_dir",
            "total",
            "succeeded_count",
            "failed_count",
            "skipped_count",
            "artifacts",
        ),
        "optional_keys": ("error",),
        "nested_keys": {"artifacts": ("id", "title", "filename", "status")},
        "evidence": ("notebooklm/cli/services/download.py:DownloadOutcome.ALL_EXECUTED",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-single-dry-run-final-wrapper",
        "keys": ("dry_run", "operation", "artifact", "output_path"),
        "nested_keys": {"artifact": ("id", "title", "selection_reason")},
        "evidence": ("notebooklm/cli/services/download.py:DownloadOutcome.SINGLE_DRY_RUN",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-single-downloaded-final-wrapper",
        "keys": ("operation", "artifact", "output_path", "status"),
        "nested_keys": {"artifact": ("id", "title", "selection_reason")},
        "evidence": ("notebooklm/cli/services/download.py:# SINGLE_DOWNLOADED",),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-poll-projection",
        "keys": ("task_id", "status", "url", "error", "error_code", "metadata"),
        "evidence": ("notebooklm/cli/artifact_cmd.py:artifact_poll",),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-wait-projection",
        "keys": ("artifact_id", "status", "url", "error"),
        "evidence": (
            "notebooklm/cli/artifact_cmd.py:artifact_wait",
            "notebooklm/cli/artifact_cmd.py:artifact_retry",
        ),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-retry-kickoff-projection",
        "keys": ("task_id", "status", "url", "error", "error_code"),
        "evidence": ("notebooklm/cli/artifact_cmd.py:if not wait",),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "transitive-retry-timeout-task-id-contribution",
        "keys": ("artifact_id", "status", "error"),
        "model_contribution_keys": ("task_id",),
        "projection_condition": "blocking artifact retry times out after kickoff",
        "contribution_semantics": (
            "The fresh GenerationStatus.task_id is renamed to artifact_id; timeout status and "
            "error text are CLI-owned scalars"
        ),
        "evidence": (
            "notebooklm/cli/artifact_cmd.py:except TimeoutError",
            'notebooklm/cli/artifact_cmd.py:"artifact_id": status.task_id',
        ),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-generation-completed-projection",
        "keys": ("task_id", "status", "url"),
        "evidence": (
            "notebooklm/_app/generate_retry.py:generation_outcome_from_status",
            "notebooklm/cli/generate_cmd.py:_output_generation_outcome",
        ),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-generation-pending-projection",
        "keys": ("task_id", "status"),
        "evidence": (
            "notebooklm/_app/generate_retry.py:generation_outcome_from_status",
            "notebooklm/cli/generate_cmd.py:_output_generation_outcome",
        ),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-generation-failure-envelope",
        "keys": ("error", "code", "message"),
        "evidence": (
            "notebooklm/_app/generate_retry.py:generation_outcome_from_status",
            "notebooklm/cli/generate_cmd.py:_output_generation_outcome",
            "notebooklm/cli/error_handler.py:response: dict =",
        ),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "nested-timeout-transition-projection",
        "keys": (
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
        ),
        "nested_keys": {
            "status_transitions": (
                "task_id",
                "status",
                "url",
                "error",
                "error_code",
                "metadata",
            )
        },
        "model_contribution_keys": (
            "task_id",
            "status",
            "url",
            "error",
            "error_code",
            "metadata",
        ),
        "projection_condition": (
            "ArtifactTimeoutError.status_transitions contains at least one public GenerationStatus"
        ),
        "contribution_semantics": (
            "Each GenerationStatus supplies the six nested status_transitions fields; an "
            "empty transition collection emits the same root without a public-model contribution"
        ),
        "evidence": ("notebooklm/cli/error_handler.py:_generation_status_extra",),
    },
    {
        "model": "notebooklm.types.AskResult",
        "mode": "app-view:cli-final-with-note-outcome",
        "evidence": (
            "notebooklm/_app/views.py:ask_result_view",
            "notebooklm/cli/chat_cmd.py:data = ask_result_view",
        ),
        "derive": "runtime:ask-result-cli-final",
        "nested_fields": ("references", "turn_key", "next_steps"),
    },
    {
        "model": "notebooklm._types.documents.StructuredDocument",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": ("note", "note_save_error"),
        "nested_keys": {"references": _FULL_REFERENCE_KEYS, "note": ("id", "title")},
        "model_contribution_keys": ("blocks", "annotations"),
        "projection_condition": (
            "the answer document contains an in-range annotation whose object_id matches "
            "a surviving ChatReference.chunk_id"
        ),
        "contribution_semantics": (
            "StructuredDocument.annotations selects the matching anchor and text/extent "
            "derived from blocks validates its range, producing answer_anchor_start/end; "
            "answer_document itself is removed from the CLI envelope"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/cli/chat_cmd.py:data = ask_result_view(result)",
        ),
    },
    {
        "model": "notebooklm._types.documents.DocumentAnnotation",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": ("note", "note_save_error"),
        "nested_keys": {"references": _FULL_REFERENCE_KEYS, "note": ("id", "title")},
        "model_contribution_keys": ("object_id", "start_index", "end_index"),
        "projection_condition": (
            "an in-range DocumentAnnotation.object_id matches a surviving ChatReference.chunk_id"
        ),
        "contribution_semantics": (
            "DocumentAnnotation object_id joins the citation and its offsets become "
            "answer_anchor_start/end"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/cli/chat_cmd.py:data = ask_result_view(result)",
        ),
    },
    {
        "model": "notebooklm._types.documents.DocumentBlock",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": ("note", "note_save_error"),
        "nested_keys": {"references": _FULL_REFERENCE_KEYS, "note": ("id", "title")},
        "model_contribution_keys": ("start_index", "end_index", "spans"),
        "projection_condition": (
            "a decoded citation fragment contains a usable DocumentBlock, or an accepted "
            "answer anchor depends on the answer document's block extent/text"
        ),
        "contribution_semantics": (
            "DocumentBlock ranges produce fragment_start/end and its span-derived text "
            "produces cited_text; answer-document blocks also bound anchor validity"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def extract_text_passages",
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/cli/chat_cmd.py:data = ask_result_view(result)",
        ),
    },
    {
        "model": "notebooklm._types.documents.TextSpan",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": ("note", "note_save_error"),
        "nested_keys": {"references": _FULL_REFERENCE_KEYS, "note": ("id", "title")},
        "model_contribution_keys": ("start_index", "end_index", "text"),
        "projection_condition": (
            "a decoded citation fragment contains a usable TextSpan, or an accepted answer "
            "anchor depends on answer-document text built from a TextSpan"
        ),
        "contribution_semantics": (
            "TextSpan offsets/text build DocumentBlock.text and StructuredDocument.text, "
            "thereby producing cited_text/ranges or validating answer anchors"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def extract_text_passages",
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/cli/chat_cmd.py:data = ask_result_view(result)",
        ),
    },
    {
        "model": "notebooklm.types.CitedSourceSelection",
        "mode": "manual-completed-wait-projection",
        "keys": ("status", "query", "sources_found", "sources", "report"),
        "optional_keys": (
            "cited_only",
            "cited_sources_selected",
            "cited_only_fallback",
            "imported",
            "imported_sources",
        ),
        "nested_keys": {"imported_sources": ("id", "title")},
        "model_contribution_keys": ("sources", "used_fallback"),
        "projection_condition": (
            "research wait --import-all completes with a CitedSourceSelection"
        ),
        "contribution_semantics": (
            "len(CitedSourceSelection.sources) becomes cited_sources_selected and "
            "used_fallback becomes cited_only_fallback; cited URL diagnostic counts do not "
            "reach JSON"
        ),
        "evidence": ("notebooklm/cli/research_cmd.py:_completed_wait_payload",),
    },
    {
        "model": "notebooklm.types.CitedSourceSelection",
        "mode": "manual-direct-import-projection",
        "keys": (
            "status",
            "run_id",
            "sources_found",
            "sources_selected",
            "imported",
            "imported_sources",
            "already_present",
            "already_present_sources",
        ),
        "optional_keys": ("cited_only", "cited_only_fallback"),
        "nested_keys": {
            "imported_sources": ("id", "title"),
            "already_present_sources": ("id", "title", "url"),
        },
        "model_contribution_keys": ("sources", "used_fallback"),
        "projection_condition": "research import performs cited-only selection",
        "contribution_semantics": (
            "len(CitedSourceSelection.sources) becomes sources_selected and used_fallback "
            "becomes cited_only_fallback; cited URL diagnostic counts do not reach JSON"
        ),
        "evidence": ("notebooklm/cli/research_cmd.py:_render_import_result",),
    },
    {
        "model": "notebooklm.types.CitedSourceSelection",
        "mode": "transitive-source-add-research-completed-projection",
        "keys": ("status", "task_id", "sources_found", "sources", "report"),
        "optional_keys": (
            "cited_only",
            "cited_sources_selected",
            "cited_only_fallback",
            "imported",
            "imported_sources",
        ),
        "nested_keys": {"imported_sources": ("id", "title")},
        "model_contribution_keys": ("sources", "used_fallback"),
        "projection_condition": (
            "source add-research completes and creates a CitedSourceSelection"
        ),
        "contribution_semantics": (
            "len(CitedSourceSelection.sources) becomes cited_sources_selected and "
            "used_fallback becomes cited_only_fallback"
        ),
        "evidence": (
            "notebooklm/_app/source_research.py:def cited_selection",
            "notebooklm/cli/_source_render.py:completed_payload",
        ),
    },
    {
        "model": "notebooklm.types.Collection",
        "mode": "manual-mutation-projection",
        "evidence": ("notebooklm/cli/collection_cmd.py:_collection_payload",),
        "derive": "runtime:cli-collection-mutation",
    },
    {
        "model": "notebooklm.types.Collection",
        "mode": "manual-add-membership-final-projection",
        "evidence": ("notebooklm/cli/collection_cmd.py:added_notebook_ids",),
        "derive": "runtime:cli-collection-add",
    },
    {
        "model": "notebooklm.types.Collection",
        "mode": "manual:remove-membership-final-projection",
        "evidence": ("notebooklm/cli/collection_cmd.py:removed_notebook_ids",),
        "derive": "runtime:cli-collection-remove",
    },
    {
        "model": "notebooklm.types.Collection",
        "mode": "manual-list-row-projection",
        "keys": ("index", "id", "name", "emoji", "notebook_count"),
        "evidence": ("notebooklm/cli/collection_cmd.py:collection_list",),
    },
    {
        "model": "notebooklm.types.Collection",
        "mode": "manual-list-final-wrapper",
        "keys": ("collections", "count"),
        "nested_keys": {"collections": ("index", "id", "name", "emoji", "notebook_count")},
        "evidence": (
            'notebooklm/cli/collection_cmd.py:items_key="collections"',
            "notebooklm/cli/services/listing.py:def prepare_list",
        ),
    },
    {
        "model": "notebooklm.types.Collection",
        "mode": "resolver-ambiguous-id-error-wrapper",
        "keys": ("error", "code", "message", "id", "candidates"),
        "nested_keys": {"candidates": ("id", "emoji", "notebook_count")},
        "model_contribution_keys": ("id", "emoji", "notebook_ids"),
        "evidence": (
            "notebooklm/_app/collections.py:_candidate_payload(prefix_matches)",
            "notebooklm/cli/collection_cmd.py:def _handle_collection_resolution_error",
        ),
    },
    {
        "model": "notebooklm.types.Collection",
        "mode": "resolver-ambiguous-name-error-wrapper",
        "keys": ("error", "code", "message", "name", "candidates"),
        "nested_keys": {"candidates": ("id", "emoji", "notebook_count")},
        "model_contribution_keys": ("id", "emoji", "notebook_ids"),
        "evidence": (
            "notebooklm/_app/collections.py:_candidate_payload(name_matches)",
            "notebooklm/cli/collection_cmd.py:def _handle_collection_resolution_error",
        ),
    },
    {
        "model": "notebooklm.types.Collection",
        "mode": "resolver-near-miss-error-wrapper",
        "keys": ("error", "code", "message", "id", "candidates"),
        "nested_keys": {"candidates": ("id", "title")},
        "model_contribution_keys": ("id", "name"),
        "evidence": (
            "notebooklm/_app/collections.py:candidates = near_miss_candidates",
            "notebooklm/cli/collection_cmd.py:def _handle_collection_resolution_error",
        ),
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "manual-list-projection",
        "evidence": ("notebooklm/cli/services/label_listing.py:_label_serialize",),
        "derive": "runtime:cli-label-list",
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "manual-list-final-wrapper",
        "keys": ("labels", "count"),
        "nested_keys": {
            "labels": ("id", "name", "emoji", "source_ids", "sources"),
            "labels[].sources": ("id", "title"),
        },
        "evidence": (
            "notebooklm/cli/services/label_listing.py:async def execute_label_list",
            "notebooklm/cli/services/listing.py:envelope =",
        ),
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "resolver-ambiguous-id-error-wrapper",
        "keys": ("error", "code", "message", "id", "candidates"),
        "nested_keys": {"candidates": ("id", "emoji", "source_count")},
        "model_contribution_keys": ("id", "emoji", "source_ids"),
        "evidence": (
            "notebooklm/_app/labels.py:_candidate_payload(prefix_matches)",
            "notebooklm/cli/label_cmd.py:def _handle_label_resolution_error",
        ),
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "resolver-ambiguous-name-error-wrapper",
        "keys": ("error", "code", "message", "name", "candidates"),
        "nested_keys": {"candidates": ("id", "emoji", "source_count")},
        "model_contribution_keys": ("id", "emoji", "source_ids"),
        "evidence": (
            "notebooklm/_app/labels.py:_candidate_payload(name_matches)",
            "notebooklm/cli/label_cmd.py:def _handle_label_resolution_error",
        ),
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "resolver-near-miss-error-wrapper",
        "keys": ("error", "code", "message", "id", "notebook_id", "candidates"),
        "nested_keys": {"candidates": ("id", "title")},
        "model_contribution_keys": ("id", "name"),
        "evidence": (
            "notebooklm/_app/labels.py:candidates = near_miss_candidates",
            "notebooklm/cli/label_cmd.py:def _handle_label_resolution_error",
        ),
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "manual-mutation-projection",
        "evidence": ("notebooklm/cli/label_cmd.py:_label_payload",),
        "derive": "runtime:cli-label-payload",
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "manual-create-rename-emoji-final-projection",
        "evidence": (
            "notebooklm/cli/label_cmd.py:label_create",
            "notebooklm/cli/label_cmd.py:label_rename",
            "notebooklm/cli/label_cmd.py:label_emoji",
        ),
        "derive": "runtime:cli-label-final-command",
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "manual-generate-final-projection",
        "evidence": ("notebooklm/cli/label_cmd.py:label_generate",),
        "derive": "runtime:cli-label-final-generate",
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "manual-add-final-projection",
        "evidence": ("notebooklm/cli/label_cmd.py:label_add",),
        "derive": "runtime:cli-label-final-add",
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "manual-remove-final-projection",
        "evidence": ("notebooklm/cli/label_cmd.py:label_remove",),
        "derive": "runtime:cli-label-final-remove",
    },
    {
        "model": "notebooklm.types.MindMap",
        "mode": "transitive-cli-final-tree-wrapper",
        "evidence": ("notebooklm/cli/generate_cmd.py:_output_mind_map_result",),
        "derive": "runtime:mind-map-cli-final",
    },
    {
        "model": "notebooklm.types.MindMapResult",
        "mode": "transitive-cli-final-tree-wrapper",
        "evidence": ("notebooklm/cli/generate_cmd.py:_output_mind_map_result",),
        "derive": "runtime:mind-map-cli-final",
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "transitive-note-backed-mind-map-generation-final-contribution",
        "keys": ("mind_map", "note_id", "kind"),
        "model_contribution_keys": ("id",),
        "projection_condition": (
            "note-backed mind-map generation returns a present leaf and successfully "
            "creates a public Note"
        ),
        "contribution_semantics": (
            "The created Note.id is copied into MindMapResult.note_id and emitted as "
            "note_id; interactive or absent-leaf paths have no Note contribution"
        ),
        "evidence": (
            "notebooklm/_studio/data_views.py:note = await self._notes.create_note_record",
            "notebooklm/cli/generate_cmd.py:_output_mind_map_result",
        ),
    },
    {
        "model": "notebooklm.types.MindMap",
        "mode": "transitive-artifact-delete-final-carveout",
        "keys": ("id", "deleted"),
        "conditional_key_groups": (
            {"condition": "note-backed mind map", "keys": ("kind", "note")},
        ),
        "model_contribution_keys": ("id",),
        "projection_condition": "list_note_backed returns a MindMap whose id matches the target",
        "contribution_semantics": (
            "MindMap.id membership selects the note-backed delete branch and adds kind/note; a "
            "regular or missing full-id artifact uses the same terminal without a public MindMap"
        ),
        "evidence": (
            "notebooklm/_app/artifacts.py:async def delete_artifact",
            "notebooklm/cli/artifact_cmd.py:def serialize_success",
        ),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-list-row-projection",
        "keys": ("id", "title", "preview"),
        "evidence": ("notebooklm/cli/note_cmd.py:note_list",),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-list-final-wrapper",
        "keys": ("notebook_id", "notes", "count"),
        "nested_keys": {"notes": ("id", "title", "preview")},
        "evidence": (
            "notebooklm/cli/note_cmd.py:def note_list",
            "notebooklm/cli/services/listing.py:envelope =",
        ),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "dataclass-get-projection",
        "keys": ("id", "notebook_id", "title", "content", "created_at", "found"),
        "evidence": ("notebooklm/cli/note_cmd.py:payload = asdict",),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-create-projection",
        "keys": ("id", "notebook_id", "title", "created"),
        "evidence": ("notebooklm/cli/note_cmd.py:note_create",),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "transitive-chat-save-note-projection",
        "evidence": (
            'notebooklm/_app/chat.py:note={"id": note.id, "title": note.title}',
            'notebooklm/cli/chat_cmd.py:data["note"] = note_save_result',
        ),
        "derive": "runtime:cli-note-chat-save",
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "transitive-history-save-note-final-wrapper",
        "evidence": (
            "notebooklm/cli/chat_cmd.py:def _history_json_payload",
            'notebooklm/cli/chat_cmd.py:"note": {"id": note.id',
        ),
        "derive": "runtime:cli-note-history-save",
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "manual-list-row-projection",
        "keys": (
            "index",
            "id",
            "title",
            "is_owner",
            "role",
            "created_at",
            "last_viewed_at",
            "modified_at",
        ),
        "evidence": ("notebooklm/cli/notebook_cmd.py:notebook_viewed_keys",),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "manual-list-final-wrapper",
        "keys": ("notebooks", "count"),
        "nested_keys": {
            "notebooks": (
                "index",
                "id",
                "title",
                "is_owner",
                "role",
                "created_at",
                "last_viewed_at",
                "modified_at",
            )
        },
        "evidence": (
            'notebooklm/cli/notebook_cmd.py:items_key="notebooks"',
            "notebooklm/cli/services/listing.py:def prepare_list",
        ),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "manual-session-use-projection",
        "evidence": ("notebooklm/cli/session_cmd.py:use_notebook",),
        "derive": {
            "kind": "ast-dict",
            "path": "notebooklm/cli/session_cmd.py",
            "function": "use_notebook",
            "contains": ("notebook",),
            "nested": ("notebook",),
            "nested_spreads": {"notebook": "notebook-viewed-keys"},
        },
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "manual-create-projection",
        "keys": ("notebook",),
        "nested_keys": {
            "notebook": (
                "id",
                "title",
                "role",
                "created_at",
                "last_viewed_at",
                "modified_at",
            )
        },
        "evidence": ("notebooklm/cli/notebook_cmd.py:create_cmd",),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "manual-create-and-use-projection",
        "keys": ("notebook", "active_notebook_id"),
        "nested_keys": {
            "notebook": (
                "id",
                "title",
                "role",
                "created_at",
                "last_viewed_at",
                "modified_at",
            )
        },
        "evidence": ("notebooklm/cli/notebook_cmd.py:active_notebook_id",),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "manual-collection-member-projection",
        "keys": ("id", "title"),
        "evidence": ("notebooklm/cli/collection_cmd.py:collection_notebooks",),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "transitive-metadata-flattened-projection",
        "evidence": (
            "notebooklm/_types/notebooks.py:class NotebookMetadata",
            "notebooklm/cli/notebook_cmd.py:metadata.to_dict",
        ),
        "derive": "runtime:cli-notebook-metadata-flattened",
    },
    {
        "model": "notebooklm.types.NotebookMetadata",
        "mode": "manual-to-dict-projection",
        "model_contribution_keys": ("notebook", "sources"),
        "evidence": ("notebooklm/cli/notebook_cmd.py:metadata.to_dict",),
        "derive": "runtime:cli-notebook-metadata-root",
    },
    {
        "model": "notebooklm.types.NotebookDescription",
        "mode": "manual-summary-projection",
        "keys": ("notebook_id", "summary"),
        "model_contribution_keys": ("summary",),
        "projection_condition": "execute_notebook_describe returns a NotebookDescription",
        "contribution_semantics": (
            "NotebookDescription.summary populates the summary value; when description is "
            "None the CLI emits the same root keys with summary null and no public-model "
            "contribution"
        ),
        "evidence": ("notebooklm/cli/notebook_cmd.py:summary_cmd",),
    },
    {
        "model": "notebooklm.types.NotebookDescription",
        "mode": "manual-summary-with-topics-projection",
        "keys": ("notebook_id", "summary", "suggested_topics"),
        "model_contribution_keys": ("summary", "suggested_topics"),
        "projection_condition": (
            "--topics is requested and execute_notebook_describe returns a NotebookDescription"
        ),
        "contribution_semantics": (
            "NotebookDescription supplies summary and the suggested_topics collection; "
            "description None still emits summary null and an empty topics list without a "
            "public-model contribution"
        ),
        "evidence": ("notebooklm/cli/notebook_cmd.py:suggested_topics",),
    },
    {
        "model": "notebooklm.types.PromptSuggestion",
        "mode": "manual-cli-final-wrapper",
        "evidence": ("notebooklm/cli/chat_cmd.py:suggestions = await",),
        "derive": "runtime:prompt-suggestion-cli",
    },
    {
        "model": "notebooklm.types.ReportSuggestion",
        "mode": "manual-field-projection",
        "keys": ("title", "description", "prompt"),
        "evidence": ("notebooklm/cli/artifact_cmd.py:artifact_suggestions",),
    },
    {
        "model": "notebooklm.types.ResearchStart",
        "mode": "transitive-source-add-research-no-wait-projection",
        "model_contribution_keys": ("task_id", "report_id"),
        "evidence": (
            "notebooklm/_app/source_research.py:start_task_id = result.task_id",
            "notebooklm/cli/_source_render.py:payload: dict[str, Any] =",
        ),
        "derive": {
            "kind": "ast-mapping-variable",
            "path": "notebooklm/cli/_source_render.py",
            "function": "_render_add_research_result",
            "variable": "payload",
            "contains": ("task_id",),
        },
    },
    {
        "model": "notebooklm.types.ResearchStart",
        "mode": "transitive-source-add-research-completed-projection",
        "model_contribution_keys": ("task_id", "report_id"),
        "evidence": (
            "notebooklm/_app/source_research.py:start_task_id = result.task_id",
            "notebooklm/cli/_source_render.py:completed_payload: dict[str, Any] =",
        ),
        "derive": {
            "kind": "ast-mapping-variable",
            "path": "notebooklm/cli/_source_render.py",
            "function": "_render_add_research_result",
            "variable": "completed_payload",
            "contains": ("sources_found",),
        },
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "manual-empty-status-projection",
        "evidence": ("notebooklm/cli/research_cmd.py:result.public_dict",),
        "derive": "runtime:research-task-public-dict-empty",
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "manual-status-projection",
        "evidence": ("notebooklm/cli/research_cmd.py:result.public_dict",),
        "derive": "runtime:research-task-public-dict",
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-wait-completed-final-projection",
        "keys": ("status", "query", "sources_found", "sources", "report"),
        "optional_keys": (
            "cited_only",
            "cited_sources_selected",
            "cited_only_fallback",
            "imported",
            "imported_sources",
        ),
        "nested_keys": {"imported_sources": ("id", "title")},
        "model_contribution_keys": ("task_id", "status", "query", "sources", "report"),
        "evidence": ("notebooklm/cli/research_cmd.py:_completed_wait_payload",),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-wait-failed-final-projection",
        "keys": ("status", "error"),
        "optional_keys": (
            "query",
            "sources",
            "sources_found",
            "report",
            "reason_message",
            "hint",
        ),
        "model_contribution_keys": (
            "status",
            "status_code",
            "source_type",
            "query",
            "sources",
            "report",
        ),
        "evidence": ("notebooklm/cli/research_cmd.py:failed_payload",),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-wait-no-research-branch-contribution",
        "keys": ("status", "error"),
        "model_contribution_keys": ("status",),
        "projection_condition": "ResearchTask.status selects the no_research branch",
        "contribution_semantics": (
            "The public status selects this fixed error envelope; the timeout branch has no "
            "ResearchTask instance and is intentionally excluded"
        ),
        "evidence": (
            'notebooklm/_app/research.py:if status_val == "no_research"',
            'notebooklm/cli/research_cmd.py:if result.outcome == "no_research"',
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-import-refusal-error-contribution",
        "keys": ("error", "code", "message"),
        "model_contribution_keys": (
            "task_id",
            "status",
            "status_code",
            "source_type",
            "query",
            "sources",
        ),
        "projection_condition": (
            "omitted run-id with an absent task_id, or import classification refuses not_found, "
            "failed, noncompleted, or completed-without-sources ResearchTask state"
        ),
        "contribution_semantics": (
            "ResearchTask fields select and populate the ValidationError message; completed-empty "
            "uses only source-list membership and no ResearchSource field is projected"
        ),
        "evidence": (
            "notebooklm/cli/research_cmd.py:resolved_run_id = run_id or status.task_id",
            "notebooklm/cli/research_cmd.py:sources, report = classify_importable_research",
            "notebooklm/_app/research.py:def classify_importable_research",
            "notebooklm/cli/error_handler.py:response: dict =",
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-import-success-final-wrapper",
        "keys": (
            "status",
            "run_id",
            "sources_found",
            "sources_selected",
            "imported",
            "imported_sources",
            "already_present",
            "already_present_sources",
        ),
        "optional_keys": ("cited_only", "cited_only_fallback"),
        "nested_keys": {
            "imported_sources": ("id", "title"),
            "already_present_sources": ("id", "title", "url"),
        },
        "model_contribution_keys": ("task_id", "status", "sources", "report"),
        "projection_condition": "research import classifies a public ResearchTask as importable",
        "contribution_semantics": (
            "task_id supplies run_id when --run-id is omitted, status selects success, source "
            "membership supplies counts, and report affects cited-only selection"
        ),
        "evidence": (
            "notebooklm/cli/research_cmd.py:resolved_run_id = run_id or status.task_id",
            "notebooklm/cli/research_cmd.py:sources, report = classify_importable_research",
            "notebooklm/cli/research_cmd.py:_render_import_result",
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-source-add-research-completed-final-projection",
        "keys": ("status", "task_id", "sources_found", "sources", "report"),
        "optional_keys": (
            "cited_only",
            "cited_sources_selected",
            "cited_only_fallback",
            "imported",
            "imported_sources",
        ),
        "nested_keys": {"imported_sources": ("id", "title")},
        "model_contribution_keys": ("status", "sources", "report"),
        "evidence": (
            "notebooklm/_app/source_research.py:sources = [src.to_public_dict()",
            "notebooklm/cli/_source_render.py:completed_payload",
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-source-add-research-failure-final-projection",
        "keys": ("status", "error"),
        "optional_keys": ("reason_message", "hint", "raw_status"),
        "model_contribution_keys": (
            "status",
            "status_code",
            "source_type",
            "query",
            "sources",
        ),
        "evidence": (
            "notebooklm/_app/source_research.py:status_val = status.status.value",
            "notebooklm/cli/_source_render.py:_exit_with_add_research_status",
        ),
    },
    {
        "model": "notebooklm.types.ResearchSource",
        "mode": "nested-to-public-dict-projection",
        "model_contribution_keys": (
            "url",
            "title",
            "result_type",
            "research_task_id",
            "report_markdown",
            "source_ordinal",
            "hint",
        ),
        "projection_condition": (
            "a research status/wait/source-add result contains at least one ResearchSource"
        ),
        "contribution_semantics": (
            "ResearchSource.to_public_dict supplies required url/title/result_type plus its "
            "conditional compatibility fields to emitted sources rows"
        ),
        "evidence": (
            "notebooklm/_types/research.py:to_public_dict",
            "notebooklm/cli/research_cmd.py:result.sources",
            "notebooklm/cli/_source_render.py:completed_payload",
        ),
        "derive": "runtime:research-source-public-dict",
    },
    {
        "model": "notebooklm.types.ResearchSource",
        "mode": "transitive-wait-completed-final-wrapper",
        "keys": ("status", "query", "sources_found", "sources", "report"),
        "optional_keys": (
            "cited_only",
            "cited_sources_selected",
            "cited_only_fallback",
            "imported",
            "imported_sources",
        ),
        "nested_keys": {"sources": ("url", "title", "result_type")},
        "nested_optional_keys": {
            "sources": ("research_task_id", "report_markdown", "source_ordinal", "hint")
        },
        "model_contribution_keys": (
            "url",
            "title",
            "result_type",
            "research_task_id",
            "report_markdown",
            "source_ordinal",
            "hint",
        ),
        "projection_condition": "completed research wait contains at least one ResearchSource",
        "contribution_semantics": (
            "Every ResearchSource public-dict field reaches sources[]; membership also supplies "
            "sources_found"
        ),
        "evidence": (
            "notebooklm/_app/research.py:sources=[src.to_public_dict() for src in status.sources]",
            "notebooklm/cli/research_cmd.py:_completed_wait_payload",
        ),
    },
    {
        "model": "notebooklm.types.ResearchSource",
        "mode": "transitive-wait-failed-final-wrapper",
        "keys": ("status", "error"),
        "optional_keys": (
            "query",
            "sources",
            "sources_found",
            "report",
            "reason_message",
            "hint",
        ),
        "nested_keys": {"sources": ("url", "title", "result_type")},
        "nested_optional_keys": {
            "sources": ("research_task_id", "report_markdown", "source_ordinal", "hint")
        },
        "model_contribution_keys": (
            "url",
            "title",
            "result_type",
            "research_task_id",
            "report_markdown",
            "source_ordinal",
            "hint",
        ),
        "projection_condition": "failed research wait contains at least one ResearchSource",
        "contribution_semantics": (
            "A nonempty ResearchSource collection adds sources/sources_found to the failed "
            "envelope and each source public-dict field reaches sources[]"
        ),
        "evidence": (
            "notebooklm/_app/research.py:sources=[src.to_public_dict() for src in status.sources]",
            "notebooklm/cli/research_cmd.py:failed_payload",
        ),
    },
    {
        "model": "notebooklm.types.ResearchSource",
        "mode": "transitive-import-selection-final-contribution",
        "keys": (
            "status",
            "run_id",
            "sources_found",
            "sources_selected",
            "imported",
            "imported_sources",
            "already_present",
            "already_present_sources",
        ),
        "optional_keys": ("cited_only", "cited_only_fallback"),
        "nested_keys": {
            "imported_sources": ("id", "title"),
            "already_present_sources": ("id", "title", "url"),
        },
        "model_contribution_keys": ("url", "result_type", "report_markdown"),
        "projection_condition": (
            "research import has at least one ResearchSource; field selection applies when "
            "cited-only mode is requested"
        ),
        "contribution_semantics": (
            "ResearchSource membership supplies sources_found/sources_selected; url, "
            "result_type, and report_markdown select cited sources and fallback behavior"
        ),
        "evidence": (
            "notebooklm/_app/research.py:sources=[src.to_public_dict() for src in status.sources]",
            "notebooklm/cli/research_cmd.py:selected, cited_selection = _select_research_sources_for_import(",
            "notebooklm/cli/research_cmd.py:_render_import_result",
        ),
    },
    {
        "model": "notebooklm.types.ShareStatus",
        "mode": "manual-status-projection",
        "keys": (
            "notebook_id",
            "is_public",
            "access",
            "view_level",
            "share_url",
            "max_individuals_share_limit",
            "is_public_sharing_allowed",
            "is_public_sharing_denied",
            "shared_users",
        ),
        "evidence": ("notebooklm/cli/share_cmd.py:share_status",),
    },
    {
        "model": "notebooklm.types.ShareStatus",
        "mode": "manual-public-projection",
        "keys": ("notebook_id", "is_public", "share_url"),
        "evidence": ("notebooklm/cli/share_cmd.py:share_public",),
    },
    {
        "model": "notebooklm.types.ShareStatus",
        "mode": "manual-view-level-projection",
        "keys": ("notebook_id", "view_level"),
        "evidence": ("notebooklm/cli/share_cmd.py:share_view_level",),
    },
    {
        "model": "notebooklm.types.SharedUser",
        "mode": "nested-manual-field-projection",
        "keys": ("email", "permission", "display_name"),
        "evidence": ("notebooklm/cli/share_cmd.py:shared_users",),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "manual-summary-projection",
        "evidence": ("notebooklm/cli/services/source_serializers.py:source_summary_payload",),
        "derive": "runtime:cli-source-summary",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "manual-row-projection",
        "evidence": ("notebooklm/cli/services/source_serializers.py:source_row_payload",),
        "derive": "runtime:cli-source-row",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "manual-list-final-wrapper",
        "evidence": (
            'notebooklm/cli/services/source_listing.py:items_key="sources"',
            "notebooklm/cli/services/listing.py:def prepare_list",
        ),
        "derive": "runtime:cli-source-final-list",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "manual-get-final-wrapper",
        "evidence": ("notebooklm/cli/_source_render.py:_render_source_get_result",),
        "derive": "runtime:cli-source-final-get",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-add-final-wrapper",
        "evidence": ("notebooklm/cli/_source_render.py:source_add_payload",),
        "derive": "runtime:cli-source-final-add",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-add-drive-final-wrapper",
        "evidence": ("notebooklm/cli/_source_render.py:_render_source_add_drive_result",),
        "derive": "runtime:cli-source-final-add-drive",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-add-drive-file-final-wrapper",
        "evidence": ("notebooklm/cli/_source_render.py:_render_source_add_drive_file_result",),
        "derive": "runtime:cli-source-final-add-drive-file",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "nested-label-list-title-join-projection",
        "evidence": ("notebooklm/cli/services/label_listing.py:_label_serialize",),
        "derive": "runtime:cli-label-title-join",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "nested-label-sources-projection",
        "evidence": ("notebooklm/cli/label_cmd.py:label_sources",),
        "derive": {
            "kind": "ast-dict",
            "path": "notebooklm/cli/label_cmd.py",
            "function": "label_sources",
            "contains": ("url",),
        },
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-wait-ready-projection",
        "evidence": ("notebooklm/cli/_source_render.py:SourceWaitReady",),
        "derive": {
            "kind": "ast-dict",
            "path": "notebooklm/cli/_source_render.py",
            "function": "_render_source_wait_outcome",
            "contains": ("title", "status_code"),
        },
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-wait-processing-error-final-wrapper",
        "keys": ("source_id", "status", "status_code", "error"),
        "model_contribution_keys": ("status",),
        "projection_condition": "Source.status is a terminal processing error",
        "contribution_semantics": (
            "Source.status is preserved through SourceProcessingError.status and emitted as "
            "status_code"
        ),
        "evidence": (
            "notebooklm/_source/polling.py:SourceProcessingError(source_id, source.status)",
            "notebooklm/cli/_source_render.py:SourceWaitProcessingError",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-wait-timeout-final-wrapper",
        "keys": (
            "source_id",
            "status",
            "last_status_code",
            "timeout_seconds",
            "error",
        ),
        "model_contribution_keys": ("status",),
        "projection_condition": "polling expires after observing a Source.status",
        "contribution_semantics": (
            "The last public Source.status is preserved through SourceTimeoutError.last_status "
            "and emitted as last_status_code"
        ),
        "evidence": (
            "notebooklm/_source/polling.py:last_status = source.status",
            "notebooklm/cli/_source_render.py:SourceWaitTimeout",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-rename-projection",
        "evidence": ("notebooklm/cli/_source_render.py:_source_rename_payload",),
        "derive": "runtime:cli-source-rename",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-delete-by-title-projection",
        "evidence": (
            "notebooklm/_app/source_mutations.py:execute_source_delete_by_title",
            "notebooklm/cli/_source_render.py:_source_delete_by_title_payload",
        ),
        "derive": "runtime:cli-source-delete-by-title",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-delete-resolver-error-text-wrapper",
        "keys": ("error", "code", "message"),
        "model_contribution_keys": ("id", "title"),
        "projection_condition": (
            "partial-id ambiguity, title collision, or exact-title ambiguity after listing Sources"
        ),
        "adapter_surface": "CLI --json standard error envelope",
        "evidence": (
            "notebooklm/_app/source_mutations.py:build_id_ambiguity_error(source_id, matches)",
            "notebooklm/_app/source_mutations.py:async def resolve_source_by_exact_title",
            "notebooklm/cli/_source_render.py:def _handle_source_mutation_error",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-delete-confirm-by-id-error-wrapper",
        "keys": ("error", "code", "message", "action", "source_id", "notebook_id"),
        "conditional_key_groups": (
            {
                "condition": "partial-id expansion differs from the requested source id",
                "keys": ("status_message",),
            },
        ),
        "model_contribution_keys": ("id", "title"),
        "contribution_semantics": (
            "Source.id always contributes to source_id; Source.title contributes only to the "
            "conditional status_message for a prefix expansion"
        ),
        "projection_condition": "noncanonical source-id resolution followed by JSON confirmation",
        "evidence": (
            "notebooklm/_app/source_mutations.py:resolution = await resolve_source_for_delete",
            "notebooklm/_app/source_mutations.py:status_message=resolution.status_message",
            "notebooklm/cli/_source_render.py:def _handle_source_mutation_error",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-delete-confirm-by-title-error-wrapper",
        "keys": (
            "error",
            "code",
            "message",
            "action",
            "source_id",
            "title",
            "notebook_id",
        ),
        "model_contribution_keys": ("id", "title"),
        "projection_condition": "exact-title Source match followed by JSON confirmation",
        "evidence": (
            "notebooklm/_app/source_mutations.py:source = await resolve_source_by_exact_title",
            'notebooklm/_app/source_mutations.py:action="delete-by-title"',
            "notebooklm/cli/_source_render.py:def _handle_source_mutation_error",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-clean-candidate-projection",
        "evidence": ("notebooklm/_app/source_clean.py:candidates_payload",),
        "derive": "runtime:cli-source-clean-candidate",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-clean-success-final-wrapper",
        "nested_keys": {
            "candidates": ("id", "title", "status", "reason"),
            "failures": ("id", "error"),
        },
        "evidence": (
            "notebooklm/cli/source_cmd.py:def _dispatch_source_clean_result",
            "notebooklm/cli/source_cmd.py:payload: dict[str, Any] =",
        ),
        "derive": {
            "kind": "ast-mapping-variable",
            "path": "notebooklm/cli/source_cmd.py",
            "function": "_dispatch_source_clean_result",
            "variable": "payload",
            "contains": ("action", "candidates", "deleted_count"),
        },
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-clean-confirm-required-error-wrapper",
        "keys": (
            "error",
            "code",
            "message",
            "action",
            "notebook_id",
            "candidate_count",
            "candidates",
        ),
        "nested_keys": {"candidates": ("id", "title", "status", "reason")},
        "evidence": (
            "notebooklm/cli/source_cmd.py:require_yes_in_json",
            "notebooklm/cli/_source_render.py:def _handle_source_mutation_error",
            "notebooklm/cli/error_handler.py:response: dict =",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-research-import-new-source-projection",
        "keys": ("id", "title"),
        "evidence": (
            "notebooklm/_research.py:def _imported_entry",
            "notebooklm/cli/research_cmd.py:imported_sources",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-research-import-existing-source-projection",
        "keys": ("id", "title", "url"),
        "evidence": (
            "notebooklm/_research.py:def _project_import_verification",
            "notebooklm/cli/research_cmd.py:already_present_sources",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-research-import-direct-final-wrapper",
        "keys": (
            "status",
            "run_id",
            "sources_found",
            "sources_selected",
            "imported",
            "imported_sources",
            "already_present",
            "already_present_sources",
        ),
        "optional_keys": ("cited_only", "cited_only_fallback"),
        "nested_keys": {
            "imported_sources": ("id", "title"),
            "already_present_sources": ("id", "title", "url"),
        },
        "evidence": ("notebooklm/cli/research_cmd.py:def _render_import_result",),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-research-wait-imported-sources-final-wrapper",
        "keys": ("status", "query", "sources_found", "sources", "report"),
        "optional_keys": (
            "cited_only",
            "cited_sources_selected",
            "cited_only_fallback",
            "imported",
            "imported_sources",
        ),
        "nested_keys": {"imported_sources": ("id", "title")},
        "evidence": ("notebooklm/cli/research_cmd.py:def _completed_wait_payload",),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-source-add-research-imported-sources-final-wrapper",
        "keys": ("status", "task_id", "sources_found", "sources", "report"),
        "optional_keys": (
            "cited_only",
            "cited_sources_selected",
            "cited_only_fallback",
            "imported",
            "imported_sources",
        ),
        "nested_keys": {"imported_sources": ("id", "title")},
        "evidence": (
            "notebooklm/cli/_source_render.py:completed_payload: dict[str, Any] =",
            'notebooklm/cli/_source_render.py:completed_payload["imported_sources"]',
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive:notebook-metadata-source-summary-final-wrapper",
        "keys": (
            "id",
            "title",
            "created_at",
            "last_viewed_at",
            "modified_at",
            "is_owner",
            "role",
            "sources",
        ),
        "nested_keys": {"sources": ("type", "title", "url")},
        "model_contribution_keys": ("title", "url", "_type_code"),
        "projection_condition": "Notebook metadata includes at least one listed public Source",
        "contribution_semantics": (
            "Each Source title/url and kind (backed by _type_code) constructs a public "
            "SourceSummary whose to_dict projection reaches NotebookMetadata.sources; the "
            "zero-source envelope has no Source contribution"
        ),
        "evidence": (
            "notebooklm/_notebook_metadata.py:SourceSummary(",
            "notebooklm/_types/notebooks.py:class SourceSummary",
            "notebooklm/cli/notebook_cmd.py:data = metadata.to_dict()",
        ),
    },
    {
        "model": "notebooklm.types.SourceFulltext",
        "mode": "manual-field-projection",
        "keys": ("source_id", "title", "kind", "content", "url", "char_count"),
        "model_contribution_keys": (
            "source_id",
            "title",
            "content",
            "_type_code",
            "url",
            "char_count",
        ),
        "evidence": ("notebooklm/cli/services/source_serializers.py:source_fulltext_payload",),
    },
    {
        "model": "notebooklm.types.SourceFulltext",
        "mode": "manual-file-output-projection",
        "keys": ("path", "bytes", "source_id", "title", "kind"),
        "model_contribution_keys": ("source_id", "title", "content", "_type_code"),
        "evidence": ("notebooklm/cli/_source_render.py:content_bytes",),
    },
    {
        "model": "notebooklm.types.SourceGuide",
        "mode": "manual-guide-projection",
        "keys": ("source_id", "summary", "keywords"),
        "model_contribution_keys": ("summary", "keywords"),
        "evidence": ("notebooklm/cli/_source_render.py:_render_source_guide_result",),
    },
    {
        "model": "notebooklm.types.SourceSummary",
        "mode": "nested-manual-to-dict-projection",
        "model_contribution_keys": ("kind", "title", "url"),
        "evidence": ("notebooklm/_types/notebooks.py:class SourceSummary",),
        "derive": "runtime:cli-notebook-metadata-source",
    },
    {
        "model": "notebooklm.types.SuggestedTopic",
        "mode": "nested-scalar-field-projection",
        "keys": ("question",),
        "model_contribution_keys": ("question",),
        "projection_condition": (
            "--topics is requested and NotebookDescription contains at least one SuggestedTopic"
        ),
        "contribution_semantics": (
            "Each SuggestedTopic.question becomes one suggested_topics string; an empty "
            "topics collection emits an empty list without a SuggestedTopic instance"
        ),
        "evidence": ("notebooklm/cli/notebook_cmd.py:topic.question",),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "conditional-noncanonical-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/cli/resolve.py:async def resolve_notebook_id",
            "notebooklm/cli/resolve.py:list_fn=lambda: client.notebooks.list()",
        ),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "always-listed:collection-membership-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/cli/collection_cmd.py:async def _resolve_notebook_ids",
            "notebooklm/cli/collection_cmd.py:notebooks = await client.notebooks.list()",
            'notebooklm/cli/collection_cmd.py:"added_notebook_ids": membership.notebook_ids',
            'notebooklm/cli/collection_cmd.py:"removed_notebook_ids": membership.notebook_ids',
        ),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "artifact-list-title-contribution",
        "keys": ("title",),
        "evidence": (
            "notebooklm/cli/artifact_cmd.py:nb = await client.notebooks.get(notebook_id)",
            'notebooklm/cli/artifact_cmd.py:"notebook_title": nb.title if nb else None',
        ),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "source-list-title-contribution",
        "keys": ("title",),
        "evidence": (
            "notebooklm/cli/services/source_listing.py:nb = await client.notebooks.get(notebook_id)",
            'notebooklm/cli/services/source_listing.py:"notebook_title": nb.title if nb else None',
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "conditional-noncanonical-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/cli/resolve.py:async def resolve_source_id",
            "notebooklm/cli/resolve.py:list_fn=lambda: client.sources.list(notebook_id)",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "conditional-noncanonical-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/cli/resolve.py:async def resolve_artifact_id",
            "notebooklm/cli/resolve.py:list_fn=lambda: client.artifacts.list(notebook_id)",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "always-listed-download-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/cli/download_helpers.py:def resolve_partial_artifact_id",
            "notebooklm/cli/services/download.py:artifact_resolver=resolve_partial_artifact_id",
        ),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "conditional-noncanonical-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/cli/resolve.py:async def resolve_note_id",
            "notebooklm/cli/resolve.py:list_fn=lambda: client.notes.list(notebook_id)",
        ),
    },
    {
        "model": "notebooklm.types.Collection",
        "mode": "always-listed-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/_app/collections.py:async def resolve_collection_id",
            "notebooklm/_app/collections.py:collections = await client.collections.list()",
        ),
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "always-listed-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/_app/labels.py:async def resolve_label_id",
            "notebooklm/_app/labels.py:labels = await client.labels.list(notebook_id)",
        ),
    },
)

__all__ = ["CLI_PROJECTION_SPECS"]
