"""Project neutral semantic records onto the existing public value models."""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.parse import quote

from .._env import get_base_url
from .._types.research import SourceGuide
from ..types import (
    AccountLimits,
    Artifact,
    ArtifactInfographic,
    ArtifactMedia,
    ArtifactMediaType,
    ArtifactSlide,
    AskResult,
    AudioArtifactUserState,
    ChatGoal,
    ChatReference,
    ChatResponseLength,
    ChatSession,
    ChatSettings,
    Collection,
    ConversationTurnKey,
    DiscoveryMode,
    DriveSourceStatus,
    FlashcardArtifactUserState,
    GenerationState,
    GenerationStatus,
    Label,
    MindMap,
    MindMapKind,
    NextStepSuggestion,
    Note,
    Notebook,
    NotebookDescription,
    PremiumFeatureInfo,
    PromptSuggestion,
    ReportSuggestion,
    ResearchSource,
    ResearchStatus,
    ResearchTask,
    ShareAccess,
    SharedUser,
    SharePermission,
    ShareStatus,
    ShareViewLevel,
    Source,
    SourceFulltext,
    SourceStatus,
    SourceType,
    SuggestedTopic,
    UnknownArtifactUserState,
    UserSettings,
)
from .records import (
    AccountLimitsRecord,
    ArtifactRecord,
    ArtifactUserStateRecord,
    ChatAskResultRecord,
    ChatGetHistoryResult,
    ChatLegacyMappingRecord,
    ChatLegacySequenceRecord,
    ChatLegacyValue,
    ChatReferenceRecord,
    ChatSavedNoteRecord,
    ChatSettingsRecord,
    CollectionRecord,
    GenerationStatusRecord,
    LabelKind,
    LabelRecord,
    MindMapRecord,
    NotebookDescriptionRecord,
    NotebookRecord,
    NoteRecord,
    PromptSuggestionRecord,
    ReportSuggestionRecord,
    ResearchSourceRecord,
    ResearchTaskRecord,
    ShareAccessLevel,
    SharedUserRecord,
    SharePermissionLevel,
    ShareStatusRecord,
    ShareViewScope,
    SourceFulltextRecord,
    SourceGuideRecord,
    SourceRecord,
    UserSettingsRecord,
)

_NOTEBOOK_ROLES = {
    "owner": SharePermission.OWNER,
    "editor": SharePermission.EDITOR,
    "viewer": SharePermission.VIEWER,
}
_CHAT_GOALS = {
    "default": ChatGoal.DEFAULT,
    "custom": ChatGoal.CUSTOM,
    "learning_guide": ChatGoal.LEARNING_GUIDE,
}
_CHAT_RESPONSE_LENGTHS = {
    "default": ChatResponseLength.DEFAULT,
    "long": ChatResponseLength.LONGER,
    "longer": ChatResponseLength.LONGER,
    "short": ChatResponseLength.SHORTER,
    "shorter": ChatResponseLength.SHORTER,
}

# ``Source`` still exposes its public ``kind`` through the legacy private integer
# constructor field. Keep this reverse table beside the compatibility projector,
# not in a semantic service: records and services remain free of wire codes.
_SOURCE_KIND_CODES = {
    "unknown": 0,
    "google_docs": 1,
    "google_slides": 2,
    "pdf": 3,
    "pasted_text": 4,
    "web_page": 5,
    "powerpoint": 6,
    "markdown": 8,
    "youtube": 9,
    "media": 10,
    "docx": 11,
    "image": 13,
    "google_spreadsheet": 14,
    "csv": 16,
    "epub": 17,
}
_SOURCE_STATUSES = {
    "unknown": SourceStatus.UNKNOWN,
    "processing": SourceStatus.PROCESSING,
    "ready": SourceStatus.READY,
    "error": SourceStatus.ERROR,
    "preparing": SourceStatus.PREPARING,
}
_DISCOVERY_MODES = {
    "unknown": DiscoveryMode.UNKNOWN,
    "default_llm_search": DiscoveryMode.DEFAULT_LLM_SEARCH,
    "raw_search": DiscoveryMode.RAW_SEARCH,
    "curious_search": DiscoveryMode.CURIOUS_SEARCH,
    "curious_raw_search": DiscoveryMode.CURIOUS_RAW_SEARCH,
    "deep_research": DiscoveryMode.DEEP_RESEARCH,
    "lite_llm_search": DiscoveryMode.LITE_LLM_SEARCH,
}
_DRIVE_STATUSES = {
    "unknown": DriveSourceStatus.UNKNOWN,
    "inaccessible": DriveSourceStatus.INACCESSIBLE,
    "syncing": DriveSourceStatus.SYNCING,
    "active": DriveSourceStatus.ACTIVE,
    "deleted": DriveSourceStatus.DELETED,
    "gen_ai_access_denied": DriveSourceStatus.GEN_AI_ACCESS_DENIED,
}

