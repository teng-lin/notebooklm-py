"""Structural P0 inventories for the client-ownership refactor.

These registries make the migration surface executable before any behavior is
changed.  Every row names the phase that owns it and a concrete disposition;
``TBD`` is deliberately forbidden.  Later phases must update a row in the same
change that moves or removes its source construct.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "notebooklm"
APP = SRC / "_app"


@dataclass(frozen=True)
class Disposition:
    phase: str
    action: str


@dataclass(frozen=True)
class SignatureInventory:
    path: str
    symbol: str
    parameters: tuple[str, ...]
    disposition: Disposition


CONSTRUCTION_SIGNATURES = (
    SignatureInventory(
        "src/notebooklm/client.py",
        "NotebookLMClient.__init__",
        (
            "self",
            "auth",
            "timeout",
            "storage_path",
            "keepalive",
            "keepalive_min_interval",
            "rate_limit_max_retries",
            "server_error_max_retries",
            "limits",
            "max_concurrent_uploads",
            "max_concurrent_rpcs",
            "upload_timeout",
            "on_rpc_event",
            "cookie_saver",
            "cookie_rotator",
            "chat_timeout",
            "chat_response_max_bytes",
            "import_research_timeout",
            "backend",
        ),
        Disposition("P5/P8", "Normalize once into typed owner configs; remove flat tuning at v1."),
    ),
    SignatureInventory(
        "src/notebooklm/client.py",
        "NotebookLMClient.from_storage",
        (
            "cls",
            "path",
            "timeout",
            "profile",
            "keepalive",
            "keepalive_min_interval",
            "rate_limit_max_retries",
            "server_error_max_retries",
            "limits",
            "max_concurrent_uploads",
            "max_concurrent_rpcs",
            "upload_timeout",
            "on_rpc_event",
            "chat_timeout",
            "chat_response_max_bytes",
            "import_research_timeout",
            "allow_headless",
            "backend",
        ),
        Disposition(
            "P5/P8",
            "Keep credential controls separate, add config normalization, then remove flat tuning.",
        ),
    ),
    SignatureInventory(
        "src/notebooklm/_client_assembly.py",
        "_assemble_client",
        (
            "client",
            "auth",
            "timeout",
            "storage_path",
            "keepalive",
            "keepalive_min_interval",
            "rate_limit_max_retries",
            "server_error_max_retries",
            "limits",
            "max_concurrent_uploads",
            "max_concurrent_rpcs",
            "upload_timeout",
            "on_rpc_event",
            "cookie_saver",
            "cookie_rotator",
            "chat_timeout",
            "import_research_timeout",
            "chat_response_max_bytes",
            "backend",
            "refresh_callback",
            "refresh_retry_delay",
            "connect_timeout",
            "keepalive_storage_path",
            "decode_response",
            "sleep",
            "is_auth_error",
            "async_client_factory",
            "master_token_reader",
            "oauth_minter",
        ),
        Disposition("P4/P5", "Return a complete graph, then accept typed specs and dependencies."),
    ),
    SignatureInventory(
        "src/notebooklm/_web/assembly.py",
        "assemble_web_backend",
        (
            "client",
            "auth",
            "timeout",
            "storage_path",
            "keepalive",
            "keepalive_min_interval",
            "rate_limit_max_retries",
            "server_error_max_retries",
            "limits",
            "max_concurrent_uploads",
            "max_concurrent_rpcs",
            "upload_timeout",
            "on_rpc_event",
            "cookie_saver",
            "cookie_rotator",
            "chat_timeout",
            "import_research_timeout",
            "chat_response_max_bytes",
            "refresh_callback",
            "use_default_refresh_callback",
            "refresh_retry_delay",
            "connect_timeout",
            "keepalive_storage_path",
            "async_client_factory",
            "decode_response",
            "sleep",
            "is_auth_error",
            "shared_config",
        ),
        Disposition("P4/P5", "Stop accepting a client; consume Web config and Web dependencies."),
    ),
    SignatureInventory(
        "src/notebooklm/_web/assembly.py",
        "build_compatibility_runtime",
        (
            "auth",
            "refresh_callback",
            "use_default_refresh_callback",
            "shared",
            "shared_config",
            "seam_overrides",
            "timeout",
            "refresh_retry_delay",
            "rate_limit_max_retries",
            "server_error_max_retries",
            "max_concurrent_uploads",
            "async_client_factory",
        ),
        Disposition(
            "P4/P5/P8", "Use a typed sidecar spec and dependencies; remove the sidecar at v1."
        ),
    ),
    SignatureInventory(
        "src/notebooklm/_android/assembly.py",
        "assemble_android_backend",
        (
            "client",
            "profile_path",
            "master_token_reader",
            "oauth_minter",
            "timeout",
            "refresh_retry_delay",
            "rate_limit_max_retries",
            "server_error_max_retries",
            "max_concurrent_uploads",
            "upload_timeout",
            "chat_timeout",
            "import_research_timeout",
            "chat_response_max_bytes",
            "sleep",
            "shared_config",
            "on_rpc_event",
        ),
        Disposition(
            "P4/P5", "Stop accepting a client; consume Android config and Android dependencies."
        ),
    ),
    SignatureInventory(
        "tests/_helpers/client_factory.py",
        "build_client_shell_for_tests",
        (
            "auth",
            "timeout",
            "connect_timeout",
            "refresh_callback",
            "refresh_retry_delay",
            "keepalive",
            "keepalive_min_interval",
            "keepalive_storage_path",
            "rate_limit_max_retries",
            "server_error_max_retries",
            "limits",
            "max_concurrent_uploads",
            "max_concurrent_rpcs",
            "on_rpc_event",
            "cookie_saver",
            "cookie_rotator",
            "chat_response_max_bytes",
            "backend",
            "decode_response",
            "sleep",
            "is_auth_error",
            "async_client_factory",
            "master_token_reader",
            "oauth_minter",
        ),
        Disposition(
            "P4/P5",
            "Keep its current public subset, split private dependencies, and use typed assembly.",
        ),
    ),
)


PUBLIC_OPTION_DISPOSITIONS = {
    "auth": Disposition("P5/P8", "Remain a credential input; remove obsolete shadows only at v1."),
    "storage_path": Disposition("P5", "Remain a direct credential-normalization override."),
    "path": Disposition("P5", "Remain a stored-credential loader input with path precedence."),
    "profile": Disposition("P5", "Remain a stored-credential profile selector."),
    "allow_headless": Disposition("P5", "Remain a stored-auth recovery policy input."),
    "backend": Disposition(
        "P5/P8", "Freeze into ClientConfig.backend; remove the flat knob at v1."
    ),
    "timeout": Disposition(
        "P5/P8", "Map losslessly to the selected backend; remove flat tuning at v1."
    ),
    "keepalive": Disposition("P5/P8", "Move to WebSessionOptions; remove flat tuning at v1."),
    "keepalive_min_interval": Disposition(
        "P5/P8", "Move to WebSessionOptions with its current validation; remove flat tuning at v1."
    ),
    "rate_limit_max_retries": Disposition(
        "P5/P8", "Move to RetryOptions; remove flat tuning after its warning window."
    ),
    "server_error_max_retries": Disposition(
        "P5/P8", "Move to RetryOptions; remove flat tuning after its warning window."
    ),
    "limits": Disposition(
        "P5/P8", "Move all pool fields to WebTransportOptions; remove flat tuning."
    ),
    "max_concurrent_uploads": Disposition(
        "P5/P8", "Move to TransferOptions while preserving None; remove flat tuning."
    ),
    "max_concurrent_rpcs": Disposition(
        "P5/P8", "Move to RuntimeOptions while preserving None; remove flat tuning."
    ),
    "upload_timeout": Disposition(
        "P5/P8", "Normalize all four components into transfer phases; remove flat tuning."
    ),
    "on_rpc_event": Disposition("P5", "Move to ClientConfig as a construction hook."),
    "cookie_saver": Disposition(
        "P5/P8", "Move to WebSessionHooks; remove the flat callback at v1."
    ),
    "cookie_rotator": Disposition(
        "P5/P8", "Move to WebSessionHooks; remove the flat callback at v1."
    ),
    "chat_timeout": Disposition("P5/P8", "Move to FeatureOptions; remove flat tuning at v1."),
    "chat_response_max_bytes": Disposition(
        "P5/P8", "Move to FeatureOptions; remove flat tuning at v1."
    ),
    "import_research_timeout": Disposition(
        "P5/P8", "Move to FeatureOptions; remove flat tuning at v1."
    ),
}


PRIVATE_DEPENDENCY_DISPOSITIONS = {
    "refresh_callback": Disposition("P5", "Move to WebDependencies; preserve UNSET versus None."),
    "keepalive_storage_path": Disposition(
        "P5", "Move to WebDependencies; preserve UNSET, None, and derived-path states."
    ),
    "refresh_retry_delay": Disposition("P5", "Keep private in WebDependencies and validate once."),
    "connect_timeout": Disposition("P5", "Keep private in WebDependencies."),
    "decode_response": Disposition("P5", "Move to WebDependencies with late binding intact."),
    "is_auth_error": Disposition("P5", "Move to WebDependencies with late binding intact."),
    "async_client_factory": Disposition("P5", "Move to WebDependencies as a test factory."),
    "sleep": Disposition("P5/P6", "Inject only into owners sharing retry/deadline time."),
    "master_token_reader": Disposition(
        "P5", "Move to AndroidDependencies; keep construction inert."
    ),
    "oauth_minter": Disposition("P5", "Move to AndroidDependencies; keep construction inert."),
}


@dataclass(frozen=True)
class SymbolInventory:
    path: str
    symbol: str
    disposition: Disposition


RETRY_INVENTORY = (
    SymbolInventory(
        "src/notebooklm/_notebooks.py",
        "NotebooksAPI._create_with_probe",
        Disposition("P1/P2", "Remove re-send and report uncorrelated candidates as unknown."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sources/add.py",
        "SourceAddService.add_url",
        Disposition("P1/P2", "Remove probe-authorized re-send; journal create and inspection."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sources/add.py",
        "SourceAddService.add_drive",
        Disposition("P1/P2", "Remove probe-authorized re-send; journal create and inspection."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sources/upload.py",
        "SourceUploadPipeline._register_file_source_result",
        Disposition(
            "P1/P2", "Remove probe-authorized re-send; journal registration and inspection."
        ),
    ),
    SymbolInventory(
        "src/notebooklm/_research.py",
        "BaseResearchAPI._import_sources_with_verification",
        Disposition("P1/P2", "Demote re-send to bounded candidate inspection and journal results."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/research.py",
        "WebResearchAPI._import_sources_with_verification",
        Disposition("P1", "Delete duplicate retry loop and inherit the neutral implementation."),
    ),
    SymbolInventory(
        "src/notebooklm/artifacts.py",
        "with_rate_limit_retry",
        Disposition(
            "P1/P2/P6", "Require shared replay evidence, one counter, and remaining budget."
        ),
    ),
    SymbolInventory(
        "src/notebooklm/_web/transport/middleware/retry.py",
        "RetryMiddleware.__call__",
        Disposition(
            "P1/P2/P6", "Apply replay evidence and deadline without replaying sent chat writes."
        ),
    ),
    SymbolInventory(
        "src/notebooklm/_web/transport/middleware/auth_refresh.py",
        "AuthRefreshMiddleware.__call__",
        Disposition(
            "P1/P2/P6", "Refresh chat credentials without re-POST and share deadline evidence."
        ),
    ),
    SymbolInventory(
        "src/notebooklm/_android/session.py",
        "AndroidSession._unary_impl",
        Disposition(
            "P1/P2/P6", "Use manifest semantics, journal attempts, and the aggregate deadline."
        ),
    ),
    SymbolInventory(
        "src/notebooklm/_artifact/polling.py",
        "ArtifactPollingService.wait_for_completion",
        Disposition("P6", "Preserve replay-safe polling and clamp it to the operation deadline."),
    ),
    SymbolInventory(
        "src/notebooklm/_source/polling.py",
        "SourcePoller.wait_until_ready",
        Disposition(
            "P2/P6", "Keep reads replay-safe and separate their evidence from create state."
        ),
    ),
)


CLEANUP_INVENTORY = (
    SymbolInventory(
        "src/notebooklm/_android/drive_staging.py",
        "DriveStagingTransfer.scope",
        Disposition(
            "P1/P2/P6", "Retain staging unless import settlement positively permits DELETE."
        ),
    ),
    SymbolInventory(
        "src/notebooklm/_web/notes.py",
        "NoteService._delete_note_best_effort",
        Disposition("P2/P6", "Preserve ordered known-ID orphan cleanup and journal its own send."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sources/upload.py",
        "SourceUploadPipeline.cancel_upload_session",
        Disposition("P2/P6", "Preserve pre-finalize cancel; fence it by generation and deadline."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sources/drive_import.py",
        "DriveImportService.add_drive_file",
        Disposition(
            "P1", "Preserve local temp-file cleanup; it is not a remote prerequisite DELETE."
        ),
    ),
    SymbolInventory(
        "src/notebooklm/_android/upload.py",
        "AndroidUploadPipeline.drive_download_scope",
        Disposition("P1/P6", "Preserve local file/directory cleanup after writer settlement."),
    ),
    SymbolInventory(
        "src/notebooklm/_artifact/_guarded_transfer.py",
        "guarded_transfer",
        Disposition("P6", "Preserve advisory local cleanup and exception precedence."),
    ),
    SymbolInventory(
        "src/notebooklm/_runtime/lifecycle.py",
        "ClientLifecycle._rollback_open",
        Disposition("P4/P6", "Preserve transactional open rollback and process-exit precedence."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/transport/kernel.py",
        "Kernel.open",
        Disposition("P4/P5", "Preserve failed-open HTTP client cleanup under typed construction."),
    ),
)

TARGET_CLEANUP_SYMBOLS = {
    "DriveStagingTransfer.scope",
}

PRESERVED_CLEANUP_SYMBOLS = {
    "NoteService._delete_note_best_effort",
    "SourceUploadPipeline.cancel_upload_session",
    "DriveImportService.add_drive_file",
    "AndroidUploadPipeline.drive_download_scope",
    "guarded_transfer",
    "ClientLifecycle._rollback_open",
    "Kernel.open",
}


WORKFLOW_INVENTORY = (
    SymbolInventory(
        "src/notebooklm/_notebooks.py",
        "NotebooksAPI._operation_scope",
        Disposition("P3", "Inject supervisor scope for create/copy and composite reads."),
    ),
    SymbolInventory(
        "src/notebooklm/_chat.py",
        "ChatAPI._operation_scope",
        Disposition("P3", "Scope locks, history, send, and cache publication."),
    ),
    SymbolInventory(
        "src/notebooklm/_research.py",
        "BaseResearchAPI._operation_scope",
        Disposition("P3", "Scope start/import/cancel and required readback."),
    ),
    SymbolInventory(
        "src/notebooklm/_artifacts.py",
        "ArtifactsAPI._operation_scope",
        Disposition("P3", "Scope source resolution, generation send, and result handling."),
    ),
    SymbolInventory(
        "src/notebooklm/_notes.py",
        "NotesAPI._operation_scope",
        Disposition("P3", "Scope note workflows including note-backed operations."),
    ),
    SymbolInventory(
        "src/notebooklm/_mind_maps_api.py",
        "MindMapsAPI._operation_scope",
        Disposition("P3", "Scope note-backed and interactive mind-map workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_settings.py",
        "SettingsAPI._operation_scope",
        Disposition("P3", "Scope account eligibility plus conditional quota read."),
    ),
    SymbolInventory(
        "src/notebooklm/_sharing.py",
        "SharingAPI._operation_scope",
        Disposition("P3", "Scope mutations and required share-status readback."),
    ),
    SymbolInventory(
        "src/notebooklm/_labels.py",
        "LabelsAPI._operation_scope",
        Disposition("P3", "Scope label mutation and verification workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_collections.py",
        "CollectionsAPI._operation_scope",
        Disposition("P3", "Scope collection mutation and verification workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sources/__init__.py",
        "WebSourcesAPI._operation_scope",
        Disposition("P3", "Preserve the already-supervised source workflow implementation."),
    ),
)


@dataclass(frozen=True, order=True)
class PresentationHit:
    path: str
    owner: str
    kind: str
    name: str


PRESENTATION_DISPOSITION = Disposition(
    "P7", "Move presentation policy to adapters or replace string callbacks with typed events."
)

PRESENTATION_INVENTORY = frozenset(
    {
        PresentationHit("_app/auth_check.py", "AuthCheckPlan", "field", "json_output"),
        PresentationHit("_app/collections.py", "resolve_collection_id", "parameter", "json_output"),
        PresentationHit("_app/download.py", "DownloadPlan", "field", "warnings"),
        PresentationHit(
            "_app/generate.py", "execute_generation", "string-callback", "wait_context"
        ),
        PresentationHit(
            "_app/generate.py", "execute_generation", "string-callback", "wait_start_sink"
        ),
        PresentationHit("_app/generate_plans.py", "GenerationPlan", "field", "json_output"),
        PresentationHit("_app/generate_plans.py", "GenerationPlan", "field", "warnings"),
        PresentationHit("_app/generate_plans.py", "GenerationPlan", "field", "stderr_warnings"),
        PresentationHit(
            "_app/generate_retry.py", "GenerationOutcome.exit_code", "property", "exit_code"
        ),
        PresentationHit(
            "_app/generate_retry.py",
            "handle_generation_result",
            "string-callback",
            "wait_context",
        ),
        PresentationHit(
            "_app/generate_retry.py",
            "handle_generation_result",
            "string-callback",
            "wait_start_sink",
        ),
        PresentationHit("_app/labels.py", "resolve_label_id", "parameter", "json_output"),
        PresentationHit(
            "_app/login_browser.py",
            "repair_playwright_account_metadata",
            "parameter",
            "quiet",
        ),
        PresentationHit("_app/login_cookie.py", "BrowserCookieProbeRequest", "field", "quiet"),
        PresentationHit("_app/notebooks.py", "execute_notebook_copy", "parameter", "json_output"),
        PresentationHit("_app/notebooks.py", "execute_notebook_rename", "parameter", "json_output"),
        PresentationHit(
            "_app/notebooks.py", "execute_notebook_describe", "parameter", "json_output"
        ),
        PresentationHit(
            "_app/notebooks.py", "execute_notebook_metadata", "parameter", "json_output"
        ),
        PresentationHit("_app/notes.py", "execute_note_create", "parameter", "json_output"),
        PresentationHit("_app/notes.py", "execute_note_get", "parameter", "json_output"),
        PresentationHit("_app/notes.py", "execute_note_save", "parameter", "json_output"),
        PresentationHit("_app/notes.py", "execute_note_rename", "parameter", "json_output"),
        PresentationHit("_app/notes.py", "resolve_note_for_delete", "parameter", "json_output"),
        PresentationHit("_app/research.py", "ResearchWaitPlan", "field", "json_output"),
        PresentationHit("_app/session.py", "verify_and_set_notebook", "parameter", "json_output"),
        PresentationHit("_app/sharing.py", "execute_share_status", "parameter", "json_output"),
        PresentationHit("_app/sharing.py", "execute_share_set_public", "parameter", "json_output"),
        PresentationHit(
            "_app/sharing.py", "execute_share_set_view_level", "parameter", "json_output"
        ),
        PresentationHit("_app/sharing.py", "execute_share_add_user", "parameter", "json_output"),
        PresentationHit("_app/sharing.py", "execute_share_update_user", "parameter", "json_output"),
        PresentationHit("_app/source_add.py", "SourceAddPlan", "field", "warnings"),
        PresentationHit("_app/source_clean.py", "run_source_clean", "parameter", "yes"),
        PresentationHit("_app/source_listing.py", "fetch_sources", "parameter", "json_output"),
        PresentationHit("_app/source_mutations.py", "SourceDeletePlan", "field", "yes"),
        PresentationHit("_app/source_mutations.py", "SourceDeletePlan", "field", "json_output"),
        PresentationHit(
            "_app/source_mutations.py", "execute_source_delete", "parameter", "confirmer"
        ),
        PresentationHit("_app/source_mutations.py", "SourceDeleteByTitlePlan", "field", "yes"),
        PresentationHit(
            "_app/source_mutations.py", "SourceDeleteByTitlePlan", "field", "json_output"
        ),
        PresentationHit(
            "_app/source_mutations.py",
            "execute_source_delete_by_title",
            "parameter",
            "confirmer",
        ),
        PresentationHit("_app/source_mutations.py", "SourceRenamePlan", "field", "json_output"),
        PresentationHit("_app/source_mutations.py", "SourceRefreshPlan", "field", "json_output"),
        PresentationHit("_app/source_research.py", "SourceAddResearchPlan", "field", "json_output"),
    }
)


GUARDRAIL_DISPOSITIONS = {
    "tests/_baselines/module_size.py": Disposition(
        "P2/P6", "Update only the reviewed exceptions.py growth; preserve shrink locks."
    ),
    "tests/fixtures/baselines/module_size.json": Disposition(
        "P2/P6", "Regenerate only for reviewed exception growth; keep capped modules bounded."
    ),
    "tests/_guardrails/test_module_size_ratchet.py": Disposition(
        "P2/P6", "Review only planned exception/runtime growth; keep shrink locks."
    ),
    "tests/_guardrails/test_middleware_context_contract.py": Disposition(
        "P1/P2/P6", "Widen the chat gate, then register journal/context keys with ADR-0009."
    ),
    "tests/_guardrails/test_backend_abstract_contracts.py": Disposition(
        "P1/P3", "Collapse research override and replace no-op Web scope pins with behavior."
    ),
    "tests/_guardrails/test_cli_boundary.py": Disposition(
        "P1/P2/P5/P7", "Keep CLI imports on public or _app surfaces as new public leaves land."
    ),
    "tests/_guardrails/test_cookie_persistence_boundary.py": Disposition(
        "P5", "Retarget callback Protocol ownership without runtime auth imports."
    ),
    "tests/_guardrails/test_auth_storage_compatibility.py": Disposition(
        "P5/P8", "Update constructor signature once, then audit announced v1 removals."
    ),
    "tests/_guardrails/test_auth_cookie_docs.py": Disposition(
        "P8", "Move AuthTokens rows to removed only at the release cut."
    ),
    "tests/_guardrails/test_adapter_support_boundary.py": Disposition(
        "P6/P7", "Register epoch access and any new batch projection consumer."
    ),
    "tests/_guardrails/test_no_facade_reach_in.py": Disposition(
        "P1/P2/P6", "Keep new outcomes/idempotency edges relative and narrowly consumed."
    ),
    "tests/unit/test_runtime_contracts.py": Disposition(
        "P2/P6", "Add temporary journal keywords, then remove them after context lookup."
    ),
    "tests/_guardrails/test_unconfirmed_contract.py": Disposition(
        "P1/P2", "Preserve identity while replacing markers with canonical projections."
    ),
    "tests/_guardrails/test_source_policy_parity.py": Disposition(
        "P7", "Replace the status-category batch oracle with public outcome parity."
    ),
    "tests/server/test_source_batch_parity.py": Disposition(
        "P7", "Delete the HTTP-status continuation oracle when typed outcomes land."
    ),
    "tests/e2e/test_mcp.py": Disposition("P7/P8", "Keep the MCP tool inventory unchanged."),
    "tests/_guardrails/test_client_composition.py": Disposition(
        "P4", "Replace client-mutation AST pins with one complete-graph installation invariant."
    ),
    "tests/_guardrails/test_public_surface_manifest.py": Disposition(
        "P1/P2/P5/P6", "Register outcomes, options, and timeout surfaces with reviewed baselines."
    ),
    "pyproject.toml": Disposition(
        "P1/P5", "Add outcomes and options to the strict public-module mypy override."
    ),
    "tests/unit/test_check_deprecation_targets.py": Disposition(
        "P5", "Teach the matcher the bounded detail keyword in the registry call."
    ),
    "tests/_guardrails/test_backend_boundaries.py": Disposition(
        "P1/P2/P4/P5/P6/P7", "Enumerate only the reviewed neutral-leaf/composition edges."
    ),
    "tests/_baselines/registry.py": Disposition(
        "P1/P2/P4/P5/P6/P7", "Regenerate only the exact reviewed backend edge identities."
    ),
    "tests/unit/test_idempotency_registry.py": Disposition(
        "P1", "Retire PROBE_THEN_CREATE and pin the four-policy axis."
    ),
    "tests/unit/mcp/test_manifest.py": Disposition("P7", "Add no tools; preserve the ceiling."),
    "tests/unit/mcp/test_tool_eval.py": Disposition(
        "P7", "Itemize any schema-character change and retain the ratchet."
    ),
    "scripts/audit_public_api_compat.py": Disposition(
        "P8", "Allow only the enumerated, runway-complete v1 removals."
    ),
    "scripts/api-compat-allowlist.json": Disposition(
        "P8", "Add each audited removal and reject stale or unreported allowances."
    ),
    "scripts/check_deprecation_targets.py": Disposition(
        "P5", "Accept the bounded dynamic-detail registry call without weakening matching."
    ),
    "src/notebooklm/_deprecation.py": Disposition(
        "P5/P6/P7", "Add the five plan-owned warning keys in their implementing phases."
    ),
    "docs/deprecations.md": Disposition(
        "P5/P6/P7/P8", "Keep warning, removal, and v1 runway rows synchronized."
    ),
    "tests/unit/test_inline_deprecations_gated.py": Disposition(
        "P5/P6/P7", "Register each new warning emitter and quiet-mode coverage."
    ),
    "tests/_guardrails/test_client_factory_parity.py": Disposition(
        "P4/P5", "Retarget lifecycle identity and keep typed factory parity."
    ),
    "docs/architecture.md": Disposition(
        "P1/P4/P5/P6", "Update module ownership and replace the no-op workflow claim."
    ),
    "tests/_guardrails/test_docs_module_refs.py": Disposition(
        "P1/P4/P5/P6", "Keep architecture index references live as ownership modules change."
    ),
}


PLANNED_GUARDRAIL_DISPOSITIONS = {
    "tests/_guardrails/_v100_breaks.py": Disposition(
        "P5/P6/P7/P8", "Create the v1 runway registry in P5, extend it, then delete it at v1."
    ),
    "tests/_guardrails/test_v100_deprecation_coverage.py": Disposition(
        "P5/P6/P7/P8", "Create the runway coverage gate in P5 and remove it at the release cut."
    ),
    "tests/_guardrails/test_v100_release_gate.py": Disposition(
        "P5/P6/P7/P8", "Create the staged release gate in P5 and drain it at the release cut."
    ),
}


def _qualified_functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            name = ".".join((*stack, node.name))
            found[name] = node
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

    Visitor().visit(tree)
    return found


def _parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    args = node.args
    return tuple(argument.arg for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs))


def _call_inventory(call_name: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        functions = _qualified_functions(path)
        for owner, function in functions.items():
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                called = None
                if isinstance(node.func, ast.Name):
                    called = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    called = node.func.attr
                if called == call_name:
                    found.add((str(path.relative_to(ROOT)), owner))
    return found


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _contains_name(node: ast.AST | None, name: str) -> bool:
    return node is not None and any(
        isinstance(part, ast.Name) and part.id == name for part in ast.walk(node)
    )


def _presentation_hits(
    app_root: Path = APP,
    *,
    source_root: Path = SRC,
) -> set[PresentationHit]:
    prohibited = {"json_output", "yes", "quiet", "exit_code", "confirmer"}
    hits: set[PresentationHit] = set()
    for path in sorted(app_root.rglob("*.py")):
        relative = str(path.relative_to(source_root))
        tree = ast.parse(path.read_text(encoding="utf-8"))

        class Visitor(ast.NodeVisitor):
            def __init__(self, relative_path: str) -> None:
                self.relative = relative_path
                self.stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.stack.append(node.name)
                if any(
                    _decorator_name(decorator) == "dataclass" for decorator in node.decorator_list
                ):
                    for statement in node.body:
                        if not (
                            isinstance(statement, ast.AnnAssign)
                            and isinstance(statement.target, ast.Name)
                        ):
                            continue
                        name = statement.target.id
                        string_sequence = name in {"warnings", "stderr_warnings"} and (
                            _contains_name(statement.annotation, "str")
                            and any(
                                _contains_name(statement.annotation, container)
                                for container in ("list", "tuple", "Sequence")
                            )
                        )
                        if name in prohibited or string_sequence:
                            hits.add(
                                PresentationHit(self.relative, ".".join(self.stack), "field", name)
                            )
                self.generic_visit(node)
                self.stack.pop()

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                owner = ".".join((*self.stack, node.name))
                is_property = any(
                    _decorator_name(decorator) == "property" for decorator in node.decorator_list
                )
                if node.name in prohibited:
                    hits.add(
                        PresentationHit(
                            self.relative,
                            owner,
                            "property" if is_property else "function",
                            node.name,
                        )
                    )
                arguments = (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                    *((node.args.vararg,) if node.args.vararg is not None else ()),
                    *((node.args.kwarg,) if node.args.kwarg is not None else ()),
                )
                for argument in arguments:
                    if argument.arg in prohibited:
                        hits.add(PresentationHit(self.relative, owner, "parameter", argument.arg))
                    if argument.arg in {"wait_context", "wait_start_sink"} and _contains_name(
                        argument.annotation, "str"
                    ):
                        hits.add(
                            PresentationHit(self.relative, owner, "string-callback", argument.arg)
                        )
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_FunctionDef = _visit_function
            visit_AsyncFunctionDef = _visit_function

        Visitor(relative).visit(tree)
    return hits


def test_constructor_and_factory_signature_inventory_is_exact() -> None:
    for row in CONSTRUCTION_SIGNATURES:
        functions = _qualified_functions(ROOT / row.path)
        assert row.symbol in functions
        assert _parameters(functions[row.symbol]) == row.parameters


def test_every_public_constructor_option_and_private_dependency_has_an_owner() -> None:
    public = {
        parameter
        for row in CONSTRUCTION_SIGNATURES[:2]
        for parameter in row.parameters
        if parameter not in {"self", "cls"}
    }
    assert public == set(PUBLIC_OPTION_DISPOSITIONS)

    assembly = set(CONSTRUCTION_SIGNATURES[2].parameters)
    private = assembly - public - {"client"}
    assert private == set(PRIVATE_DEPENDENCY_DISPOSITIONS)


def test_probe_create_retry_call_inventory_is_exact() -> None:
    assert _call_inventory("idempotent_create") == {
        ("src/notebooklm/_notebooks.py", "NotebooksAPI._create_with_probe"),
        ("src/notebooklm/_web/sources/add.py", "SourceAddService.add_url"),
        ("src/notebooklm/_web/sources/add.py", "SourceAddService.add_drive"),
        (
            "src/notebooklm/_web/sources/upload.py",
            "SourceUploadPipeline._register_file_source_result",
        ),
    }
    assert _call_inventory("with_rate_limit_retry") == {
        ("src/notebooklm/_app/generate_retry.py", "generate_with_retry")
    }


def test_cleanup_inventory_separates_target_from_preserved_families() -> None:
    symbols = {row.symbol for row in CLEANUP_INVENTORY}
    assert TARGET_CLEANUP_SYMBOLS.isdisjoint(PRESERVED_CLEANUP_SYMBOLS)
    assert symbols == TARGET_CLEANUP_SYMBOLS | PRESERVED_CLEANUP_SYMBOLS


@pytest.mark.parametrize("row", (*RETRY_INVENTORY, *CLEANUP_INVENTORY, *WORKFLOW_INVENTORY))
def test_operation_inventory_symbol_still_exists(row: SymbolInventory) -> None:
    assert row.symbol in _qualified_functions(ROOT / row.path)


def test_presentation_inventory_is_structural_and_exact() -> None:
    assert _presentation_hits() == set(PRESENTATION_INVENTORY)


def test_presentation_matcher_flags_fields_properties_parameters_and_callbacks(
    tmp_path: Path,
) -> None:
    package = tmp_path / "_app"
    package.mkdir()
    (package / "synthetic.py").write_text(
        """
