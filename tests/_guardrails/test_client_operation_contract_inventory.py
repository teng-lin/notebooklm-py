"""Structural P0 inventories for the client-ownership refactor.

These registries make the migration surface executable before any behavior is
changed.  Every row names the phase that owns it and a concrete disposition;
``TBD`` is deliberately forbidden.  Later phases must update a row in the same
change that moves or removes its source construct.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
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
            "config",
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
            "config",
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
            "options",
            "storage_path",
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
            "shared",
            "config",
            "credentials",
            "deps",
        ),
        Disposition("P4/P5", "Stop accepting a client; consume Web config and Web dependencies."),
    ),
    SignatureInventory(
        "src/notebooklm/_web/assembly.py",
        "build_compatibility_runtime",
        (
            "shared",
            "spec",
            "deps",
        ),
        Disposition(
            "P4/P5/P8", "Use a typed sidecar spec and dependencies; remove the sidecar at v1."
        ),
    ),
    SignatureInventory(
        "src/notebooklm/_android/assembly.py",
        "assemble_android_backend",
        (
            "shared",
            "config",
            "credentials",
            "deps",
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
    SignatureInventory(
        "src/notebooklm/_client_assembly.py",
        "_install_client",
        (
            "client",
            "auth",
            "preference",
            "assembly",
            "sidecar",
            "android_seams",
        ),
        Disposition("P4", "Install the complete lifecycle graph once without backend rebinding."),
    ),
    SignatureInventory(
        "src/notebooklm/_client_assembly.py",
        "_finalize_loaded_client",
        (
            "client",
            "preference",
            "loaded_auth",
        ),
        Disposition("P4", "Finalize frozen preference and loaded-auth persistence once."),
    ),
    SignatureInventory(
        "src/notebooklm/_client_compat.py",
        "build_compatibility_sidecar",
        (
            "shared",
            "spec",
            "deps",
        ),
        Disposition("P4/P8", "Install the inert compatibility sidecar once, then remove it at v1."),
    ),
)


PUBLIC_OPTION_DISPOSITIONS = {
    "config": Disposition("P5", "Add the frozen owner-grouped construction facade."),
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
    "options": Disposition("P5", "Carry the resolved config and compatibility diagnostics."),
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


@dataclass(frozen=True)
class CallFamilyInventory:
    path: str
    owner: str
    callee: str
    count: int
    disposition: Disposition


def _call_rows(
    callee: str,
    rows: tuple[tuple[str, str, int], ...],
    disposition: Disposition,
) -> tuple[CallFamilyInventory, ...]:
    return tuple(
        CallFamilyInventory(path, owner, callee, count, disposition) for path, owner, count in rows
    )


RETRY_INVENTORY = (
    SymbolInventory(
        "src/notebooklm/_research.py",
        "BaseResearchAPI._import_sources_with_verification",
        Disposition("P1/P2", "Demote re-send to bounded candidate inspection and journal results."),
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
        "src/notebooklm/_web/transport/executor.py",
        "RpcExecutor.try_refresh_and_retry",
        Disposition(
            "P1/P2/P6", "Apply shared replay evidence and retain one logical retry deadline."
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
        "src/notebooklm/_android/session.py",
        "AndroidSession._stream_impl",
        Disposition(
            "P1/P2/P6", "Apply manifest evidence to stream setup and the aggregate deadline."
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


RETRY_CALL_INVENTORY = (
    *_call_rows(
        "with_rate_limit_retry",
        (("src/notebooklm/_app/generate_retry.py", "generate_with_retry", 1),),
        Disposition("P1/P2/P6", "Honor producer replay evidence and the shared attempt budget."),
    ),
    *_call_rows(
        "try_refresh_and_retry",
        (("src/notebooklm/_web/transport/executor.py", "RpcExecutor._execute_once", 1),),
        Disposition("P1/P2/P6", "Gate decoded-RPC refresh replay on shared send evidence."),
    ),
    *_call_rows(
        "_unary_impl",
        (("src/notebooklm/_android/session.py", "AndroidSession.unary", 1),),
        Disposition("P1/P2/P6", "Carry manifest replay evidence into Android unary execution."),
    ),
    *_call_rows(
        "_stream_impl",
        (("src/notebooklm/_android/session.py", "AndroidSession.stream", 1),),
        Disposition("P1/P2/P6", "Carry manifest replay evidence into Android stream setup."),
    ),
)


UNCERTAINTY_WRAPPER_CALL_INVENTORY = _call_rows(
    "call_unconfirmed_on_transport_loss",
    (
        ("src/notebooklm/_android/artifact_mutations.py", "export_to_drive", 1),
        ("src/notebooklm/_android/artifact_mutations.py", "retry_failed_artifact", 1),
        (
            "src/notebooklm/_android/artifact_note_mind_maps.py",
            "generate_note_backed_mind_map",
            1,
        ),
        (
            "src/notebooklm/_android/artifact_transfers.py",
            "AndroidArtifactTransferMixin._send_copy",
            1,
        ),
        ("src/notebooklm/_android/artifacts.py", "AndroidArtifactsAPI.revise_slide", 1),
        ("src/notebooklm/_android/notes.py", "create_note", 1),
        ("src/notebooklm/_android/organization.py", "create_manual", 1),
        ("src/notebooklm/_android/organization.py", "generate_labels", 1),
        ("src/notebooklm/_android/research.py", "AndroidResearchAPI.discover", 1),
        ("src/notebooklm/_android/research.py", "AndroidResearchAPI.start", 2),
        ("src/notebooklm/_android/sharing.py", "AndroidSharingAPI._mutate_users", 1),
        ("src/notebooklm/_android/sharing.py", "AndroidSharingAPI.set_public", 1),
        ("src/notebooklm/_android/sharing.py", "AndroidSharingAPI.set_view_level", 1),
        (
            "src/notebooklm/_android/source_transfers.py",
            "AndroidSourceTransferMixin._send_add_urls_async",
            1,
        ),
        (
            "src/notebooklm/_android/source_transfers.py",
            "AndroidSourceTransferMixin._send_append_text",
            1,
        ),
        (
            "src/notebooklm/_android/source_transfers.py",
            "AndroidSourceTransferMixin._send_copy",
            1,
        ),
        (
            "src/notebooklm/_web/artifact/generation.py",
            "ArtifactGenerationService._call_generate",
            1,
        ),
        (
            "src/notebooklm/_web/artifact/generation.py",
            "ArtifactGenerationService.generate_mind_map",
            1,
        ),
        (
            "src/notebooklm/_web/artifact/generation.py",
            "ArtifactGenerationService.retry_failed",
            1,
        ),
        (
            "src/notebooklm/_web/artifact/generation.py",
            "ArtifactGenerationService.revise_slide",
            1,
        ),
        ("src/notebooklm/_notebooks.py", "NotebooksAPI.create", 1),
        ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI._send_export", 1),
        ("src/notebooklm/_web/collections.py", "WebCollectionsAPI.create", 1),
        ("src/notebooklm/_web/labels.py", "WebLabelsAPI.create", 1),
        ("src/notebooklm/_web/labels.py", "WebLabelsAPI.generate", 1),
        (
            "src/notebooklm/_web/mind_maps.py",
            "WebMindMapsAPI._start_interactive_mind_map",
            1,
        ),
        ("src/notebooklm/_web/notes.py", "NoteService._create_note_admitted", 1),
        ("src/notebooklm/_web/research.py", "WebResearchAPI.start", 1),
        ("src/notebooklm/_web/sharing.py", "WebSharingAPI._share_and_readback", 1),
        ("src/notebooklm/_web/sources/add.py", "SourceAddService.add_drive", 1),
        ("src/notebooklm/_web/sources/add.py", "SourceAddService.add_text", 1),
        ("src/notebooklm/_web/sources/add.py", "SourceAddService.add_url", 1),
        (
            "src/notebooklm/_web/sources/upload.py",
            "SourceUploadPipeline._register_file_source_result",
            1,
        ),
    ),
    Disposition(
        "P1/P2", "Preserve positive refusal evidence and force unknown for composite readback."
    ),
)


UNRESOLVED_COMMIT_CALL_INVENTORY = _call_rows(
    "unresolved_commit_error",
    (
        ("src/notebooklm/_android/artifact_creation.py", "create_artifact_once", 1),
        ("src/notebooklm/_android/sources.py", "_unresolved_add_error", 1),
        ("src/notebooklm/_notebooks.py", "NotebooksAPI.copy", 1),
        ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI._send_copy", 1),
        ("src/notebooklm/_web/sources/batch.py", "SourceBatchAddService.add_urls", 2),
        ("src/notebooklm/_web/sources/batch.py", "_unresolved_batch_error", 1),
        ("src/notebooklm/_web/sources/play_books.py", "_unconfirmed_add", 1),
        ("src/notebooklm/_web/sources/transfers.py", "_unconfirmed", 1),
    ),
    Disposition(
        "P1/P2", "Preserve verified rejection/not-sent evidence and synthesize unknown only."
    ),
)


READINESS_CALL_INVENTORY = _call_rows(
    "wait_until_ready",
    (
        (
            "src/notebooklm/_android/sources.py",
            "AndroidSourcesAPI._add_registered_content",
            1,
        ),
        ("src/notebooklm/_android/sources.py", "AndroidSourcesAPI._wait_uploaded_source", 1),
        ("src/notebooklm/_android/sources.py", "AndroidSourcesAPI.add_url", 1),
        (
            "src/notebooklm/_android/upload.py",
            "AndroidUploadPipeline._upload_file_impl._wait_until_ready",
            1,
        ),
        ("src/notebooklm/_app/source_wait.py", "execute_source_wait", 1),
        (
            "src/notebooklm/_source/polling.py",
            "SourcePoller.wait_for_sources._wait_factory._wait",
            1,
        ),
        ("src/notebooklm/_sources.py", "SourcesAPI._finalize_uploaded_file", 1),
        ("src/notebooklm/_sources.py", "SourcesAPI.wait_until_ready", 1),
        ("src/notebooklm/_web/sources/__init__.py", "WebSourcesAPI.add_play_book", 1),
        ("src/notebooklm/_web/sources/add.py", "SourceAddService.add_drive", 1),
        ("src/notebooklm/_web/sources/add.py", "SourceAddService.add_text", 1),
        ("src/notebooklm/_web/sources/add.py", "SourceAddService.add_url", 1),
        (
            "src/notebooklm/_web/sources/upload.py",
            "SourceUploadPipeline._add_file_admitted._wait_until_ready",
            1,
        ),
        (
            "src/notebooklm/_web/sources/upload.py",
            "SourceUploadPipeline.wait_until_ready",
            1,
        ),
    ),
    Disposition(
        "P1/P2/P6", "Keep readiness as a separate read send and never replay its confirmed create."
    ),
)


COMPOSITE_WRAPPER_INVENTORY = {
    (
        "src/notebooklm/_web/collections.py",
        "WebCollectionsAPI.create",
        "WebCollectionsAPI.create.create_and_readback",
    ): Disposition("P1/P2", "Force unknown in P1, then journal mutation and readback separately."),
    (
        "src/notebooklm/_web/sharing.py",
        "WebSharingAPI._share_and_readback",
        "WebSharingAPI._share_and_readback.mutate_and_readback",
    ): Disposition("P1/P2", "Force unknown in P1, then journal mutation and readback separately."),
    (
        "src/notebooklm/_web/sources/upload.py",
        "SourceUploadPipeline._register_file_source_result",
        "SourceUploadPipeline._register_file_source_result._create",
    ): Disposition("P1/P2", "Force unknown around registration plus quota diagnostics in P1."),
}


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
        Disposition("P3", "Require the concrete backend supervisor for neutral workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_chat.py",
        "ChatAPI._operation_scope",
        Disposition("P3", "Require the concrete backend supervisor across the chat workflow."),
    ),
    SymbolInventory(
        "src/notebooklm/_research.py",
        "BaseResearchAPI._operation_scope",
        Disposition("P3", "Require the concrete backend supervisor across research workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_artifacts.py",
        "ArtifactsAPI._operation_scope",
        Disposition("P3", "Require the concrete backend supervisor across artifact workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_notes.py",
        "NotesAPI._operation_scope",
        Disposition("P3", "Require the concrete backend supervisor across note workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_mind_maps_api.py",
        "MindMapsAPI._operation_scope",
        Disposition("P3", "Require the concrete backend supervisor across mind-map workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_settings.py",
        "SettingsAPI._operation_scope",
        Disposition("P3", "Require one supervisor lease across both usage reads."),
    ),
    SymbolInventory(
        "src/notebooklm/_sharing.py",
        "SharingAPI._operation_scope",
        Disposition("P3", "Require the concrete backend supervisor through sharing readback."),
    ),
    SymbolInventory(
        "src/notebooklm/_labels.py",
        "LabelsAPI._operation_scope",
        Disposition("P3", "Require the concrete backend supervisor across label workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_collections.py",
        "CollectionsAPI._operation_scope",
        Disposition("P3", "Require the concrete backend supervisor through reconciliation."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/notebooks.py",
        "WebNotebooksAPI._operation_scope",
        Disposition("P3", "Delegate Web notebook workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/chat.py",
        "WebChatAPI._operation_scope",
        Disposition("P3", "Delegate Web chat workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/research.py",
        "WebResearchAPI._operation_scope",
        Disposition("P3", "Delegate Web research workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/artifacts.py",
        "WebArtifactsAPI._operation_scope",
        Disposition("P3", "Delegate Web artifact workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/notes.py",
        "WebNotesAPI._operation_scope",
        Disposition("P3", "Delegate Web note workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/mind_maps.py",
        "WebMindMapsAPI._operation_scope",
        Disposition("P3", "Delegate Web mind-map workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/settings.py",
        "WebSettingsAPI._operation_scope",
        Disposition("P3", "Delegate Web settings workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sharing.py",
        "WebSharingAPI._operation_scope",
        Disposition("P3", "Delegate Web sharing workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/labels.py",
        "WebLabelsAPI._operation_scope",
        Disposition("P3", "Delegate Web label workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/collections.py",
        "WebCollectionsAPI._operation_scope",
        Disposition("P3", "Delegate Web collection workflows to the shared supervisor."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sources/__init__.py",
        "WebSourcesAPI._operation_scope",
        Disposition("P3", "Preserve the already-supervised source workflow implementation."),
    ),
    SymbolInventory(
        "src/notebooklm/_sources.py",
        "SourcesAPI._operation_scope",
        Disposition("P3", "Make the source hook abstract and preserve supervised wiring."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/notebooks.py",
        "AndroidNotebooksAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/chat.py",
        "AndroidChatAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/research.py",
        "AndroidResearchAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/artifacts.py",
        "AndroidArtifactsAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/notes.py",
        "AndroidNotesAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/mind_maps.py",
        "AndroidMindMapsAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/settings.py",
        "AndroidSettingsAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/sharing.py",
        "AndroidSharingAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/labels.py",
        "AndroidLabelsAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/collections.py",
        "AndroidCollectionsAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/sources.py",
        "AndroidSourcesAPI._operation_scope",
        Disposition("P3", "Preserve the existing Android supervisor scope."),
    ),
)


WORKFLOW_SCOPE_CALL_INVENTORY = (
    *_call_rows(
        "_operation_scope",
        (
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_audio", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_cinematic_video", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_data_table", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_flashcards", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_infographic", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_quiz", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_report", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_slide_deck", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_study_guide", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.generate_video", 1),
            ("src/notebooklm/_chat.py", "ChatAPI.cancel", 1),
            ("src/notebooklm/_collections.py", "CollectionsAPI._mutate_members", 1),
            ("src/notebooklm/_collections.py", "CollectionsAPI.delete", 1),
            ("src/notebooklm/_collections.py", "CollectionsAPI.get", 1),
            ("src/notebooklm/_collections.py", "CollectionsAPI.get_or_none", 1),
            ("src/notebooklm/_collections.py", "CollectionsAPI.notebooks", 1),
            ("src/notebooklm/_collections.py", "CollectionsAPI.rename", 1),
            ("src/notebooklm/_labels.py", "LabelsAPI._mutate_members", 1),
            ("src/notebooklm/_labels.py", "LabelsAPI.delete", 1),
            ("src/notebooklm/_labels.py", "LabelsAPI.get", 1),
            ("src/notebooklm/_labels.py", "LabelsAPI.get_or_none", 1),
            ("src/notebooklm/_labels.py", "LabelsAPI.sources", 1),
            ("src/notebooklm/_labels.py", "LabelsAPI.update", 1),
            ("src/notebooklm/_mind_maps_api.py", "MindMapsAPI._detect_kind", 1),
            ("src/notebooklm/_mind_maps_api.py", "MindMapsAPI.delete", 1),
            ("src/notebooklm/_mind_maps_api.py", "MindMapsAPI.generate", 1),
            ("src/notebooklm/_mind_maps_api.py", "MindMapsAPI.get_tree", 1),
            ("src/notebooklm/_mind_maps_api.py", "MindMapsAPI.list", 1),
            ("src/notebooklm/_mind_maps_api.py", "MindMapsAPI.rename", 1),
            ("src/notebooklm/_notebooks.py", "NotebooksAPI.create", 1),
            ("src/notebooklm/_notebooks.py", "NotebooksAPI.copy", 1),
            ("src/notebooklm/_artifacts.py", "ArtifactsAPI.copy", 1),
            ("src/notebooklm/_chat.py", "ChatAPI.ask", 1),
            ("src/notebooklm/_chat.py", "ChatAPI.session_status", 1),
            ("src/notebooklm/_notebooks.py", "NotebooksAPI.get_metadata", 1),
            (
                "src/notebooklm/_research.py",
                "BaseResearchAPI._import_sources_with_verification",
                1,
            ),
            ("src/notebooklm/_research.py", "BaseResearchAPI._wait_for_completion", 1),
            ("src/notebooklm/_settings.py", "SettingsAPI.get_usage", 1),
            ("src/notebooklm/_sources.py", "SourcesAPI.wait_all_until_ready", 1),
            ("src/notebooklm/_sources.py", "SourcesAPI.wait_for_sources", 1),
            ("src/notebooklm/_sources.py", "SourcesAPI.wait_until_ready", 1),
            ("src/notebooklm/_sources.py", "SourcesAPI.wait_until_registered", 1),
        ),
        Disposition("P3", "Require shared supervisor admission for every neutral workflow."),
    ),
    *_call_rows(
        "_operation_scope",
        (
            (
                "src/notebooklm/_android/artifacts.py",
                "AndroidArtifactsAPI._generate_supported_family",
                1,
            ),
            ("src/notebooklm/_android/chat.py", "AndroidChatAPI.get_history", 1),
            ("src/notebooklm/_android/notebooks.py", "AndroidNotebooksAPI.suggest_prompts", 1),
            ("src/notebooklm/_android/sources.py", "AndroidSourcesAPI._send_upload", 1),
            ("src/notebooklm/_android/sources.py", "AndroidSourcesAPI.add_drive", 1),
            ("src/notebooklm/_android/sources.py", "AndroidSourcesAPI.add_drive_file", 1),
            ("src/notebooklm/_android/sources.py", "AndroidSourcesAPI.add_play_book", 1),
            ("src/notebooklm/_sources.py", "SourcesAPI.add_urls_async", 1),
            ("src/notebooklm/_sources.py", "SourcesAPI.append_text", 1),
            ("src/notebooklm/_sources.py", "SourcesAPI.copy", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.generate_mind_map", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.download_audio", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.download_data_table", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.download_flashcards", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.download_infographic", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.download_mind_map", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.download_quiz", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.download_report", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.download_slide_deck", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.download_video", 1),
            ("src/notebooklm/_web/artifacts.py", "WebArtifactsAPI.rename", 1),
            ("src/notebooklm/_web/chat.py", "WebChatAPI.get_history", 1),
            ("src/notebooklm/_web/collections.py", "WebCollectionsAPI.create", 1),
            ("src/notebooklm/_web/labels.py", "WebLabelsAPI.create", 1),
            ("src/notebooklm/_web/notebooks.py", "WebNotebooksAPI.suggest_prompts", 1),
            ("src/notebooklm/_web/notebooks.py", "WebNotebooksAPI.update", 1),
            ("src/notebooklm/_web/notes.py", "WebNotesAPI.update", 1),
            (
                "src/notebooklm/_web/sharing.py",
                "WebSharingAPI._share_and_readback",
                1,
            ),
            ("src/notebooklm/_web/sharing.py", "WebSharingAPI.set_view_level", 1),
            ("src/notebooklm/_web/sources/__init__.py", "WebSourcesAPI.rename", 1),
        ),
        Disposition("P3", "Preserve existing scopes and cover Web-specific composites."),
    ),
)


REQUIRED_WORKFLOW_ENTRYPOINT_INVENTORY = (
    SymbolInventory(
        "src/notebooklm/_notebooks.py",
        "NotebooksAPI.create",
        Disposition("P3", "Holds the one-send create and decoded-result settlement."),
    ),
    SymbolInventory(
        "src/notebooklm/_notebooks.py",
        "NotebooksAPI.copy",
        Disposition("P3", "Holds notebook source-read/copy send from its first await."),
    ),
    SymbolInventory(
        "src/notebooklm/_notebooks.py",
        "NotebooksAPI.get_metadata",
        Disposition("P3", "Holds both registered metadata children through composition."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/notebooks.py",
        "WebNotebooksAPI.suggest_prompts",
        Disposition("P3", "Holds source resolution through the prompt request."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/notebooks.py",
        "AndroidNotebooksAPI.suggest_prompts",
        Disposition("P3", "Holds source resolution through the prompt request."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/notebooks.py",
        "WebNotebooksAPI.update",
        Disposition("P3", "Holds the notebook mutation and required readback."),
    ),
    SymbolInventory(
        "src/notebooklm/_chat.py",
        "ChatAPI.ask",
        Disposition("P3", "Holds locks, history, send, and cache publication."),
    ),
    SymbolInventory(
        "src/notebooklm/_chat.py",
        "ChatAPI.session_status",
        Disposition("P3", "Holds session resolution and status read."),
    ),
    SymbolInventory(
        "src/notebooklm/_chat.py",
        "ChatAPI.cancel",
        Disposition("P3", "Holds session resolution and cancel send."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/chat.py",
        "WebChatAPI.get_history",
        Disposition("P3", "Holds session resolution and the Web turn read."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/chat.py",
        "AndroidChatAPI.get_history",
        Disposition("P3", "Holds session resolution and the Android turn read."),
    ),
    SymbolInventory(
        "src/notebooklm/_artifacts.py",
        "ArtifactsAPI.copy",
        Disposition("P3", "Holds artifact lookup and copy send."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/artifacts.py",
        "WebArtifactsAPI.generate_mind_map",
        Disposition("P3", "Holds source resolution, generation, and note persistence."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/artifacts.py",
        "WebArtifactsAPI.rename",
        Disposition("P3", "Holds the artifact mutation and required readback."),
    ),
    *(
        SymbolInventory(
            "src/notebooklm/_web/artifacts.py",
            f"WebArtifactsAPI.{method}",
            Disposition("P3", "Holds resolution, transfer, and local-file settlement."),
        )
        for method in (
            "download_audio",
            "download_video",
            "download_infographic",
            "download_slide_deck",
            "download_report",
            "download_mind_map",
            "download_data_table",
            "download_quiz",
            "download_flashcards",
        )
    ),
    SymbolInventory(
        "src/notebooklm/_mind_maps_api.py",
        "MindMapsAPI.generate",
        Disposition("P3", "Holds note-backed and interactive generation workflows."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/labels.py",
        "WebLabelsAPI.create",
        Disposition("P3", "Holds the label baseline and create send."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sharing.py",
        "WebSharingAPI._share_and_readback",
        Disposition("P3", "Holds the sharing mutation and required readback."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sharing.py",
        "WebSharingAPI.set_view_level",
        Disposition("P3", "Holds the view mutation and required status readback."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/collections.py",
        "WebCollectionsAPI.create",
        Disposition("P3", "Holds baseline, create send, and collection readback."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/notes.py",
        "WebNotesAPI.update",
        Disposition("P3", "Holds note preflight, mutation, and result settlement."),
    ),
    SymbolInventory(
        "src/notebooklm/_research.py",
        "BaseResearchAPI._wait_for_completion",
        Disposition("P3/P6", "Holds the complete research polling leader."),
    ),
    SymbolInventory(
        "src/notebooklm/_research.py",
        "BaseResearchAPI._import_sources_with_verification",
        Disposition("P3/P6", "Holds baseline, one import send, and bounded candidate inspection."),
    ),
    SymbolInventory(
        "src/notebooklm/_settings.py",
        "SettingsAPI.get_usage",
        Disposition("P3", "Holds account eligibility and conditional quota reads."),
    ),
    SymbolInventory(
        "src/notebooklm/_source/polling.py",
        "SourcePoller.wait_until_ready",
        Disposition("P3/P6", "Preserve polling leader admission and inherit operation context."),
    ),
    SymbolInventory(
        "src/notebooklm/_sources.py",
        "SourcesAPI.wait_until_ready",
        Disposition("P3/P6", "Holds a complete source readiness poll."),
    ),
    SymbolInventory(
        "src/notebooklm/_sources.py",
        "SourcesAPI.wait_until_registered",
        Disposition("P3/P6", "Holds a complete source registration poll."),
    ),
    SymbolInventory(
        "src/notebooklm/_sources.py",
        "SourcesAPI.wait_all_until_ready",
        Disposition("P3/P6", "Holds batched source polling through final outcomes."),
    ),
    SymbolInventory(
        "src/notebooklm/_sources.py",
        "SourcesAPI.wait_for_sources",
        Disposition("P3/P6", "Holds registered polling children through settlement."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sources/__init__.py",
        "WebSourcesAPI.rename",
        Disposition("P3", "Holds source mutation and fallback hydration."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/sources.py",
        "AndroidSourcesAPI.add_drive",
        Disposition("P3", "Holds registration, commit, wait, and title finalization."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/sources.py",
        "AndroidSourcesAPI.add_play_book",
        Disposition("P3", "Holds library lookup, registration, commit, and readiness."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/sources.py",
        "AndroidSourcesAPI._send_upload",
        Disposition("P3", "Holds path resolution through upload settlement."),
    ),
    SymbolInventory(
        "src/notebooklm/_sources.py",
        "SourcesAPI.add_file",
        Disposition("P3", "Delegates Android files into admission before path resolution."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/sources.py",
        "AndroidSourcesAPI.add_drive_file",
        Disposition("P3", "Holds Drive download through imported-source settlement."),
    ),
    SymbolInventory(
        "src/notebooklm/_artifact/polling.py",
        "ArtifactPollingService.wait_for_completion",
        Disposition("P3/P6", "Preserve polling leader admission and inherit operation context."),
    ),
    SymbolInventory(
        "src/notebooklm/_web/sources/upload.py",
        "SourceUploadPipeline.add_file",
        Disposition("P3/P6", "Preserve transfer admission across register, upload, and readiness."),
    ),
    SymbolInventory(
        "src/notebooklm/_android/upload.py",
        "AndroidUploadPipeline._upload_file_impl",
        Disposition("P3/P6", "Preserve transfer admission through staging and settlement."),
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
    "tests/_guardrails/test_app_boundary.py": Disposition(
        "P0/P1/P2/P5/P7",
        "Add MCP in P0, then admit only reviewed public outcomes/options/adapter surfaces.",
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


PLANNED_GUARDRAIL_DISPOSITIONS: dict[str, Disposition] = {}


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


def _call_family_census(call_names: set[str]) -> Counter[tuple[str, str, str]]:
    found: Counter[tuple[str, str, str]] = Counter()
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        class Visitor(ast.NodeVisitor):
            def __init__(self, relative_path: str) -> None:
                self.relative = relative_path
                self.stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_FunctionDef = _visit_function
            visit_AsyncFunctionDef = _visit_function

            def visit_Call(self, node: ast.Call) -> None:
                called = None
                if isinstance(node.func, ast.Name):
                    called = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    called = node.func.attr
                if called in call_names:
                    found[(self.relative, ".".join(self.stack), called)] += 1
                self.generic_visit(node)

        Visitor(relative).visit(tree)
    return found


def _inventory_census(rows: tuple[CallFamilyInventory, ...]) -> Counter[tuple[str, str, str]]:
    found: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        found[(row.path, row.owner, row.callee)] += row.count
    return found


def _symbol_census(symbol_name: str) -> set[tuple[str, str]]:
    return {
        (path.relative_to(ROOT).as_posix(), qualified)
        for path in sorted(SRC.rglob("*.py"))
        for qualified in _qualified_functions(path)
        if qualified.rsplit(".", 1)[-1] == symbol_name
    }


def _composite_wrapper_census() -> set[tuple[str, str, str]]:
    """Find multi-await local callbacks passed to the uncertainty wrapper."""
    found: set[tuple[str, str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        functions = _qualified_functions(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))

        class Visitor(ast.NodeVisitor):
            def __init__(
                self,
                relative_path: str,
                function_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
            ) -> None:
                self.relative = relative_path
                self.functions = function_nodes
                self.stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_FunctionDef = _visit_function
            visit_AsyncFunctionDef = _visit_function

            def visit_Call(self, node: ast.Call) -> None:
                called = None
                if isinstance(node.func, ast.Name):
                    called = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    called = node.func.attr
                if (
                    called == "call_unconfirmed_on_transport_loss"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                ):
                    owner = ".".join(self.stack)
                    callback = f"{owner}.{node.args[0].id}"
                    callback_node = self.functions.get(callback)
                    if callback_node is not None:
                        await_count = sum(
                            isinstance(part, ast.Await) for part in ast.walk(callback_node)
                        )
                        if await_count > 1:
                            found.add((self.relative, owner, callback))
                self.generic_visit(node)

        Visitor(relative, functions).visit(tree)
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


def _callable_has_bare_str_positional(node: ast.AST | None) -> bool:
    """Inspect ``Callable[[...], result]`` inputs without inspecting its result type."""
    if node is None:
        return False
    for part in ast.walk(node):
        if not isinstance(part, ast.Subscript):
            continue
        callable_name = None
        if isinstance(part.value, ast.Name):
            callable_name = part.value.id
        elif isinstance(part.value, ast.Attribute):
            callable_name = part.value.attr
        if callable_name != "Callable":
            continue
        arguments_and_result = part.slice
        if not isinstance(arguments_and_result, ast.Tuple) or len(arguments_and_result.elts) != 2:
            continue
        positional = arguments_and_result.elts[0]
        if not isinstance(positional, (ast.List, ast.Tuple)):
            continue
        if any(
            isinstance(argument, ast.Name) and argument.id == "str" for argument in positional.elts
        ):
            return True
    return False


def _presentation_hits(
    app_root: Path = APP,
    *,
    source_root: Path = SRC,
) -> set[PresentationHit]:
    prohibited = {"json_output", "yes", "quiet", "exit_code", "confirmer"}
    hits: set[PresentationHit] = set()
    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(source_root).as_posix()
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
                    if argument.arg in {
                        "wait_context",
                        "wait_start_sink",
                    } and _callable_has_bare_str_positional(argument.annotation):
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


def test_plan_named_retry_and_evidence_call_inventories_are_exact() -> None:
    rows = (
        *RETRY_CALL_INVENTORY,
        *UNCERTAINTY_WRAPPER_CALL_INVENTORY,
        *UNRESOLVED_COMMIT_CALL_INVENTORY,
        *READINESS_CALL_INVENTORY,
    )
    call_names = {row.callee for row in rows}
    assert _call_family_census(call_names) == _inventory_census(rows)


def test_multi_send_uncertainty_wrapper_inventory_is_exact() -> None:
    assert _composite_wrapper_census() == set(COMPOSITE_WRAPPER_INVENTORY)


def test_workflow_scope_call_inventory_is_exact() -> None:
    assert _call_family_census({"_operation_scope"}) == _inventory_census(
        WORKFLOW_SCOPE_CALL_INVENTORY
    )


def test_workflow_hook_inventory_is_exact() -> None:
    expected = {(row.path, row.symbol) for row in WORKFLOW_INVENTORY}
    assert _symbol_census("_operation_scope") == expected


@pytest.mark.parametrize("row", REQUIRED_WORKFLOW_ENTRYPOINT_INVENTORY)
def test_required_plan_named_workflow_entrypoint_is_structural(row: SymbolInventory) -> None:
    function = _qualified_functions(ROOT / row.path)[row.symbol]
    assert any(isinstance(part, ast.Await) for part in ast.walk(function))


def test_cleanup_inventory_separates_target_from_preserved_families() -> None:
    symbols = {row.symbol for row in CLEANUP_INVENTORY}
    assert TARGET_CLEANUP_SYMBOLS.isdisjoint(PRESERVED_CLEANUP_SYMBOLS)
    assert symbols == TARGET_CLEANUP_SYMBOLS | PRESERVED_CLEANUP_SYMBOLS


@pytest.mark.parametrize("row", RETRY_INVENTORY)
def test_retry_owner_inventory_is_structural(row: SymbolInventory) -> None:
    function = _qualified_functions(ROOT / row.path)[row.symbol]
    assert any(
        isinstance(part, (ast.Await, ast.For, ast.Try, ast.While)) for part in ast.walk(function)
    )


@pytest.mark.parametrize("row", CLEANUP_INVENTORY)
def test_cleanup_inventory_symbol_still_exists(row: SymbolInventory) -> None:
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

def wait(
    wait_context: Callable[[GenerationWaitStarted], str],
    wait_start_sink: Callable[[GenerationWaitStarted], str],
    test_fetch: bool,
) -> None:
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
        *(row.disposition for row in RETRY_CALL_INVENTORY),
        *(row.disposition for row in UNCERTAINTY_WRAPPER_CALL_INVENTORY),
        *(row.disposition for row in UNRESOLVED_COMMIT_CALL_INVENTORY),
        *(row.disposition for row in READINESS_CALL_INVENTORY),
        *COMPOSITE_WRAPPER_INVENTORY.values(),
        *(row.disposition for row in CLEANUP_INVENTORY),
        *(row.disposition for row in WORKFLOW_INVENTORY),
        *(row.disposition for row in WORKFLOW_SCOPE_CALL_INVENTORY),
        *(row.disposition for row in REQUIRED_WORKFLOW_ENTRYPOINT_INVENTORY),
        PRESENTATION_DISPOSITION,
        *GUARDRAIL_DISPOSITIONS.values(),
        *PLANNED_GUARDRAIL_DISPOSITIONS.values(),
    ]
    for disposition in dispositions:
        assert re.fullmatch(r"P[0-8](?:/P[0-8])*", disposition.phase)
        assert disposition.action.strip()
        assert "TBD" not in disposition.action.upper()
