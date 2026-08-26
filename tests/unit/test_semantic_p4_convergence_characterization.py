"""Characterization and audit sentinels for Phase 4 convergence.

This suite pins:
1. Migrated OperationDef CallPolicy vs current native idempotency policy.
2. Exact known divergence reporting without controlling transport retries.
3. RuntimeDeadline identity, no nested budget reset for migrated notebook/source
   paths, and the documented shared-poll follower exception.
4. BackendError diagnostic payload population (method_id, rpc_code, found_ids,
   raw_response, rpc_id, code), outcome_unknown -> unconfirmed marker projection,
   and the public exception mixin/catch-order lattice.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from scripts._operation_catalog_specs import DIVERGENCE_KINDS, OPERATION_SPECS
from scripts._web_policy_intent import (
    SERVICE_OWNED_WORKFLOW_BINDINGS,
    WEB_CALL_POLICY_BINDINGS,
    StreamedPolicyBinding,
)
from scripts.audit_operation_catalog import (
    audit_direct_row_native_parity,
    audit_web_call_policy_bindings,
    derive_row_dispatch,
    web_policy_parity_report,
)

from notebooklm import artifacts as artifact_helpers
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._artifact.polling import ArtifactPollingService
from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
)
from notebooklm._backend_compat import project_backend_error
from notebooklm._deadline import RuntimeDeadline
from notebooklm._idempotency import (
    IDEMPOTENCY_REGISTRY,
    IdempotencyPolicy,
    resolve_effective_disable_internal_retries,
)
from notebooklm._notebook_mutation_service import NotebookMutationService
from notebooklm._operations import CallPolicy, Operation, OperationDef
from notebooklm._read_services import NotebookReadService, SourceReadService
from notebooklm._records import (
    ARTIFACT_CATALOG_DEF,
    ARTIFACT_DELETE_DEF,
    ARTIFACT_DOWNLOAD_DEF,
    ARTIFACT_EXPORT_DEF,
    ARTIFACT_GENERATE_AUDIO_DEF,
    ARTIFACT_GENERATE_DATA_TABLE_DEF,
    ARTIFACT_GENERATE_FLASHCARDS_DEF,
    ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
    ARTIFACT_GENERATE_QUIZ_DEF,
    ARTIFACT_GENERATE_REPORT_DEF,
    ARTIFACT_GENERATE_SLIDE_DECK_DEF,
    ARTIFACT_GENERATE_VIDEO_DEF,
    ARTIFACT_PATCH_TITLE_DEF,
    ARTIFACT_RETRY_DEF,
    ARTIFACT_REVISE_SLIDE_DEF,
    ARTIFACT_SUGGEST_REPORTS_DEF,
    ARTIFACT_WAIT_DEF,
    CHAT_CONFIGURE_DEF,
    CHAT_DELETE_HISTORY_DEF,
    CHAT_GET_CONVERSATION_DEF,
    CHAT_GET_HISTORY_DEF,
    CHAT_SAVE_NOTE_DEF,
    CHAT_STREAM_ANSWER_DEF,
    COLLECTION_DELETE_DEF,
    COLLECTION_GET_DEF,
    COLLECTION_LIST_DEF,
    LABEL_ALLOCATE_DEF,
    LABEL_DELETE_DEF,
    LABEL_GENERATE_DEF,
    LABEL_GET_DEF,
    LABEL_LIST_DEF,
    LABEL_MUTATE_DEF,
    LEGACY_SHARE_ARTIFACT_DEF,
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GENERATE_DEF,
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GENERATE_NOTE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_LIST_DEF,
    MIND_MAP_UPDATE_DEF,
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    NOTE_UPDATE_DEF,
    NOTEBOOK_ALLOCATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_PATCH_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_SUGGEST_PROMPTS_DEF,
    NOTEBOOK_SUMMARIZE_DEF,
    RESEARCH_CANCEL_DEF,
    RESEARCH_IMPORT_DEF,
    RESEARCH_POLL_DEF,
    RESEARCH_START_DEF,
    SETTINGS_GET_DEF,
    SETTINGS_GET_LIMITS_DEF,
    SETTINGS_SET_LANGUAGE_DEF,
    SHARING_GET_DEF,
    SHARING_MUTATE_DEF,
    SHARING_PATCH_VIEW_LEVEL_DEF,
    SOURCE_ADD_FILE_DEF,
    SOURCE_CHECK_FRESHNESS_DEF,
    SOURCE_DELETE_DEF,
    SOURCE_GET_DEF,
    SOURCE_GET_FULLTEXT_DEF,
    SOURCE_GET_GUIDE_DEF,
    SOURCE_LIST_DEF,
    SOURCE_PATCH_TITLE_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_REGISTER_DEF,
    SOURCE_WAIT_DEF,
    NotebookGetInput,
    NotebookGetResult,
    NotebookListResult,
    NotebookRecord,
    SourceGetResult,
    SourceListResult,
    SourceRecord,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SUPPORTED_OPERATIONS
from notebooklm.exceptions import (
    ArtifactError,
    ArtifactInProgressTimeoutError,
    ArtifactNotFoundError,
    ArtifactPendingTimeoutError,
    ArtifactTimeoutError,
    AuthError,
    ClientError,
    CollectionError,
    CollectionNotFoundError,
    DecodingError,
    LabelError,
    LabelNotFoundError,
    LockUnavailableError,
    MindMapError,
    MindMapNotFoundError,
    NetworkError,
    NonIdempotentRetryError,
    NotebookError,
    NotebookLMError,
    NotebookNotFoundError,
    NoteError,
    NoteNotFoundError,
    NotFoundError,
    RateLimitError,
    ResearchError,
    ResearchTimeoutError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
    SourceError,
    SourceNotFoundError,
    SourceTimeoutError,
    UnknownRPCMethodError,
    WaitTimeoutError,
)
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend

# =============================================================================
# 1. Migrated OperationDef CallPolicy vs Current Idempotency Policy
# =============================================================================


def test_call_policy_vocabulary_is_closed_four_way_enum() -> None:
    """CallPolicy must have exactly the four declared semantic policies."""
    assert {policy.value for policy in CallPolicy} == {
        "read",
        "stateful_start",
        "mutation",
        "stream",
    }
    assert len(CallPolicy) == 4


def test_migrated_operation_defs_are_frozen_and_attach_expected_call_policy() -> None:
    """Every migrated operation definition carries its canonical CallPolicy."""
    expected_migrated: dict[OperationDef[Any, Any], tuple[Operation, CallPolicy]] = {
        NOTEBOOK_LIST_DEF: (Operation.NOTEBOOK_LIST, CallPolicy.READ),
        NOTEBOOK_GET_DEF: (Operation.NOTEBOOK_GET, CallPolicy.MUTATION),
        NOTEBOOK_ALLOCATE_DEF: (Operation.NOTEBOOK_ALLOCATE, CallPolicy.MUTATION),
        NOTEBOOK_PATCH_DEF: (Operation.NOTEBOOK_PATCH, CallPolicy.MUTATION),
        NOTEBOOK_DELETE_DEF: (Operation.NOTEBOOK_DELETE, CallPolicy.MUTATION),
        NOTEBOOK_REMOVE_RECENT_DEF: (
            Operation.NOTEBOOK_REMOVE_RECENT,
            CallPolicy.MUTATION,
        ),
        NOTEBOOK_SUMMARIZE_DEF: (
            Operation.NOTEBOOK_SUMMARIZE,
            CallPolicy.STATEFUL_START,
        ),
        NOTEBOOK_DESCRIBE_DEF: (
            Operation.NOTEBOOK_DESCRIBE,
            CallPolicy.STATEFUL_START,
        ),
        SOURCE_LIST_DEF: (Operation.SOURCE_LIST, CallPolicy.MUTATION),
        SOURCE_GET_DEF: (Operation.SOURCE_GET, CallPolicy.MUTATION),
        CHAT_STREAM_ANSWER_DEF: (Operation.CHAT_STREAM_ANSWER, CallPolicy.STREAM),
        CHAT_GET_CONVERSATION_DEF: (Operation.CHAT_GET_CONVERSATION, CallPolicy.READ),
        CHAT_GET_HISTORY_DEF: (Operation.CHAT_GET_HISTORY, CallPolicy.READ),
        CHAT_DELETE_HISTORY_DEF: (Operation.CHAT_DELETE_HISTORY, CallPolicy.MUTATION),
        CHAT_CONFIGURE_DEF: (Operation.CHAT_CONFIGURE, CallPolicy.MUTATION),
        CHAT_SAVE_NOTE_DEF: (Operation.CHAT_SAVE_NOTE, CallPolicy.MUTATION),
        SHARING_GET_DEF: (Operation.SHARING_GET, CallPolicy.READ),
        SHARING_PATCH_VIEW_LEVEL_DEF: (
            Operation.SHARING_PATCH_VIEW_LEVEL,
            CallPolicy.MUTATION,
        ),
        LEGACY_SHARE_ARTIFACT_DEF: (
            Operation.LEGACY_SHARE_ARTIFACT,
            CallPolicy.MUTATION,
        ),
        RESEARCH_START_DEF: (Operation.RESEARCH_START, CallPolicy.STATEFUL_START),
        RESEARCH_POLL_DEF: (Operation.RESEARCH_POLL, CallPolicy.READ),
        RESEARCH_CANCEL_DEF: (Operation.RESEARCH_CANCEL, CallPolicy.MUTATION),
        RESEARCH_IMPORT_DEF: (Operation.RESEARCH_IMPORT, CallPolicy.MUTATION),
        SETTINGS_GET_DEF: (Operation.SETTINGS_GET, CallPolicy.READ),
        SETTINGS_GET_LIMITS_DEF: (Operation.SETTINGS_GET_LIMITS, CallPolicy.READ),
        SETTINGS_SET_LANGUAGE_DEF: (Operation.SETTINGS_SET_LANGUAGE, CallPolicy.MUTATION),
        NOTEBOOK_SUGGEST_PROMPTS_DEF: (
            Operation.NOTEBOOK_SUGGEST_PROMPTS,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_SUGGEST_REPORTS_DEF: (
            Operation.ARTIFACT_SUGGEST_REPORTS,
            CallPolicy.STATEFUL_START,
        ),
        SOURCE_ADD_FILE_DEF: (Operation.SOURCE_ADD_FILE, CallPolicy.MUTATION),
        SOURCE_DELETE_DEF: (Operation.SOURCE_DELETE, CallPolicy.MUTATION),
        SOURCE_REFRESH_DEF: (Operation.SOURCE_REFRESH, CallPolicy.MUTATION),
        SOURCE_CHECK_FRESHNESS_DEF: (Operation.SOURCE_CHECK_FRESHNESS, CallPolicy.READ),
        SOURCE_GET_GUIDE_DEF: (Operation.SOURCE_GET_GUIDE, CallPolicy.STATEFUL_START),
        SOURCE_GET_FULLTEXT_DEF: (Operation.SOURCE_GET_FULLTEXT, CallPolicy.READ),
        SOURCE_WAIT_DEF: (Operation.SOURCE_WAIT, CallPolicy.MUTATION),
        SOURCE_PATCH_TITLE_DEF: (Operation.SOURCE_PATCH_TITLE, CallPolicy.MUTATION),
        ARTIFACT_GENERATE_AUDIO_DEF: (
            Operation.ARTIFACT_GENERATE_AUDIO,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_GENERATE_QUIZ_DEF: (
            Operation.ARTIFACT_GENERATE_QUIZ,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_GENERATE_FLASHCARDS_DEF: (
            Operation.ARTIFACT_GENERATE_FLASHCARDS,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_GENERATE_VIDEO_DEF: (
            Operation.ARTIFACT_GENERATE_VIDEO,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_GENERATE_REPORT_DEF: (
            Operation.ARTIFACT_GENERATE_REPORT,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_GENERATE_INFOGRAPHIC_DEF: (
            Operation.ARTIFACT_GENERATE_INFOGRAPHIC,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_GENERATE_SLIDE_DECK_DEF: (
            Operation.ARTIFACT_GENERATE_SLIDE_DECK,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_GENERATE_DATA_TABLE_DEF: (
            Operation.ARTIFACT_GENERATE_DATA_TABLE,
            CallPolicy.STATEFUL_START,
        ),
        ARTIFACT_EXPORT_DEF: (Operation.ARTIFACT_EXPORT, CallPolicy.MUTATION),
        ARTIFACT_REVISE_SLIDE_DEF: (
            Operation.ARTIFACT_REVISE_SLIDE,
            CallPolicy.MUTATION,
        ),
        ARTIFACT_RETRY_DEF: (Operation.ARTIFACT_RETRY, CallPolicy.STATEFUL_START),
        ARTIFACT_DELETE_DEF: (Operation.ARTIFACT_DELETE, CallPolicy.MUTATION),
        ARTIFACT_PATCH_TITLE_DEF: (Operation.ARTIFACT_PATCH_TITLE, CallPolicy.MUTATION),
        ARTIFACT_CATALOG_DEF: (Operation.ARTIFACT_CATALOG, CallPolicy.READ),
        ARTIFACT_DOWNLOAD_DEF: (Operation.ARTIFACT_DOWNLOAD, CallPolicy.READ),
        ARTIFACT_WAIT_DEF: (Operation.ARTIFACT_WAIT, CallPolicy.READ),
        NOTE_LIST_DEF: (Operation.NOTE_LIST, CallPolicy.READ),
        NOTE_GET_DEF: (Operation.NOTE_GET, CallPolicy.READ),
        NOTE_CREATE_DEF: (Operation.NOTE_CREATE, CallPolicy.MUTATION),
        NOTE_UPDATE_DEF: (Operation.NOTE_UPDATE, CallPolicy.MUTATION),
        NOTE_DELETE_DEF: (Operation.NOTE_DELETE, CallPolicy.MUTATION),
        MIND_MAP_LIST_DEF: (Operation.MIND_MAP_LIST, CallPolicy.READ),
        MIND_MAP_GET_DEF: (Operation.MIND_MAP_GET, CallPolicy.READ),
        MIND_MAP_GENERATE_NOTE_DEF: (
            Operation.MIND_MAP_GENERATE_NOTE,
            CallPolicy.STATEFUL_START,
        ),
        MIND_MAP_GENERATE_INTERACTIVE_DEF: (
            Operation.MIND_MAP_GENERATE_INTERACTIVE,
            CallPolicy.STATEFUL_START,
        ),
        MIND_MAP_UPDATE_DEF: (Operation.MIND_MAP_UPDATE, CallPolicy.MUTATION),
        MIND_MAP_DELETE_DEF: (Operation.MIND_MAP_DELETE, CallPolicy.MUTATION),
        MIND_MAP_GENERATE_DEF: (Operation.MIND_MAP_GENERATE, CallPolicy.STATEFUL_START),
        # P6.4 migrates labels and collections as one slice: they are a single
        # wire surface addressed through an explicit kind discriminator, so each
        # dialect's key carries the same policy as its twin.
        LABEL_LIST_DEF: (Operation.LABEL_LIST, CallPolicy.READ),
        LABEL_GET_DEF: (Operation.LABEL_GET, CallPolicy.READ),
        LABEL_GENERATE_DEF: (Operation.LABEL_GENERATE, CallPolicy.STATEFUL_START),
        LABEL_DELETE_DEF: (Operation.LABEL_DELETE, CallPolicy.MUTATION),
        COLLECTION_LIST_DEF: (Operation.COLLECTION_LIST, CallPolicy.READ),
        COLLECTION_GET_DEF: (Operation.COLLECTION_GET, CallPolicy.READ),
        COLLECTION_DELETE_DEF: (Operation.COLLECTION_DELETE, CallPolicy.MUTATION),
        LABEL_MUTATE_DEF: (Operation.LABEL_MUTATE, CallPolicy.MUTATION),
        LABEL_ALLOCATE_DEF: (Operation.LABEL_ALLOCATE, CallPolicy.MUTATION),
        SHARING_MUTATE_DEF: (Operation.SHARING_MUTATE, CallPolicy.MUTATION),
        SOURCE_REGISTER_DEF: (Operation.SOURCE_REGISTER, CallPolicy.MUTATION),
    }

    for op_def, (expected_key, expected_policy) in expected_migrated.items():
        assert op_def.key is expected_key
        assert op_def.policy is expected_policy
        assert hasattr(op_def, "__slots__")
        with pytest.raises(FrozenInstanceError):
            op_def.policy = CallPolicy.READ  # type: ignore[misc]

    assert {op_def.key for op_def in expected_migrated} == set(WEB_SUPPORTED_OPERATIONS)


@pytest.mark.parametrize(
    ("operation_def", "expected_native_rpcs", "expected_idempotency_policies"),
    [
        (
            NOTEBOOK_LIST_DEF,
            [(RPCMethod.LIST_NOTEBOOKS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            NOTEBOOK_GET_DEF,
            [(RPCMethod.GET_NOTEBOOK, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            NOTEBOOK_ALLOCATE_DEF,
            [(RPCMethod.CREATE_NOTEBOOK, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            NOTEBOOK_PATCH_DEF,
            [(RPCMethod.RENAME_NOTEBOOK, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            NOTEBOOK_DELETE_DEF,
            [(RPCMethod.DELETE_NOTEBOOK, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            SOURCE_LIST_DEF,
            [(RPCMethod.GET_NOTEBOOK, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            SOURCE_GET_DEF,
            [(RPCMethod.GET_NOTEBOOK, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            SETTINGS_GET_DEF,
            [(RPCMethod.GET_USER_SETTINGS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            SETTINGS_GET_LIMITS_DEF,
            [(RPCMethod.GET_USER_SETTINGS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            SETTINGS_SET_LANGUAGE_DEF,
            [(RPCMethod.SET_USER_SETTINGS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            NOTEBOOK_SUGGEST_PROMPTS_DEF,
            [(RPCMethod.SUGGEST_PROMPTS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            ARTIFACT_SUGGEST_REPORTS_DEF,
            [(RPCMethod.GET_SUGGESTED_REPORTS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            CHAT_GET_CONVERSATION_DEF,
            [(RPCMethod.GET_LAST_CONVERSATION_ID, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            # P9.3 ledger correction: the history read never resolved the
            # conversation below the port (gate table §9).
            CHAT_GET_HISTORY_DEF,
            [(RPCMethod.GET_CONVERSATION_TURNS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            CHAT_DELETE_HISTORY_DEF,
            [(RPCMethod.DELETE_CONVERSATION, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            CHAT_CONFIGURE_DEF,
            [(RPCMethod.GET_NOTEBOOK, None), (RPCMethod.RENAME_NOTEBOOK, None)],
            [
                IdempotencyPolicy.IDEMPOTENT_SET_OP,
                IdempotencyPolicy.IDEMPOTENT_SET_OP,
            ],
        ),
        (
            CHAT_SAVE_NOTE_DEF,
            [(RPCMethod.CREATE_NOTE, "saved_from_chat")],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
        (
            ARTIFACT_GENERATE_AUDIO_DEF,
            [(RPCMethod.CREATE_ARTIFACT, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            ARTIFACT_GENERATE_QUIZ_DEF,
            [(RPCMethod.CREATE_ARTIFACT, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            ARTIFACT_GENERATE_FLASHCARDS_DEF,
            [(RPCMethod.CREATE_ARTIFACT, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            ARTIFACT_GENERATE_VIDEO_DEF,
            [(RPCMethod.CREATE_ARTIFACT, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            ARTIFACT_GENERATE_REPORT_DEF,
            [(RPCMethod.CREATE_ARTIFACT, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
            [(RPCMethod.CREATE_ARTIFACT, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            ARTIFACT_GENERATE_SLIDE_DECK_DEF,
            [(RPCMethod.CREATE_ARTIFACT, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            ARTIFACT_GENERATE_DATA_TABLE_DEF,
            [(RPCMethod.CREATE_ARTIFACT, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            ARTIFACT_EXPORT_DEF,
            [(RPCMethod.EXPORT_ARTIFACT, None)],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
        (
            ARTIFACT_REVISE_SLIDE_DEF,
            [(RPCMethod.REVISE_SLIDE, None)],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
        (
            ARTIFACT_RETRY_DEF,
            [(RPCMethod.RETRY_ARTIFACT, None)],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
        (
            ARTIFACT_DELETE_DEF,
            [(RPCMethod.DELETE_ARTIFACT, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            ARTIFACT_PATCH_TITLE_DEF,
            [(RPCMethod.RENAME_ARTIFACT, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            ARTIFACT_CATALOG_DEF,
            [(RPCMethod.LIST_ARTIFACTS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            ARTIFACT_DOWNLOAD_DEF,
            [
                (RPCMethod.LIST_ARTIFACTS, None),
                (RPCMethod.GET_NOTES_AND_MIND_MAPS, None),
                (RPCMethod.GET_INTERACTIVE_HTML, None),
            ],
            [
                IdempotencyPolicy.IDEMPOTENT_SET_OP,
                IdempotencyPolicy.IDEMPOTENT_SET_OP,
                IdempotencyPolicy.IDEMPOTENT_SET_OP,
            ],
        ),
        (
            ARTIFACT_WAIT_DEF,
            [(RPCMethod.LIST_ARTIFACTS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            ARTIFACT_SUGGEST_REPORTS_DEF,
            [(RPCMethod.GET_SUGGESTED_REPORTS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            NOTE_LIST_DEF,
            [(RPCMethod.GET_NOTES_AND_MIND_MAPS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            NOTE_GET_DEF,
            [(RPCMethod.GET_NOTES_AND_MIND_MAPS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            NOTE_CREATE_DEF,
            [(RPCMethod.CREATE_NOTE, "plain")],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
        (
            NOTE_UPDATE_DEF,
            [(RPCMethod.UPDATE_NOTE, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            NOTE_DELETE_DEF,
            [(RPCMethod.DELETE_NOTE, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            MIND_MAP_LIST_DEF,
            [(RPCMethod.GET_NOTES_AND_MIND_MAPS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            MIND_MAP_GET_DEF,
            [(RPCMethod.GET_INTERACTIVE_HTML, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            MIND_MAP_GENERATE_DEF,
            [(RPCMethod.GENERATE_MIND_MAP, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            MIND_MAP_GENERATE_NOTE_DEF,
            [(RPCMethod.GENERATE_MIND_MAP, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            MIND_MAP_GENERATE_INTERACTIVE_DEF,
            [(RPCMethod.CREATE_ARTIFACT, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            MIND_MAP_UPDATE_DEF,
            [(RPCMethod.RENAME_ARTIFACT, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            MIND_MAP_DELETE_DEF,
            [(RPCMethod.DELETE_ARTIFACT, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            LABEL_LIST_DEF,
            [(RPCMethod.LIST_LABELS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            LABEL_GET_DEF,
            [(RPCMethod.LIST_LABELS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            LABEL_GENERATE_DEF,
            [(RPCMethod.CREATE_LABEL, None)],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
        (
            LABEL_DELETE_DEF,
            [(RPCMethod.DELETE_LABEL, None)],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
        (
            LABEL_MUTATE_DEF,
            [
                (RPCMethod.UPDATE_LABEL, None),
                (RPCMethod.UPDATE_LABEL, "add_sources"),
                (RPCMethod.UPDATE_LABEL, "remove_sources"),
                (RPCMethod.UPDATE_LABEL, "add_notebooks"),
                (RPCMethod.UPDATE_LABEL, "remove_notebooks"),
            ],
            [
                IdempotencyPolicy.IDEMPOTENT_SET_OP,
                IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
                IdempotencyPolicy.IDEMPOTENT_SET_OP,
                IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
                IdempotencyPolicy.IDEMPOTENT_SET_OP,
            ],
        ),
        (
            LABEL_ALLOCATE_DEF,
            [(RPCMethod.CREATE_LABEL, None)],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
        (
            SHARING_MUTATE_DEF,
            [(RPCMethod.SHARE_NOTEBOOK, None)],
            [IdempotencyPolicy.PROBE_THEN_CREATE],
        ),
        (
            COLLECTION_LIST_DEF,
            [(RPCMethod.LIST_LABELS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            COLLECTION_GET_DEF,
            [(RPCMethod.LIST_LABELS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            COLLECTION_DELETE_DEF,
            [(RPCMethod.DELETE_LABEL, None)],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
        (
            SHARING_GET_DEF,
            [(RPCMethod.GET_SHARE_STATUS, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            SHARING_PATCH_VIEW_LEVEL_DEF,
            [(RPCMethod.RENAME_NOTEBOOK, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            RESEARCH_START_DEF,
            [
                (RPCMethod.START_FAST_RESEARCH, None),
                (RPCMethod.START_DEEP_RESEARCH, None),
            ],
            [
                IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
                IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
            ],
        ),
        (
            RESEARCH_POLL_DEF,
            [(RPCMethod.POLL_RESEARCH, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            RESEARCH_CANCEL_DEF,
            [(RPCMethod.CANCEL_RESEARCH, None)],
            [IdempotencyPolicy.IDEMPOTENT_SET_OP],
        ),
        (
            RESEARCH_IMPORT_DEF,
            [(RPCMethod.IMPORT_RESEARCH, None)],
            [IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY],
        ),
    ],
)
def test_migrated_operation_defs_match_web_binding_and_native_idempotency(
    operation_def: OperationDef[Any, Any],
    expected_native_rpcs: list[tuple[RPCMethod, str | None]],
    expected_idempotency_policies: list[IdempotencyPolicy],
) -> None:
    """Each migrated OperationDef binds to its registered native RPCs and idempotency policies."""
    binding = WEB_OPERATION_REGISTRY[operation_def.key]
    assert binding.is_supported is True
    assert binding.definition == operation_def
    assert binding.row is not None

    policy_binding = WEB_CALL_POLICY_BINDINGS[operation_def.key]
    assert policy_binding.policy is operation_def.policy
    assert [(item.method, item.variant) for item in policy_binding.native_bindings] == (
        expected_native_rpcs
    )
    assert [item.expected_policy for item in policy_binding.native_bindings] == (
        expected_idempotency_policies
    )
    for (rpc_method, variant), expected_policy in zip(
        expected_native_rpcs, expected_idempotency_policies, strict=True
    ):
        entry = IDEMPOTENCY_REGISTRY.get_entry(rpc_method, operation_variant=variant)
        assert entry.policy is expected_policy


def test_active_policy_binding_audit_fails_closed_on_semantic_or_native_drift() -> None:
    definitions = {
        operation: binding.definition
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.is_supported and binding.definition is not None
    }
    assert audit_web_call_policy_bindings(definitions) == ()

    notebook_get = WEB_CALL_POLICY_BINDINGS[Operation.NOTEBOOK_GET]
    drifted = dict(WEB_CALL_POLICY_BINDINGS)
    drifted[Operation.NOTEBOOK_GET] = replace(
        notebook_get,
        policy=CallPolicy.READ,
        native_bindings=(
            replace(
                notebook_get.native_bindings[0],
                expected_policy=IdempotencyPolicy.PROBE_THEN_CREATE,
            ),
        ),
    )
    errors = audit_web_call_policy_bindings(definitions, bindings=drifted)
    assert any("semantic policy" in error for error in errors)
    assert any("idempotency is" in error for error in errors)


def test_direct_row_parity_derives_the_actual_natives_from_the_row_spec() -> None:
    """P10 R2.5 / invariant I7: the audit's left side comes from ``NativeCallSpec``.

    Nothing in production hand-lists what a row dispatches, so the reviewed
    intent is compared against a set the rows themselves produce.
    """
    assert audit_direct_row_native_parity() == ()

    natives, streams = derive_row_dispatch(WEB_BINDING_ROWS[Operation.NOTEBOOK_GET])
    assert natives == frozenset({(RPCMethod.GET_NOTEBOOK, None)})
    assert streams == frozenset()

    # A stream-only row (R2.2's chat.stream_answer) reaches the wire with no
    # RPCMethod at all; the streamed half of the ledger is what records it.
    natives, streams = derive_row_dispatch(WEB_BINDING_ROWS[Operation.CHAT_STREAM_ANSWER])
    assert natives == frozenset()
    assert streams == frozenset({"chat.ask"})


def test_direct_row_parity_fails_closed_on_reviewed_native_drift() -> None:
    drifted = dict(WEB_CALL_POLICY_BINDINGS)
    notebook_get = drifted[Operation.NOTEBOOK_GET]
    drifted[Operation.NOTEBOOK_GET] = replace(
        notebook_get,
        native_bindings=(
            replace(notebook_get.native_bindings[0], method=RPCMethod.LIST_NOTEBOOKS),
        ),
    )
    errors = audit_direct_row_native_parity(bindings=drifted)
    assert [error for error in errors if error.startswith("direct-row parity: notebook.get ")]

    # The streamed half fails closed on its own terms: dropping the declared
    # streamed verb is not excused by a row having no natives to compare.
    drifted = dict(WEB_CALL_POLICY_BINDINGS)
    stream_row = drifted[Operation.CHAT_STREAM_ANSWER]
    drifted[Operation.CHAT_STREAM_ANSWER] = replace(stream_row, streamed_bindings=())
    errors = audit_direct_row_native_parity(bindings=drifted)
    assert any("chat.stream_answer streams ['chat.ask']" in error for error in errors)

    drifted[Operation.CHAT_STREAM_ANSWER] = replace(
        stream_row, streamed_bindings=(StreamedPolicyBinding("chat.other", "renamed"),)
    )
    errors = audit_direct_row_native_parity(bindings=drifted)
    assert any("chat.stream_answer streams" in error for error in errors)


def test_the_parity_audit_reports_two_distinguishable_columns() -> None:
    """Direct-row parity and end-to-end operation authority are not the same check.

    ``chat.ask`` is green in the first sense (its two leaf rows dispatch exactly
    what they are reviewed to dispatch) and divergent in the second: the
    reviewed product operation reaches ``GET_NOTEBOOK``, which the facade issues
    through ``NOTEBOOK_GET`` and no leaf row of the workflow dispatches. P10
    R2.5 keeps that recorded rather than "fixing" it.
    """
    report = web_policy_parity_report()
    direct = report["direct_row_parity"]
    end_to_end = report["end_to_end_authority"]
    assert isinstance(direct, dict) and isinstance(end_to_end, dict)

    assert set(direct) == {operation.value for operation in WEB_BINDING_ROWS}
    assert set(end_to_end) == {operation.value for operation in SERVICE_OWNED_WORKFLOW_BINDINGS}
    assert set(direct) & set(end_to_end) == set()

    assert all(row["parity"] == "match" for row in direct.values())
    assert direct["chat.stream_answer"]["derived_streams"] == ["chat.ask"]

    chat_ask = end_to_end["chat.ask"]
    assert chat_ask["parity"] == "recorded_divergence"
    assert chat_ask["leaf_operations"] == ["chat.stream_answer", "chat.get_conversation"]
    assert "GET_NOTEBOOK:<default>" in chat_ask["reviewed_natives"]
    assert "GET_NOTEBOOK:<default>" not in chat_ask["leaf_derived_natives"]
    assert chat_ask["known_divergence"] is not None

    assert report["summary"] == {
        "direct_rows": 75,
        "direct_row_divergences": 0,
        "service_owned_workflows": 21,
        "end_to_end_divergences": 1,
    }


def test_call_policy_does_not_control_transport_retries() -> None:
    """Transport retry resolution is governed strictly by native idempotency, not CallPolicy.

    P4.1 invariant: CallPolicy is an operational categorization; it must never
    alter whether the underlying RPC executor retries or disables retries.
    """
    # A semantic READ on LIST_NOTEBOOKS keeps internal retries enabled (disable=False)
    read_disable = resolve_effective_disable_internal_retries(
        IDEMPOTENCY_REGISTRY,
        RPCMethod.LIST_NOTEBOOKS,
        caller_disable_internal_retries=False,
        operation_variant=None,
    )
    assert read_disable is False

    # A semantic MUTATION on CREATE_NOTEBOOK disables internal retries (disable=True)
    # because CREATE_NOTEBOOK is classified PROBE_THEN_CREATE in IDEMPOTENCY_REGISTRY.
    mutation_disable = resolve_effective_disable_internal_retries(
        IDEMPOTENCY_REGISTRY,
        RPCMethod.CREATE_NOTEBOOK,
        caller_disable_internal_retries=False,
        operation_variant=None,
    )
    assert mutation_disable is True

    # Explicit caller intent always dominates registry/policy.
    assert (
        resolve_effective_disable_internal_retries(
            IDEMPOTENCY_REGISTRY,
            RPCMethod.LIST_NOTEBOOKS,
            caller_disable_internal_retries=True,
            operation_variant=None,
        )
        is True
    )


# =============================================================================
# 2. Exact Known Divergence Reporting Without Controlling Retries
# =============================================================================


def test_exact_known_divergences_inventory_is_three_and_passes_audit() -> None:
    """Only the three still-reviewed policy/authority divergences remain.

    P9.4 added ``chat.ask``: its row dispatches only ``GET_LAST_CONVERSATION_ID``
    and the streamed query, while the ledger keeps the facade's ``GET_NOTEBOOK``
    recency read because the catalog's recency contract is keyed on it.
    """
    assert len(DIVERGENCE_KINDS) == 3

    described_divergences = {
        spec.operation: (spec.known_divergence, DIVERGENCE_KINDS.get(spec.operation))
        for spec in OPERATION_SPECS
        if spec.known_divergence is not None
    }
    assert len(described_divergences) == 3
    assert set(described_divergences) == set(DIVERGENCE_KINDS)

    # Exact operations with reviewed divergences
    policy_divergences = [
        (op, detail) for op, (detail, kind) in described_divergences.items() if kind == "policy"
    ]
    authority_divergences = [
        (op, detail) for op, (detail, kind) in described_divergences.items() if kind == "authority"
    ]

    assert len(policy_divergences) == 1
    assert policy_divergences[0][0] is Operation.SOURCE_REFRESH
    assert policy_divergences[0][1] is not None
    assert "AT_LEAST_ONCE_ACCEPTED" in policy_divergences[0][1]

    assert {op for op, _detail in authority_divergences} == {
        Operation.ARTIFACT_DOWNLOAD,
        Operation.CHAT_ASK,
    }
    assert all(detail for _op, detail in authority_divergences)


def test_app_generation_workflow_entry_is_not_exported() -> None:
    """The P4 workflow bridge must not expand the supported public surface."""
    assert "_run_generation_workflow" not in artifact_helpers.__all__


def test_known_divergences_do_not_alter_transport_retries() -> None:
    """Divergence annotations report known mismatches without overriding native retry logic.

    For example, source.refresh is a semantic MUTATION with a 'policy' divergence,
    but its backing RPC (REFRESH_SOURCE) has AT_LEAST_ONCE_ACCEPTED and therefore
    leaves retries enabled (disable=False).
    """
    refresh_disable = resolve_effective_disable_internal_retries(
        IDEMPOTENCY_REGISTRY,
        RPCMethod.REFRESH_SOURCE,
        caller_disable_internal_retries=False,
        operation_variant=None,
    )
    assert refresh_disable is False

    # artifact.generate_audio is backed by CREATE_ARTIFACT (PROBE_THEN_CREATE),
    # so retries remain disabled (disable=True).
    audio_disable = resolve_effective_disable_internal_retries(
        IDEMPOTENCY_REGISTRY,
        RPCMethod.CREATE_ARTIFACT,
        caller_disable_internal_retries=False,
        operation_variant=None,
    )
    assert audio_disable is True

    # artifact.revise_slide is backed by REVISE_SLIDE (NON_IDEMPOTENT_NO_RETRY),
    # so retries remain disabled (disable=True).
    revise_disable = resolve_effective_disable_internal_retries(
        IDEMPOTENCY_REGISTRY,
        RPCMethod.REVISE_SLIDE,
        caller_disable_internal_retries=False,
        operation_variant=None,
    )
    assert revise_disable is True


def test_unacknowledged_divergence_fails_catalog_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unclassified divergence must fail the audit, while reviewed ones pass."""
    mutated_kinds = dict(DIVERGENCE_KINDS)
    mutated_kinds.pop(Operation.SOURCE_REFRESH)
    monkeypatch.setattr("scripts.audit_operation_catalog.DIVERGENCE_KINDS", mutated_kinds)
    monkeypatch.setattr("scripts._operation_catalog_specs.DIVERGENCE_KINDS", mutated_kinds)

    described_divergences = {
        spec.operation for spec in OPERATION_SPECS if spec.known_divergence is not None
    }
    assert described_divergences != set(mutated_kinds)


