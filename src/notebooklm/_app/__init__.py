"""Transport-neutral application layer for notebooklm-py.

``_app`` holds the **business logic** that is shared between transport
adapters — the Click CLI, the FastMCP server, and any future HTTP surface.
Code in this package MUST stay free of any transport dependency: no
``click``, no ``rich``, no ``notebooklm.cli`` import, no ``fastmcp`` (the
boundary is enforced by ``tests/_guardrails/test_app_boundary.py``).

Each adapter is a thin shell that:

* parses its own inputs into the typed request/plan objects defined here,
* calls the neutral logic (raising the public ``notebooklm.exceptions``
  hierarchy on failure), and
* renders the typed result into its own envelope vocabulary.

The Wave-0 foundation primitives every adapter needs:

* :func:`~notebooklm._app.serialize.to_jsonable` — recursive JSON-able
  conversion of dataclasses / enums / datetimes / bytes / containers.
* :func:`~notebooklm._app.serialize.source_summary` — the single neutral
  ``{"id", "title", "type", "url"}`` source-summary shape both adapters import.
* :func:`~notebooklm._app.errors.classify` — class-sensitive
  exception → :class:`~notebooklm._app.errors.ClassifiedError` mapping that
  each adapter projects onto its own code table.
* :func:`~notebooklm._app.resolve.validate_id` /
  :func:`~notebooklm._app.resolve.resolve_ref` — Click-free id validation
  and partial-id resolution.
* :class:`~notebooklm._app.events.ProgressEvent` /
  :class:`~notebooklm._app.events.ProgressSink` — a transport-neutral
  progress-reporting seam for long-running operations.

The domain modules (``artifacts``, ``chat``, ``doctor``, ``download``,
``generate``, ``labels``, ``language``, ``notebooks``, ``notes``,
``research``, ``sharing``, ``skill``, ``source_*``) hold the relocated CLI
business logic each command's thin adapter now calls.
"""

from __future__ import annotations

