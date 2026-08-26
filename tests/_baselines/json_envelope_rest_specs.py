"""Reviewed REST response public-model projection declarations."""

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

REST_PROJECTION_SPECS: tuple[dict[str, object], ...] = (
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
            "notebooklm/server/routes/meta.py:async def _account_block",
            "notebooklm/server/routes/meta.py:async def server_info",
        ),
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
            "server_info include_account is true and a bound client exists with no startup error"
        ),
        "adapter_surface": "REST explicitly redacted safe-field identity contribution",
        "contribution_semantics": (
            "Only the live mutable AuthTokens.authuser and account_email may supply emitted "
            "account values; storage_path and _profile_session_generation only select "
            "persisted/cache/live fallback behavior. The startup-error persisted-identity "
            "branch has no AuthTokens contribution, and no credential-bearing field reaches "
            "either account union"
        ),
        "redacted_projection": "safe-field-contribution",
        "evidence": (
            "notebooklm/client.py:return self.auth.authuser",
            "notebooklm/client.py:return self._provider.auth",
            "notebooklm/_auth/account_email.py:def _session_key",
            "notebooklm/_auth/account_email.py:storage_path = auth.storage_path",
            "notebooklm/server/routes/meta.py:async def _account_block",
            'notebooklm/server/routes/meta.py:info["account"] = await _account_block',
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "dataclass-full",
        "evidence": ("notebooklm/server/routes/artifacts.py:to_jsonable(artifacts)",),
        "nested_fields": "all",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "dataclass-list-default-final-wrapper",
        "keys": ("notebook_id", "artifacts"),
        "nested_keys": {
            "artifacts": (
                "id",
                "title",
                "kind",
                "status",
                "created_at",
                "updated_at",
                "url",
                "generation_prompt",
                "source_ids",
                "audio_url",
                "video_url",
                "slides_url",
                "report_url",
                "data_table_url",
                "infographic_url",
                "mind_map_id",
                "mind_map_kind",
                "is_note_backed_mind_map",
            )
        },
        "evidence": (
            "notebooklm/server/routes/artifacts.py:def list_artifacts",
            "notebooklm/server/_pagination.py:if limit is None",
        ),
        "derive": "runtime:list-wrapper-rest-artifact-default",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "dataclass-list-paged-final-wrapper",
        "keys": ("notebook_id", "artifacts", "meta"),
        "nested_keys": {
            "artifacts": (
                "id",
                "title",
                "kind",
                "status",
                "created_at",
                "updated_at",
                "url",
                "generation_prompt",
                "source_ids",
                "audio_url",
                "video_url",
                "slides_url",
                "report_url",
                "data_table_url",
                "infographic_url",
                "mind_map_id",
                "mind_map_kind",
                "is_note_backed_mind_map",
            ),
            "meta": ("total", "has_more", "limit", "offset"),
        },
        "evidence": (
            "notebooklm/server/routes/artifacts.py:def list_artifacts",
            "notebooklm/server/_pagination.py:def paginate_envelope",
        ),
        "derive": "runtime:list-wrapper-rest-artifact-paged",
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-download-no-artifacts-409-error-contribution",
        "keys": ("error",),
        "nested_keys": {"error": ("category", "message")},
        "model_contribution_keys": ("_artifact_type", "status", "_variant"),
        "projection_condition": (
            "REST artifact download lists at least one public Artifact, but every row is "
            "excluded by requested kind or completion state"
        ),
        "contribution_semantics": (
            "Artifact kind (backed by _artifact_type/_variant) and completion (backed by "
            "status) select the NO_ARTIFACTS 409 conflict envelope; a truly empty list is "
            "non-public and successful downloads return binary FileResponse data"
        ),
        "evidence": (
            "notebooklm/_app/download.py:typed = [",
            "notebooklm/_app/download.py:if not type_artifacts",
            "notebooklm/server/routes/artifacts.py:if result.outcome != download_core.DownloadOutcome.SINGLE_DOWNLOADED",
            "notebooklm/server/_errors.py:_STATUS_LABEL",
        ),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "app-status-view-projection",
        "evidence": (
            "notebooklm/_app/artifacts.py:status_view",
            "notebooklm/server/routes/artifacts.py:projected =",
        ),
        "derive": "runtime:generation-status-view",
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-retry-projection",
        "keys": ("notebook_id", "artifact_id", "task_id", "status"),
        "evidence": ("notebooklm/server/routes/artifacts.py:async def retry",),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "manual-generate-projection",
        "keys": ("notebook_id", "kind", "task_id", "status", "url", "error"),
        "evidence": (
            "notebooklm/_app/generate_retry.py:generation_outcome_from_status",
            "notebooklm/server/routes/artifacts.py:_generation_payload",
        ),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "http-failed-409-error-envelope",
        "keys": ("error",),
        "nested_keys": {"error": ("category", "message")},
        "evidence": (
            "notebooklm/server/routes/artifacts.py:state == GenerationState.FAILED",
            "notebooklm/server/_errors.py:http_error_response",
        ),
    },
    {
        "model": "notebooklm.types.GenerationStatus",
        "mode": "http-removed-410-error-envelope",
        "keys": ("error",),
        "nested_keys": {"error": ("category", "message")},
        "evidence": (
            "notebooklm/server/routes/artifacts.py:state == GenerationState.REMOVED",
            "notebooklm/server/_errors.py:http_error_response",
        ),
    },
    {
        "model": "notebooklm.types.AskResult",
        "mode": "app-view:ask_result_view",
        "evidence": (
            "notebooklm/_app/views.py:ask_result_view",
            "notebooklm/server/routes/chat.py:ask_result_view",
        ),
        "derive": "runtime:ask-result-base",
        "nested_fields": ("references", "turn_key", "next_steps"),
    },
    {
        "model": "notebooklm._types.documents.StructuredDocument",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "nested_keys": {"references": _FULL_REFERENCE_KEYS},
        "model_contribution_keys": ("blocks", "annotations"),
        "projection_condition": (
            "the answer document contains an in-range annotation whose object_id matches "
            "a surviving ChatReference.chunk_id"
        ),
        "contribution_semantics": (
            "StructuredDocument annotations and block-derived text/extent produce "
            "answer_anchor_start/end; answer_document itself is removed from the REST response"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/server/routes/chat.py:return ask_result_view(result)",
        ),
    },
    {
        "model": "notebooklm._types.documents.DocumentAnnotation",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "nested_keys": {"references": _FULL_REFERENCE_KEYS},
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
            "notebooklm/server/routes/chat.py:return ask_result_view(result)",
        ),
    },
    {
        "model": "notebooklm._types.documents.DocumentBlock",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "nested_keys": {"references": _FULL_REFERENCE_KEYS},
        "model_contribution_keys": ("start_index", "end_index", "spans"),
        "projection_condition": (
            "a decoded citation fragment contains a usable DocumentBlock, or an accepted "
            "answer anchor depends on the answer document's block extent/text"
        ),
        "contribution_semantics": (
            "DocumentBlock ranges/text produce cited_text and fragment ranges, while "
            "answer-document blocks also bound anchor validity"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def extract_text_passages",
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/server/routes/chat.py:return ask_result_view(result)",
        ),
    },
    {
        "model": "notebooklm._types.documents.TextSpan",
        "mode": "transitive-chat-reference-full-contribution",
        "keys": _ASK_ROOT_KEYS,
        "nested_keys": {"references": _FULL_REFERENCE_KEYS},
        "model_contribution_keys": ("start_index", "end_index", "text"),
        "projection_condition": (
            "a decoded citation fragment contains a usable TextSpan, or an accepted answer "
            "anchor depends on answer-document text built from a TextSpan"
        ),
        "contribution_semantics": (
            "TextSpan offsets/text build cited_text/ranges or answer-document text used to "
            "validate answer anchors"
        ),
        "evidence": (
            "notebooklm/_web/codec/chat_stream.py:def extract_text_passages",
            "notebooklm/_web/codec/chat_stream.py:def attach_answer_anchors",
            "notebooklm/server/routes/chat.py:return ask_result_view(result)",
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-interactive-mind-map-generation-final-contribution",
        "keys": ("notebook_id", "kind", "mind_map"),
        "nested_keys": {"mind_map": ("id", "notebook_id", "title", "kind", "created_at", "tree")},
        "model_contribution_keys": ("id", "title", "_artifact_type", "created_at", "_variant"),
        "projection_condition": (
            "interactive mind-map generation finds the newly created public Artifact"
        ),
        "contribution_semantics": (
            "Artifact id/title/created_at construct the recursively emitted MindMap while "
            "_artifact_type/_variant qualify the row as interactive (or the known-id "
            "unclassified settling case); the raw create-id fallback has no Artifact"
        ),
        "evidence": (
            "notebooklm/_mind_maps_api.py:artifact = project_artifact(record)",
            "notebooklm/_mind_maps_api.py:return MindMap(",
            'notebooklm/server/routes/artifacts.py:payload["mind_map"] = to_jsonable',
        ),
    },
    {
        "model": "notebooklm.types.MindMap",
        "mode": "dataclass-full-nested-in-generation-wrapper",
        "evidence": ('notebooklm/server/routes/artifacts.py:payload["mind_map"]',),
        "derive": "runtime:mind-map-rest-final",
    },
    {
        "model": "notebooklm.types.MindMapResult",
        "mode": "dataclass-full-nested-in-generation-wrapper",
        "evidence": ('notebooklm/server/routes/artifacts.py:payload["mind_map"]',),
        "derive": "runtime:mind-map-rest-final",
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "transitive-note-backed-mind-map-generation-final-contribution",
        "keys": ("notebook_id", "kind", "mind_map"),
        "nested_keys": {"mind_map": ("mind_map", "note_id", "created_at")},
        "model_contribution_keys": ("id", "created_at"),
        "projection_condition": (
            "note-backed mind-map generation returns a present leaf and successfully "
            "creates a public Note"
        ),
        "contribution_semantics": (
            "The created Note.id and Note.created_at are copied into MindMapResult and "
            "recursively emitted as mind_map.note_id/created_at; interactive or absent-leaf "
            "paths have no Note contribution"
        ),
        "evidence": (
            "notebooklm/_web/bindings/mind_maps.py:note = await _note_service",
            'notebooklm/server/routes/artifacts.py:payload["mind_map"] = to_jsonable',
        ),
    },
    {
        "model": "notebooklm.types.Artifact",
        "mode": "transitive-mind-map-rename-membership-final-contribution",
        "keys": (
            "status",
            "notebook_id",
            "artifact_id",
            "new_title",
            "is_mind_map",
        ),
        "model_contribution_keys": ("id", "_artifact_type", "_variant"),
        "projection_condition": (
            "mind_maps.list converts a listed interactive Artifact whose id matches the target"
        ),
        "contribution_semantics": (
            "Artifact id and interactive kind (backed by _artifact_type/_variant) construct "
            "the matching MindMap whose membership sets is_mind_map; unmatched regular "
            "artifacts use the same terminal without this contribution"
        ),
        "evidence": (
            "notebooklm/_mind_maps_api.py:artifact = project_artifact(record)",
            "notebooklm/server/routes/artifacts.py:is_mind_map",
        ),
    },
    {
        "model": "notebooklm.types.MindMap",
        "mode": "transitive-artifact-rename-final-wrapper",
        "keys": (
            "status",
            "notebook_id",
            "artifact_id",
            "new_title",
            "is_mind_map",
        ),
        "model_contribution_keys": ("id",),
        "projection_condition": "mind_maps.list returns a MindMap whose id matches the target",
        "contribution_semantics": (
            "MindMap.id membership determines is_mind_map; MindMap.kind routes the mutation but "
            "is not projected, and an unmatched artifact uses the terminal without a MindMap"
        ),
        "evidence": (
            "notebooklm/_app/artifacts.py:async def rename_artifact",
            "notebooklm/server/routes/artifacts.py:async def rename",
        ),
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "dataclass-full",
        "evidence": ("notebooklm/server/routes/notes.py:to_jsonable",),
        "nested_fields": "all",
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "dataclass-list-default-final-wrapper",
        "keys": ("notebook_id", "notes"),
        "nested_keys": {"notes": ("id", "notebook_id", "title", "content", "created_at")},
        "evidence": (
            "notebooklm/server/routes/notes.py:def list_notes",
            "notebooklm/server/_pagination.py:if limit is None",
        ),
        "derive": "runtime:list-wrapper-rest-note-default",
    },
    {
        "model": "notebooklm.types.Note",
        "mode": "dataclass-list-paged-final-wrapper",
        "keys": ("notebook_id", "notes", "meta"),
        "nested_keys": {
            "notes": ("id", "notebook_id", "title", "content", "created_at"),
            "meta": ("total", "has_more", "limit", "offset"),
        },
        "evidence": (
            "notebooklm/server/routes/notes.py:def list_notes",
            "notebooklm/server/_pagination.py:def paginate_envelope",
        ),
        "derive": "runtime:list-wrapper-rest-note-paged",
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "app-view:notebook_view",
        "evidence": (
            "notebooklm/_app/views.py:notebook_view",
            "notebooklm/server/routes/notebooks.py:notebook_view",
        ),
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "app-view:notebook-list-default-final-wrapper",
        "evidence": (
            "notebooklm/server/routes/notebooks.py:def list_notebooks",
            "notebooklm/server/_pagination.py:if limit is None",
        ),
        "derive": "runtime:list-wrapper-rest-notebook-default",
    },
    {
        "model": "notebooklm.types.Notebook",
        "mode": "app-view:notebook-list-paged-final-wrapper",
        "evidence": (
            "notebooklm/server/routes/notebooks.py:def list_notebooks",
            "notebooklm/server/_pagination.py:def paginate_envelope",
        ),
        "derive": "runtime:list-wrapper-rest-notebook-paged",
    },
    {
        "model": "notebooklm.types.PromptSuggestion",
        "mode": "manual-rest-final-wrapper",
        "evidence": ('notebooklm/server/routes/notebooks.py:"suggestions":',),
        "derive": "runtime:prompt-suggestion-rest",
    },
    {
        "model": "notebooklm.types.ResearchStart",
        "mode": "dataclass-full-with-poll-id",
        "keys": ("task_id", "report_id", "notebook_id", "query", "mode", "poll_id"),
        "model_contribution_keys": ("task_id", "report_id", "query", "mode"),
        "evidence": ("notebooklm/server/routes/research.py:to_jsonable(result)",),
    },
    {
        "model": "notebooklm.types.ResearchStart",
        "mode": "transitive-start-missing-poll-id-error-contribution",
        "keys": ("error",),
        "nested_keys": {"error": ("category", "message", "retriable")},
        "model_contribution_keys": ("task_id", "report_id"),
        "projection_condition": (
            "deep start has no report_id or fast start has no task_id, so no poll_id can be formed"
        ),
        "contribution_semantics": (
            "ResearchStart field emptiness selects the fixed DecodingError variant; field values "
            "are not copied into the REST error message"
        ),
        "evidence": (
            "notebooklm/server/routes/research.py:if not result.report_id",
            "notebooklm/server/routes/research.py:if not result.task_id",
            "notebooklm/server/_errors.py:async def _handle_library",
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "manual-status-projection",
        "keys": (
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
        "evidence": ("notebooklm/server/routes/research.py:research_status",),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-import-final-wrapper",
        "keys": ("status", "notebook_id", "run_id", "imported", "sources_found"),
        "model_contribution_keys": ("status", "sources"),
        "projection_condition": "ResearchTask is completed and classified as importable",
        "contribution_semantics": (
            "status selects the import success terminal and source membership supplies "
            "sources_found"
        ),
        "evidence": (
            "notebooklm/_app/research.py:status = await client.research.poll",
            'notebooklm/server/routes/research.py:"sources_found": len(sources)',
        ),
    },
    {
        "model": "notebooklm.types.ResearchTask",
        "mode": "transitive-import-refusal-error-contribution",
        "keys": ("error",),
        "nested_keys": {"error": ("category", "message", "retriable")},
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
        "contribution_semantics": (
            "ResearchTask fields select and populate the ValidationError body; completed-empty "
            "uses only source-list membership and no ResearchSource field is projected"
        ),
        "evidence": (
            "notebooklm/_app/research.py:def classify_importable_research",
            "notebooklm/server/routes/research.py:sources = await research_core.poll_sources_for_import",
            "notebooklm/server/_errors.py:async def _handle_library",
        ),
    },
    {
        "model": "notebooklm.types.ResearchSource",
        "mode": "nested-public-dict-projection",
        "evidence": (
            "notebooklm/_app/research.py:src.to_public_dict()",
            "notebooklm/server/routes/research.py:to_jsonable(result.sources)",
        ),
        "derive": "runtime:research-source-public-dict",
    },
    {
        "model": "notebooklm.types.ResearchSource",
        "mode": "transitive-import-source-count-contribution",
        "keys": ("status", "notebook_id", "run_id", "imported", "sources_found"),
        "projection_condition": ("an importable ResearchTask contains at least one ResearchSource"),
        "contribution_semantics": (
            "ResearchSource list membership contributes only to sources_found; no source "
            "field envelope is serialized on this path"
        ),
        "evidence": (
            "notebooklm/_app/research.py:sources=[src.to_public_dict() for src in status.sources]",
            'notebooklm/server/routes/research.py:"sources_found": len(sources)',
        ),
    },
    {
        "model": "notebooklm.types.ShareStatus",
        "mode": "app-view:share_status_view",
        "evidence": (
            "notebooklm/_app/views.py:share_status_view",
            "notebooklm/server/routes/share.py:share_status_view",
        ),
    },
    {
        "model": "notebooklm.types.ShareStatus",
        "mode": "app-view:share_status_view+view_level",
        "evidence": ("notebooklm/server/routes/share.py:include_view_level=True",),
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
            "notebooklm/server/routes/sources.py:source_view",
        ),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "app-view:source-list-default-final-wrapper",
        "evidence": (
            "notebooklm/server/routes/sources.py:def list_sources",
            "notebooklm/server/_pagination.py:if limit is None",
        ),
        "derive": "runtime:list-wrapper-rest-source-default",
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "app-view:source-list-paged-final-wrapper",
        "evidence": (
            "notebooklm/server/routes/sources.py:def list_sources",
            "notebooklm/server/_pagination.py:def paginate_envelope",
        ),
        "derive": "runtime:list-wrapper-rest-source-paged",
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
            "at least one ready Source or wait-all lists a Source whose id reaches an error bucket"
        ),
        "contribution_semantics": (
            "Ready rows use the full Source view; wait-all error buckets may carry only a "
            "Source-derived id. An explicit canonical-id wait whose outcomes are all timeout, "
            "failure, or not-found is non-public, as is an empty wait-all result"
        ),
        "evidence": (
            "notebooklm/server/routes/sources.py:def _aggregate_wait_outcomes",
            "notebooklm/_app/views.py:source_view",
            "notebooklm/server/routes/sources.py:ids = _dedupe_source_ids([s.id for s in await client.sources.list",
        ),
        "derive": "runtime:source-view-rest-wait",
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
        "nested_keys": {"results[].error": ("category", "message", "retriable")},
        "nested_optional_keys": {"results[].error": ("unconfirmed", "candidates", "hint")},
        "model_contribution_keys": ("id", "title", "status"),
        "projection_condition": "batch contains at least one successfully added public Source",
        "contribution_semantics": (
            "Each added Source contributes source_id, title, status_label, the added count, and "
            "aggregate status; scalar error siblings may coexist but an all-error batch is "
            "non-public"
        ),
        "evidence": ("notebooklm/server/routes/sources.py:async def add_batch",),
    },
    {
        "model": "notebooklm.types.Source",
        "mode": "transitive-source-content-readiness-contribution",
        "keys": (
            "notebook_id",
            "source_id",
            "content",
            "char_count",
            "truncated",
            "output_format",
        ),
        "model_contribution_keys": ("status",),
        "projection_condition": "full source-content response after the public Source readiness gate",
        "contribution_semantics": (
            "Source.status/is_ready selects fetched content versus the stable null, zero, false "
            "response values; the SourceFulltext row covers only the fetched branch"
        ),
        "evidence": (
            "notebooklm/_app/source_content.py:if source.is_ready",
            "notebooklm/server/routes/sources.py:read = await content_core.execute_source_read",
        ),
    },
    {
        "model": "notebooklm.types.SourceFulltext",
        "mode": "manual-content-projection",
        "keys": (
            "notebook_id",
            "source_id",
            "content",
            "char_count",
            "truncated",
            "output_format",
        ),
        "model_contribution_keys": ("content", "char_count"),
        "evidence": ("notebooklm/server/routes/sources.py:get_source_content",),
    },
    {
        "model": "notebooklm.types.SourceGuide",
        "mode": "manual-guide-projection",
        "keys": ("notebook_id", "source_id", "summary", "keywords"),
        "model_contribution_keys": ("summary", "keywords"),
        "evidence": ('notebooklm/server/routes/sources.py:detail == "summary"',),
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
            "notebooklm/server/routes/meta.py:async def _account_block",
            "notebooklm/server/routes/meta.py:async def server_info",
        ),
    },
)

__all__ = ["REST_PROJECTION_SPECS"]