# =============================================================================
# 3. RuntimeDeadline Identity, No Nested Reset, and Shared-Poll Exception
# =============================================================================


def test_runtime_deadline_methods_and_clamping_invariants() -> None:
    """RuntimeDeadline must calculate elapsed, remaining, expired, and clamp_sleep accurately."""
    clock_time = 100.0
    deadline = RuntimeDeadline.start(30.0, monotonic=lambda: clock_time)

    assert deadline.timeout == 30.0
    assert deadline.started_at == 100.0
    assert deadline.elapsed() == 0.0
    assert deadline.remaining() == 30.0
    assert not deadline.expired()
    assert not deadline.exceeded()
    assert deadline.clamp_sleep(10.0) == 10.0
    assert deadline.clamp_sleep(45.0) == 30.0

    # Advance time by 20s
    clock_time = 120.0
    assert deadline.elapsed() == 20.0
    assert deadline.remaining() == 10.0
    assert deadline.clamp_sleep(15.0) == 10.0
    assert not deadline.expired()

    # Advance time past deadline
    clock_time = 135.0
    assert deadline.elapsed() == 35.0
    assert deadline.remaining() == 0.0
    assert deadline.expired()
    assert deadline.exceeded()
    assert deadline.clamp_sleep(5.0) == 0.0
    assert deadline.timeout_message("notebook.get") == "notebook.get timed out after 30.0s"