_ARTIFACT_FAMILY_CODES = {
    "unknown": 0,
    "audio": 1,
    "report": 2,
    "video": 3,
    "mind_map": 5,
    "fantasy_map": 6,
    "infographic": 7,
    "slide_deck": 8,
    "data_table": 9,
    "file": 10,
}
_ARTIFACT_VARIANT_CODES = {
    "flashcards": 1,
    "quiz": 2,
    "interactive_mind_map": 4,
}
_ARTIFACT_STATUS_CODES = {
    "unknown": 0,
    "pending": 1,
    "in_progress": 2,
    "completed": 3,
    "failed": 4,
    "suggested": 5,
    "pending_review": 6,
}
_ARTIFACT_MEDIA_TYPES = {
    "progressive": ArtifactMediaType.PROGRESSIVE,
    "hls": ArtifactMediaType.HLS,
    "dash": ArtifactMediaType.DASH,
    "download": ArtifactMediaType.DOWNLOAD,
    "unknown": ArtifactMediaType.UNKNOWN,
}


def _project_chat_settings(record: NotebookRecord) -> ChatSettings | None:
    settings = record.chat_settings
    if settings is None:
        return None
    goal = _CHAT_GOALS.get(settings.goal)
    response_length = _CHAT_RESPONSE_LENGTHS.get(settings.response_length)
    if goal is None or response_length is None:
        # The legacy whole-notebook mapper soft-degrades a malformed optional
        # settings block instead of discarding the notebook.
        return None
    return ChatSettings(goal, response_length, settings.custom_prompt)


def project_notebook(record: NotebookRecord) -> Notebook:
    """Construct one public :class:`Notebook` from a neutral record."""

    premium = record.premium_features
    premium_features = (
        PremiumFeatureInfo(
            premium.can_edit_advanced_settings,
            premium.can_edit_guidebook_config,
            premium.can_view_analytics,
        )
        if premium is not None
        else None
    )
    return Notebook(
        id=record.id,
        title=record.title,
        created_at=record.created_at,
        sources_count=record.sources_count,
        is_owner=record.is_owner,
        role=_NOTEBOOK_ROLES.get(record.role or ""),
        last_viewed_at=record.last_viewed_at,
        emoji=record.emoji,
        premium_features=premium_features,
        chat_sessions=[ChatSession(session.id) for session in record.chat_sessions],
        chat_settings=_project_chat_settings(record),
    )


def project_account_limits(record: AccountLimitsRecord) -> AccountLimits:
    """Construct the existing public account-limit model from neutral facts."""
    return AccountLimits(
        notebook_limit=record.notebook_limit,
        source_limit=record.source_limit,
        raw_limits=record.raw_limits,
        tier=record.tier,
    )


def project_user_settings(record: UserSettingsRecord) -> UserSettings:
    """Construct the combined public settings model from one neutral row."""
    return UserSettings(
        limits=project_account_limits(record.limits),
        output_language=cast(str | None, record.output_language),
    )


def project_prompt_suggestions(
    records: tuple[PromptSuggestionRecord, ...],
) -> list[PromptSuggestion]:
    """Construct immutable public prompt suggestions in backend order."""
    return [PromptSuggestion(record.title, record.prompt) for record in records]


def project_report_suggestions(
    records: tuple[ReportSuggestionRecord, ...],
) -> list[ReportSuggestion]:
    """Construct legacy mutable report suggestions in backend order."""
    return [project_report_suggestion(record) for record in records]