from .artifacts import (
    ArtifactExportResult,
    ArtifactRenameResult,
    ArtifactStatusView,
    delete_artifact,
    export_artifact,
    get_artifact,
    poll_artifact,
    rename_artifact,
    retry_artifact,
    status_view,
    wait_for_artifact,
)
from .auth_check import AuthCheckPlan, AuthCheckResult, run_auth_check
from .chat import (
    ChatModeChoice,
    ClearCacheResult,
    ConfigureResult,
    HistoryFetch,
    ResponseLengthChoice,
    SaveNoteOutcome,
    determine_conversation_id,
    execute_clear_cache,
    execute_configure,
    fetch_history,
    format_history,
    format_single_qa,
    get_latest_conversation_from_server,
    save_answer_as_note,
    validate_ask_flags,
)
from .doctor import DoctorPaths, DoctorReport, run_checks
from .download import (
    FORMAT_EXTENSIONS,
    ArtifactDict,
    DownloadOutcome,
    DownloadPlan,
    DownloadPlanValidationError,
    DownloadResult,
    DownloadTypeSpec,
    artifact_title_to_filename,
    build_download_plan,
    execute_download,
    select_artifact,
)
from .errors import ClassifiedError, ErrorCategory, classify
from .events import ProgressEvent, ProgressSink
from .generate import (
    GenerationExecutionResult,
    GenerationKind,
    GenerationOutcome,
    GenerationPlan,
    GenerationPlanValidationError,
    build_generation_plan,
    execute_generation,
    generate_with_retry,
    generation_outcome_from_status,
    handle_generation_result,
)
from .labels import (
    LabelGenerateResult,
    LabelMembershipResult,
    LabelResolutionError,
    execute_label_add_sources,
    execute_label_create,
    execute_label_delete,
    execute_label_generate,
    execute_label_remove_sources,
    execute_label_rename,
    execute_label_set_emoji,
    execute_label_sources,
    resolve_label_id,
)
from .language import SUPPORTED_LANGUAGES, LanguageConfigStore, is_supported_language, language_name
from .notebooks import (
    NotebookCreateResult,
    NotebookDescribeResult,
    NotebookMetadataResult,
    NotebookRenameResult,
    execute_notebook_create,
    execute_notebook_delete,
    execute_notebook_describe,
    execute_notebook_metadata,
    execute_notebook_rename,
)
from .notes import (
    NoteCreateResult,
    NoteGetResult,
    NoteRenameResult,
    NoteSaveResult,
    execute_note_create,
    execute_note_delete,
    execute_note_get,
    execute_note_rename,
    execute_note_save,
    extract_new_note_id,
    resolve_note_for_delete,
)
from .profile import (
    ProfileEntry,
    gather_profile_list,
    is_protected_profile,
    retarget_default_profile_mutator,
    set_default_profile_mutator,
)
from .research import (
    ResearchStatusResult,
    ResearchWaitOutcome,
    ResearchWaitPlan,
    ResearchWaitResult,
    execute_research_wait,
    poll_and_classify,
    validate_research_wait_flags,
)
from .resolve import AmbiguousIdError, Resolution, resolve_ref, validate_id
from .serialize import source_summary, to_jsonable
from .session import (
    LogoutFailure,
    LogoutFailureKind,
    LogoutInputs,
    LogoutOutcome,
    StatusContext,
    StatusInputs,
    StatusReport,
    UseNotebookResult,
    execute_logout,
    read_status,
    verify_and_set_notebook,
)
from .sharing import (
    execute_share_add_user,
    execute_share_remove_user,
    execute_share_set_public,
    execute_share_set_view_level,
    execute_share_status,
    execute_share_update_user,
)
from .skill import (
    SCOPES,
    TARGET_CREATE,
    TARGET_OVERWRITE,
    TARGET_UP_TO_DATE,
    TARGETS,
    SkillTarget,
    add_version_comment,
    classify_target,
    get_installed_content,
    get_package_version,
    get_scope_root,
    get_skill_path,
    get_skill_version,
    iter_targets,
    remove_empty_parents,
    report_mixed_no_clobber_up_to_date,
)
from .source_add import (
    SourceAddExecutionPlan,
    SourceAddFacade,
    SourceAddPlan,
    SourceAddResult,
    SourceAddType,
    SourceAddValidationError,
    add_source,
    build_source_add_plan,
    execute_source_add,
    looks_like_path,
    validate_upload_path,
    validate_url,
)
from .source_clean import (
    CleanCandidate,
    CleanFailure,
    CleanStatus,
    SourceCleanResult,
    candidates_payload,
    classify_junk_sources,
    normalize_url_for_dedup,
    run_source_clean,
)
from .source_content import (
    FulltextFormat,
    SourceFulltextPlan,
    SourceFulltextResult,
    SourceGetPlan,
    SourceGetResult,
    SourceGuidePlan,
    SourceGuideResult,
    SourceStalePlan,
    SourceStaleResult,
    execute_source_fulltext,
    execute_source_get,
    execute_source_guide,
    execute_source_stale,
)
from .source_listing import fetch_sources
from .source_mutations import (
    DriveMimeChoice,
    SourceAddDrivePlan,
    SourceAddDriveResult,
    SourceDeleteByTitlePlan,
    SourceDeleteByTitleResult,
    SourceDeletePlan,
    SourceDeleteResult,
    SourceIdResolution,
    SourceMutationError,
    SourceRefreshPlan,
    SourceRefreshResult,
    SourceRenamePlan,
    SourceRenameResult,
    build_id_ambiguity_error,
    execute_source_add_drive,
    execute_source_delete,
    execute_source_delete_by_title,
    execute_source_refresh,
    execute_source_rename,
    looks_like_full_source_id,
    require_yes_in_json,
    resolve_source_by_exact_title,
    resolve_source_for_delete,
)
from .source_wait import (
    SourceWaitNotFound,
    SourceWaitOutcome,
    SourceWaitPlan,
    SourceWaitProcessingError,
    SourceWaitReady,
    SourceWaitTimeout,
    execute_source_wait,
)