@pytest.mark.asyncio
async def test_deadline_instance_identity_preserved_through_read_services() -> None:
    """Read services hand off the exact caller-provided RuntimeDeadline instance to the backend."""
    deadline = RuntimeDeadline(timeout=10.0, started_at=50.0, monotonic=lambda: 52.0)
    backend = RecordingBackend()

    notebook = NotebookRecord("nb-1", "Title")
    source = SourceRecord("src-1", "Source")
    backend.set_result(NOTEBOOK_LIST_DEF, NotebookListResult((notebook,)))
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(notebook))
    backend.set_result(SOURCE_LIST_DEF, SourceListResult((source,)))
    backend.set_result(SOURCE_GET_DEF, SourceGetResult(source))

    notebooks = NotebookReadService(backend)
    sources = SourceReadService(backend)

    await notebooks.list(deadline=deadline)
    await notebooks.get("nb-1", deadline=deadline)
    await sources.list("nb-1", deadline=deadline)
    await sources.get("nb-1", "src-1", deadline=deadline)

    assert len(backend.invocations) == 4
    for invocation in backend.invocations:
        assert invocation.deadline is deadline


@pytest.mark.asyncio
async def test_web_rpc_backend_passes_single_deadline_without_nested_resets() -> None:
    """WebRpcBackend threads the single caller deadline through composite operations without resets."""
    clock_time = 100.0
    deadline = RuntimeDeadline.start(20.0, monotonic=lambda: clock_time)

    rpc_call = AsyncMock(
        side_effect=[
            # 1. _notebook_title_update: RENAME_NOTEBOOK
            None,
            # 2. _notebook_title_update: GET_NOTEBOOK readback
            [["Renamed", [], "nb-1"]],
        ]
    )
    executor = MagicMock(rpc_call=rpc_call)
    backend = WebRpcBackend(executor)

    # Advance simulated time during execution
    clock_time = 105.0
    result = await NotebookMutationService(backend).update(
        "nb-1",
        title="Renamed",
        deadline=deadline,
    )

    assert result.title == "Renamed"
    assert len(rpc_call.await_args_list) == 2

    # Both RPC calls received the SAME deadline object and clamped read_timeout
    first_call_kw = rpc_call.await_args_list[0].kwargs
    second_call_kw = rpc_call.await_args_list[1].kwargs

    assert first_call_kw["_retry_deadline"] is deadline
    assert second_call_kw["_retry_deadline"] is deadline
    # Monotonic remaining timeout without resetting to 20.0
    assert first_call_kw["read_timeout"] == 15.0
    assert second_call_kw["read_timeout"] == 15.0


