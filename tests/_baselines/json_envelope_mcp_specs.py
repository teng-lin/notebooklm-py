"""Reviewed MCP tool and auxiliary JSON public-model projection declarations."""

from __future__ import annotations

_ASK_ROOT_KEYS = (
    "notebook_id",
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
_ASK_OPTIONAL_KEYS = ("history", "suggested_prompts", "source_ids")
_ASK_SUGGESTED_PROMPT_KEYS = ("title", "prompt")

MCP_PROJECTION_SPECS: tuple[dict[str, object], ...] = (
    {
        "model": "notebooklm.types.AccountLimits",
        "mode": "transitive-server-info-account-success-wrapper",
        "keys": ("server", "version", "auth", "account"),
        "nested_keys": {
            "auth": (
                "authenticated",
                "storage_exists",
                "json_valid",
                "cookies_present",
                "sid_cookie",
                "profile",
            ),
            "account": (
                "email",
                "authuser",
                "available",
                "notebook_limit",
                "source_limit",
                "tier",
                "output_language",
                "output_language_is_default",
            ),
        },
        "model_contribution_keys": ("notebook_limit", "source_limit", "tier"),
        "projection_condition": (
            "server_info include_account has a bound authenticated client and "
            "get_user_settings succeeds"
        ),
        "evidence": (
            "notebooklm/mcp/tools/meta.py:async def _account_block",
            "notebooklm/mcp/tools/meta.py:async def server_info",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-studio-full-projection",
        "keys": ("id", "title", "type", "status_label", "url"),
        "evidence": ("notebooklm/mcp/tools/_studio_items.py:studio_items",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-studio-compact-projection",
        "keys": ("id", "title", "type", "status_label", "created_at"),
        "evidence": ("notebooklm/mcp/tools/_studio_items.py:compact_studio_item",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-studio-summary-projection",
        "keys": (
            "id",
            "title",
            "type",
            "status_label",
            "url",
            "created_at",
            "generation_prompt",
        ),
        "evidence": ("notebooklm/mcp/tools/_studio_items.py:summarize_studio_item",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-studio-full-list-final-wrapper",
        "keys": ("notebook_id", "items", "total", "offset", "has_more"),
        "nested_union_keys": {
            "items": {
                "note": ("id", "title", "type", "content"),
                "artifact": ("id", "title", "type", "status_label", "url"),
            }
        },
        "evidence": ("notebooklm/mcp/tools/studio.py:async def studio_list",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-studio-compact-list-final-wrapper",
        "keys": ("notebook_id", "items", "total", "offset", "has_more"),
        "nested_keys": {"items": ("id", "title", "type", "status_label", "created_at")},
        "evidence": ('notebooklm/mcp/tools/studio.py:detail == "compact"',),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-studio-summary-list-final-wrapper",
        "keys": ("notebook_id", "items", "total", "offset", "has_more"),
        "nested_union_keys": {
            "items": {
                "note": ("id", "title", "type", "content_preview", "char_count"),
                "artifact": (
                    "id",
                    "title",
                    "type",
                    "status_label",
                    "url",
                    "created_at",
                    "generation_prompt",
                ),
            }
        },
        "evidence": ('notebooklm/mcp/tools/studio.py:detail == "summary"',),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "manual-studio-by-item-final-wrapper",
        "keys": ("notebook_id", "items", "total", "offset", "has_more"),
        "nested_union_keys": {
            "items": {
                "note": ("id", "title", "type", "content"),
                "artifact": (
                    "id",
                    "title",
                    "type",
                    "status_label",
                    "url",
                    "created_at",
                    "generation_prompt",
                ),
            }
        },
        "evidence": ("notebooklm/mcp/tools/studio.py:if item is not None",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-interactive-mind-map-generation-final-contribution",
        "keys": ("notebook_id", "kind", "mind_map", "mind_map_id"),
        "model_contribution_keys": ("id",),
        "projection_condition": (
            "interactive mind-map generation finds the newly created public Artifact"
        ),
        "contribution_semantics": (
            "Artifact.id constructs MindMap.id and is emitted as mind_map_id; the raw "
            "create-id fallback emits the same wrapper without a public Artifact contribution"
        ),
        "evidence": (
            "notebooklm/_mind_maps_api.py:artifact = project_artifact(record)",
            "notebooklm/_mind_maps_api.py:return MindMap(",
            "notebooklm/mcp/tools/_studio_payloads.py:_mind_map_id",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-studio-rename-final-projection",
        "keys": (
            "status",
            "notebook_id",
            "item_id",
            "type",
            "new_title",
            "is_mind_map",
        ),
        "model_contribution_keys": ("id", "_artifact_type", "_variant"),
        "projection_condition": "resolve_studio_item returns a listed public Artifact",
        "contribution_semantics": (
            "Artifact.id becomes item_id and Artifact.kind (backed by _artifact_type and "
            "_variant) becomes type on the resolved-artifact branch; the full-UUID "
            "resolver-miss branch uses the same helper without a public Artifact"
        ),
        "evidence": ("notebooklm/mcp/tools/_studio_payloads.py:_artifact_rename_payload",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-mind-map-rename-membership-final-contribution",
        "keys": (
            "status",
            "notebook_id",
            "item_id",
            "type",
            "new_title",
            "is_mind_map",
        ),
        "model_contribution_keys": ("id", "_artifact_type", "_variant"),
        "projection_condition": (
            "mind_maps.list converts a listed interactive Artifact whose id matches the target"
        ),
        "contribution_semantics": (
            "Artifact id and interactive kind (backed by _artifact_type/_variant) construct "
            "the matching MindMap whose membership sets is_mind_map and the fallback type; "
            "this scan can contribute on the full-UUID resolver-miss branch as well as the "
            "resolved-Artifact branch"
        ),
        "evidence": (
            "notebooklm/_mind_maps_api.py:artifact = project_artifact(record)",
            "notebooklm/mcp/tools/studio.py:is_mind_map",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-studio-delete-confirmation-wrapper",
        "keys": ("status", "preview"),
        "nested_keys": {"preview": ("action", "notebook_id", "item_id", "type", "title")},
        "evidence": ("notebooklm/mcp/tools/studio.py:async def studio_delete",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-studio-delete-final-projection",
        "keys": ("status", "notebook_id", "item_id", "type", "was_note_backed"),
        "evidence": ("notebooklm/mcp/tools/studio.py:was_note_backed",),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-resolver-retry-final-projection",
        "keys": ("notebook_id", "artifact_id", "task_id", "status"),
        "evidence": (
            "notebooklm/mcp/_resolve.py:async def resolve_artifact",
            "notebooklm/mcp/tools/studio.py:async def studio_retry",
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
        "mode": "transitive-mcp-stdio-download-selected-wrapper",
        "evidence": (
            "notebooklm/_app/download.py:class DownloadResult",
            "notebooklm/mcp/tools/studio.py:return {**to_jsonable(result)",
        ),
        "derive": "runtime:artifact-download-mcp-root-selected",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-mcp-stdio-download-error-wrapper",
        "model_contribution_keys": (
            "id",
            "title",
            "_artifact_type",
            "status",
            "created_at",
            "_variant",
        ),
        "projection_condition": (
            "at least one public Artifact participates in type/completion filtering, "
            "selection, conflict handling, or a selected-artifact download failure"
        ),
        "contribution_semantics": (
            "Artifact identity/title/created_at drive selection and error details while "
            "kind and completion are backed by _artifact_type, _variant, and status; the "
            "NO_ARTIFACTS alternative produced by a truly empty listing has no public "
            "Artifact contribution"
        ),
        "evidence": (
            "notebooklm/_app/download.py:outcome=DownloadOutcome.ERROR",
            "notebooklm/mcp/tools/studio.py:return {**to_jsonable(result)",
        ),
        "derive": "runtime:artifact-download-mcp-root-error",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-mcp-stdio-download-dry-all-wrapper",
        "evidence": (
            "notebooklm/_app/download.py:outcome=DownloadOutcome.ALL_DRY_RUN",
            "notebooklm/mcp/tools/studio.py:return {**to_jsonable(result)",
        ),
        "derive": "runtime:artifact-download-mcp-root-dry-all",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-mcp-stdio-download-executed-wrapper",
        "evidence": (
            "notebooklm/_app/download.py:artifacts_results.append",
            "notebooklm/mcp/tools/studio.py:return {**to_jsonable(result)",
        ),
        "derive": "runtime:artifact-download-mcp-root-executed",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-mcp-remote-download-broker-wrapper",
        "keys": (
            "status",
            "notebook_id",
            "artifact_type",
            "filename",
            "mime_type",
            "size_bytes",
            "url",
            "expires_at",
        ),
        "optional_keys": ("artifact_id",),
        "model_contribution_keys": (
            "id",
            "title",
            "_artifact_type",
            "status",
            "created_at",
            "_variant",
        ),
        "projection_condition": (
            "the artifact-ref branch, explicit artifact-id prevalidation, or inline-text "
            "selection reads at least one public Artifact"
        ),
        "contribution_semantics": (
            "Artifact fields validate membership/type/completion, supply the selected id "
            "and title, and choose the latest inline-text artifact; artifact_type without "
            "artifact_id for a non-inline kind mints the broker envelope without listing "
            "or reading any public Artifact and is non-public"
        ),
        "conditional_key_groups": (
            {
                "condition": "inline textual artifact content",
                "keys": ("content", "char_count", "truncated"),
            },
        ),
        "evidence": (
            "notebooklm/mcp/tools/studio.py:resolved_title",
            "notebooklm/mcp/tools/_studio_download.py:structured: dict[str, Any] =",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-incomplete-status-error-text-contribution",
        "keys": ("message",),
        "model_contribution_keys": ("status",),
        "projection_condition": (
            "a resolved artifact or explicit typed artifact id names an incomplete artifact"
        ),
        "adapter_surface": "MCP ToolError flat message",
        "contribution_semantics": (
            "Artifact.status is rendered through status_str into the actionable incomplete "
            "download ValidationError"
        ),
        "evidence": (
            "notebooklm/mcp/tools/studio.py:match.status_str",
            "notebooklm/mcp/tools/studio.py:incomplete.status_str",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-retry-wrong-state-status-error-text-contribution",
        "keys": ("message",),
        "model_contribution_keys": ("status",),
        "projection_condition": "retry refusal resolves to a non-failed public Artifact",
        "adapter_surface": "MCP ToolError flat message",
        "contribution_semantics": (
            "Artifact.status is rendered through status_str into the wrong-state retry "
            "ValidationError"
        ),
        "evidence": (
            "notebooklm/mcp/tools/studio.py:art.status_str",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "app-status-view-projection",
        "evidence": (
            "notebooklm/_app/artifacts.py:status_view",
            "notebooklm/mcp/tools/studio.py:studio_status",
        ),
        "derive": "runtime:generation-status-view",
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-retry-projection",
        "keys": ("notebook_id", "artifact_id", "task_id", "status"),
        "evidence": ("notebooklm/mcp/tools/studio.py:studio_retry",),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-generate-projection",
        "keys": ("notebook_id", "kind", "task_id", "status", "url", "error"),
        "optional_keys": ("source_ids",),
        "evidence": (
            "notebooklm/_app/generate_retry.py:generation_outcome_from_status",
            "notebooklm/mcp/tools/_studio_payloads.py:_generation_payload",
        ),
    },
    {
        "model": "notebooklm.types.AskResult",
        "mode": "app-view:mcp-final-lite-references",
        "evidence": (
            "notebooklm/_app/views.py:ask_result_view",
            'notebooklm/mcp/tools/chat.py:references == "lite"',
        ),
        "derive": "runtime:ask-result-mcp-lite",
        # ``references`` is deliberately excluded: the tool trims each row
        # to the explicit all-optional ChatReference projection below.
        "nested_fields": ("turn_key", "next_steps"),
    },
    {
        "model": "notebooklm.types.AskResult",
        "mode": "app-view:mcp-final-full-references",
        "evidence": (
            "notebooklm/_app/views.py:ask_result_view",
            "notebooklm/mcp/tools/chat.py:payload.update(ask_payload)",
        ),
        "derive": "runtime:ask-result-mcp-full",
        "nested_fields": ("references", "turn_key", "next_steps"),
    },
    {
        "model": "notebooklm._types.documents.StructuredDocument",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": _ASK_OPTIONAL_KEYS,
        "nested_keys": {
            "references": _FULL_REFERENCE_KEYS,
            "suggested_prompts": _ASK_SUGGESTED_PROMPT_KEYS,
        },
        "model_contribution_keys": ("blocks", "annotations"),
        "projection_condition": (
            "references=full and the answer document contains an in-range annotation whose "
            "object_id matches a surviving ChatReference.chunk_id"
        ),
        "contribution_semantics": (
            "StructuredDocument annotations and block-derived text/extent produce "
            "answer_anchor_start/end; answer_document itself is removed from the tool result"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/mcp/tools/chat.py:payload.update(ask_payload)",
        ),
    },
    {
        "model": "notebooklm._types.documents.DocumentAnnotation",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": _ASK_OPTIONAL_KEYS,
        "nested_keys": {
            "references": _FULL_REFERENCE_KEYS,
            "suggested_prompts": _ASK_SUGGESTED_PROMPT_KEYS,
        },
        "model_contribution_keys": ("object_id", "start_index", "end_index"),
        "projection_condition": (
            "references=full and an in-range DocumentAnnotation.object_id matches a "
            "surviving ChatReference.chunk_id"
        ),
        "contribution_semantics": (
            "DocumentAnnotation object_id joins the citation and its offsets become "
            "answer_anchor_start/end"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/mcp/tools/chat.py:payload.update(ask_payload)",
        ),
    },
    {
        "model": "notebooklm._types.documents.DocumentBlock",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": _ASK_OPTIONAL_KEYS,
        "nested_keys": {
            "references": _FULL_REFERENCE_KEYS,
            "suggested_prompts": _ASK_SUGGESTED_PROMPT_KEYS,
        },
        "model_contribution_keys": ("start_index", "end_index", "spans"),
        "projection_condition": (
            "references=full and a decoded citation fragment contains a usable "
            "DocumentBlock, or an accepted answer anchor depends on an answer block"
        ),
        "contribution_semantics": (
            "DocumentBlock ranges/text produce cited_text and fragment ranges, while "
            "answer-document blocks also bound anchor validity"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def extract_text_passages",
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/mcp/tools/chat.py:payload.update(ask_payload)",
        ),
    },
    {
        "model": "notebooklm._types.documents.TextSpan",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": _ASK_OPTIONAL_KEYS,
        "nested_keys": {
            "references": _FULL_REFERENCE_KEYS,
            "suggested_prompts": _ASK_SUGGESTED_PROMPT_KEYS,
        },
        "model_contribution_keys": ("start_index", "end_index", "text"),
        "projection_condition": (
            "references=full and a decoded citation fragment contains a usable TextSpan, "
            "or an accepted anchor depends on answer-document text built from a TextSpan"
        ),
        "contribution_semantics": (
            "TextSpan offsets/text build cited_text/ranges or answer-document text used to "
            "validate answer anchors"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def extract_text_passages",
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/mcp/tools/chat.py:payload.update(ask_payload)",
        ),
    },
    {
        "model": "notebooklm._types.documents.DocumentBlock",
        "mode": "transitive-chat-reference-lite-fragment-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": _ASK_OPTIONAL_KEYS,
        "nested_keys": {
            "references": (),
            "suggested_prompts": _ASK_SUGGESTED_PROMPT_KEYS,
        },
        "nested_optional_keys": {"references": ("source_id", "citation_number", "cited_text")},
        "model_contribution_keys": ("start_index", "end_index", "spans"),
        "projection_condition": (
            "references=lite and a decoded citation fragment contains at least one usable "
            "DocumentBlock"
        ),
        "contribution_semantics": (
            "DocumentBlock span-derived text may survive as optional cited_text; all block "
            "range and answer-anchor fields are trimmed from lite references"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def extract_text_passages",
            "notebooklm/mcp/tools/chat.py:_LITE_REFERENCE_FIELDS",
        ),
    },
    {
        "model": "notebooklm._types.documents.TextSpan",
        "mode": "transitive-chat-reference-lite-fragment-contribution",
        "keys": _ASK_ROOT_KEYS,
        "optional_keys": _ASK_OPTIONAL_KEYS,
        "nested_keys": {
            "references": (),
            "suggested_prompts": _ASK_SUGGESTED_PROMPT_KEYS,
        },
        "nested_optional_keys": {"references": ("source_id", "citation_number", "cited_text")},
        "model_contribution_keys": ("start_index", "end_index", "text"),
        "projection_condition": (
            "references=lite and a decoded citation fragment contains at least one usable TextSpan"
        ),
        "contribution_semantics": (
            "TextSpan text builds optional cited_text; span offsets and answer-anchor fields "
            "are trimmed from lite references"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def extract_text_passages",
            "notebooklm/mcp/tools/chat.py:_LITE_REFERENCE_FIELDS",
        ),
    },
    {
        "model": "notebooklm.types.ChatReference",
        "mode": "nested-lite-projection",
        "keys": (),
        "optional_keys": ("source_id", "citation_number", "cited_text"),
        "evidence": ("notebooklm/mcp/tools/chat.py:_LITE_REFERENCE_FIELDS",),
    },
    {
        "model": "notebooklm.types.CitedSourceSelection",
        "mode": "transitive-research-import-final-projection",
        "keys": (
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
        ),
        "optional_keys": ("cited_only_fallback", "deprecation"),
        "model_contribution_keys": ("sources", "used_fallback"),
        "projection_condition": "research_import performs cited-source selection",
        "contribution_semantics": (
            "len(CitedSourceSelection.sources) becomes sources_selected and used_fallback "
            "becomes cited_only_fallback; diagnostic URL counts do not reach the tool result"
        ),
        "evidence": (
            "notebooklm/mcp/tools/research.py:selection = select_cited_sources",
            "notebooklm/mcp/tools/research.py:result: dict[str, Any] =",
        ),
    },
    {
        "model": "notebooklm.types.MindMap",
        "mode": "transitive-mcp-generation-final-wrapper",
        "evidence": ("notebooklm/mcp/tools/_studio_payloads.py:_generation_payload",),
        "derive": "runtime:mind-map-mcp-final",
    },
    {
        "model": "notebooklm.types.MindMapResult",
        "mode": "transitive-mcp-generation-final-wrapper",
        "evidence": ("notebooklm/mcp/tools/_studio_payloads.py:_generation_payload",),
        "derive": "runtime:mind-map-mcp-final",
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "transitive-note-backed-mind-map-generation-final-contribution",
        "keys": ("notebook_id", "kind", "mind_map", "mind_map_id"),
        "model_contribution_keys": ("id",),
        "projection_condition": (
            "note-backed mind-map generation returns a present leaf and successfully "
            "creates a public Note"
        ),
        "contribution_semantics": (
            "The created Note.id is copied into MindMapResult.note_id and emitted as "
            "mind_map_id; interactive or absent-leaf paths have no Note contribution"
        ),
        "evidence": (
            "notebooklm/_web/bindings/mind_maps.py:note = await _note_service",
            "notebooklm/mcp/tools/_studio_payloads.py:return result_obj.note_id",
            "notebooklm/mcp/tools/studio.py:payload = _generation_payload",
        ),
    },
    {
        "model": "notebooklm.types.MindMap",
        "mode": "transitive-resolver-rename-final-wrapper",
        "keys": (
            "status",
            "notebook_id",
            "item_id",
            "type",
            "new_title",
            "is_mind_map",
        ),
        "model_contribution_keys": ("id",),
        "projection_condition": "mind_maps.list returns a MindMap whose id matches the target",
        "contribution_semantics": (
            "MindMap.id membership determines is_mind_map and the MCP full-UUID fallback type; "
            "MindMap.kind routes the mutation but is not projected as a model field"
        ),
        "evidence": (
            "notebooklm/_app/artifacts.py:async def rename_artifact",
            "notebooklm/mcp/tools/_studio_payloads.py:_artifact_rename_payload",
        ),
    },
    {
        "model": "notebooklm.types.MindMap",
        "mode": "transitive-resolver-delete-final-wrapper",
        "keys": ("status", "notebook_id", "item_id", "type", "was_note_backed"),
        "model_contribution_keys": ("id",),
        "projection_condition": "list_note_backed returns a MindMap whose id matches the target",
        "contribution_semantics": (
            "MindMap.id membership selects the note-backed path and determines type and "
            "was_note_backed; regular or missing full-id artifacts use the terminal without it"
        ),
        "evidence": (
            "notebooklm/_app/artifacts.py:async def delete_artifact",
            "notebooklm/mcp/tools/studio.py:was_note_backed = await artifact_core.delete_artifact",
        ),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-studio-full-projection",
        "keys": ("id", "title", "type", "content"),
        "evidence": ("notebooklm/mcp/tools/_studio_items.py:studio_items",),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-studio-compact-projection",
        "keys": ("id", "title", "type", "status_label", "created_at"),
        "evidence": ("notebooklm/mcp/tools/_studio_items.py:compact_studio_item",),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-studio-summary-projection",
        "keys": ("id", "title", "type", "content_preview", "char_count"),
        "evidence": ("notebooklm/mcp/tools/_studio_items.py:summarize_studio_item",),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-studio-full-list-final-wrapper",
        "keys": ("notebook_id", "items", "total", "offset", "has_more"),
        "nested_union_keys": {
            "items": {
                "note": ("id", "title", "type", "content"),
                "artifact": ("id", "title", "type", "status_label", "url"),
            }
        },
        "evidence": ("notebooklm/mcp/tools/studio.py:async def studio_list",),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-studio-compact-list-final-wrapper",
        "keys": ("notebook_id", "items", "total", "offset", "has_more"),
        "nested_keys": {"items": ("id", "title", "type", "status_label", "created_at")},
        "evidence": ('notebooklm/mcp/tools/studio.py:detail == "compact"',),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-studio-summary-list-final-wrapper",
        "keys": ("notebook_id", "items", "total", "offset", "has_more"),
        "nested_union_keys": {
            "items": {
                "note": ("id", "title", "type", "content_preview", "char_count"),
                "artifact": (
                    "id",
                    "title",
                    "type",
                    "status_label",
                    "url",
                    "created_at",
                    "generation_prompt",
                ),
            }
        },
        "evidence": ('notebooklm/mcp/tools/studio.py:detail == "summary"',),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "manual-studio-by-item-final-wrapper",
        "keys": ("notebook_id", "items", "total", "offset", "has_more"),
        "nested_union_keys": {
            "items": {
                "note": ("id", "title", "type", "content"),
                "artifact": (
                    "id",
                    "title",
                    "type",
                    "status_label",
                    "url",
                    "created_at",
                    "generation_prompt",
                ),
            }
        },
        "evidence": ("notebooklm/mcp/tools/studio.py:if item is not None",),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "transitive-studio-rename-final-projection",
        "keys": (
            "status",
            "notebook_id",
            "item_id",
            "type",
            "new_title",
            "is_mind_map",
        ),
        "evidence": ('notebooklm/mcp/tools/studio.py:resolved.type == "note"',),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "transitive-studio-delete-confirmation-wrapper",
        "keys": ("status", "preview"),
        "nested_keys": {"preview": ("action", "notebook_id", "item_id", "type", "title")},
        "evidence": ("notebooklm/mcp/tools/studio.py:async def studio_delete",),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "transitive-studio-delete-final-projection",
        "keys": ("status", "notebook_id", "item_id", "type", "was_note_backed"),
        "evidence": ('notebooklm/mcp/tools/studio.py:resolved.type == "note"',),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "transitive-note-save-create-final-projection",
        "evidence": ('notebooklm/mcp/tools/notes.py:"status": "created"',),
        "derive": "runtime:mcp-note-create",
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "app-view:notebook_view",
        "evidence": (
            "notebooklm/_app/views.py:notebook_view",
            "notebooklm/mcp/tools/notebooks.py:_notebook_view",
        ),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "app-view:notebook-list-final-wrapper",
        "evidence": (
            "notebooklm/mcp/tools/notebooks.py:page, meta = paginate",
            'notebooklm/mcp/tools/notebooks.py:return {"notebooks": page',
        ),
        "derive": "runtime:list-wrapper-mcp-notebook-page",
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "app-view:notebook-create-final",
        "evidence": (
            "notebooklm/_app/views.py:notebook_view",
            'notebooklm/mcp/tools/notebooks.py:record.pop("id")',
        ),
        "derive": "runtime:mcp-notebook-create",
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "always-listed-delete-confirmation-title-wrapper",
        "keys": ("status", "preview"),
        "nested_keys": {"preview": ("action", "notebook_id", "title")},
        "model_contribution_keys": ("title",),
        "evidence": (
            "notebooklm/mcp/tools/notebooks.py:title = title_for_id(await client.notebooks.list()",
            "notebooklm/mcp/tools/notebooks.py:return needs_confirmation",
        ),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "app-view:notebook-metadata-nested",
        "evidence": (
            'notebooklm/mcp/tools/notebooks.py:metadata_block["notebook"] =',
            "notebooklm/mcp/tools/notebooks.py:sources_count",
        ),
        "derive": "runtime:mcp-notebook-metadata-notebook",
    },
    {
        "model": "notebooklm.types.NotebookMetadata",
        "mode": "transitive-notebook-describe-final-with-metadata",
        "model_contribution_keys": ("notebook", "sources"),
        "projection_condition": (
            "notebook_describe include_metadata is true and description is a populated "
            "NotebookDescription"
        ),
        "evidence": (
            "notebooklm/mcp/tools/notebooks.py:output = to_jsonable(result)",
            "notebooklm/mcp/tools/notebooks.py:metadata_block = to_jsonable",
            'notebooklm/mcp/tools/notebooks.py:output["metadata"] = metadata_block',
        ),
        "derive": "runtime:mcp-notebook-describe-with-metadata",
        "nested_fields": ("sources",),
    },
    {
        "model": "notebooklm.types.NotebookMetadata",
        "mode": "transitive:notebook-describe-final-with-metadata-null-description",
        "keys": ("notebook_id", "description", "metadata"),
        "nested_keys": {
            "metadata": ("notebook", "sources"),
            "metadata.notebook": (
                "id",
                "title",
                "created_at",
                "sources_count",
                "is_owner",
                "modified_at",
                "role",
                "last_viewed_at",
                "role_label",
            ),
            "metadata.sources": ("kind", "title", "url"),
        },
        "model_contribution_keys": ("notebook", "sources"),
        "projection_condition": (
            "notebook_describe include_metadata is true and description is None"
        ),
        "contribution_semantics": (
            "NotebookMetadata still supplies the complete metadata block while the required "
            "description root key is null and has no nested description shape"
        ),
        "evidence": (
            "notebooklm/mcp/tools/notebooks.py:output = to_jsonable(result)",
            "notebooklm/mcp/tools/notebooks.py:metadata_block = to_jsonable",
            'notebooklm/mcp/tools/notebooks.py:output["metadata"] = metadata_block',
        ),
    },
    {
        "model": "notebooklm.types.NotebookDescription",
        "mode": "nested-notebook-describe-final",
        "model_contribution_keys": ("summary", "suggested_topics"),
        "projection_condition": "NotebookDescribeResult.description is not None",
        "contribution_semantics": (
            "NotebookDescription is recursively serialized under description; when the "
            "field is None the same root carries description null without a public "
            "NotebookDescription instance"
        ),
        "evidence": ("notebooklm/mcp/tools/notebooks.py:output = to_jsonable(result)",),
        "derive": "runtime:mcp-notebook-describe",
        "nested_fields": "all",
        "nested_projection_metadata": {
            "suggested_topics": {
                "model_contribution_keys": ("question", "prompt"),
                "projection_condition": (
                    "NotebookDescribeResult.description is not None and contains at least "
                    "one SuggestedTopic"
                ),
                "contribution_semantics": (
                    "Each SuggestedTopic is recursively serialized in description."
                    "suggested_topics; an empty collection has no SuggestedTopic instance"
                ),
            }
        },
    },
    {
        "model": "notebooklm.types.PromptSuggestion",
        "mode": "manual-standalone-final-wrapper",
        "evidence": ('notebooklm/mcp/tools/chat.py:payload["suggestions"]',),
        "derive": "runtime:prompt-suggestion-mcp",
    },
    {
        "model": "notebooklm.types.PromptSuggestion",
        "mode": "manual-chat-inline-projection",
        "evidence": ('notebooklm/mcp/tools/chat.py:payload["suggested_prompts"]',),
        "derive": "runtime:prompt-suggestion-mcp-chat-inline",
    },
    {
        "model": "notebooklm.types.PromptSuggestion",
        "mode": "manual:chat-suggestion-only-final-wrapper",
        "keys": ("notebook_id", "suggested_prompts"),
        "optional_keys": ("history", "conversation_id", "source_ids"),
        "nested_keys": {"suggested_prompts": ("title", "prompt")},
        "evidence": (
            "notebooklm/mcp/tools/chat.py:ask_result, suggestions = None, await suggest_coro",
            'notebooklm/mcp/tools/chat.py:payload["suggested_prompts"]',
        ),
    },
    {
        "model": "notebooklm.types.ResearchStart",
        "mode": "manual-start-projection",
        "keys": ("notebook_id", "query", "mode", "poll_task_id"),
        "model_contribution_keys": ("task_id", "report_id", "notebook_id", "query", "mode"),
        "evidence": ("notebooklm/mcp/tools/research.py:start_fields",),
    },
    {
        "model": "notebooklm.types.ResearchStart",
        "mode": "transitive-start-missing-report-id-error-contribution",
        "keys": ("message",),
        "model_contribution_keys": ("task_id", "report_id"),
        "projection_condition": "deep research start returns no report_id",
        "adapter_surface": "MCP ToolError flat message",
        "contribution_semantics": (
            "ResearchStart.report_id emptiness selects the ValidationError and task_id is "
            "interpolated as the unpollable session identifier"
        ),
        "evidence": (
            "notebooklm/mcp/tools/research.py:if not result.report_id",
            'notebooklm/mcp/tools/research.py:f"{result.task_id!r}',
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "manual-status-projection",
        "keys": (
            "notebook_id",
            "task_id",
            "poll_task_id",
            "kind",
            "status",
            "status_code",
            "termination_reason",
            "discovery_mode",
            "created_at",
            "updated_at",
            "duration_seconds",
            "query",
            "sources",
            "sources_total",
            "sources_returned",
            "sources_offset",
            "summary",
            "report",
            "report_char_count",
            "report_truncated",
        ),
        "optional_keys": ("reason_message", "hint", "cancelled", "deprecation"),
        "model_contribution_keys": (
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
        ),
        "evidence": ("notebooklm/mcp/tools/research.py:payload",),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-cancel-terminal-final-wrapper",
        "keys": (
            "status",
            "notebook_id",
            "poll_task_id",
            "run_id",
            "cancel_requested",
        ),
        "optional_keys": ("deprecation",),
        "model_contribution_keys": ("status",),
        "evidence": (
            "notebooklm/_app/research.py:status = await client.research.poll",
            'notebooklm/mcp/tools/research.py:if status.status in ("completed", "failed")',
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-cancel-nonterminal-final-wrapper",
        "keys": (
            "status",
            "notebook_id",
            "poll_task_id",
            "run_id",
            "cancel_requested",
            "run_status_before",
        ),
        "optional_keys": ("deprecation",),
        "model_contribution_keys": ("status",),
        "evidence": (
            "notebooklm/_app/research.py:status = await client.research.poll",
            'notebooklm/mcp/tools/research.py:"run_status_before": status.status',
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-import-final-wrapper",
        "keys": (
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
        ),
        "optional_keys": ("cited_only_fallback", "deprecation"),
        "nested_keys": {
            "imported": ("id", "title"),
            "newly_imported": ("id", "title"),
            "already_present": ("id", "title", "url"),
        },
        "model_contribution_keys": ("status", "sources", "report"),
        "projection_condition": "ResearchTask is completed and classified as importable",
        "contribution_semantics": (
            "status selects the success terminal, source membership supplies sources_found, "
            "and report affects cited-only selection"
        ),
        "evidence": (
            "notebooklm/_app/research.py:status = await client.research.poll",
            'notebooklm/mcp/tools/research.py:"sources_found": len(sources)',
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-import-refusal-error-contribution",
        "keys": ("message",),
        "model_contribution_keys": (
            "status",
            "status_code",
            "source_type",
            "query",
            "sources",
        ),
        "projection_condition": (
            "import classification refuses not_found, failed, noncompleted, or "
            "completed-without-sources ResearchTask state"
        ),
        "adapter_surface": "MCP ToolError flat message",
        "contribution_semantics": (
            "ResearchTask fields select and populate the ValidationError message; completed-empty "
            "uses only source-list membership and no ResearchSource field is projected"
        ),
        "evidence": (
            "notebooklm/_app/research.py:def classify_importable_research",
            "notebooklm/mcp/tools/research.py:sources, report = await research_core.poll_importable_research",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.ResearchSource",
        "mode": "nested-public-dict-report-omitted",
        "evidence": (
            "notebooklm/_app/research.py:src.to_public_dict()",
            "notebooklm/mcp/tools/research.py:to_jsonable(result.sources)",
            'notebooklm/mcp/tools/research.py:del src["report_markdown"]',
        ),
        "derive": "runtime:research-source-public-dict-without-report",
    },
    {
        "model": "notebooklm.types.ResearchSource",
        "mode": "nested-public-dict-report-included-truncated",
        "evidence": (
            "notebooklm/_app/research.py:src.to_public_dict()",
            "notebooklm/mcp/tools/research.py:to_jsonable(result.sources)",
            'notebooklm/mcp/tools/research.py:src["report_markdown"] =',
        ),
        "derive": "runtime:research-source-public-dict",
    },
    {
        "model": "notebooklm.types.ResearchSource",
        "mode": "transitive-import-source-count-contribution",
        "keys": (
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
        ),
        "optional_keys": ("cited_only_fallback", "deprecation"),
        "nested_keys": {
            "imported": ("id", "title"),
            "newly_imported": ("id", "title"),
            "already_present": ("id", "title", "url"),
        },
        "model_contribution_keys": ("url", "result_type", "report_markdown"),
        "projection_condition": (
            "an importable ResearchTask contains at least one ResearchSource; field selection "
            "applies when cited-only mode is requested"
        ),
        "contribution_semantics": (
            "ResearchSource membership contributes sources_found/sources_selected; url, "
            "result_type, and report_markdown select cited sources and fallback behavior, "
            "while no ResearchSource field envelope is serialized"
        ),
        "evidence": (
            "notebooklm/_app/research.py:sources=[src.to_public_dict() for src in status.sources]",
            'notebooklm/mcp/tools/research.py:"sources_found": len(sources)',
        ),
    },
    {
        "model": "notebooklm.types.ShareStatus",
        "mode": "app-view:share_status_view",
        "evidence": (
            "notebooklm/_app/views.py:share_status_view",
            "notebooklm/mcp/tools/sharing.py:_status_payload",
        ),
    },
    {
        "model": "notebooklm.types.ShareStatus",
        "mode": "app-view:mutation-final-updated",
        "keys": (
            "status",
            "notebook_id",
            "is_public",
            "access",
            "share_url",
            "max_individuals_share_limit",
            "is_public_sharing_allowed",
            "is_public_sharing_denied",
            "shared_users",
        ),
        "evidence": ('notebooklm/mcp/tools/sharing.py:return {"status": "updated"',),
    },
    {
        "model": "notebooklm.types.ShareStatus",
        "mode": "app-view:mutation-final-updated+view_level",
        "keys": (
            "status",
            "notebook_id",
            "is_public",
            "access",
            "share_url",
            "max_individuals_share_limit",
            "is_public_sharing_allowed",
            "is_public_sharing_denied",
            "shared_users",
            "view_level",
        ),
        "evidence": (
            'notebooklm/mcp/tools/sharing.py:payload["view_level"]',
            'notebooklm/mcp/tools/sharing.py:return {"status": "updated"',
        ),
    },
    {
        "model": "notebooklm.types.ShareStatus",
        "mode": "conditional-public-widening-confirmation-wrapper",
        "keys": ("status", "preview"),
        "nested_keys": {
            "preview": ("action", "notebook_id", "change"),
        },
        "nested_optional_keys": {"preview": ("view_level",)},
        "model_contribution_keys": ("is_public",),
        "projection_condition": "current ShareStatus.is_public is false",
        "evidence": (
            "notebooklm/mcp/tools/sharing.py:current = await client.sharing.get_status(nb_id)",
            "notebooklm/mcp/tools/sharing.py:if not current.is_public",
            "notebooklm/mcp/tools/sharing.py:return needs_confirmation(preview)",
        ),
    },
    {
        "model": "notebooklm.types.SharedUser",
        "mode": "nested-manual-field-projection",
        "keys": ("email", "permission", "display_name", "avatar_url"),
        "evidence": ("notebooklm/_app/views.py:shared_users",),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "app-view:source_view",
        "evidence": (
            "notebooklm/_app/views.py:source_view",
            "notebooklm/mcp/tools/sources.py:_source_view",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "app-view:source-list-final-wrapper",
        "evidence": (
            "notebooklm/mcp/tools/sources.py:async def source_list",
            "notebooklm/mcp/tools/sources.py:page, meta = paginate",
        ),
        "derive": "runtime:list-wrapper-mcp-source-page",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "manual-compact-list-final-wrapper",
        "evidence": (
            "notebooklm/mcp/tools/sources.py:async def source_list",
            "notebooklm/mcp/tools/sources.py:_source_compact",
            "notebooklm/_app/pagination.py:return page",
        ),
        "derive": {
            "kind": "ast-compact-list-wrapper",
            "path": "notebooklm/mcp/tools/sources.py",
            "function": "source_list",
            "variable": "_COMPACT_SOURCE_FIELDS",
            "meta_path": "notebooklm/_app/pagination.py",
            "meta_function": "paginate",
        },
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "manual-compact-projection",
        "evidence": ("notebooklm/mcp/tools/sources.py:_source_compact",),
        "derive": {
            "kind": "ast-assigned-sequence",
            "path": "notebooklm/mcp/tools/sources.py",
            "variable": "_COMPACT_SOURCE_FIELDS",
        },
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "nested-dataclass-source-rename-result",
        "evidence": (
            "notebooklm/_app/source_mutations.py:class SourceRenameResult",
            "notebooklm/mcp/tools/sources.py:to_jsonable(result)",
        ),
        "derive": "runtime:mcp-source-rename-result",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "app-view:source-add-final-wrapper",
        "evidence": (
            "notebooklm/mcp/tools/sources.py:def _add_result_payload",
            "notebooklm/_app/views.py:source_view",
        ),
        "derive": "runtime:source-view-mcp-add",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "app-view:source-add-drive-final-wrapper",
        "evidence": ("notebooklm/mcp/tools/sources.py:to_jsonable(drive_result)",),
        "derive": "runtime:source-view-mcp-add-drive",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "app-view:source-add-drive-file-final-wrapper",
        "evidence": (
            "notebooklm/mcp/tools/sources_drive.py:payload = to_jsonable(result)",
            'notebooklm/mcp/tools/sources_drive.py:payload["source"] = _source_view',
        ),
        "derive": "runtime:source-view-mcp-add-drive-file",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "app-view:source-read-full-final-wrapper",
        "evidence": (
            'notebooklm/mcp/tools/sources.py:detail == "full"',
            'notebooklm/mcp/tools/sources.py:"source": _source_view(read.source)',
        ),
        "derive": "runtime:source-view-mcp-read-full",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "app-view:source-wait-final-wrapper",
        "model_contribution_keys": (
            "id",
            "title",
            "url",
            "_type_code",
            "created_at",
            "status",
            "drive_document_id",
            "drive_status",
        ),
        "projection_condition": (
            "at least one ready Source, a listed/noncanonical-resolved Source id reaches an "
            "error bucket, or source_add wait carries the freshly added Source.id"
        ),
        "contribution_semantics": (
            "Ready rows use the full Source view; error buckets may carry only a Source-derived "
            "id, and source_add wait may contribute only its top-level source_id. An explicit "
            "canonical-id wait whose outcomes are all timeout, failure, or not-found is non-public"
        ),
        "evidence": (
            "notebooklm/mcp/tools/_waitagg.py:def _aggregate_wait_outcomes",
            "notebooklm/mcp/tools/sources.py:def _wait_after_add",
            "notebooklm/mcp/tools/sources.py:src_ids = list(dict.fromkeys(await resolve_sources",
        ),
        "derive": "runtime:source-view-mcp-wait",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-batch-added-item-final-wrapper",
        "keys": ("status", "notebook_id", "added", "failed", "results"),
        "nested_union_keys": {
            "results": {
                "added": ("input", "status", "source_id", "title", "status_label"),
                "error": ("input", "status", "error"),
            }
        },
        "nested_keys": {"results[].error": ("code", "message", "retriable")},
        "nested_optional_keys": {
            "results[].added": ("warning",),
            "results[].error": ("unconfirmed", "candidates", "hint"),
        },
        "model_contribution_keys": ("id", "title", "status"),
        "projection_condition": "batch contains at least one successfully added public Source",
        "contribution_semantics": (
            "Each added Source contributes source_id, title, status_label, the added count, and "
            "aggregate status; scalar error siblings may coexist but an all-error batch is "
            "non-public"
        ),
        "evidence": ("notebooklm/mcp/tools/sources.py:async def _add_url_batch",),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-delete-confirmation-final-wrapper",
        "keys": ("status", "preview"),
        "nested_keys": {"preview": ("action", "notebook_id", "source_id", "title")},
        "evidence": (
            "notebooklm/mcp/tools/sources.py:title_for_id",
            "notebooklm/mcp/_confirm.py:def needs_confirmation",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-research-import-new-source-projection",
        "keys": ("id", "title"),
        "evidence": (
            "notebooklm/_research_import.py:def _imported_source_entry",
            "notebooklm/mcp/tools/research.py:newly_imported = to_jsonable(outcome.newly_imported)",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-research-import-existing-source-projection",
        "keys": ("id", "title", "url"),
        "evidence": (
            "notebooklm/_research_import.py:def _partition_requested_sources",
            "notebooklm/mcp/tools/research.py:already_present = to_jsonable(outcome.already_present)",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive:notebook-describe-metadata-source-summary-final-wrapper",
        "keys": ("notebook_id", "description", "metadata"),
        "nested_keys": {
            "metadata": ("notebook", "sources"),
            "metadata.sources": ("kind", "title", "url"),
        },
        "model_contribution_keys": ("title", "url", "_type_code"),
        "projection_condition": (
            "notebook_describe is called with include_metadata and metadata contains at "
            "least one listed public Source"
        ),
        "contribution_semantics": (
            "Each Source title/url and kind (backed by _type_code) constructs a public "
            "SourceSummary recursively serialized under metadata.sources; an empty "
            "metadata source list has no Source contribution"
        ),
        "evidence": (
            "notebooklm/_notebook_metadata.py:SourceSummary(",
            "notebooklm/mcp/tools/notebooks.py:metadata_block = to_jsonable",
            'notebooklm/mcp/tools/notebooks.py:output["metadata"] = metadata_block',
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-remote-upload-await-final-wrapper",
        "adapter_surface": "MCP await_upload tool result",
        "keys": ("status", "source_id", "file"),
        "nested_keys": {"file": ("source_id", "name", "size", "mime", "sha256")},
        "model_contribution_keys": ("id",),
        "evidence": (
            "notebooklm/mcp/_fileroutes.py:source_id = str(result.source.id)",
            'notebooklm/mcp/tools/_fileupload.py:return {"status": "received"',
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-remote-upload-http-final-wrapper",
        "adapter_surface": "MCP auxiliary file-route JSON response",
        "keys": ("status", "source_id"),
        "model_contribution_keys": ("id",),
        "evidence": (
            "notebooklm/mcp/_fileroutes.py:source_id = str(result.source.id)",
            'notebooklm/mcp/_fileroutes.py:{"status": "added", "source_id": source_id}',
        ),
    },
    {
        "model": "notebooklm.types.SourceFulltext",
        "mode": "manual-content-projection",
        "keys": (
            "notebook_id",
            "source_id",
            "source",
            "content",
            "char_count",
            "truncated",
            "output_format",
        ),
        "model_contribution_keys": ("content", "char_count"),
        "evidence": ('notebooklm/mcp/tools/sources.py:detail == "full"',),
    },
    {
        "model": "notebooklm.types.SourceGuide",
        "mode": "manual-guide-projection",
        "keys": ("notebook_id", "source_id", "summary", "keywords"),
        "model_contribution_keys": ("summary", "keywords"),
        "evidence": ('notebooklm/mcp/tools/sources.py:detail == "summary"',),
    },
    {
        "model": "notebooklm.auth.AuthTokens",
        "mode": "redacted-server-info-account-identity-contribution",
        "keys": ("server", "version", "auth", "account"),
        "nested_keys": {
            "auth": (
                "authenticated",
                "storage_exists",
                "json_valid",
                "cookies_present",
                "sid_cookie",
                "profile",
            )
        },
        "nested_union_keys": {
            "account": {
                "success": (
                    "email",
                    "authuser",
                    "available",
                    "notebook_limit",
                    "source_limit",
                    "tier",
                    "output_language",
                    "output_language_is_default",
                ),
                "unavailable": ("email", "authuser", "available", "reason"),
            }
        },
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
        "projection_condition": (
            "server_info include_account is true; authuser always comes from the live mutable "
            "in-memory AuthTokens returned by client.auth, while account_email contributes only "
            "when that in-memory/cached identity wins before persisted or live fallback"
        ),
        "adapter_surface": "MCP explicitly redacted safe-field identity contribution",
        "contribution_semantics": (
            "Only the live mutable AuthTokens.authuser and account_email may supply emitted "
            "account values; storage_path and _profile_session_generation only select "
            "persisted/cache/live fallback behavior, and the account unions expose no "
            "credential-bearing fields"
        ),
        "redacted_projection": "safe-field-contribution",
        "evidence": (
            "notebooklm/client.py:return self.auth.authuser",
            "notebooklm/client.py:return self._provider.auth",
            "notebooklm/_auth/account_email.py:def _session_key",
            "notebooklm/_auth/account_email.py:storage_path = auth.storage_path",
            "notebooklm/mcp/tools/meta.py:async def _account_block",
            'notebooklm/mcp/tools/meta.py:info["account"] = await _account_block',
        ),
    },
    {
        "model": "notebooklm.types.UserSettings",
        "mode": "transitive-server-info-account-success-wrapper",
        "keys": ("server", "version", "auth", "account"),
        "nested_keys": {
            "auth": (
                "authenticated",
                "storage_exists",
                "json_valid",
                "cookies_present",
                "sid_cookie",
                "profile",
            ),
            "account": (
                "email",
                "authuser",
                "available",
                "notebook_limit",
                "source_limit",
                "tier",
                "output_language",
                "output_language_is_default",
            ),
        },
        "model_contribution_keys": ("limits", "output_language"),
        "projection_condition": (
            "server_info include_account has a bound authenticated client and "
            "get_user_settings succeeds"
        ),
        "evidence": (
            "notebooklm/mcp/tools/meta.py:async def _account_block",
            "notebooklm/mcp/tools/meta.py:async def server_info",
        ),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "conditional-noncanonical-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/mcp/_resolve.py:async def resolve_notebook",
            "notebooklm/mcp/_resolve.py:items = await client.notebooks.list()",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "conditional-noncanonical-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/mcp/_resolve.py:async def resolve_source",
            "notebooklm/mcp/_resolve.py:items = await client.sources.list(notebook_id)",
        ),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "conditional-noncanonical-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/mcp/_resolve.py:async def resolve_note",
            "notebooklm/mcp/_resolve.py:items = await client.notes.list(notebook_id)",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "conditional-noncanonical-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/mcp/_resolve.py:async def resolve_artifact",
            "notebooklm/mcp/_resolve.py:items = await client.artifacts.list(notebook_id)",
        ),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "always-listed-studio-item-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/mcp/tools/_studio_items.py:async def resolve_studio_item",
            "notebooklm/mcp/tools/_studio_items.py:items = await studio_items(client, nb_id",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "always-listed-studio-item-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/mcp/tools/_studio_items.py:async def resolve_studio_item",
            "notebooklm/mcp/tools/_studio_items.py:items = await studio_items(client, nb_id",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "always-listed-studio-download-resolver-id-contribution",
        "keys": ("id",),
        "evidence": (
            "notebooklm/mcp/tools/_studio_download.py:def _resolve_artifact_id",
            "notebooklm/mcp/tools/studio.py:artifact_resolver=_resolve_artifact_id",
        ),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "conditional-resolver-error-text-contribution",
        "keys": ("message",),
        "optional_keys": ("hint",),
        "model_contribution_keys": ("id", "title"),
        "adapter_surface": "MCP ToolError flat message/hint",
        "evidence": (
            "notebooklm/mcp/_resolve.py:async def resolve_notebook",
            "notebooklm/mcp/_resolve.py:def _ambiguous_title_error",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "conditional-resolver-error-text-contribution",
        "keys": ("message",),
        "optional_keys": ("hint",),
        "model_contribution_keys": ("id", "title"),
        "adapter_surface": "MCP ToolError flat message/hint",
        "evidence": (
            "notebooklm/mcp/_resolve.py:async def resolve_source",
            "notebooklm/mcp/_resolve.py:def _ambiguous_title_error",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "conditional-resolver-error-text-contribution",
        "keys": ("message",),
        "optional_keys": ("hint",),
        "model_contribution_keys": ("id", "title"),
        "adapter_surface": "MCP ToolError flat message/hint",
        "evidence": (
            "notebooklm/mcp/_resolve.py:async def resolve_note",
            "notebooklm/mcp/_resolve.py:def _ambiguous_title_error",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "conditional-resolver-error-text-contribution",
        "keys": ("message",),
        "optional_keys": ("hint",),
        "model_contribution_keys": ("id", "title"),
        "adapter_surface": "MCP ToolError flat message/hint",
        "evidence": (
            "notebooklm/mcp/_resolve.py:async def resolve_artifact",
            "notebooklm/mcp/_resolve.py:def _ambiguous_title_error",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.Label",
        "mode": "always-listed-resolver-error-text-contribution",
        "keys": ("message",),
        "optional_keys": ("hint",),
        "model_contribution_keys": ("id", "name"),
        "adapter_surface": "MCP ToolError flat message/hint",
        "evidence": (
            "notebooklm/_app/labels.py:async def resolve_label_id",
            "notebooklm/mcp/tools/sources.py:label_resolver=labels_core.resolve_label_id",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "always-listed-studio-resolver-error-text-contribution",
        "keys": ("message",),
        "optional_keys": ("hint",),
        "model_contribution_keys": ("id", "title"),
        "adapter_surface": "MCP ToolError flat message/hint",
        "evidence": (
            "notebooklm/mcp/tools/_studio_items.py:def _match_studio_ref",
            "notebooklm/mcp/tools/_studio_items.py:async def resolve_studio_item",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "always-listed-studio-resolver-error-text-contribution",
        "keys": ("message",),
        "optional_keys": ("hint",),
        "model_contribution_keys": ("id", "title"),
        "adapter_surface": "MCP ToolError flat message/hint",
        "evidence": (
            "notebooklm/mcp/tools/_studio_items.py:def _match_studio_ref",
            "notebooklm/mcp/tools/_studio_items.py:async def resolve_studio_item",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "always-listed-studio-download-resolver-error-text-contribution",
        "keys": ("message",),
        "optional_keys": ("hint",),
        "model_contribution_keys": ("id", "title"),
        "adapter_surface": "MCP ToolError flat message/hint",
        "evidence": (
            "notebooklm/mcp/tools/_studio_download.py:def _resolve_artifact_id",
            "notebooklm/mcp/tools/studio.py:artifact_resolver=_resolve_artifact_id",
            "notebooklm/mcp/_errors.py:def to_tool_error",
        ),
    },
)

__all__ = ["MCP_PROJECTION_SPECS"]