def _source_type_code(record: SourceRecord) -> int | None:
    if not record.kind_present:
        return None
    if record.unrecognized_kind is not None:
        # An adapter has already identified this as an unrecognized backend
        # discriminator. Preserve it even though the normalized semantic kind
        # is necessarily ``"unknown"``.
        return cast(int, record.unrecognized_kind)
    known = _SOURCE_KIND_CODES.get(record.kind)
    if known is not None:
        return known
    # A future backend may identify an unknown kind with a string. The legacy
    # public model stores only its opaque private discriminator, but its normal
    # constructor and ``kind`` property safely preserve that value and project
    # ``SourceType.UNKNOWN``. The cast documents that compatibility impedance.
    return cast(int, record.kind)


def project_source(record: SourceRecord) -> Source:
    """Construct one public :class:`Source` from a neutral record."""

    drive_status = None if record.drive_status is None else _DRIVE_STATUSES.get(record.drive_status)
    if record.drive_status is not None and drive_status is None:
        drive_status = DriveSourceStatus.UNKNOWN
    return Source(
        id=record.id,
        title=record.title,
        url=record.url,
        _type_code=_source_type_code(record),
        created_at=record.created_at,
        status=_SOURCE_STATUSES.get(record.status, SourceStatus.UNKNOWN),
        drive_document_id=record.drive_document_id,
        drive_status=drive_status,
        download_url=record.download_url,
        viewer_url=record.viewer_url,
        content_mime=record.content_mime,
        word_count=record.word_count,
        revision_id=record.revision_id,
        revision_timestamp=record.revision_timestamp,
        last_modified_at=record.last_modified_at,
    )


def project_research_source(record: ResearchSourceRecord) -> ResearchSource:
    """Construct one public :class:`ResearchSource` from a neutral record."""

    return ResearchSource(
        url=record.url,
        title=record.title,
        result_type=record.result_type,
        research_task_id=record.research_task_id,
        report_markdown=record.report_markdown,
        source_ordinal=record.source_ordinal,
        hint=record.hint,
    )


def project_research_task(record: ResearchTaskRecord) -> ResearchTask:
    """Construct one public :class:`ResearchTask` from a neutral record."""

    discovery_mode = (
        None
        if record.discovery_mode is None
        else _DISCOVERY_MODES.get(record.discovery_mode, DiscoveryMode.UNKNOWN)
    )
    return ResearchTask(
        task_id=record.task_id,
        status=ResearchStatus(record.status),
        query=record.query,
        sources=tuple(project_research_source(source) for source in record.sources),
        summary=record.summary,
        report=record.report,
        status_code=record.status_code,
        source_type=record.source_type,
        discovery_mode=discovery_mode,
        created_at=record.created_at,
        updated_at=record.updated_at,
        account_id=record.account_id,
    )


def record_source(source: Source) -> SourceRecord:
    """Capture one public source as a transport-neutral semantic record."""

    type_code = source._type_code
    kind = source.kind
    unrecognized_kind: int | str | None = (
        type_code if type_code is not None and kind is SourceType.UNKNOWN else None
    )
    status = next(
        (name for name, candidate in _SOURCE_STATUSES.items() if candidate is source.status),
        "unknown",
    )
    drive_status = (
        None
        if source.drive_status is None
        else next(
            (
                name
                for name, candidate in _DRIVE_STATUSES.items()
                if candidate is source.drive_status
            ),
            "unknown",
        )
    )
    return SourceRecord(
        id=source.id,
        title=source.title,
        url=source.url,
        kind=kind.value,
        unrecognized_kind=unrecognized_kind,
        kind_present=type_code is not None,
        created_at=source.created_at,
        status=status,
        drive_document_id=source.drive_document_id,
        drive_status=drive_status,
        download_url=source.download_url,
        viewer_url=source.viewer_url,
        content_mime=source.content_mime,
        word_count=source.word_count,
        revision_id=source.revision_id,
        revision_timestamp=source.revision_timestamp,
        last_modified_at=source.last_modified_at,
    )


def project_source_guide(record: SourceGuideRecord) -> SourceGuide:
    """Construct the existing frozen source-guide model."""
    return SourceGuide(summary=record.summary, keywords=record.keywords)