@pytest.mark.asyncio
async def test_web_rpc_backend_rejects_expired_deadline_immediately() -> None:
    """WebRpcBackend raises BackendDeadlineExceededError before invoking when deadline is expired."""
    clock_time = 100.0
    deadline = RuntimeDeadline.start(10.0, monotonic=lambda: clock_time)
    clock_time = 115.0  # Expired

    executor = MagicMock(rpc_call=AsyncMock())
    backend = WebRpcBackend(executor)

    with pytest.raises(BackendDeadlineExceededError) as exc_info:
        await backend.invoke(
            NOTEBOOK_GET_DEF,
            NotebookGetInput(notebook_id="nb-1"),
            deadline=deadline,
        )

    assert exc_info.value.operation is Operation.NOTEBOOK_GET
    assert exc_info.value.reason is BackendErrorReason.TIMEOUT
    assert exc_info.value.diagnostics is not None
    assert exc_info.value.diagnostics["timeout"] == 10.0
    assert exc_info.value.diagnostics["remaining"] == 0.0
    projected = project_backend_error(exc_info.value)
    assert type(projected) is RPCTimeoutError
    assert projected.timeout_seconds == 10.0
    executor.rpc_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_poll_follower_exception_is_preserved() -> None:
    """Shared-poll followers attach via asyncio.shield and do not alter or reset the leader's deadline.

    Sanctioned exception to Principle 6: The leader owns the background poll loop with its
    own deadline, while followers await the shared future. A follower timeout or cancellation
    does not cancel or reset the leader's poll task.
    """

    class DummyLoopGuard:
        def assert_bound_loop(self) -> None:
            pass

    service = ArtifactPollingService(
        loop_guard=DummyLoopGuard(),  # type: ignore[arg-type]
        op_scope=MagicMock(),
    )

    # Leader starts a poll
    poll_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    async def _dummy_leader_loop() -> None:
        try:
            await asyncio.sleep(60.0)
        except asyncio.CancelledError:
            pass

    poll_task = asyncio.create_task(_dummy_leader_loop())
    service.poll_registry.register(("nb-1", "task-1"), poll_future, poll_task)

    # Follower attaches to existing poll
    follower_task = asyncio.create_task(
        service.wait_for_completion("nb-1", "task-1", poll_status=AsyncMock())
    )
    await asyncio.sleep(0.01)

    # Leader task is still active and registered
    assert service.poll_registry.get(("nb-1", "task-1")) is not None
    assert not poll_task.done()

    # Cancel follower task only
    follower_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower_task

    # Shielded leader task was NOT cancelled by follower cancellation
    assert not poll_task.cancelled()
    assert not poll_task.done()

    # Clean up
    poll_task.cancel()
    await poll_task
    service.poll_registry.pop(("nb-1", "task-1"))