@dataclass
class BadPlan:
    json_output: bool
    warnings: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return 1

def execute(*, yes: bool, **confirmer: object) -> None:
    pass

def wait(wait_context: Callable[[str], object]) -> None:
    pass
""",
        encoding="utf-8",
    )

    hits = _presentation_hits(package, source_root=tmp_path)

    assert {(hit.kind, hit.name) for hit in hits} == {
        ("field", "json_output"),
        ("field", "warnings"),
        ("property", "exit_code"),
        ("parameter", "yes"),
        ("parameter", "confirmer"),
        ("string-callback", "wait_context"),
    }


def test_presentation_matcher_allows_typed_domain_events(tmp_path: Path) -> None:
    package = tmp_path / "_app"
    package.mkdir()
    (package / "synthetic.py").write_text(
        """
@dataclass
class DomainPlan:
    warnings: tuple[WarningEvent, ...]

def wait(wait_context: Callable[[GenerationWaitStarted], object], test_fetch: bool) -> None:
    pass
""",
        encoding="utf-8",
    )

    assert _presentation_hits(package, source_root=tmp_path) == set()


def test_guardrail_inventory_paths_are_current() -> None:
    assert all((ROOT / path).exists() for path in GUARDRAIL_DISPOSITIONS)
    assert all(not (ROOT / path).exists() for path in PLANNED_GUARDRAIL_DISPOSITIONS)


def test_every_inventory_row_has_an_explicit_phase_disposition() -> None:
    dispositions = [
        *(row.disposition for row in CONSTRUCTION_SIGNATURES),
        *PUBLIC_OPTION_DISPOSITIONS.values(),
        *PRIVATE_DEPENDENCY_DISPOSITIONS.values(),
        *(row.disposition for row in RETRY_INVENTORY),
        *(row.disposition for row in CLEANUP_INVENTORY),
        *(row.disposition for row in WORKFLOW_INVENTORY),
        PRESENTATION_DISPOSITION,
        *GUARDRAIL_DISPOSITIONS.values(),
        *PLANNED_GUARDRAIL_DISPOSITIONS.values(),
    ]
    for disposition in dispositions:
        assert re.fullmatch(r"P[1-8](?:/P[1-8])*", disposition.phase)
        assert disposition.action.strip()
        assert "TBD" not in disposition.action.upper()