def project_source_fulltext(record: SourceFulltextRecord) -> SourceFulltext:
    """Construct the existing fulltext model from its neutral record."""
    return SourceFulltext(
        source_id=record.source_id,
        title=record.title,
        content=record.content,
        _type_code=_source_type_code(
            SourceRecord(
                id=record.source_id,
                kind=record.kind,
                unrecognized_kind=record.unrecognized_kind,
                kind_present=record.kind_present,
            )
        ),
        url=record.url,
        char_count=record.char_count,
        document=record.document,
    )


def project_notebook_description(record: NotebookDescriptionRecord) -> NotebookDescription:
    """Construct a public notebook guide from its neutral decode."""

    return NotebookDescription(
        summary=record.summary,
        suggested_topics=[
            SuggestedTopic(question=topic.question, prompt=topic.prompt)
            for topic in record.suggested_topics
        ],
    )


def chat_reference_record(reference: ChatReference) -> ChatReferenceRecord:
    """Copy a public citation into the neutral saved-note input shape."""
    return ChatReferenceRecord(
        source_id=reference.source_id,
        citation_number=reference.citation_number,
        cited_text=reference.cited_text,
        start_char=reference.start_char,
        end_char=reference.end_char,
        chunk_id=reference.chunk_id,
        passage_id=reference.passage_id,
        score=reference.score,
        fragment_start_char=reference.fragment_start_char,
        fragment_end_char=reference.fragment_end_char,
        answer_anchor_start=reference.answer_anchor_start,
        answer_anchor_end=reference.answer_anchor_end,
    )


def project_chat_reference(record: ChatReferenceRecord) -> ChatReference:
    """Construct a validated public citation from one neutral record."""
    return ChatReference(
        source_id=record.source_id,
        citation_number=record.citation_number,
        cited_text=record.cited_text,
        start_char=record.start_char,
        end_char=record.end_char,
        chunk_id=record.chunk_id,
        passage_id=record.passage_id,
        score=record.score,
        fragment_start_char=record.fragment_start_char,
        fragment_end_char=record.fragment_end_char,
        answer_anchor_start=record.answer_anchor_start,
        answer_anchor_end=record.answer_anchor_end,
    )


def project_chat_ask_result(
    record: ChatAskResultRecord,
    *,
    turn_number: int,
    is_follow_up: bool,
) -> AskResult:
    """Build the unary public result without re-deriving document citation anchors."""
    turn_key = record.turn_key
    return AskResult(
        answer=record.answer,
        conversation_id=record.conversation_id,
        turn_number=turn_number,
        is_follow_up=is_follow_up,
        references=[project_chat_reference(reference) for reference in record.references],
        raw_response=record.raw_response,
        answer_document=record.answer_document,
        turn_key=(
            ConversationTurnKey(turn_key.session_id, turn_key.turn_id, turn_key.turn_code)
            if turn_key is not None
            else None
        ),
        next_steps=[
            NextStepSuggestion(item.question, item.type_code) for item in record.next_steps
        ],
    )


def project_chat_settings(record: ChatSettingsRecord) -> ChatSettings:
    """Project semantic setting labels onto the existing public enums."""
    return ChatSettings(
        goal=_CHAT_GOALS[record.goal],
        response_length=_CHAT_RESPONSE_LENGTHS[record.response_length],
        custom_prompt=record.custom_prompt,
    )


def project_chat_saved_note(record: ChatSavedNoteRecord) -> Note:
    """Construct the mutable public note returned by save-answer."""
    return Note(
        id=record.id,
        notebook_id=record.notebook_id,
        title=record.title,
        content=record.content,
        created_at=record.created_at,
    )


def project_note(record: NoteRecord) -> Note:
    """Construct one public :class:`Note` from a neutral record."""

    return Note(
        id=record.id,
        notebook_id=record.notebook_id,
        title=record.title,
        content=record.content,
        created_at=record.created_at,
    )


