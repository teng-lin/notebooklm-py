"""P1 web semantic-backend dispatch and registry tests."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    BackendKind,
    UnsupportedOperationError,
)
from notebooklm._backend_compat import project_backend_error
from notebooklm._deadline import RuntimeDeadline
from notebooklm._notebook_payloads import (
    build_get_notebook_params,
)
from notebooklm._operations import CallPolicy, Operation, OperationDef
from notebooklm._records import (
    ARTIFACT_CATALOG_DEF,
    ARTIFACT_DELETE_DEF,
    ARTIFACT_DOWNLOAD_DEF,
    ARTIFACT_EXPORT_DEF,
    ARTIFACT_GENERATE_AUDIO_DEF,
    ARTIFACT_GENERATE_DATA_TABLE_DEF,
    ARTIFACT_GENERATE_FLASHCARDS_DEF,
    ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
    ARTIFACT_GENERATE_MIND_MAP_DEF,
    ARTIFACT_GENERATE_QUIZ_DEF,
    ARTIFACT_GENERATE_REPORT_DEF,
    ARTIFACT_GENERATE_SLIDE_DECK_DEF,
    ARTIFACT_GENERATE_VIDEO_DEF,
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    ARTIFACT_PATCH_TITLE_DEF,
    ARTIFACT_RETRY_DEF,
    ARTIFACT_REVISE_SLIDE_DEF,
    ARTIFACT_SUGGEST_REPORTS_DEF,
    ARTIFACT_WAIT_DEF,
    CHAT_ASK_DEF,
    CHAT_CONFIGURE_DEF,
    CHAT_DELETE_HISTORY_DEF,
    CHAT_GET_CONVERSATION_DEF,
    CHAT_GET_HISTORY_DEF,
    CHAT_SAVE_NOTE_DEF,
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
    SOURCE_ADD_DRIVE_DEF,
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
    ArtifactDeleteInput,
    ArtifactDownloadInput,
    ArtifactPollInput,
    ArtifactRetryInput,
    ArtifactReviseSlideInput,
    ArtifactSuggestReportsInput,
    AudioGenerateInput,
    InfographicGenerateInput,
    InteractiveGenerateInput,
    MindMapDeleteInput,
    MindMapGenerateInteractiveInput,
    MindMapGenerateNoteInput,
    MindMapGetInput,
    MindMapListInput,
    MindMapUpdateInput,
    NotebookDeleteInput,
    NotebookDeleteResult,
    NotebookGetInput,
    NotebookGuideInput,
    NotebookListInput,
    NotebookListResult,
    NotebookRemoveRecentInput,
    NotebookRemoveRecentResult,
    NoteCreateInput,
    NoteDeleteInput,
    NoteGetInput,
    NoteListInput,
    NoteUpdateInput,
    ReportGenerateInput,
    SlideDeckGenerateInput,
    SourceAddDriveInput,
    SourceAddFailureKind,
    SourceAddFailureRecord,
    SourceGetInput,
    SourceListInput,
    VideoGenerateInput,
)
from notebooklm._transport_errors import TransportRateLimited, TransportServerError
from notebooklm._web.backend import (
    ROW_COLLABORATOR_NAMES,
    WebRpcBackend,
    _build_binding_table,
    _row_collaborators_of,
)
from notebooklm._web.bindings import studio as studio_rows_module
from notebooklm._web.codec.artifact_payloads import (
    build_audio_artifact_params,
    build_cinematic_video_artifact_params,
    build_flashcards_artifact_params,
    build_infographic_artifact_params,
    build_interactive_mind_map_artifact_params,
    build_mind_map_params,
    build_quiz_artifact_params,
    build_report_artifact_params,
    build_slide_deck_artifact_params,
    build_video_artifact_params,
)
from notebooklm._web.errors import translate_web_error
from notebooklm._web.registry import (
    WEB_OPERATION_REGISTRY,
    WEB_SUPPORTED_OPERATIONS,
)
from notebooklm.exceptions import (
    ArtifactFeatureUnavailableError,
    AuthError,
    ChatError,
    ChatResponseParseError,
    ClientError,
    DecodingError,
    IdempotencyVariantError,
    NetworkError,
    NotebookLMError,
    RateLimitError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
    UnknownRPCMethodError,
)
from notebooklm.rpc import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    RPCMethod,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)


@dataclass(frozen=True)
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _backend(executor: _RecordingExecutor) -> WebRpcBackend:
    return WebRpcBackend(executor)  # type: ignore[arg-type]


def test_row_collaborator_names_are_exactly_what_the_rows_declare() -> None:
    """P10 invariant I4: the head offers no collaborator no row asks for.

    ``ROW_COLLABORATOR_NAMES`` is the closed set the backend head supplies to
    custom rows, audited at construction against each row's declaration. The
    audit is one-directional — it rejects a row declaring a name the head does
    not provide, but never noticed a name the head provided that *no* row
    declares. ``deadline_factory`` was exactly that, and was dropped in P10
    slice R0.1; this pin is the ratchet that keeps the two sides equal.

    The plan's target is ``{"source_uploader"}``: ``capture_public_failure``
    left with the last source-add hoist (R3.5) — ``SOURCE_ADD_URL_BATCH``'s
    per-item captures were its final consumer, and ``SOURCE_ADD_FILE``, which
    is permanent under D4, imports the same ``_web.failure_projection``
    function directly instead. The three ``chat_*`` names leave when
    ``chat.ask`` becomes service-owned (R2.2). Removing an entry here is
    expected; adding one is not.
    """
    expected = {
        "chat_reqid",
        "chat_timeout",
        "chat_transport_composed",
        "source_uploader",
    }
    provided = set(ROW_COLLABORATOR_NAMES)
    assert provided == expected

    table = _build_binding_table()
    declared = {
        name for operation in table for name in getattr(table.get(operation), "collaborators", ())
    }
    assert declared == ROW_COLLABORATOR_NAMES, (
        "the head's collaborator set drifted from what the rows declare: "
        f"unused={sorted(ROW_COLLABORATOR_NAMES - declared)}, "
        f"undeclared={sorted(declared - ROW_COLLABORATOR_NAMES)}"
    )


def test_the_per_invocation_collaborator_map_covers_exactly_the_closed_set() -> None:
    backend = _backend(_RecordingExecutor())
    assert set(_row_collaborators_of(backend)) == ROW_COLLABORATOR_NAMES


def test_registry_is_closed_and_exposes_only_reviewed_live_handlers() -> None:
    assert set(WEB_OPERATION_REGISTRY) == set(Operation)
    assert {
        Operation.NOTEBOOK_LIST,
        Operation.NOTEBOOK_GET,
        Operation.NOTEBOOK_ALLOCATE,
        Operation.NOTEBOOK_PATCH,
        Operation.NOTEBOOK_DELETE,
        Operation.NOTEBOOK_REMOVE_RECENT,
        Operation.NOTEBOOK_SUMMARIZE,
        Operation.NOTEBOOK_DESCRIBE,
        Operation.SOURCE_LIST,
        Operation.SOURCE_GET,
        Operation.NOTE_LIST,
        Operation.NOTE_GET,
        Operation.NOTE_CREATE,
        Operation.NOTE_UPDATE,
        Operation.NOTE_DELETE,
        Operation.ARTIFACT_LIST,
        Operation.ARTIFACT_GET,
        Operation.ARTIFACT_CATALOG,
        Operation.ARTIFACT_PATCH_TITLE,
        Operation.ARTIFACT_GENERATE_AUDIO,
        Operation.ARTIFACT_GENERATE_QUIZ,
        Operation.ARTIFACT_GENERATE_FLASHCARDS,
        Operation.ARTIFACT_GENERATE_REPORT,
        Operation.ARTIFACT_GENERATE_VIDEO,
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC,
        Operation.ARTIFACT_GENERATE_SLIDE_DECK,
        Operation.ARTIFACT_GENERATE_DATA_TABLE,
        Operation.ARTIFACT_GENERATE_MIND_MAP,
        Operation.ARTIFACT_EXPORT,
        Operation.MIND_MAP_LIST,
        Operation.MIND_MAP_GET,
        Operation.MIND_MAP_GENERATE_NOTE,
        Operation.MIND_MAP_GENERATE_INTERACTIVE,
        Operation.MIND_MAP_UPDATE,
        Operation.MIND_MAP_DELETE,
        Operation.LABEL_LIST,
        Operation.LABEL_GET,
        Operation.LABEL_GENERATE,
        Operation.LABEL_DELETE,
        Operation.LABEL_MUTATE,
        Operation.LABEL_ALLOCATE,
        Operation.COLLECTION_LIST,
        Operation.COLLECTION_GET,
        Operation.COLLECTION_DELETE,
        Operation.SHARING_GET,
        Operation.SHARING_PATCH_VIEW_LEVEL,
        Operation.LEGACY_SHARE_ARTIFACT,
        Operation.SHARING_MUTATE,
        Operation.RESEARCH_START,
        Operation.RESEARCH_POLL,
        Operation.RESEARCH_CANCEL,
        Operation.RESEARCH_IMPORT,
        Operation.NOTEBOOK_SUGGEST_PROMPTS,
        Operation.ARTIFACT_SUGGEST_REPORTS,
        Operation.SETTINGS_GET,
        Operation.SETTINGS_GET_LIMITS,
        Operation.SETTINGS_SET_LANGUAGE,
        Operation.ARTIFACT_REVISE_SLIDE,
        Operation.ARTIFACT_RETRY,
        Operation.ARTIFACT_DELETE,
        Operation.ARTIFACT_DOWNLOAD,
        Operation.ARTIFACT_WAIT,
        Operation.SOURCE_ADD_DRIVE,
        Operation.SOURCE_ADD_FILE,
        Operation.SOURCE_DELETE,
        Operation.SOURCE_PATCH_TITLE,
        Operation.SOURCE_REGISTER,
        Operation.SOURCE_REFRESH,
        Operation.SOURCE_CHECK_FRESHNESS,
        Operation.SOURCE_GET_GUIDE,
        Operation.SOURCE_GET_FULLTEXT,
        Operation.CHAT_ASK,
        Operation.CHAT_GET_CONVERSATION,
        Operation.CHAT_GET_HISTORY,
        Operation.CHAT_DELETE_HISTORY,
        Operation.CHAT_CONFIGURE,
        Operation.CHAT_SAVE_NOTE,
        Operation.SOURCE_WAIT,
    } == WEB_SUPPORTED_OPERATIONS
    assert {
        operation: binding.definition
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.is_supported
    } == {
        Operation.NOTEBOOK_LIST: NOTEBOOK_LIST_DEF,
        Operation.NOTEBOOK_GET: NOTEBOOK_GET_DEF,
        Operation.NOTEBOOK_ALLOCATE: NOTEBOOK_ALLOCATE_DEF,
        Operation.NOTEBOOK_PATCH: NOTEBOOK_PATCH_DEF,
        Operation.NOTEBOOK_DELETE: NOTEBOOK_DELETE_DEF,
        Operation.NOTEBOOK_REMOVE_RECENT: NOTEBOOK_REMOVE_RECENT_DEF,
        Operation.NOTEBOOK_SUMMARIZE: NOTEBOOK_SUMMARIZE_DEF,
        Operation.NOTEBOOK_DESCRIBE: NOTEBOOK_DESCRIBE_DEF,
        Operation.SOURCE_LIST: SOURCE_LIST_DEF,
        Operation.SOURCE_GET: SOURCE_GET_DEF,
        Operation.NOTE_LIST: NOTE_LIST_DEF,
        Operation.NOTE_GET: NOTE_GET_DEF,
        Operation.NOTE_CREATE: NOTE_CREATE_DEF,
        Operation.NOTE_UPDATE: NOTE_UPDATE_DEF,
        Operation.NOTE_DELETE: NOTE_DELETE_DEF,
        Operation.ARTIFACT_LIST: ARTIFACT_LIST_DEF,
        Operation.ARTIFACT_GET: ARTIFACT_GET_DEF,
        Operation.ARTIFACT_CATALOG: ARTIFACT_CATALOG_DEF,
        Operation.ARTIFACT_PATCH_TITLE: ARTIFACT_PATCH_TITLE_DEF,
        Operation.ARTIFACT_GENERATE_AUDIO: ARTIFACT_GENERATE_AUDIO_DEF,
        Operation.ARTIFACT_GENERATE_QUIZ: ARTIFACT_GENERATE_QUIZ_DEF,
        Operation.ARTIFACT_GENERATE_FLASHCARDS: ARTIFACT_GENERATE_FLASHCARDS_DEF,
        Operation.ARTIFACT_GENERATE_REPORT: ARTIFACT_GENERATE_REPORT_DEF,
        Operation.ARTIFACT_GENERATE_VIDEO: ARTIFACT_GENERATE_VIDEO_DEF,
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC: ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
        Operation.ARTIFACT_GENERATE_SLIDE_DECK: ARTIFACT_GENERATE_SLIDE_DECK_DEF,
        Operation.ARTIFACT_GENERATE_DATA_TABLE: ARTIFACT_GENERATE_DATA_TABLE_DEF,
        Operation.ARTIFACT_GENERATE_MIND_MAP: ARTIFACT_GENERATE_MIND_MAP_DEF,
        Operation.ARTIFACT_EXPORT: ARTIFACT_EXPORT_DEF,
        Operation.MIND_MAP_LIST: MIND_MAP_LIST_DEF,
        Operation.MIND_MAP_GET: MIND_MAP_GET_DEF,
        Operation.MIND_MAP_GENERATE_NOTE: MIND_MAP_GENERATE_NOTE_DEF,
        Operation.MIND_MAP_GENERATE_INTERACTIVE: MIND_MAP_GENERATE_INTERACTIVE_DEF,
        Operation.MIND_MAP_UPDATE: MIND_MAP_UPDATE_DEF,
        Operation.MIND_MAP_DELETE: MIND_MAP_DELETE_DEF,
        Operation.LABEL_LIST: LABEL_LIST_DEF,
        Operation.LABEL_GET: LABEL_GET_DEF,
        Operation.LABEL_GENERATE: LABEL_GENERATE_DEF,
        Operation.LABEL_DELETE: LABEL_DELETE_DEF,
        Operation.LABEL_MUTATE: LABEL_MUTATE_DEF,
        Operation.LABEL_ALLOCATE: LABEL_ALLOCATE_DEF,
        Operation.COLLECTION_LIST: COLLECTION_LIST_DEF,
        Operation.COLLECTION_GET: COLLECTION_GET_DEF,
        Operation.COLLECTION_DELETE: COLLECTION_DELETE_DEF,
        Operation.SHARING_GET: SHARING_GET_DEF,
        Operation.SHARING_PATCH_VIEW_LEVEL: SHARING_PATCH_VIEW_LEVEL_DEF,
        Operation.LEGACY_SHARE_ARTIFACT: LEGACY_SHARE_ARTIFACT_DEF,
        Operation.SHARING_MUTATE: SHARING_MUTATE_DEF,
        Operation.RESEARCH_START: RESEARCH_START_DEF,
        Operation.RESEARCH_POLL: RESEARCH_POLL_DEF,
        Operation.RESEARCH_CANCEL: RESEARCH_CANCEL_DEF,
        Operation.RESEARCH_IMPORT: RESEARCH_IMPORT_DEF,
        Operation.NOTEBOOK_SUGGEST_PROMPTS: NOTEBOOK_SUGGEST_PROMPTS_DEF,
        Operation.ARTIFACT_SUGGEST_REPORTS: ARTIFACT_SUGGEST_REPORTS_DEF,
        Operation.SETTINGS_GET: SETTINGS_GET_DEF,
        Operation.SETTINGS_GET_LIMITS: SETTINGS_GET_LIMITS_DEF,
        Operation.SETTINGS_SET_LANGUAGE: SETTINGS_SET_LANGUAGE_DEF,
        Operation.ARTIFACT_REVISE_SLIDE: ARTIFACT_REVISE_SLIDE_DEF,
        Operation.ARTIFACT_RETRY: ARTIFACT_RETRY_DEF,
        Operation.ARTIFACT_DELETE: ARTIFACT_DELETE_DEF,
        Operation.ARTIFACT_DOWNLOAD: ARTIFACT_DOWNLOAD_DEF,
        Operation.ARTIFACT_WAIT: ARTIFACT_WAIT_DEF,
        Operation.SOURCE_ADD_DRIVE: SOURCE_ADD_DRIVE_DEF,
        Operation.SOURCE_ADD_FILE: SOURCE_ADD_FILE_DEF,
        Operation.SOURCE_DELETE: SOURCE_DELETE_DEF,
        Operation.SOURCE_PATCH_TITLE: SOURCE_PATCH_TITLE_DEF,
        Operation.SOURCE_REGISTER: SOURCE_REGISTER_DEF,
        Operation.SOURCE_REFRESH: SOURCE_REFRESH_DEF,
        Operation.SOURCE_CHECK_FRESHNESS: SOURCE_CHECK_FRESHNESS_DEF,
        Operation.SOURCE_GET_GUIDE: SOURCE_GET_GUIDE_DEF,
        Operation.SOURCE_GET_FULLTEXT: SOURCE_GET_FULLTEXT_DEF,
        Operation.CHAT_ASK: CHAT_ASK_DEF,
        Operation.CHAT_GET_CONVERSATION: CHAT_GET_CONVERSATION_DEF,
        Operation.CHAT_GET_HISTORY: CHAT_GET_HISTORY_DEF,
        Operation.CHAT_DELETE_HISTORY: CHAT_DELETE_HISTORY_DEF,
        Operation.CHAT_CONFIGURE: CHAT_CONFIGURE_DEF,
        Operation.CHAT_SAVE_NOTE: CHAT_SAVE_NOTE_DEF,
        Operation.SOURCE_WAIT: SOURCE_WAIT_DEF,
    }
    assert Operation.RESEARCH_WAIT not in WEB_SUPPORTED_OPERATIONS
    assert Operation.RESEARCH_IMPORT_VERIFY not in WEB_SUPPORTED_OPERATIONS
    assert all(
        binding.unsupported_reason
        for binding in WEB_OPERATION_REGISTRY.values()
        if not binding.is_supported
    )


@pytest.mark.asyncio
async def test_artifact_management_handlers_preserve_exact_native_shapes() -> None:
    executor = _RecordingExecutor(None, [["retry-id", None, None, None, 1]])
    backend = _backend(executor)

    await backend.invoke(
        ARTIFACT_DELETE_DEF,
        ArtifactDeleteInput("nb", "artifact-id"),
        deadline=None,
    )
    retry = await backend.invoke(
        ARTIFACT_RETRY_DEF,
        ArtifactRetryInput("nb", "retry-id"),
        deadline=None,
    )

    assert executor.calls[0].method is RPCMethod.DELETE_ARTIFACT
    assert executor.calls[0].params == [[2], "artifact-id"]
    assert executor.calls[1].method is RPCMethod.RETRY_ARTIFACT
    assert retry.status.task_id == "retry-id"


@pytest.mark.asyncio
async def test_artifact_revision_wait_and_suggestions_use_typed_results() -> None:
    suggestion = ["Title", "Description", "Prompt", None, None, "Advanced"]
    executor = _RecordingExecutor(
        [["task-id", None, None, None, 1]],
        [["task-id", "Deck", 8, None, 3]],
        [[suggestion]],
    )
    backend = _backend(executor)

    revision = await backend.invoke(
        ARTIFACT_REVISE_SLIDE_DEF,
        ArtifactReviseSlideInput("nb", "deck", 2, "Improve"),
        deadline=None,
    )
    observed = await backend.invoke(
        ARTIFACT_WAIT_DEF,
        ArtifactPollInput("nb", "task-id"),
        deadline=None,
    )
    suggestions = await backend.invoke(
        ARTIFACT_SUGGEST_REPORTS_DEF,
        ArtifactSuggestReportsInput("nb"),
        deadline=None,
    )

    assert revision.status.task_id == "task-id"
    assert observed.status.task_id == "task-id"
    assert observed.status.status == "in_progress"
    assert [item.title for item in suggestions.suggestions] == ["Title"]


@pytest.mark.asyncio
async def test_artifact_download_actions_are_closed_and_transport_neutral() -> None:
    executor = _RecordingExecutor([], [[None] * 9 + [["<html>"]]])
    backend = _backend(executor)

    catalog = await backend.invoke(
        ARTIFACT_DOWNLOAD_DEF,
        ArtifactDownloadInput("nb", "catalog"),
        deadline=None,
    )
    content = await backend.invoke(
        ARTIFACT_DOWNLOAD_DEF,
        ArtifactDownloadInput("nb", "interactive_html", "artifact-id"),
        deadline=None,
    )

    assert catalog.representations == ()
    assert content.content == "<html>"
    with pytest.raises(BackendContractError, match="unrecognized"):
        await backend.invoke(
            ARTIFACT_DOWNLOAD_DEF,
            ArtifactDownloadInput("nb", "wire_passthrough"),
            deadline=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["interactive_html", "mind_map_tree"])
async def test_artifact_interactive_options_block_drift_fails_loud(action: str) -> None:
    executor = _RecordingExecutor([[None] * 9 + [{"moved": True}]])
    backend = _backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            ARTIFACT_DOWNLOAD_DEF,
            ArtifactDownloadInput("nb", action, "artifact-id"),
            deadline=None,
        )

    assert caught.value.reason is BackendErrorReason.UNKNOWN_RPC_METHOD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "value", "expected"),
    [
        (
            ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
            InfographicGenerateInput(
                "nb",
                ("src-a", "src-b"),
                "fr",
                "Show relationships",
                "portrait",
                "detailed",
                "scientific",
            ),
            build_infographic_artifact_params(
                "nb",
                ["src-a", "src-b"],
                language="fr",
                instructions="Show relationships",
                orientation=InfographicOrientation.PORTRAIT,
                detail_level=InfographicDetail.DETAILED,
                style=InfographicStyle.SCIENTIFIC,
            ),
        ),
        (
            ARTIFACT_GENERATE_SLIDE_DECK_DEF,
            SlideDeckGenerateInput(
                "nb",
                ("src-a", "src-b"),
                "fr",
                "Speaker notes",
                "presenter_slides",
                "short",
            ),
            build_slide_deck_artifact_params(
                "nb",
                ["src-a", "src-b"],
                language="fr",
                instructions="Speaker notes",
                slide_format=SlideDeckFormat.PRESENTER_SLIDES,
                slide_length=SlideDeckLength.SHORT,
            ),
        ),
    ],
)
async def test_visual_generation_reuses_payloads_and_one_absolute_deadline(
    definition: object,
    value: object,
    expected: list[Any],
) -> None:
    executor = _RecordingExecutor([["artifact-id", "Visual", 1, None, 1]])
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 12.0)

    result = await _backend(executor).invoke(definition, value, deadline=deadline)  # type: ignore[arg-type]

    assert (result.status.task_id, result.status.status) == ("artifact-id", "pending")
    assert [call.method for call in executor.calls] == [RPCMethod.CREATE_ARTIFACT]
    assert executor.calls[0].params == expected
    assert executor.calls[0].kwargs["read_timeout"] == 3.0
    assert executor.calls[0].kwargs["disable_internal_retries"] is False
    assert executor.calls[0].kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_visual_generation_omitted_sources_share_deadline_and_tolerant_extraction() -> None:
    executor = _RecordingExecutor(
        [["Notebook", [[["src-a"], "A"], [["src-b"], "B"]], "nb"]],
        [["artifact-id", "Visual", 1, None, 1]],
    )
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await _backend(executor).invoke(
        ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
        InfographicGenerateInput("nb", source_ids=None),
        deadline=deadline,
    )

    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.CREATE_ARTIFACT,
    ]
    assert executor.calls[0].params == build_get_notebook_params("nb")
    assert executor.calls[1].params[2][3] == [[["src-a"]], [["src-b"]]]
    assert all(call.kwargs["_retry_deadline"] is deadline for call in executor.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        InfographicGenerateInput("nb", (), orientation="future"),
        InfographicGenerateInput("nb", (), detail_level="future"),
        InfographicGenerateInput("nb", (), style="future"),
        SlideDeckGenerateInput("nb", (), slide_format="future"),
        SlideDeckGenerateInput("nb", (), slide_length="future"),
    ],
)
async def test_visual_generation_rejects_unreviewed_options_before_executor(
    value: InfographicGenerateInput | SlideDeckGenerateInput,
) -> None:
    executor = _RecordingExecutor([])
    definition = (
        ARTIFACT_GENERATE_INFOGRAPHIC_DEF
        if isinstance(value, InfographicGenerateInput)
        else ARTIFACT_GENERATE_SLIDE_DECK_DEF
    )

    with pytest.raises(BackendContractError, match="unrecognized visual"):
        await _backend(executor).invoke(definition, value, deadline=None)  # type: ignore[arg-type]

    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "value", "artifact_type"),
    [
        (
            ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
            InfographicGenerateInput("nb", ()),
            "infographic",
        ),
        (
            ARTIFACT_GENERATE_SLIDE_DECK_DEF,
            SlideDeckGenerateInput("nb", ()),
            "slide deck",
        ),
    ],
)
async def test_visual_generation_feature_unavailable_reconstructs_public_error(
    definition: object,
    value: object,
    artifact_type: str,
) -> None:
    executor = _RecordingExecutor(None)

    with pytest.raises(BackendError) as caught:
        await _backend(executor).invoke(definition, value, deadline=None)  # type: ignore[arg-type]

    assert caught.value.reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE
    projected = project_backend_error(caught.value)
    assert isinstance(projected, ArtifactFeatureUnavailableError)
    assert projected.artifact_type == artifact_type
    assert projected.method_id == RPCMethod.CREATE_ARTIFACT.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [operation for operation in Operation if operation not in WEB_SUPPORTED_OPERATIONS],
)
async def test_every_unsupported_operation_fails_before_executor(operation: Operation) -> None:
    executor = _RecordingExecutor([])
    backend = _backend(executor)
    definition = OperationDef(
        key=operation,
        policy=CallPolicy.READ,
        input_type=NotebookListInput,
        output_type=NotebookListResult,
    )

    with pytest.raises(UnsupportedOperationError) as caught:
        await backend.invoke(definition, NotebookListInput(), deadline=None)

    assert caught.value.operation is operation
    assert caught.value.backend_kind is BackendKind.WEB
    assert executor.calls == []


@pytest.mark.asyncio
async def test_video_generate_uses_exact_payload_and_one_absolute_deadline() -> None:
    executor = _RecordingExecutor([["video-id", "Video", 3, None, 1]])
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 12.0)
    value = VideoGenerateInput(
        "nb-video",
        ("src-a", "src-b"),
        "fr",
        "Focus on the contrast",
        "brief",
        "anime",
    )

    result = await _backend(executor).invoke(
        ARTIFACT_GENERATE_VIDEO_DEF,
        value,
        deadline=deadline,
    )

    assert (result.status.task_id, result.status.status) == ("video-id", "pending")
    assert executor.calls[0].params == build_video_artifact_params(
        "nb-video",
        ["src-a", "src-b"],
        language="fr",
        instructions="Focus on the contrast",
        video_format=VideoFormat.BRIEF,
        video_style=VideoStyle.ANIME,
        style_prompt=None,
    )
    assert executor.calls[0].kwargs["read_timeout"] == 3.0
    assert executor.calls[0].kwargs["_retry_deadline"] is deadline
    assert executor.calls[0].kwargs["operation_variant"] is None


@pytest.mark.asyncio
async def test_cinematic_video_uses_distinct_exact_payload() -> None:
    executor = _RecordingExecutor([["cinematic-id", "Video", 3, None, 1]])
    value = VideoGenerateInput(
        "nb-video",
        ("src-a",),
        "en",
        "Dramatic pacing",
        "cinematic",
        cinematic_route=True,
    )

    await _backend(executor).invoke(ARTIFACT_GENERATE_VIDEO_DEF, value, deadline=None)

    assert executor.calls[0].params == build_cinematic_video_artifact_params(
        "nb-video",
        ["src-a"],
        language="en",
        instructions="Dramatic pacing",
    )


@pytest.mark.asyncio
async def test_report_generate_resolves_sources_once_and_uses_exact_payload() -> None:
    executor = _RecordingExecutor(
        [["Notebook", [[["src-a"], "A"], [["src-b"], "B"]], "nb-report"]],
        [["report-id", "Report", 2, None, 1]],
    )
    value = ReportGenerateInput(
        "nb-report",
        "study_guide",
        source_ids=None,
        language="de",
        extra_instructions="Emphasize key terms",
    )

    result = await _backend(executor).invoke(
        ARTIFACT_GENERATE_REPORT_DEF,
        value,
        deadline=None,
    )

    assert result.status.task_id == "report-id"
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.CREATE_ARTIFACT,
    ]
    assert executor.calls[0].params == build_get_notebook_params("nb-report")
    assert executor.calls[1].params == build_report_artifact_params(
        "nb-report",
        ["src-a", "src-b"],
        report_format=ReportFormat.STUDY_GUIDE,
        language="de",
        custom_prompt=None,
        extra_instructions="Emphasize key terms",
    )


@pytest.mark.asyncio
async def test_document_generation_preserves_source_shape_drift_warning(caplog) -> None:
    executor = _RecordingExecutor(
        [["Notebook without a sources slot"]],
        [["video-id", "Video", 3, None, 1]],
    )

    await _backend(executor).invoke(
        ARTIFACT_GENERATE_VIDEO_DEF,
        VideoGenerateInput("nb-video", source_ids=None),
        deadline=None,
    )

    assert "get_source_ids: notebook_info has no sources slot for nb-video" in caplog.text
    assert executor.calls[1].params == build_video_artifact_params(
        "nb-video",
        [],
        language="en",
        instructions=None,
        video_format=None,
        video_style=None,
        style_prompt=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_def", "value", "artifact_type"),
    [
        (ARTIFACT_GENERATE_VIDEO_DEF, VideoGenerateInput("nb", ()), "video"),
        (ARTIFACT_GENERATE_REPORT_DEF, ReportGenerateInput("nb", source_ids=()), "report"),
    ],
)
async def test_document_generation_reconstructs_feature_unavailable_error(
    operation_def: Any,
    value: object,
    artifact_type: str,
) -> None:
    executor = _RecordingExecutor(None)

    with pytest.raises(BackendError) as caught:
        await _backend(executor).invoke(operation_def, value, deadline=None)

    assert caught.value.reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE
    projected = project_backend_error(caught.value)
    assert isinstance(projected, ArtifactFeatureUnavailableError)
    assert projected.artifact_type == artifact_type
    assert projected.method_id == RPCMethod.CREATE_ARTIFACT.value


@pytest.mark.asyncio
async def test_note_handlers_preserve_classification_exact_id_and_wire_shapes() -> None:
    rows = [
        [
            ["note-123", ["note-123", "Body", None, None, "Title"]],
            ["note-12", ["note-12", "Prefix", None, None, "Other"]],
            ["mind-map", '{"name":"Map","children":[]}'],
            ["deleted", None, 2],
        ]
    ]
    executor = _RecordingExecutor(
        rows,
        rows,
        [["created", "", [1, "user", [1_700_000_000, 0]], None, "Ignored"]],
        None,
        None,
    )
    backend = _backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    listed = await backend.invoke(NOTE_LIST_DEF, NoteListInput("nb"), deadline=deadline)
    selected = await backend.invoke(
        NOTE_GET_DEF,
        NoteGetInput("nb", "note-123"),
        deadline=deadline,
    )
    created = await backend.invoke(
        NOTE_CREATE_DEF,
        NoteCreateInput("nb", "Title", "Body"),
        deadline=deadline,
    )
    await backend.invoke(
        NOTE_UPDATE_DEF,
        NoteUpdateInput("nb", "note-123", "New body", "New title"),
        deadline=deadline,
    )
    await backend.invoke(
        NOTE_DELETE_DEF,
        NoteDeleteInput("nb", "note-123"),
        deadline=deadline,
    )

    assert [note.id for note in listed.notes] == ["note-123", "note-12"]
    assert selected.note is not None and selected.note.id == "note-123"
    assert (created.note.id, created.note.title, created.note.content) == (
        "created",
        "Title",
        "Body",
    )
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTES_AND_MIND_MAPS,
        RPCMethod.GET_NOTES_AND_MIND_MAPS,
        RPCMethod.CREATE_NOTE,
        RPCMethod.UPDATE_NOTE,
        RPCMethod.DELETE_NOTE,
    ]
    assert all(call.kwargs["_retry_deadline"] is deadline for call in executor.calls)
    assert executor.calls[2].params == ["nb", "", [1], None, "Title"]
    assert executor.calls[3].params == ["nb", "note-123", [[["New body", "New title", [], 0]]]]
    assert executor.calls[4].params == ["nb", None, ["note-123"]]


@pytest.mark.asyncio
async def test_mind_map_handlers_preserve_codecs_payloads_and_deadline() -> None:
    tree_json = '{"name":"Map","children":[]}'
    interactive_row = [None] * 10
    interactive_row[9] = [None, None, None, tree_json]
    executor = _RecordingExecutor(
        [[["map-note", ["map-note", tree_json, None, None, "Map"]]]],
        [interactive_row],
        [[tree_json]],
        [["map-interactive", "Map", 4]],
        None,
        None,
    )
    backend = _backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    listed = await backend.invoke(MIND_MAP_LIST_DEF, MindMapListInput("nb"), deadline=deadline)
    tree = await backend.invoke(
        MIND_MAP_GET_DEF,
        MindMapGetInput("nb", "map-interactive"),
        deadline=deadline,
    )
    generated_note = await backend.invoke(
        MIND_MAP_GENERATE_NOTE_DEF,
        MindMapGenerateNoteInput("nb", ("src",), "fr", "Focus"),
        deadline=deadline,
    )
    generated_interactive = await backend.invoke(
        MIND_MAP_GENERATE_INTERACTIVE_DEF,
        MindMapGenerateInteractiveInput("nb", ("src",), "Focus"),
        deadline=deadline,
    )
    await backend.invoke(
        MIND_MAP_UPDATE_DEF,
        MindMapUpdateInput("nb", "map-interactive", "Renamed"),
        deadline=deadline,
    )
    await backend.invoke(
        MIND_MAP_DELETE_DEF,
        MindMapDeleteInput("nb", "map-interactive"),
        deadline=deadline,
    )

    assert [(record.id, record.title, record.tree_json) for record in listed.mind_maps] == [
        ("map-note", "Map", tree_json)
    ]
    assert tree.tree_json == tree_json
    assert generated_note.tree_json == tree_json
    assert generated_interactive.mind_map_id == "map-interactive"
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTES_AND_MIND_MAPS,
        RPCMethod.GET_INTERACTIVE_HTML,
        RPCMethod.GENERATE_MIND_MAP,
        RPCMethod.CREATE_ARTIFACT,
        RPCMethod.RENAME_ARTIFACT,
        RPCMethod.DELETE_ARTIFACT,
    ]
    assert all(call.kwargs["_retry_deadline"] is deadline for call in executor.calls)
    assert executor.calls[2].params == build_mind_map_params(
        ["src"], language="fr", instructions="Focus"
    )
    assert executor.calls[3].params == build_interactive_mind_map_artifact_params(
        "nb", ["src"], instructions="Focus"
    )
    assert executor.calls[4].params == [["map-interactive", "Renamed"], [["title"]]]
    assert executor.calls[5].params == [[2], "map-interactive"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "value"),
    [
        (MIND_MAP_GENERATE_NOTE_DEF, MindMapGenerateNoteInput("nb", None)),
        (
            MIND_MAP_GENERATE_INTERACTIVE_DEF,
            MindMapGenerateInteractiveInput("nb", None),
        ),
    ],
)
async def test_mind_map_generation_resolves_default_sources_once(
    definition: OperationDef[Any, Any],
    value: object,
) -> None:
    generated = [["id"]] if definition is MIND_MAP_GENERATE_INTERACTIVE_DEF else [["{}"]]
    executor = _RecordingExecutor(
        [["Notebook", [[[["src-a"]]], [["src-b"]]], "nb"]],
        generated,
    )

    await _backend(executor).invoke(definition, value, deadline=None)

    assert [call.method for call in executor.calls[:1]] == [RPCMethod.GET_NOTEBOOK]
    assert len(executor.calls) == 2


@pytest.mark.asyncio
async def test_audio_generate_reuses_payload_builder_and_one_absolute_deadline() -> None:
    executor = _RecordingExecutor([["audio-id", "Audio", 1, None, 1]])
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 12.0)
    value = AudioGenerateInput(
        notebook_id="nb-audio",
        source_ids=("src-a", "src-b"),
        language="fr",
        instructions="Compare the sources",
        audio_format="debate",
        audio_length="long",
    )

    result = await _backend(executor).invoke(
        ARTIFACT_GENERATE_AUDIO_DEF,
        value,
        deadline=deadline,
    )

    assert (result.status.task_id, result.status.status) == ("audio-id", "pending")
    assert [call.method for call in executor.calls] == [RPCMethod.CREATE_ARTIFACT]
    assert executor.calls[0].params == build_audio_artifact_params(
        "nb-audio",
        ["src-a", "src-b"],
        language="fr",
        instructions="Compare the sources",
        audio_format=AudioFormat.DEBATE,
        audio_length=AudioLength.LONG,
    )
    assert executor.calls[0].kwargs["read_timeout"] == 3.0
    assert executor.calls[0].kwargs["disable_internal_retries"] is False
    assert executor.calls[0].kwargs["operation_variant"] is None


@pytest.mark.asyncio
async def test_audio_generate_none_language_uses_current_profile_default(monkeypatch) -> None:
    monkeypatch.setattr(studio_rows_module, "get_default_language", lambda: "ja")
    executor = _RecordingExecutor([["audio-id", "Audio", 1, None, 1]])

    await _backend(executor).invoke(
        ARTIFACT_GENERATE_AUDIO_DEF,
        AudioGenerateInput("nb", (), language=None),
        deadline=None,
    )

    assert executor.calls[0].params[2][6][1][4] == "ja"


@pytest.mark.asyncio
async def test_audio_generate_resolves_all_sources_once_inside_backend() -> None:
    executor = _RecordingExecutor(
        [["Notebook", [[["src-a"], "A"], [["src-b"], "B"]], "nb-audio"]],
        [["audio-id", "Audio", 1, None, 1]],
    )

    await _backend(executor).invoke(
        ARTIFACT_GENERATE_AUDIO_DEF,
        AudioGenerateInput("nb-audio", source_ids=None),
        deadline=None,
    )

    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.CREATE_ARTIFACT,
    ]
    assert executor.calls[0].params == build_get_notebook_params("nb-audio")
    assert executor.calls[1].params[2][3] == [[["src-a"]], [["src-b"]]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        AudioGenerateInput("nb", (), audio_format="future_format"),
        AudioGenerateInput("nb", (), audio_length="future_length"),
    ],
)
async def test_audio_generate_rejects_unreviewed_options_before_executor(
    value: AudioGenerateInput,
) -> None:
    executor = _RecordingExecutor([])

    with pytest.raises(BackendContractError, match="unrecognized audio"):
        await _backend(executor).invoke(ARTIFACT_GENERATE_AUDIO_DEF, value, deadline=None)

    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "artifact_type"),
    [
        (None, "audio"),
        ([[None, "Audio", 1, None, 1]], "artifact"),
    ],
)
async def test_audio_generate_feature_unavailable_reconstructs_public_error(
    response: object,
    artifact_type: str,
) -> None:
    executor = _RecordingExecutor(response)

    with pytest.raises(BackendError) as caught:
        await _backend(executor).invoke(
            ARTIFACT_GENERATE_AUDIO_DEF,
            AudioGenerateInput("nb", ()),
            deadline=None,
        )

    assert caught.value.reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE
    projected = project_backend_error(caught.value)
    assert isinstance(projected, ArtifactFeatureUnavailableError)
    assert projected.artifact_type == artifact_type
    assert projected.method_id == RPCMethod.CREATE_ARTIFACT.value


@pytest.mark.asyncio
async def test_noncanonical_definition_and_wrong_input_fail_before_executor() -> None:
    executor = _RecordingExecutor([])
    backend = _backend(executor)
    noncanonical = OperationDef(
        key=Operation.NOTEBOOK_LIST,
        policy=CallPolicy.MUTATION,
        input_type=NotebookListInput,
        output_type=NotebookListResult,
    )

    with pytest.raises(BackendContractError, match="non-canonical"):
        await backend.invoke(noncanonical, NotebookListInput(), deadline=None)
    with pytest.raises(BackendContractError, match="requires NotebookListInput"):
        await backend.invoke(NOTEBOOK_LIST_DEF, NotebookGetInput("nb"), deadline=None)

    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "builder"),
    [
        (ARTIFACT_GENERATE_QUIZ_DEF, build_quiz_artifact_params),
        (ARTIFACT_GENERATE_FLASHCARDS_DEF, build_flashcards_artifact_params),
    ],
    ids=["quiz", "flashcards"],
)
async def test_interactive_generation_preserves_exact_payload_and_deadline(
    definition: OperationDef[InteractiveGenerateInput, object],
    builder: Any,
) -> None:
    executor = _RecordingExecutor([["task", "Title", 4, None, 1]])
    backend = _backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 12.0)
    value = InteractiveGenerateInput(
        notebook_id="nb",
        source_ids=("src-a", "src-b"),
        instructions="Focus on details",
        quantity="fewer",
        difficulty="hard",
    )

    result = await backend.invoke(definition, value, deadline=deadline)

    assert (result.status.task_id, result.status.status) == ("task", "pending")
    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call.method is RPCMethod.CREATE_ARTIFACT
    assert call.params == builder(
        "nb",
        ["src-a", "src-b"],
        instructions="Focus on details",
        quantity=QuizQuantity.FEWER,
        difficulty=QuizDifficulty.HARD,
    )
    assert call.kwargs["read_timeout"] == 3.0
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_interactive_generation_resolves_omitted_sources_once_with_same_deadline() -> None:
    executor = _RecordingExecutor(
        [["Notebook", [[["src-a"], "A"], [["src-b"], "B"]], "nb"]],
        [["task", "Title", 4, None, 2]],
    )
    deadline = RuntimeDeadline(timeout=8.0, started_at=10.0, monotonic=lambda: 12.0)

    result = await _backend(executor).invoke(
        ARTIFACT_GENERATE_QUIZ_DEF,
        InteractiveGenerateInput("nb", None, None, None, None),
        deadline=deadline,
    )

    assert result.status.status == "in_progress"
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.CREATE_ARTIFACT,
    ]
    assert executor.calls[1].params == build_quiz_artifact_params(
        "nb",
        ["src-a", "src-b"],
        instructions=None,
        quantity=None,
        difficulty=None,
    )
    assert all(call.kwargs["_retry_deadline"] is deadline for call in executor.calls)


@pytest.mark.asyncio
async def test_interactive_source_resolution_preserves_schema_drift_warning(caplog) -> None:
    executor = _RecordingExecutor(
        [["Notebook"]],
        [["task", "Title", 4, None, 1]],
    )

    with caplog.at_level("WARNING", logger="notebooklm._notebooks"):
        await _backend(executor).invoke(
            ARTIFACT_GENERATE_FLASHCARDS_DEF,
            InteractiveGenerateInput("nb-short", None, None, None, None),
            deadline=None,
        )

    assert executor.calls[1].params == build_flashcards_artifact_params(
        "nb-short",
        [],
        instructions=None,
        quantity=None,
        difficulty=None,
    )
    assert any(
        "schema drift" in record.message and "nb-short" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        InteractiveGenerateInput("nb", (), None, "future", None),
        InteractiveGenerateInput("nb", (), None, None, "future"),
    ],
)
async def test_interactive_generation_rejects_unknown_neutral_options_before_rpc(
    value: InteractiveGenerateInput,
) -> None:
    executor = _RecordingExecutor()

    with pytest.raises(BackendContractError, match="unrecognized interactive"):
        await _backend(executor).invoke(ARTIFACT_GENERATE_QUIZ_DEF, value, deadline=None)

    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "response", "artifact_type"),
    [
        (ARTIFACT_GENERATE_QUIZ_DEF, None, "quiz"),
        (ARTIFACT_GENERATE_FLASHCARDS_DEF, None, "flashcards"),
        (ARTIFACT_GENERATE_QUIZ_DEF, [[None]], "artifact"),
    ],
    ids=["quiz-null-result", "flashcards-null-result", "null-task-id"],
)
async def test_interactive_generation_null_feature_reconstructs_exact_public_error(
    definition: OperationDef[InteractiveGenerateInput, object],
    response: object,
    artifact_type: str,
) -> None:
    executor = _RecordingExecutor(response)

    with pytest.raises(BackendError) as caught:
        await _backend(executor).invoke(
            definition,
            InteractiveGenerateInput("nb", (), None, None, None),
            deadline=None,
        )

    projected = project_backend_error(caught.value)
    assert type(projected) is ArtifactFeatureUnavailableError
    assert projected.artifact_type == artifact_type
    assert projected.method_id == RPCMethod.CREATE_ARTIFACT.value


@pytest.mark.asyncio
async def test_notebook_handlers_reuse_current_payloads_and_return_neutral_records() -> None:
    list_row = ["Listed", [], "nb-list", "📚"]
    get_row = [
        "Fetched",
        [[["source-a"], "Source A"]],
        "nb-get",
        "🧬",
        None,
        [2, False, True, None, None, [1700000000, 0], 1, False, [1690000000, 0]],
        None,
        [[2, "Tutor"], [4]],
        None,
        [True, False, True],
        None,
        [["chat-session"]],
    ]
    executor = _RecordingExecutor([[list_row]], [get_row])
    backend = _backend(executor)

    listed = await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)
    fetched = await backend.invoke(
        NOTEBOOK_GET_DEF,
        NotebookGetInput("nb-get"),
        deadline=None,
    )

    assert [(item.id, item.title, item.emoji) for item in listed.notebooks] == [
        ("nb-list", "Listed", "📚")
    ]
    assert listed.notebooks[0].chat_settings is None
    assert fetched.notebook is not None
    assert fetched.notebook.id == "nb-get"
    assert fetched.notebook.role == "editor"
    assert fetched.notebook.chat_settings is not None
    assert fetched.notebook.chat_settings.goal == "custom"
    assert fetched.notebook.chat_settings.response_length == "longer"
    assert fetched.notebook.chat_sessions[0].id == "chat-session"
    assert fetched.source_ids == ("source-a",)
    assert executor.calls[0].method is RPCMethod.LIST_NOTEBOOKS
    assert executor.calls[0].params == [None, 1, None, [2]]
    assert executor.calls[1].method is RPCMethod.GET_NOTEBOOK
    assert executor.calls[1].params[0] == "nb-get"
    assert executor.calls[1].kwargs["source_path"] == "/notebook/nb-get"


@pytest.mark.asyncio
async def test_notebook_get_empty_payload_is_typed_not_found_state() -> None:
    executor = _RecordingExecutor([[]])
    result = await _backend(executor).invoke(
        NOTEBOOK_GET_DEF,
        NotebookGetInput("missing"),
        deadline=None,
    )
    assert result.notebook is None


@pytest.mark.asyncio
async def test_notebook_delete_is_one_id_and_returns_empty_result() -> None:
    executor = _RecordingExecutor(None)

    result = await _backend(executor).invoke(
        NOTEBOOK_DELETE_DEF,
        NotebookDeleteInput("nb-1"),
        deadline=None,
    )

    assert result == NotebookDeleteResult()
    assert executor.calls[0].method is RPCMethod.DELETE_NOTEBOOK
    assert executor.calls[0].params == [["nb-1"], [2]]


@pytest.mark.asyncio
async def test_notebook_remove_recent_preserves_null_status_and_deadline() -> None:
    executor = _RecordingExecutor(None)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    result = await _backend(executor).invoke(
        NOTEBOOK_REMOVE_RECENT_DEF,
        NotebookRemoveRecentInput("nb-1"),
        deadline=deadline,
    )

    assert result == NotebookRemoveRecentResult()
    assert executor.calls[0].method is RPCMethod.REMOVE_RECENTLY_VIEWED
    assert executor.calls[0].params == ["nb-1"]
    assert executor.calls[0].kwargs["allow_null"] is True
    assert executor.calls[0].kwargs["read_timeout"] == 4.0
    assert executor.calls[0].kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("definition", [NOTEBOOK_SUMMARIZE_DEF, NOTEBOOK_DESCRIBE_DEF])
async def test_notebook_guide_variants_preserve_wire_shape_and_neutral_decode(
    definition: object,
) -> None:
    executor = _RecordingExecutor([[["Summary"], [[["Question?", "Ask this"]]]]])
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    result = await _backend(executor).invoke(  # type: ignore[arg-type]
        definition,
        NotebookGuideInput("nb-1"),
        deadline=deadline,
    )

    assert result.description.summary == "Summary"
    assert [(topic.question, topic.prompt) for topic in result.description.suggested_topics] == [
        ("Question?", "Ask this")
    ]
    assert executor.calls[0].method is RPCMethod.SUMMARIZE
    assert executor.calls[0].params == ["nb-1", [2]]
    assert executor.calls[0].kwargs["source_path"] == "/notebook/nb-1"
    assert executor.calls[0].kwargs["read_timeout"] == 4.0
    assert executor.calls[0].kwargs["_retry_deadline"] is deadline


def _source_entry(
    source_id: str,
    *,
    title: str | None = None,
    url: str = "https://example.com",
    status: int = 1,
    kind: int = 5,
) -> list[Any]:
    return [
        [source_id],
        title or f"Source {source_id}",
        [None, 11, [1704067200, 0], None, kind, None, None, [url]],
        [None, status],
    ]


def _source_result(
    source_id: str,
    *,
    title: str,
    url: str,
    kind: int = 5,
) -> list[Any]:
    return [[_source_entry(source_id, title=title, url=url, status=2, kind=kind)]]


@pytest.mark.asyncio
async def test_source_handlers_reuse_source_lister_and_apply_semantic_filters() -> None:
    source_rows = [_source_entry("src-web"), _source_entry("src-pdf", status=2, kind=3)]
    executor = _RecordingExecutor(
        [["Notebook", source_rows, "nb"]],
        [["Notebook", source_rows, "nb"]],
    )
    backend = _backend(executor)

    listed = await backend.invoke(
        SOURCE_LIST_DEF,
        SourceListInput(
            notebook_id="nb",
            statuses=frozenset({"processing"}),
            kinds=frozenset({"web_page"}),
        ),
        deadline=None,
    )
    fetched = await backend.invoke(
        SOURCE_GET_DEF,
        SourceGetInput(notebook_id="nb", source_id="src-pdf"),
        deadline=None,
    )

    assert [(item.id, item.status, item.kind) for item in listed.sources] == [
        ("src-web", "processing", "web_page")
    ]
    assert fetched.source is not None
    assert fetched.source.id == "src-pdf"
    assert fetched.source.kind == "pdf"
    assert all(call.method is RPCMethod.GET_NOTEBOOK for call in executor.calls)
    assert all(call.params[0] == "nb" for call in executor.calls)


@pytest.mark.asyncio
async def test_absolute_deadline_is_forwarded_unchanged_with_remaining_read_timeout() -> None:
    executor = _RecordingExecutor([[]])
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 12.0)

    await _backend(executor).invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=deadline)

    assert executor.calls[0].kwargs["read_timeout"] == 3.0
    assert executor.calls[0].kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_expired_deadline_fails_before_executor() -> None:
    executor = _RecordingExecutor([])
    deadline = RuntimeDeadline(timeout=2.0, started_at=10.0, monotonic=lambda: 12.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _backend(executor).invoke(
            NOTEBOOK_LIST_DEF,
            NotebookListInput(),
            deadline=deadline,
        )

    assert caught.value.operation is Operation.NOTEBOOK_LIST
    assert caught.value.reason is BackendErrorReason.TIMEOUT
    # P9.2: a codec row lets the transport raise the pre-dispatch expiry so the
    # error names the blocked native, as the composite handlers' phases did.
    assert caught.value.diagnostics == {
        "timeout": 2.0,
        "remaining": 0.0,
        "timeout_seconds": 2.0,
        "method_id": RPCMethod.LIST_NOTEBOOKS.value,
    }
    assert caught.value.dispatched is False
    assert executor.calls == []


@pytest.mark.asyncio
async def test_expired_custom_row_fails_before_the_handler_and_names_no_native() -> None:
    """A multi-native row resolves no native pre-dispatch, so it reports no ``method_id``."""
    executor = _RecordingExecutor()
    deadline = RuntimeDeadline(timeout=2.0, started_at=10.0, monotonic=lambda: 12.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _backend(executor).invoke(
            SOURCE_ADD_DRIVE_DEF,
            SourceAddDriveInput("nb", "file-id", "Doc", "application/pdf"),
            deadline=deadline,
        )

    assert caught.value.operation is Operation.SOURCE_ADD_DRIVE
    assert caught.value.reason is BackendErrorReason.TIMEOUT
    assert caught.value.diagnostics == {
        "timeout": 2.0,
        "remaining": 0.0,
        "timeout_seconds": 2.0,
    }
    assert caught.value.dispatched is False
    assert caught.value.outcome_unknown is False
    assert executor.calls == []


@pytest.mark.asyncio
async def test_mutation_expiring_before_dispatch_is_not_marked_unconfirmed() -> None:
    executor = _RecordingExecutor(None)
    times = iter((11.0, 12.0))
    deadline = RuntimeDeadline(timeout=1.5, started_at=10.0, monotonic=lambda: next(times))

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _backend(executor).invoke(
            NOTEBOOK_DELETE_DEF,
            NotebookDeleteInput("nb-1"),
            deadline=deadline,
        )

    assert executor.calls == []
    assert caught.value.outcome_unknown is False
    projected = project_backend_error(caught.value)
    assert isinstance(projected, RPCTimeoutError)
    assert getattr(projected, "unconfirmed", False) is False


@pytest.mark.asyncio
async def test_rpc_error_is_translated_with_scrubbed_diagnostics() -> None:
    error = RPCError(
        "decode failed",
        method_id=RPCMethod.LIST_NOTEBOOKS.value,
        rpc_code=13,
        found_ids=["other"],
        raw_response="already-scrubbed",
    )
    executor = _RecordingExecutor(error)

    with pytest.raises(BackendError) as caught:
        await _backend(executor).invoke(
            NOTEBOOK_LIST_DEF,
            NotebookListInput(),
            deadline=None,
        )

    assert caught.value.message == "decode failed"
    assert caught.value.operation is Operation.NOTEBOOK_LIST
    assert caught.value.outcome_unknown is False
    assert caught.value.reason is BackendErrorReason.RPC
    assert caught.value.diagnostics is not None
    assert {
        name: caught.value.diagnostics[name]
        for name in ("method_id", "rpc_code", "found_ids", "raw_response")
    } == {
        "method_id": RPCMethod.LIST_NOTEBOOKS.value,
        "rpc_code": 13,
        "found_ids": ["other"],
        "raw_response": "already-scrubbed",
    }
    assert caught.value.diagnostics["public_error_failure"].kind is SourceAddFailureKind.RPC
    assert caught.value.diagnostics["found_ids"] is error.found_ids
    assert isinstance(caught.value.diagnostics["found_ids"], list)


@pytest.mark.parametrize(
    ("error", "reason", "specific_diagnostics"),
    [
        (AuthError("auth"), BackendErrorReason.AUTH, {"recoverable": False}),
        (ChatError("chat"), BackendErrorReason.CHAT, {}),
        (ChatResponseParseError("parse"), BackendErrorReason.CHAT_RESPONSE_PARSE, {}),
        (
            ClientError("client", status_code=404, rpc_code=5),
            BackendErrorReason.CLIENT,
            {"status_code": 404},
        ),
        (DecodingError("decode"), BackendErrorReason.DECODING, {}),
        (NetworkError("network"), BackendErrorReason.NETWORK, {}),
        (
            RateLimitError("rate", retry_after=7),
            BackendErrorReason.RATE_LIMIT,
            {"retry_after": 7},
        ),
        (
            RPCResponseTooLargeError("large", limit_bytes=10, bytes_read=11),
            BackendErrorReason.RESPONSE_TOO_LARGE,
            {"limit_bytes": 10, "bytes_read": 11},
        ),
        (RPCError("rpc"), BackendErrorReason.RPC, {}),
        (
            ServerError("server", status_code=503),
            BackendErrorReason.SERVER,
            {"status_code": 503},
        ),
        (
            RPCTimeoutError("timeout", timeout_seconds=3.0),
            BackendErrorReason.TIMEOUT,
            {"timeout_seconds": 3.0},
        ),
        (
            UnknownRPCMethodError(
                "unknown",
                path=(0, 2),
                source="test",
                data_at_failure="scrubbed",
            ),
            BackendErrorReason.UNKNOWN_RPC_METHOD,
            {"path": (0, 2), "source": "test", "data_at_failure": "scrubbed"},
        ),
    ],
)
def test_web_error_reasons_are_closed_and_preserve_reconstruction_evidence(
    error: RPCError | NetworkError | ChatError,
    reason: BackendErrorReason,
    specific_diagnostics: dict[str, object],
) -> None:
    translated = translate_web_error(Operation.NOTEBOOK_LIST, error)

    assert translated.reason is reason
    assert translated.message == str(error.args[0])
    assert (
        type(error)
        is {
            BackendErrorReason.AUTH: AuthError,
            BackendErrorReason.CHAT: ChatError,
            BackendErrorReason.CHAT_RESPONSE_PARSE: ChatResponseParseError,
            BackendErrorReason.CLIENT: ClientError,
            BackendErrorReason.DECODING: DecodingError,
            BackendErrorReason.NETWORK: NetworkError,
            BackendErrorReason.RATE_LIMIT: RateLimitError,
            BackendErrorReason.RESPONSE_TOO_LARGE: RPCResponseTooLargeError,
            BackendErrorReason.RPC: RPCError,
            BackendErrorReason.SERVER: ServerError,
            BackendErrorReason.TIMEOUT: RPCTimeoutError,
            BackendErrorReason.UNKNOWN_RPC_METHOD: UnknownRPCMethodError,
        }[translated.reason]
    )
    assert set(BackendErrorReason) == {
        BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE,
        BackendErrorReason.AUTH,
        BackendErrorReason.ARTIFACT_NOT_FOUND,
        BackendErrorReason.CHAT,
        BackendErrorReason.CHAT_RESPONSE_PARSE,
        BackendErrorReason.CLIENT,
        BackendErrorReason.DECODING,
        BackendErrorReason.IDEMPOTENCY_VARIANT,
        BackendErrorReason.LABEL_AMBIGUOUS_CREATE,
        BackendErrorReason.LABEL_NOT_FOUND,
        BackendErrorReason.NETWORK,
        BackendErrorReason.NOTEBOOK_LIMIT,
        BackendErrorReason.NOTEBOOK_NOT_FOUND,
        BackendErrorReason.NOT_FOUND,
        BackendErrorReason.SOURCE_NOT_FOUND,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.RESEARCH_START_UNAVAILABLE,
        BackendErrorReason.RESPONSE_TOO_LARGE,
        BackendErrorReason.RPC,
        BackendErrorReason.SERVER,
        BackendErrorReason.SOURCE_ADD,
        BackendErrorReason.TIMEOUT,
        BackendErrorReason.UNKNOWN_RPC_METHOD,
    }
    assert translated.diagnostics is not None
    assert {
        name: translated.diagnostics[name] for name in specific_diagnostics
    } == specific_diagnostics
    if isinstance(error, (RPCError, NetworkError, ChatError)):
        assert isinstance(translated.diagnostics["public_error_failure"], SourceAddFailureRecord)
    else:
        assert "public_error_failure" not in translated.diagnostics


def test_translated_server_error_preserves_http_status_cause() -> None:
    request = httpx.Request(
        "POST", "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
    )
    response = httpx.Response(503, request=request)
    cause = httpx.HTTPStatusError("service unavailable", request=request, response=response)
    error = ServerError(
        "server",
        status_code=503,
        method_id=RPCMethod.LIST_NOTEBOOKS.value,
    )
    error.__cause__ = cause
    error.__context__ = cause
    error.__suppress_context__ = True

    translated = translate_web_error(Operation.NOTEBOOK_LIST, error)
    projected = project_backend_error(translated)

    assert isinstance(projected, ServerError)
    assert isinstance(projected.__cause__, httpx.HTTPStatusError)
    assert projected.__context__ is projected.__cause__
    assert projected.__cause__.response.status_code == 503
    assert projected.__cause__.request.method == "POST"
    assert str(projected.__cause__.request.url) == str(request.url)
    assert projected.__suppress_context__ is True


@pytest.mark.parametrize("public_type", [RateLimitError, ServerError])
def test_translated_transport_error_drops_suppressed_private_context(
    public_type: type[RateLimitError] | type[ServerError],
) -> None:
    request = httpx.Request(
        "POST", "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
    )
    response = httpx.Response(503, request=request)
    cause = httpx.HTTPStatusError("service unavailable", request=request, response=response)
    if public_type is RateLimitError:
        private = TransportRateLimited(
            "rate limited",
            retry_after=7,
            response=response,
            original=cause,
        )
        error: RateLimitError | ServerError = RateLimitError(
            "rate limited",
            method_id=RPCMethod.LIST_NOTEBOOKS.value,
            retry_after=7,
        )
    else:
        private = TransportServerError(
            "server error",
            original=cause,
            response=response,
            status_code=503,
        )
        error = ServerError(
            "server error",
            status_code=503,
            method_id=RPCMethod.LIST_NOTEBOOKS.value,
        )
    error.__cause__ = cause
    error.__context__ = private
    error.__suppress_context__ = True

    translated = translate_web_error(Operation.NOTEBOOK_LIST, error)
    projected = project_backend_error(translated)

    assert type(projected) is public_type
    assert isinstance(projected.__cause__, httpx.HTTPStatusError)
    assert projected.__cause__.response.status_code == 503
    assert projected.__context__ is None
    assert projected.__suppress_context__ is True


def test_translated_network_error_omits_private_transport_wrapper_context() -> None:
    request = httpx.Request("POST", "https://notebook.google.com/_/rpc")
    leaf = httpx.ConnectError("connection failed", request=request)
    transport = TransportServerError("retry budget exhausted", original=leaf)
    error = NetworkError(
        "Connection failed calling LIST_NOTEBOOKS",
        method_id=RPCMethod.LIST_NOTEBOOKS.value,
        original_error=leaf,
    )
    error.__cause__ = leaf
    error.__context__ = transport
    error.__suppress_context__ = True

    translated = translate_web_error(Operation.NOTEBOOK_LIST, error)
    projected = project_backend_error(translated)

    assert isinstance(projected, NetworkError)
    assert isinstance(projected.original_error, httpx.ConnectError)
    assert projected.__cause__ is projected.original_error
    assert projected.__context__ is None
    assert projected.__suppress_context__ is True


def test_translated_error_rejects_unreviewed_context_without_public_cause() -> None:
    class _PrivateExecutionError(Exception):
        pass

    error = ServerError("server error", status_code=503)
    error.__context__ = _PrivateExecutionError("private")
    error.__suppress_context__ = True

    with pytest.raises(BackendContractError, match="unsupported public failure type"):
        translate_web_error(Operation.NOTEBOOK_LIST, error)


@pytest.mark.parametrize(
    "cause",
    [
        IndexError("row index"),
        KeyError("field"),
        TypeError("not indexable"),
    ],
)
def test_translated_decode_drift_preserves_reviewed_builtin_cause(cause: Exception) -> None:
    error = UnknownRPCMethodError(
        "shape drift",
        method_id=RPCMethod.GET_NOTEBOOK.value,
        path=(0, 2),
        source="test",
    )
    error.__cause__ = cause
    error.__context__ = cause
    error.__suppress_context__ = True

    translated = translate_web_error(Operation.NOTEBOOK_GET, error)
    projected = project_backend_error(translated)

    assert isinstance(projected, UnknownRPCMethodError)
    assert type(projected.__cause__) is type(cause)
    assert projected.__cause__.args == cause.args
    assert projected.__context__ is projected.__cause__
    assert projected.__suppress_context__ is True


@pytest.mark.asyncio
async def test_reviewed_idempotency_variant_error_round_trips_as_typed_caller_error() -> None:
    executor = _RecordingExecutor(IdempotencyVariantError("unknown variant"))

    with pytest.raises(BackendError) as caught:
        await _backend(executor).invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)

    assert caught.value.reason is BackendErrorReason.IDEMPOTENCY_VARIANT
    projected = project_backend_error(caught.value)
    assert type(projected) is IdempotencyVariantError
    assert str(projected) == "unknown variant"


@pytest.mark.asyncio
async def test_unreviewed_non_rpc_library_error_remains_a_closed_contract_failure() -> None:
    executor = _RecordingExecutor(NotebookLMError("unreviewed semantic error"))

    with pytest.raises(BackendContractError, match="unclassified web error type"):
        await _backend(executor).invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)


def test_unreviewed_rpc_error_subclass_fails_closed() -> None:
    class _UnreviewedRPCError(RPCError):
        pass

    with pytest.raises(BackendContractError, match="unclassified web error type"):
        translate_web_error(Operation.NOTEBOOK_LIST, _UnreviewedRPCError("new"))


@pytest.mark.asyncio
async def test_nonexpired_transport_timeout_remains_a_typed_backend_timeout() -> None:
    timeout = RPCTimeoutError(
        "request timed out",
        method_id=RPCMethod.LIST_NOTEBOOKS.value,
        timeout_seconds=3.0,
    )
    executor = _RecordingExecutor(timeout)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 12.0)

    with pytest.raises(BackendError) as caught:
        await _backend(executor).invoke(
            NOTEBOOK_LIST_DEF,
            NotebookListInput(),
            deadline=deadline,
        )

    assert caught.value.operation is Operation.NOTEBOOK_LIST
    assert type(caught.value) is BackendError
    assert caught.value.reason is BackendErrorReason.TIMEOUT
    assert caught.value.__cause__ is timeout
    assert caught.value.diagnostics is not None
    assert {
        name: caught.value.diagnostics[name]
        for name in ("method_id", "rpc_code", "found_ids", "raw_response", "timeout_seconds")
    } == {
        "method_id": RPCMethod.LIST_NOTEBOOKS.value,
        "rpc_code": None,
        "found_ids": None,
        "raw_response": None,
        "timeout_seconds": 3.0,
    }
    assert caught.value.diagnostics["public_error_failure"].kind is (
        SourceAddFailureKind.RPC_TIMEOUT
    )


@pytest.mark.asyncio
async def test_expired_midflight_transport_timeout_maps_to_semantic_deadline_error() -> None:
    leaf = httpx.ReadTimeout(
        "socket stalled",
        request=httpx.Request(
            "POST", "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
        ),
    )
    timeout = RPCTimeoutError(
        "request timed out",
        method_id=RPCMethod.LIST_NOTEBOOKS.value,
        timeout_seconds=3.0,
        original_error=leaf,
    )
    timeout.__cause__ = leaf
    timeout.__context__ = leaf
    timeout.__suppress_context__ = True
    executor = _RecordingExecutor(timeout)
    times = iter((12.0, 12.0, 16.0, 16.0))
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: next(times))

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _backend(executor).invoke(
            NOTEBOOK_LIST_DEF,
            NotebookListInput(),
            deadline=deadline,
        )

    assert caught.value.reason is BackendErrorReason.TIMEOUT
    assert caught.value.outcome_unknown is False
    assert caught.value.__cause__ is timeout
    assert caught.value.diagnostics is not None
    failure = caught.value.diagnostics["public_error_failure"]
    assert isinstance(failure, SourceAddFailureRecord)
    assert failure.kind is SourceAddFailureKind.RPC_TIMEOUT
    assert {
        key: value
        for key, value in caught.value.diagnostics.items()
        if key != "public_error_failure"
    } == {
        "method_id": RPCMethod.LIST_NOTEBOOKS.value,
        "rpc_code": None,
        "found_ids": None,
        "raw_response": None,
        "timeout_seconds": 3.0,
        "timeout": 5.0,
        "remaining": 0.0,
    }
    projected = project_backend_error(caught.value)
    assert isinstance(projected, RPCTimeoutError)
    assert isinstance(projected.original_error, httpx.ReadTimeout)
    assert projected.original_error.args == ("socket stalled",)
    assert projected.__cause__ is projected.original_error
    assert projected.__context__ is projected.original_error
    assert projected.__suppress_context__ is True


@pytest.mark.asyncio
async def test_close_does_not_close_client_owned_executor() -> None:
    executor = _RecordingExecutor([])
    backend = _backend(executor)

    await backend.close()

    assert not hasattr(executor, "close")
    with pytest.raises(BackendContractError, match="closed"):
        await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)
    assert executor.calls == []


def test_only_migrated_feature_runtime_reads_private_backend() -> None:
    """Only composition plus the migrated semantic slices may use the port."""
    package = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
    allowed = {
        package / "_client_composition.py",
        package / "_artifacts.py",
        package / "client.py",  # annotation-only declaration
        package / "_label_service.py",
        package / "_notebooks.py",
        package / "_notebook_guide_service.py",
        package / "_notebook_mutation_service.py",
        package / "_note_service.py",
        package / "_read_services.py",
        package / "_sharing.py",
        package / "_sharing_manager.py",
        package / "_sharing_service.py",
        package / "_research.py",
        package / "_research_service.py",
        package / "_settings.py",
        package / "_settings_service.py",
        package / "_sources.py",
        package / "_suggestion_service.py",
        package / "_source_service.py",
        package / "_chat" / "service.py",
    }
    allowed.update((package / "_studio").rglob("*.py"))
    allowed.update((package / "_web").rglob("*.py"))
    violations: list[str] = []
    for path in package.rglob("*.py"):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_backend":
                violations.append(f"{path.relative_to(package)}:{node.lineno}")
    assert violations == []