# =============================================================================
# 4. BackendError / Exception Population, outcome_unknown Marker, Mixin Lattice
# =============================================================================


@pytest.mark.parametrize(
    ("reason", "expected_type", "expected_fields"),
    [
        (
            BackendErrorReason.AUTH,
            AuthError,
            {"recoverable": True, "method_id": "auth_rpc", "rpc_code": 16},
        ),
        (
            BackendErrorReason.CLIENT,
            ClientError,
            {"status_code": 404, "method_id": "client_rpc", "rpc_code": 5},
        ),
        (
            BackendErrorReason.DECODING,
            DecodingError,
            {"method_id": "decode_rpc", "rpc_code": "PARSE_ERR"},
        ),
        (
            BackendErrorReason.NETWORK,
            NetworkError,
            {"method_id": "net_rpc"},
        ),
        (
            BackendErrorReason.RATE_LIMIT,
            RateLimitError,
            {"retry_after": 15, "method_id": "rate_rpc", "rpc_code": 8},
        ),
        (
            BackendErrorReason.RESPONSE_TOO_LARGE,
            RPCResponseTooLargeError,
            {"limit_bytes": 1000, "bytes_read": 1001, "method_id": "large_rpc"},
        ),
        (
            BackendErrorReason.RPC,
            RPCError,
            {"method_id": "generic_rpc", "rpc_code": 2},
        ),
        (
            BackendErrorReason.SERVER,
            ServerError,
            {"status_code": 503, "method_id": "srv_rpc", "rpc_code": 14},
        ),
        (
            BackendErrorReason.TIMEOUT,
            RPCTimeoutError,
            {"timeout_seconds": 12.5, "method_id": "timeout_rpc"},
        ),
        (
            BackendErrorReason.UNKNOWN_RPC_METHOD,
            UnknownRPCMethodError,
            {
                "method_id": "unknown_rpc",
                "path": (0, 2),
                "source": "unit_test",
                "data_at_failure": "preview",
                "rpc_code": 13,
            },
        ),
    ],
)
def test_project_backend_error_fully_populates_all_public_exception_attributes(
    reason: BackendErrorReason,
    expected_type: type[Exception],
    expected_fields: dict[str, Any],
) -> None:
    """project_backend_error preserves every documented attribute and permanent alias."""
    diagnostics: dict[str, Any] = {
        "method_id": expected_fields.get("method_id", "default_rpc"),
        "rpc_code": expected_fields.get("rpc_code", 1),
        "found_ids": ["src-1", "src-2"],
        "raw_response": "raw_preview_text",
        "recoverable": expected_fields.get("recoverable", False),
        "status_code": expected_fields.get("status_code", 500),
        "retry_after": expected_fields.get("retry_after", 5),
        "limit_bytes": expected_fields.get("limit_bytes", 500),
        "bytes_read": expected_fields.get("bytes_read", 501),
        "timeout_seconds": expected_fields.get("timeout_seconds", 30.0),
        "path": expected_fields.get("path", (0, 1)),
        "source": expected_fields.get("source", "test_source"),
        "data_at_failure": expected_fields.get("data_at_failure", "preview_data"),
    }

    backend_error = BackendError(
        message="Backend failed",
        operation=Operation.NOTEBOOK_GET,
        outcome_unknown=False,
        diagnostics=MappingProxyType(diagnostics),
        reason=reason,
    )

    projected = project_backend_error(backend_error)

    assert isinstance(projected, expected_type)
    assert type(projected) is expected_type
    assert str(projected).startswith("Backend failed")

    # Check reason-specific fields
    for field_name, expected_val in expected_fields.items():
        assert getattr(projected, field_name) == expected_val

    # Check permanent aliases and RPC common fields on RPCError subclasses
    if issubclass(expected_type, RPCError):
        assert projected.method_id == diagnostics["method_id"]
        assert projected.rpc_id == diagnostics["method_id"]  # Permanent alias
        if expected_type is not RPCResponseTooLargeError:
            assert projected.rpc_code == diagnostics["rpc_code"]
            assert projected.code == diagnostics["rpc_code"]  # Permanent alias
            assert projected.found_ids == ["src-1", "src-2"]
            assert projected.raw_response == "raw_preview_text"