def project_generation_status(record: GenerationStatusRecord) -> GenerationStatus:
    """Construct one public generation kickoff state from a neutral record."""

    try:
        status = GenerationState(record.status)
    except ValueError:
        status = GenerationState.UNKNOWN
    return GenerationStatus(
        task_id=record.task_id,
        status=status,
        url=record.url,
        error=record.error,
        error_code=record.error_code,
        metadata=dict(record.metadata) or None,
    )


def project_mind_map(record: MindMapRecord) -> MindMap:
    """Construct a public mind-map value without leaking backend row shapes."""

    tree = None
    if record.tree_json:
        try:
            parsed = json.loads(record.tree_json)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            tree = parsed
    return MindMap(
        id=record.id,
        notebook_id=record.notebook_id,
        title=record.title,
        kind=(
            MindMapKind.INTERACTIVE
            if record.kind == MindMapKind.INTERACTIVE.value
            else MindMapKind.NOTE_BACKED
        ),
        created_at=record.created_at,
        tree=tree,
    )


def _project_artifact_user_state(
    record: ArtifactUserStateRecord | None,
) -> AudioArtifactUserState | FlashcardArtifactUserState | UnknownArtifactUserState | None:
    if record is None:
        return None
    if record.kind == "audio":
        return AudioArtifactUserState(record.playback_position_seconds or 0.0)
    if record.kind == "flashcards":
        return FlashcardArtifactUserState(
            card_acquisitions=dict(record.card_acquisitions),
            current_card_index=record.current_card_index,
            hidden_card_indices=record.hidden_card_indices,
            last_shown_order=record.last_shown_order,
            current_view=record.current_view,
        )
    return UnknownArtifactUserState(raw=record.raw)


def _artifact_type_code(record: ArtifactRecord) -> int:
    if record.unrecognized_family is not None:
        return cast(int, record.unrecognized_family)
    if record.family in {"quiz", "flashcards"} or record.variant == "interactive_mind_map":
        return 4
    return _ARTIFACT_FAMILY_CODES.get(record.family, 0)


def project_artifact(record: ArtifactRecord) -> Artifact:
    """Construct a public :class:`Artifact` without losing catalog fields."""

    variant = (
        cast(int | None, record.unrecognized_variant)
        if record.unrecognized_variant is not None
        else _ARTIFACT_VARIANT_CODES.get(record.variant or "")
    )
    status = (
        cast(int, record.unrecognized_status)
        if record.unrecognized_status is not None
        else _ARTIFACT_STATUS_CODES.get(record.status, 0)
    )
    return Artifact(
        id=record.id,
        title=record.title,
        _artifact_type=_artifact_type_code(record),
        status=status,
        created_at=record.created_at,
        url=record.url,
        _variant=variant,
        generation_prompt=record.generation_prompt,
        media_urls=tuple(
            ArtifactMedia(
                url=media.url,
                kind=_ARTIFACT_MEDIA_TYPES.get(media.kind, ArtifactMediaType.UNKNOWN),
                type_code=(
                    cast(int, media.unrecognized_kind)
                    if media.unrecognized_kind is not None
                    else {"progressive": 1, "hls": 2, "dash": 3, "download": 4}.get(media.kind)
                ),
                mime_type=media.mime_type,
            )
            for media in record.media_urls
        ),
        duration_seconds=record.duration_seconds,
        slides=tuple(
            ArtifactSlide(
                slide.image_url,
                slide.width,
                slide.height,
                slide.alt_text,
                slide.text,
            )
            for slide in record.slides
        ),
        infographics=tuple(
            ArtifactInfographic(
                infographic.title,
                infographic.image_url,
                infographic.width,
                infographic.height,
                infographic.alt_text,
                infographic.text,
            )
            for infographic in record.infographics
        ),
        report_kind=record.report_kind,
        source_ids=record.source_ids,
        last_modified_at=record.last_modified_at,
        etag=record.etag,
        user_state=_project_artifact_user_state(record.user_state),
    )


def project_report_suggestion(record: ReportSuggestionRecord) -> ReportSuggestion:
    """Construct one public suggested-report value."""

    return ReportSuggestion(
        title=record.title,
        description=record.description,
        prompt=record.prompt,
        audience_level=cast(int, record.audience_level),
    )