__all__ = [
    # Wave-0 foundation
    "to_jsonable",
    "source_summary",
    "ClassifiedError",
    "ErrorCategory",
    "classify",
    "AmbiguousIdError",
    "Resolution",
    "resolve_ref",
    "validate_id",
    "ProgressEvent",
    "ProgressSink",
    # auth_check
    "AuthCheckPlan",
    "AuthCheckResult",
    "run_auth_check",
    # session
    "LogoutFailure",
    "LogoutFailureKind",
    "LogoutInputs",
    "LogoutOutcome",
    "StatusContext",
    "StatusInputs",
    "StatusReport",
    "UseNotebookResult",
    "execute_logout",
    "read_status",
    "verify_and_set_notebook",
    # profile
    "ProfileEntry",
    "gather_profile_list",
    "is_protected_profile",
    "retarget_default_profile_mutator",
    "set_default_profile_mutator",
    # artifacts
    "ArtifactExportResult",
    "ArtifactRenameResult",
    "ArtifactStatusView",
    "delete_artifact",
    "export_artifact",
    "get_artifact",
    "poll_artifact",
    "rename_artifact",
    "retry_artifact",
    "status_view",
    "wait_for_artifact",
    # chat
    "ChatModeChoice",
    "ClearCacheResult",
    "ConfigureResult",
    "HistoryFetch",
    "ResponseLengthChoice",
    "SaveNoteOutcome",
    "determine_conversation_id",
    "execute_clear_cache",
    "execute_configure",
    "fetch_history",
    "format_history",
    "format_single_qa",
    "get_latest_conversation_from_server",
    "save_answer_as_note",
    "validate_ask_flags",
    # doctor
    "DoctorPaths",
    "DoctorReport",
    "run_checks",
    # generate
    "GenerationExecutionResult",
    "GenerationKind",
    "GenerationOutcome",
    "GenerationPlan",
    "GenerationPlanValidationError",
    "build_generation_plan",
    "execute_generation",
    "generate_with_retry",
    "generation_outcome_from_status",
    "handle_generation_result",
    # labels
    "LabelGenerateResult",
    "LabelMembershipResult",
    "LabelResolutionError",
    "execute_label_add_sources",
    "execute_label_create",
    "execute_label_delete",
    "execute_label_generate",
    "execute_label_remove_sources",
    "execute_label_rename",
    "execute_label_set_emoji",
    "execute_label_sources",
    "resolve_label_id",
    # language
    "SUPPORTED_LANGUAGES",
    "LanguageConfigStore",
    "is_supported_language",
    "language_name",
    # notebooks
    "NotebookCreateResult",
    "NotebookDescribeResult",
    "NotebookMetadataResult",
    "NotebookRenameResult",
    "execute_notebook_create",
    "execute_notebook_delete",
    "execute_notebook_describe",
    "execute_notebook_metadata",
    "execute_notebook_rename",
    # notes
    "NoteCreateResult",
    "NoteGetResult",
    "NoteRenameResult",
    "NoteSaveResult",
    "execute_note_create",
    "execute_note_delete",
    "execute_note_get",
    "execute_note_rename",
    "execute_note_save",
    "extract_new_note_id",
    "resolve_note_for_delete",
    # research
    "ResearchStatusResult",
    "ResearchWaitOutcome",
    "ResearchWaitPlan",
    "ResearchWaitResult",
    "execute_research_wait",
    "poll_and_classify",
    "validate_research_wait_flags",
    # sharing
    "execute_share_add_user",
    "execute_share_remove_user",
    "execute_share_set_public",
    "execute_share_set_view_level",
    "execute_share_status",
    "execute_share_update_user",
    # skill
    "SCOPES",
    "TARGET_CREATE",
    "TARGET_OVERWRITE",
    "TARGET_UP_TO_DATE",
    "TARGETS",
    "SkillTarget",
    "add_version_comment",
    "classify_target",
    "get_installed_content",
    "get_package_version",
    "get_scope_root",
    "get_skill_path",
    "get_skill_version",
    "iter_targets",
    "remove_empty_parents",
    "report_mixed_no_clobber_up_to_date",
    # download
    "FORMAT_EXTENSIONS",
    "ArtifactDict",
    "DownloadOutcome",
    "DownloadPlan",
    "DownloadPlanValidationError",
    "DownloadResult",
    "DownloadTypeSpec",
    "artifact_title_to_filename",
    "build_download_plan",
    "execute_download",
    "select_artifact",
    # source_add
    "SourceAddExecutionPlan",
    "SourceAddFacade",
    "SourceAddPlan",
    "SourceAddResult",
    "SourceAddType",
    "SourceAddValidationError",
    "add_source",
    "build_source_add_plan",
    "execute_source_add",
    "looks_like_path",
    "validate_upload_path",
    "validate_url",
    # source_clean
    "CleanCandidate",
    "CleanFailure",
    "CleanStatus",
    "SourceCleanResult",
    "candidates_payload",
    "classify_junk_sources",
    "normalize_url_for_dedup",
    "run_source_clean",
    # source_content
    "FulltextFormat",
    "SourceFulltextPlan",
    "SourceFulltextResult",
    "SourceGetPlan",
    "SourceGetResult",
    "SourceGuidePlan",
    "SourceGuideResult",
    "SourceStalePlan",
    "SourceStaleResult",
    "execute_source_fulltext",
    "execute_source_get",
    "execute_source_guide",
    "execute_source_stale",
    # source_listing
    "fetch_sources",
    # source_mutations
    "DriveMimeChoice",
    "SourceAddDrivePlan",
    "SourceAddDriveResult",
    "SourceDeleteByTitlePlan",
    "SourceDeleteByTitleResult",
    "SourceDeletePlan",
    "SourceDeleteResult",
    "SourceIdResolution",
    "SourceMutationError",
    "SourceRefreshPlan",
    "SourceRefreshResult",
    "SourceRenamePlan",
    "SourceRenameResult",
    "build_id_ambiguity_error",
    "execute_source_add_drive",
    "execute_source_delete",
    "execute_source_delete_by_title",
    "execute_source_refresh",
    "execute_source_rename",
    "looks_like_full_source_id",
    "require_yes_in_json",
    "resolve_source_by_exact_title",
    "resolve_source_for_delete",
    # source_wait
    "SourceWaitNotFound",
    "SourceWaitOutcome",
    "SourceWaitPlan",
    "SourceWaitProcessingError",
    "SourceWaitReady",
    "SourceWaitTimeout",
    "execute_source_wait",
]