def test_outcome_unknown_projects_unconfirmed_marker_and_stops_batch() -> None:
    """BackendError outcome_unknown=True must set unconfirmed=True and classify as non-retriable RPC.

    P4.3 invariant: _app reads getattr(exc, "unconfirmed", False) to ensure uncertain
    writes stop batch execution rather than replaying one uncertain write per item.
    """
    diagnostics = MappingProxyType({"method_id": RPCMethod.CREATE_NOTEBOOK.value, "rpc_code": 14})
    backend_error = BackendError(
        message="Ambiguous write",
        operation=Operation.NOTEBOOK_CREATE,
        outcome_unknown=True,
        diagnostics=diagnostics,
        reason=BackendErrorReason.SERVER,
    )

    projected = project_backend_error(backend_error)

    assert getattr(projected, "unconfirmed", False) is True
    assert isinstance(projected, ServerError)

    # Classification by _app.errors:
    # unconfirmed dominates the type and classifies as non-retriable RPC
    classified = classify(projected)
    assert classified.category is ErrorCategory.RPC
    assert classified.retriable is False


def test_exception_mixin_lattice_and_catch_order_invariants() -> None:
    """Assert the full mixin lattice for *NotFoundError and *TimeoutError hierarchies."""
    # 1. *NotFoundError lattice: each must inherit from NotFoundError, RPCError, and domain error
    not_found_classes: list[tuple[type[Exception], type[Exception]]] = [
        (NotebookNotFoundError, NotebookError),
        (SourceNotFoundError, SourceError),
        (ArtifactNotFoundError, ArtifactError),
        (NoteNotFoundError, NoteError),
        (MindMapNotFoundError, MindMapError),
        (LabelNotFoundError, LabelError),
        (CollectionNotFoundError, CollectionError),
    ]
    for not_found_cls, domain_cls in not_found_classes:
        assert issubclass(not_found_cls, NotFoundError)
        assert issubclass(not_found_cls, RPCError)
        assert issubclass(not_found_cls, domain_cls)
        assert issubclass(not_found_cls, NotebookLMError)

    # 2. *TimeoutError lattice: wait timeouts must inherit from WaitTimeoutError and TimeoutError
    assert issubclass(WaitTimeoutError, TimeoutError)
    assert issubclass(WaitTimeoutError, NotebookLMError)
    assert issubclass(SourceTimeoutError, WaitTimeoutError)
    assert issubclass(SourceTimeoutError, SourceError)
    assert issubclass(SourceTimeoutError, TimeoutError)

    assert issubclass(ArtifactTimeoutError, WaitTimeoutError)
    assert issubclass(ArtifactTimeoutError, ArtifactError)
    assert issubclass(ArtifactTimeoutError, TimeoutError)

    assert issubclass(ArtifactPendingTimeoutError, ArtifactTimeoutError)
    assert issubclass(ArtifactPendingTimeoutError, WaitTimeoutError)
    assert issubclass(ArtifactPendingTimeoutError, TimeoutError)

    assert issubclass(ArtifactInProgressTimeoutError, ArtifactTimeoutError)
    assert issubclass(ArtifactInProgressTimeoutError, WaitTimeoutError)
    assert issubclass(ArtifactInProgressTimeoutError, TimeoutError)

    assert issubclass(ResearchTimeoutError, WaitTimeoutError)
    assert issubclass(ResearchTimeoutError, ResearchError)
    assert issubclass(ResearchTimeoutError, TimeoutError)

    # RPCTimeoutError is a NetworkError (pre-RPC / transport timeout, not WaitTimeoutError)
    assert issubclass(RPCTimeoutError, NetworkError)
    assert issubclass(RPCTimeoutError, NotebookLMError)
    assert not issubclass(RPCTimeoutError, TimeoutError)

    # LockUnavailableError is a NotebookLMError + TimeoutError
    assert issubclass(LockUnavailableError, TimeoutError)
    assert issubclass(LockUnavailableError, NotebookLMError)

    # 3. Transport subtrees
    assert issubclass(AuthError, RPCError)
    assert issubclass(RateLimitError, RPCError)
    assert issubclass(ServerError, RPCError)
    assert issubclass(ClientError, RPCError)
    assert issubclass(DecodingError, RPCError)
    assert issubclass(UnknownRPCMethodError, DecodingError)
    assert issubclass(RPCResponseTooLargeError, RPCError)
    assert issubclass(NonIdempotentRetryError, NotebookLMError)
    assert not issubclass(NetworkError, RPCError)