def project_label(record: LabelRecord) -> Label:
    """Construct one public label from its kind-discriminated record."""

    if record.kind is not LabelKind.SOURCE_LABEL:
        raise ValueError(f"cannot project {record.kind.value} record as a source Label")

    return Label(
        id=record.id,
        name=record.name,
        notebook_id=record.notebook_id,
        emoji=record.emoji,
        source_ids=list(record.member_ids),
    )


def project_collection(record: LabelRecord | CollectionRecord) -> Collection:
    """Construct one public collection from its kind-discriminated record."""

    if isinstance(record, LabelRecord) and record.kind is not LabelKind.COLLECTION:
        raise ValueError(f"cannot project {record.kind.value} record as a Collection")
    member_ids = record.member_ids if isinstance(record, LabelRecord) else record.notebook_ids

    return Collection(
        id=record.id,
        name=record.name,
        emoji=record.emoji,
        notebook_ids=list(member_ids),
    )


_SHARE_PERMISSIONS = {
    SharePermissionLevel.OWNER: SharePermission.OWNER,
    SharePermissionLevel.EDITOR: SharePermission.EDITOR,
    SharePermissionLevel.VIEWER: SharePermission.VIEWER,
    SharePermissionLevel.REMOVE: SharePermission._REMOVE,
}
_SHARE_ACCESS = {
    ShareAccessLevel.RESTRICTED: ShareAccess.RESTRICTED,
    ShareAccessLevel.ANYONE_WITH_LINK: ShareAccess.ANYONE_WITH_LINK,
}
_SHARE_VIEW_LEVELS = {
    ShareViewScope.FULL_NOTEBOOK: ShareViewLevel.FULL_NOTEBOOK,
    ShareViewScope.CHAT_ONLY: ShareViewLevel.CHAT_ONLY,
}


def project_shared_user(record: SharedUserRecord) -> SharedUser:
    """Construct one public collaborator from a neutral record."""

    return SharedUser(
        email=record.email,
        permission=_SHARE_PERMISSIONS[record.permission],
        display_name=record.display_name,
        avatar_url=record.avatar_url,
    )


def project_share_status(record: ShareStatusRecord) -> ShareStatus:
    """Construct one public sharing status and its collaborator values."""

    return ShareStatus(
        notebook_id=record.notebook_id,
        is_public=record.is_public,
        access=_SHARE_ACCESS[record.access],
        view_level=_SHARE_VIEW_LEVELS[record.view_level],
        shared_users=[project_shared_user(user) for user in record.shared_users],
        share_url=(
            record.share_url
            if record.share_url is not None
            else (
                f"{get_base_url()}/notebook/{quote(record.notebook_id, safe='')}"
                if record.is_public
                else None
            )
        ),
        max_individuals_share_limit=record.max_individuals_share_limit,
        is_public_sharing_allowed=record.is_public_sharing_allowed,
    )


def _thaw_chat_legacy(value: ChatLegacyValue) -> Any:
    if isinstance(value, ChatLegacyMappingRecord):
        return {key: _thaw_chat_legacy(item) for key, item in value.items}
    if isinstance(value, ChatLegacySequenceRecord):
        return [_thaw_chat_legacy(item) for item in value.items]
    return value


def project_chat_turns_legacy(result: ChatGetHistoryResult) -> Any:
    """Reproduce the documented raw history envelope without leaking it from the backend."""
    if not result.envelope_present:
        return []
    if not result.turns_container_present:
        return [None]
    return [[_thaw_chat_legacy(turn.legacy_row) for turn in result.turns]]


__all__ = [
    "project_account_limits",
    "chat_reference_record",
    "project_artifact",
    "project_collection",
    "project_chat_ask_result",
    "project_chat_reference",
    "project_chat_saved_note",
    "project_chat_settings",
    "project_chat_turns_legacy",
    "project_generation_status",
    "project_label",
    "project_mind_map",
    "project_note",
    "project_notebook",
    "project_notebook_description",
    "project_prompt_suggestions",
    "project_report_suggestion",
    "project_research_source",
    "project_research_task",
    "project_report_suggestions",
    "project_share_status",
    "project_shared_user",
    "project_source",
    "project_user_settings",
    "project_source_fulltext",
    "project_source_guide",
]