def test_exception_catch_clauses_behave_consistently_with_lattice() -> None:
    """Assert runtime try/except behavior matches the documented catch ordering."""
    caught_umbrella: list[str] = []

    # NotFoundError catch clause intercepts all *NotFoundError instances
    for exc in [
        NotebookNotFoundError("nb-1"),
        SourceNotFoundError("src-1"),
        ArtifactNotFoundError("art-1"),
        NoteNotFoundError("note-1"),
        MindMapNotFoundError("map-1"),
        LabelNotFoundError("lbl-1"),
        CollectionNotFoundError("col-1"),
    ]:
        try:
            raise exc
        except NotFoundError:
            caught_umbrella.append(type(exc).__name__)

    assert len(caught_umbrella) == 7

    # TimeoutError catch clause intercepts all *WaitTimeoutError instances
    caught_timeouts: list[str] = []
    for exc in [
        SourceTimeoutError("src-1", 10.0),
        ArtifactTimeoutError("nb-1", "task-1", 10.0),
        ArtifactPendingTimeoutError("nb-1", "task-1", 10.0),
        ArtifactInProgressTimeoutError("nb-1", "task-1", 10.0),
        ResearchTimeoutError("nb-1", "task-1", 10.0),
        LockUnavailableError("lock timeout"),
    ]:
        try:
            raise exc
        except TimeoutError:
            caught_timeouts.append(type(exc).__name__)

    assert len(caught_timeouts) == 6

    # NetworkError catch clause intercepts RPCTimeoutError
    caught_network: list[str] = []
    for exc in [
        RPCTimeoutError("timeout"),
        NetworkError("network connection reset"),
    ]:
        try:
            raise exc
        except NetworkError:
            caught_network.append(type(exc).__name__)

    assert len(caught_network) == 2
