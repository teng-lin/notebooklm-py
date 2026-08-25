"""Private transport-neutral records for migrated semantic slices."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ._artifact_records import (
    ArtifactParseFailureKind,
    ArtifactParseFailureRecord,
    sanitize_artifact_parse_text,
)
from ._chat_records import (
    CHAT_ASK_DEF,
    CHAT_CONFIGURE_DEF,
    CHAT_DELETE_HISTORY_DEF,
    CHAT_GET_CONVERSATION_DEF,
    CHAT_GET_HISTORY_DEF,
    CHAT_SAVE_NOTE_DEF,
    ChatAskInput,
    ChatAskResultRecord,
    ChatConfigureAction,
    ChatConfigureInput,
    ChatConfigureResult,
    ChatConversationTurnRecord,
    ChatDeleteHistoryInput,
    ChatDeleteHistoryResult,
    ChatGetConversationInput,
    ChatGetConversationResult,
    ChatGetHistoryInput,
    ChatGetHistoryResult,
    ChatHistoryPairRecord,
    ChatLegacyMappingRecord,
    ChatLegacyScalar,
    ChatLegacySequenceRecord,
    ChatLegacyValue,
    ChatNextStepRecord,
    ChatReferenceRecord,
    ChatSavedNoteRecord,
    ChatSaveNoteInput,
    ChatSaveNoteResult,
    ChatSettingsRecord,
    ChatStreamAnswerRecord,
    ChatTurnDecodeErrorRecord,
    ChatTurnKeyRecord,
)
from ._label_records import (
    COLLECTION_CREATE_DEF,
    COLLECTION_DELETE_DEF,
    COLLECTION_GET_DEF,
    COLLECTION_LIST_DEF,
    COLLECTION_UPDATE_DEF,
    LABEL_ALLOCATE_DEF,
    LABEL_CREATE_DEF,
    LABEL_DELETE_DEF,
    LABEL_GENERATE_DEF,
    LABEL_GET_DEF,
    LABEL_LIST_DEF,
    LABEL_MUTATE_DEF,
    LABEL_UPDATE_DEF,
    LabelAllocateInput,
    LabelAllocateResult,
    LabelCreateInput,
    LabelCreateResult,
    LabelDeleteInput,
    LabelDeleteResult,
    LabelGenerateInput,
    LabelGenerateResult,
    LabelGetInput,
    LabelGetResult,
    LabelKind,
    LabelListInput,
    LabelListResult,
    LabelMutateInput,
    LabelMutateResult,
    LabelRecord,
    LabelUpdateInput,
    LabelUpdateResult,
)
from ._note_records import (
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    NOTE_UPDATE_DEF,
    NoteCreateInput,
    NoteCreateResult,
    NoteDeleteInput,
    NoteDeleteResult,
    NoteGetInput,
    NoteGetResult,
    NoteListInput,
    NoteListResult,
    NoteRecord,
    NoteUpdateInput,
    NoteUpdateResult,
)
from ._notebook_records import (
    NOTEBOOK_ALLOCATE_DEF,
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_PATCH_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_SUGGEST_PROMPTS_DEF,
    NOTEBOOK_SUMMARIZE_DEF,
    NOTEBOOK_UPDATE_DEF,
    NotebookAllocateInput,
    NotebookAllocateResult,
    NotebookChatSessionRecord,
    NotebookChatSettingsRecord,
    NotebookCreateInput,
    NotebookCreateResult,
    NotebookDeleteInput,
    NotebookDeleteResult,
    NotebookDescriptionRecord,
    NotebookGetInput,
    NotebookGetResult,
    NotebookGuideInput,
    NotebookGuideResult,
    NotebookListInput,
    NotebookListResult,
    NotebookPatchInput,
    NotebookPatchResult,
    NotebookPremiumFeaturesRecord,
    NotebookRecord,
    NotebookRemoveRecentInput,
    NotebookRemoveRecentResult,
    NotebookSuggestPromptsInput,
    NotebookSuggestPromptsResult,
    NotebookUpdateInput,
    NotebookUpdateResult,
    PromptSuggestionRecord,
    SuggestedTopicRecord,
)
from ._operations import CallPolicy, Operation, OperationDef
from ._research_records import (
    RESEARCH_CANCEL_DEF,
    RESEARCH_IMPORT_DEF,
    RESEARCH_POLL_DEF,
    RESEARCH_START_DEF,
    ResearchCancelInput,
    ResearchCancelResult,
    ResearchImportedSourceRecord,
    ResearchImportEntry,
    ResearchImportEntryKind,
    ResearchImportInput,
    ResearchImportResult,
    ResearchMode,
    ResearchPollInput,
    ResearchPollResult,
    ResearchSearchSource,
    ResearchSourceRecord,
    ResearchStartInput,
    ResearchStartResult,
    ResearchTaskRecord,
)
from ._settings_records import (
    SETTINGS_GET_DEF,
    SETTINGS_GET_LIMITS_DEF,
    SETTINGS_SET_LANGUAGE_DEF,
    AccountLimitsRecord,
    SettingsGetInput,
    SettingsGetLimitsInput,
    SettingsGetLimitsResult,
    SettingsGetResult,
    SettingsSetLanguageInput,
    SettingsSetLanguageResult,
    UserSettingsRecord,
)
from ._sharing_records import (
    LEGACY_SHARE_ARTIFACT_DEF,
    SHARING_GET_DEF,
    SHARING_MUTATE_DEF,
    SHARING_PATCH_VIEW_LEVEL_DEF,
    SHARING_SET_PUBLIC_DEF,
    SHARING_SET_VIEW_LEVEL_DEF,
    SHARING_UPDATE_USERS_DEF,
    LegacyShareArtifactInput,
    LegacyShareArtifactResult,
    ShareAccessLevel,
    SharedUserRecord,
    SharePermissionLevel,
    ShareStatusRecord,
    ShareViewScope,
    SharingGetInput,
    SharingGetResult,
    SharingGrants,
    SharingMutateInput,
    SharingMutateResult,
    SharingPatchViewLevelInput,
    SharingPatchViewLevelResult,
    SharingSetPublicInput,
    SharingSetPublicResult,
    SharingSetViewLevelInput,
    SharingSetViewLevelResult,
    SharingUpdateUsersInput,
    SharingUpdateUsersResult,
    SharingUserGrant,
    SharingVisibility,
)
from ._source_records import (
    SourceAddCommitState,
    SourceAddDriveInput,
    SourceAddDriveResult,
    SourceAddFailureKind,
    SourceAddFailureRecord,
    SourceAddFileInput,
    SourceAddFileResult,
    SourceAddTextInput,
    SourceAddTextResult,
    SourceAddTitleState,
    SourceAddUrlBatchInput,
    SourceAddUrlBatchResult,
    SourceAddUrlInput,
    SourceAddUrlReceipt,
    SourceAddUrlResult,
    SourceDeleteInput,
    SourceDeleteResult,
    SourceFileInputKind,
    SourceFileRegistrationRecord,
    SourceFreshnessInput,
    SourceFreshnessResult,
    SourceFulltextInput,
    SourceFulltextRecord,
    SourceFulltextResult,
    SourceGetInput,
    SourceGetResult,
    SourceGuideInput,
    SourceGuideRecord,
    SourceGuideResult,
    SourceListInput,
    SourceListResult,
    SourcePatchTitleInput,
    SourcePatchTitleResult,
    SourceProgressCallback,
    SourceRecord,
    SourceRefreshInput,
    SourceRefreshResult,
    SourceUpdateInput,
    SourceUpdateResult,
    SourceUrlBatchItemRecord,
    SourceWaitSnapshotInput,
    SourceWaitSnapshotResult,
)


@dataclass(frozen=True, slots=True)
class ReportSuggestionRecord:
    """Neutral suggested-report row."""

    title: str
    description: str
    prompt: str
    audience_level: object = 2


@dataclass(frozen=True, slots=True)
class CollectionRecord:
    """Neutral account-level notebook collection."""

    id: str
    name: str
    emoji: str | None = None
    notebook_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactSuggestReportsInput:
    """Notebook identity requested by report-format suggestions."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class ArtifactSuggestReportsResult:
    """Report-format suggestions in backend order."""

    suggestions: tuple[ReportSuggestionRecord, ...]


@dataclass(frozen=True, slots=True)
class MindMapRecord:
    """One backend-neutral mind map with its optional JSON tree payload."""

    id: str
    notebook_id: str
    title: str
    kind: str
    created_at: datetime | None = None
    tree_json: str | None = None


@dataclass(frozen=True, slots=True)
class MindMapListInput:
    """Notebook whose active note-backed mind maps are requested."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class MindMapListResult:
    """Active note-backed mind maps in backend order."""

    mind_maps: tuple[MindMapRecord, ...]


@dataclass(frozen=True, slots=True)
class MindMapGetInput:
    """Interactive mind-map identity whose tree is requested."""

    notebook_id: str
    mind_map_id: str


@dataclass(frozen=True, slots=True)
class MindMapGetResult:
    """Interactive tree JSON, or ``None`` while absent/not populated."""

    tree_json: str | None


@dataclass(frozen=True, slots=True)
class MindMapGenerateNoteInput:
    """Note-backed mind-map generation options."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MindMapGenerateNoteResult:
    """Generated note-backed tree before semantic note persistence."""

    tree_json: str | None


@dataclass(frozen=True, slots=True)
class MindMapGenerateInteractiveInput:
    """Interactive Studio mind-map generation options."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MindMapGenerateInteractiveResult:
    """Allocated interactive Studio mind-map identity."""

    mind_map_id: str


@dataclass(frozen=True, slots=True)
class MindMapUpdateInput:
    """Interactive mind-map title replacement."""

    notebook_id: str
    mind_map_id: str
    title: str


@dataclass(frozen=True, slots=True)
class MindMapUpdateResult:
    """Successful interactive mind-map rename."""


@dataclass(frozen=True, slots=True)
class MindMapDeleteInput:
    """Interactive mind-map identity to delete idempotently."""

    notebook_id: str
    mind_map_id: str


@dataclass(frozen=True, slots=True)
class MindMapDeleteResult:
    """Successful idempotent interactive mind-map deletion."""


@dataclass(frozen=True, slots=True)
class ArtifactMediaRecord:
    """One transport-neutral artifact media rendition."""

    url: str = field(repr=False)
    kind: str = "unknown"
    unrecognized_kind: int | str | None = None
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactSlideRecord:
    """One rendered slide without exposing asset contents in representations."""

    image_url: str | None = field(default=None, repr=False)
    width: int | None = None
    height: int | None = None
    alt_text: str | None = field(default=None, repr=False)
    text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ArtifactInfographicRecord:
    """One rendered infographic."""

    title: str | None = None
    image_url: str | None = field(default=None, repr=False)
    width: int | None = None
    height: int | None = None
    alt_text: str | None = field(default=None, repr=False)
    text: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ArtifactUserStateRecord:
    """Closed known user-state summary with opaque forward-compatible payload."""

    kind: str
    playback_position_seconds: float | None = None
    card_acquisitions: tuple[tuple[str, str], ...] = ()
    current_card_index: int | None = None
    hidden_card_indices: tuple[int, ...] = ()
    last_shown_order: tuple[int, ...] = ()
    current_view: str | None = None
    raw: object | None = field(default=None, repr=False, compare=True)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Complete neutral Studio catalog entry from one catalog snapshot."""

    id: str
    title: str
    family: str
    status: str
    unrecognized_family: int | str | None = None
    variant: str | None = None
    interactive_variant_pending: bool = False
    unrecognized_variant: int | str | None = None
    unrecognized_status: int | str | None = None
    created_at: datetime | None = None
    url: str | None = field(default=None, repr=False)
    generation_prompt: str | None = field(default=None, repr=False)
    media_urls: tuple[ArtifactMediaRecord, ...] = field(default=(), repr=False)
    duration_seconds: float | None = None
    slides: tuple[ArtifactSlideRecord, ...] = field(default=(), repr=False)
    infographics: tuple[ArtifactInfographicRecord, ...] = field(default=(), repr=False)
    report_kind: str | None = None
    source_ids: tuple[str, ...] = ()
    last_modified_at: datetime | None = None
    etag: str | None = field(default=None, repr=False)
    user_state: ArtifactUserStateRecord | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ArtifactListInput:
    """Notebook whose complete Studio catalog is requested."""

    notebook_id: str
    family: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactListResult:
    """Complete heterogeneous Studio catalog in backend order."""

    artifacts: tuple[ArtifactRecord, ...]


@dataclass(frozen=True, slots=True)
class ArtifactGetInput:
    """Notebook and artifact identities requested from one catalog snapshot."""

    notebook_id: str
    artifact_id: str


@dataclass(frozen=True, slots=True)
class ArtifactGetResult:
    """Artifact get result; ``None`` is the semantic not-found state."""

    artifact: ArtifactRecord | None


@dataclass(frozen=True, slots=True)
class GenerationStatusRecord:
    """Transport-neutral artifact generation task state."""

    task_id: str
    status: str
    url: str | None = field(default=None, repr=False)
    error: str | None = field(default=None, repr=False)
    error_code: str | None = None
    metadata: tuple[tuple[str, object], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ArtifactReviseSlideInput:
    """One slide-revision request without web payload vocabulary."""

    notebook_id: str
    artifact_id: str
    slide_index: int
    prompt: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ArtifactReviseSlideResult:
    """Accepted slide revision and its lifecycle task state."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class ArtifactRetryInput:
    """Identity of one failed artifact to retry in place."""

    notebook_id: str
    artifact_id: str


@dataclass(frozen=True, slots=True)
class ArtifactRetryResult:
    """Accepted in-place retry and its lifecycle task state."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class ArtifactDeleteInput:
    """Identity of one Studio artifact to delete idempotently."""

    notebook_id: str
    artifact_id: str


@dataclass(frozen=True, slots=True)
class ArtifactDeleteResult:
    """Successful idempotent Studio artifact deletion."""


@dataclass(frozen=True, slots=True)
class ArtifactPatchTitleInput:
    """One Studio artifact title set-op."""

    notebook_id: str
    artifact_id: str
    new_title: str


@dataclass(frozen=True, slots=True)
class ArtifactPatchTitleResult:
    """Successful Studio artifact title set-op acknowledgement."""


@dataclass(frozen=True, slots=True)
class ArtifactCatalogInput:
    """Notebook whose plain Studio catalog is requested without mind-map merging."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class ArtifactCatalogResult:
    """Plain Studio catalog in backend order."""

    artifacts: tuple[ArtifactRecord, ...]


@dataclass(frozen=True, slots=True)
class ArtifactRenameInput:
    """Identity and replacement title for one Studio artifact."""

    notebook_id: str
    artifact_id: str
    new_title: str


@dataclass(frozen=True, slots=True)
class ArtifactRenameResult:
    """Post-mutation Studio row, or ``None`` when the target is absent."""

    artifact: ArtifactRecord | None


@dataclass(frozen=True, slots=True)
class ArtifactPollInput:
    """Identity of one generation task whose lifecycle state is requested."""

    notebook_id: str
    task_id: str


@dataclass(frozen=True, slots=True)
class ArtifactPollResult:
    """One lifecycle status observation."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class ArtifactRepresentationRecord:
    """Artifact fields needed for downloads, decoded from one Studio row.
    Bodies and URLs are hidden from ``repr``; no public models or wire positions remain.
    """

    artifact: ArtifactRecord
    audio_url: str | None = field(default=None, repr=False)
    video_url: str | None = field(default=None, repr=False)
    infographic_url: str | None = field(default=None, repr=False)
    slide_deck_pdf_url: str | None = field(default=None, repr=False)
    slide_deck_pptx_url: str | None = field(default=None, repr=False)
    report_markdown: str | None = field(default=None, repr=False)
    data_table_headers: tuple[str, ...] = field(default=(), repr=False)
    data_table_rows: tuple[tuple[str, ...], ...] = field(default=(), repr=False)
    data_table_error: str | None = field(default=None, repr=False)
    data_table_failure: ArtifactParseFailureRecord | None = field(default=None, repr=False)
    parse_error: str | None = field(default=None, repr=False)
    parse_failure: ArtifactParseFailureRecord | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MindMapRepresentationRecord:
    """One note-backed mind-map identity and serialized tree."""

    id: str
    title: str
    content: str | None = field(default=None, repr=False)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ArtifactDownloadInput:
    """One representation lookup through the semantic download operation."""

    notebook_id: str
    action: str
    artifact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactDownloadResult:
    """Decoded representation inventory or one interactive content leaf."""

    representations: tuple[ArtifactRepresentationRecord, ...] = ()
    mind_maps: tuple[MindMapRepresentationRecord, ...] = ()
    content: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class AudioGenerateInput:
    """Audio generation options without web enum or payload vocabulary."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)
    audio_format: str | None = None
    audio_length: str | None = None


@dataclass(frozen=True, slots=True)
class AudioGenerateResult:
    """Audio generation kickoff result."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class VideoGenerateInput:
    """Video generation options without web enum or payload vocabulary."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)
    video_format: str | None = None
    video_style: str | None = None
    style_prompt: str | None = field(default=None, repr=False)
    cinematic_route: bool = False


@dataclass(frozen=True, slots=True)
class VideoGenerateResult:
    """Video generation kickoff result."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class InteractiveGenerateInput:
    """Quiz or flashcard generation options without web enum vocabulary."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    instructions: str | None = field(default=None, repr=False)
    quantity: str | None = None
    difficulty: str | None = None


@dataclass(frozen=True, slots=True)
class InteractiveGenerateResult:
    """Quiz or flashcard generation kickoff result."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class ReportGenerateInput:
    """Report generation options without web enum or payload vocabulary."""

    notebook_id: str
    report_format: str = "briefing_doc"
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    custom_prompt: str | None = field(default=None, repr=False)
    extra_instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ReportGenerateResult:
    """Report generation kickoff result."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class AudioMetadataRecord:
    """Audio readiness and representation metadata derived from one catalog row."""

    artifact_id: str
    lifecycle_status: str
    usable: bool
    preferred_url: str | None = field(default=None, repr=False)
    media_urls: tuple[ArtifactMediaRecord, ...] = field(default=(), repr=False)
    duration_seconds: float | None = None
    generation_prompt: str | None = field(default=None, repr=False)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VideoMetadataRecord:
    """Video readiness and representation metadata derived from one catalog row."""

    artifact_id: str
    lifecycle_status: str
    usable: bool
    preferred_url: str | None = field(default=None, repr=False)
    media_urls: tuple[ArtifactMediaRecord, ...] = field(default=(), repr=False)
    duration_seconds: float | None = None
    generation_prompt: str | None = field(default=None, repr=False)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InteractiveMetadataRecord:
    """Interactive-family readiness and per-user study metadata."""

    artifact_id: str
    family: str
    lifecycle_status: str
    usable: bool
    generation_prompt: str | None = field(default=None, repr=False)
    user_state: ArtifactUserStateRecord | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ReportMetadataRecord:
    """Report readiness and format metadata derived from one catalog row."""

    artifact_id: str
    lifecycle_status: str
    usable: bool
    report_kind: str | None = None
    report_format: str | None = None
    generation_prompt: str | None = field(default=None, repr=False)
    source_ids: tuple[str, ...] = ()
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DataTableGenerateInput:
    """Data-table generation options without web payload vocabulary."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DataTableGenerateResult:
    """Data-table generation kickoff result."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class MindMapGenerateInput:
    """Note-backed mind-map generation options."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MindMapGenerateResult:
    """Generated mind-map tree plus its persisted note identity."""

    mind_map: object | None = field(default=None, repr=False)
    note_id: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DriveExportInput:
    """One explicit web companion export to Google Drive."""

    notebook_id: str
    artifact_id: str | None = None
    content: str | None = field(default=None, repr=False)
    title: str = "Export"
    destination: str = "docs"


@dataclass(frozen=True, slots=True)
class DriveExportResult:
    """Opaque decoded export response preserved for facade compatibility."""

    value: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class InfographicGenerateInput:
    """Infographic options without web enum or payload vocabulary."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)
    orientation: str | None = None
    detail_level: str | None = None
    style: str | None = None


@dataclass(frozen=True, slots=True)
class SlideDeckGenerateInput:
    """Slide-deck options without web enum or payload vocabulary."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)
    slide_format: str | None = None
    slide_length: str | None = None


@dataclass(frozen=True, slots=True)
class VisualGenerateResult:
    """Visual generation kickoff result."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class VisualMetadataRecord:
    """Visual readiness and accessibility metadata from one catalog row."""

    artifact_id: str
    family: str
    lifecycle_status: str
    usable: bool
    slides: tuple[ArtifactSlideRecord, ...] = field(default=(), repr=False)
    infographics: tuple[ArtifactInfographicRecord, ...] = field(default=(), repr=False)
    preferred_url: str | None = field(default=None, repr=False)
    generation_prompt: str | None = field(default=None, repr=False)
    created_at: datetime | None = None


SOURCE_LIST_DEF: OperationDef[SourceListInput, SourceListResult] = OperationDef(
    Operation.SOURCE_LIST,
    # Both source reads use GET_NOTEBOOK and therefore update notebook recency.
    CallPolicy.MUTATION,
    SourceListInput,
    SourceListResult,
)
ARTIFACT_LIST_DEF: OperationDef[ArtifactListInput, ArtifactListResult] = OperationDef(
    Operation.ARTIFACT_LIST,
    CallPolicy.READ,
    ArtifactListInput,
    ArtifactListResult,
)
ARTIFACT_GET_DEF: OperationDef[ArtifactGetInput, ArtifactGetResult] = OperationDef(
    Operation.ARTIFACT_GET,
    CallPolicy.READ,
    ArtifactGetInput,
    ArtifactGetResult,
)
ARTIFACT_GENERATE_DATA_TABLE_DEF: OperationDef[DataTableGenerateInput, DataTableGenerateResult] = (
    OperationDef(
        Operation.ARTIFACT_GENERATE_DATA_TABLE,
        CallPolicy.STATEFUL_START,
        DataTableGenerateInput,
        DataTableGenerateResult,
    )
)
ARTIFACT_GENERATE_MIND_MAP_DEF: OperationDef[MindMapGenerateInput, MindMapGenerateResult] = (
    OperationDef(
        Operation.ARTIFACT_GENERATE_MIND_MAP,
        CallPolicy.STATEFUL_START,
        MindMapGenerateInput,
        MindMapGenerateResult,
    )
)
ARTIFACT_EXPORT_DEF: OperationDef[DriveExportInput, DriveExportResult] = OperationDef(
    Operation.ARTIFACT_EXPORT,
    CallPolicy.MUTATION,
    DriveExportInput,
    DriveExportResult,
)
ARTIFACT_REVISE_SLIDE_DEF: OperationDef[ArtifactReviseSlideInput, ArtifactReviseSlideResult] = (
    OperationDef(
        Operation.ARTIFACT_REVISE_SLIDE,
        CallPolicy.MUTATION,
        ArtifactReviseSlideInput,
        ArtifactReviseSlideResult,
    )
)
ARTIFACT_RETRY_DEF: OperationDef[ArtifactRetryInput, ArtifactRetryResult] = OperationDef(
    Operation.ARTIFACT_RETRY,
    CallPolicy.STATEFUL_START,
    ArtifactRetryInput,
    ArtifactRetryResult,
)
ARTIFACT_DELETE_DEF: OperationDef[ArtifactDeleteInput, ArtifactDeleteResult] = OperationDef(
    Operation.ARTIFACT_DELETE,
    CallPolicy.MUTATION,
    ArtifactDeleteInput,
    ArtifactDeleteResult,
)
ARTIFACT_PATCH_TITLE_DEF: OperationDef[ArtifactPatchTitleInput, ArtifactPatchTitleResult] = (
    OperationDef(
        Operation.ARTIFACT_PATCH_TITLE,
        CallPolicy.MUTATION,
        ArtifactPatchTitleInput,
        ArtifactPatchTitleResult,
    )
)
ARTIFACT_CATALOG_DEF: OperationDef[ArtifactCatalogInput, ArtifactCatalogResult] = OperationDef(
    Operation.ARTIFACT_CATALOG,
    CallPolicy.READ,
    ArtifactCatalogInput,
    ArtifactCatalogResult,
)
ARTIFACT_RENAME_DEF: OperationDef[ArtifactRenameInput, ArtifactRenameResult] = OperationDef(
    Operation.ARTIFACT_RENAME,
    CallPolicy.MUTATION,
    ArtifactRenameInput,
    ArtifactRenameResult,
)
ARTIFACT_DOWNLOAD_DEF: OperationDef[ArtifactDownloadInput, ArtifactDownloadResult] = OperationDef(
    Operation.ARTIFACT_DOWNLOAD,
    CallPolicy.READ,
    ArtifactDownloadInput,
    ArtifactDownloadResult,
)
ARTIFACT_WAIT_DEF: OperationDef[ArtifactPollInput, ArtifactPollResult] = OperationDef(
    Operation.ARTIFACT_WAIT,
    CallPolicy.READ,
    ArtifactPollInput,
    ArtifactPollResult,
)
ARTIFACT_SUGGEST_REPORTS_DEF: OperationDef[
    ArtifactSuggestReportsInput, ArtifactSuggestReportsResult
] = OperationDef(
    Operation.ARTIFACT_SUGGEST_REPORTS,
    CallPolicy.STATEFUL_START,
    ArtifactSuggestReportsInput,
    ArtifactSuggestReportsResult,
)
ARTIFACT_GENERATE_AUDIO_DEF: OperationDef[AudioGenerateInput, AudioGenerateResult] = OperationDef(
    Operation.ARTIFACT_GENERATE_AUDIO,
    CallPolicy.STATEFUL_START,
    AudioGenerateInput,
    AudioGenerateResult,
)
ARTIFACT_GENERATE_QUIZ_DEF: OperationDef[InteractiveGenerateInput, InteractiveGenerateResult] = (
    OperationDef(
        Operation.ARTIFACT_GENERATE_QUIZ,
        CallPolicy.STATEFUL_START,
        InteractiveGenerateInput,
        InteractiveGenerateResult,
    )
)
ARTIFACT_GENERATE_FLASHCARDS_DEF: OperationDef[
    InteractiveGenerateInput, InteractiveGenerateResult
] = OperationDef(
    Operation.ARTIFACT_GENERATE_FLASHCARDS,
    CallPolicy.STATEFUL_START,
    InteractiveGenerateInput,
    InteractiveGenerateResult,
)
ARTIFACT_GENERATE_VIDEO_DEF: OperationDef[VideoGenerateInput, VideoGenerateResult] = OperationDef(
    Operation.ARTIFACT_GENERATE_VIDEO,
    CallPolicy.STATEFUL_START,
    VideoGenerateInput,
    VideoGenerateResult,
)
ARTIFACT_GENERATE_REPORT_DEF: OperationDef[ReportGenerateInput, ReportGenerateResult] = (
    OperationDef(
        Operation.ARTIFACT_GENERATE_REPORT,
        CallPolicy.STATEFUL_START,
        ReportGenerateInput,
        ReportGenerateResult,
    )
)
ARTIFACT_GENERATE_INFOGRAPHIC_DEF: OperationDef[InfographicGenerateInput, VisualGenerateResult] = (
    OperationDef(
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC,
        CallPolicy.STATEFUL_START,
        InfographicGenerateInput,
        VisualGenerateResult,
    )
)
ARTIFACT_GENERATE_SLIDE_DECK_DEF: OperationDef[SlideDeckGenerateInput, VisualGenerateResult] = (
    OperationDef(
        Operation.ARTIFACT_GENERATE_SLIDE_DECK,
        CallPolicy.STATEFUL_START,
        SlideDeckGenerateInput,
        VisualGenerateResult,
    )
)
SOURCE_GET_DEF: OperationDef[SourceGetInput, SourceGetResult] = OperationDef(
    Operation.SOURCE_GET,
    CallPolicy.MUTATION,
    SourceGetInput,
    SourceGetResult,
)
SOURCE_ADD_URL_DEF: OperationDef[SourceAddUrlInput, SourceAddUrlResult] = OperationDef(
    Operation.SOURCE_ADD_URL,
    CallPolicy.MUTATION,
    SourceAddUrlInput,
    SourceAddUrlResult,
)
SOURCE_ADD_URL_BATCH_DEF: OperationDef[SourceAddUrlBatchInput, SourceAddUrlBatchResult] = (
    OperationDef(
        Operation.SOURCE_ADD_URL_BATCH,
        CallPolicy.MUTATION,
        SourceAddUrlBatchInput,
        SourceAddUrlBatchResult,
    )
)
SOURCE_ADD_TEXT_DEF: OperationDef[SourceAddTextInput, SourceAddTextResult] = OperationDef(
    Operation.SOURCE_ADD_TEXT,
    CallPolicy.MUTATION,
    SourceAddTextInput,
    SourceAddTextResult,
)
SOURCE_ADD_DRIVE_DEF: OperationDef[SourceAddDriveInput, SourceAddDriveResult] = OperationDef(
    Operation.SOURCE_ADD_DRIVE,
    CallPolicy.MUTATION,
    SourceAddDriveInput,
    SourceAddDriveResult,
)
SOURCE_ADD_FILE_DEF: OperationDef[SourceAddFileInput, SourceAddFileResult] = OperationDef(
    Operation.SOURCE_ADD_FILE,
    CallPolicy.MUTATION,
    SourceAddFileInput,
    SourceAddFileResult,
)
SOURCE_DELETE_DEF: OperationDef[SourceDeleteInput, SourceDeleteResult] = OperationDef(
    Operation.SOURCE_DELETE,
    CallPolicy.MUTATION,
    SourceDeleteInput,
    SourceDeleteResult,
)
SOURCE_UPDATE_DEF: OperationDef[SourceUpdateInput, SourceUpdateResult] = OperationDef(
    Operation.SOURCE_UPDATE,
    CallPolicy.MUTATION,
    SourceUpdateInput,
    SourceUpdateResult,
)
SOURCE_PATCH_TITLE_DEF: OperationDef[SourcePatchTitleInput, SourcePatchTitleResult] = OperationDef(
    Operation.SOURCE_PATCH_TITLE,
    CallPolicy.MUTATION,
    SourcePatchTitleInput,
    SourcePatchTitleResult,
)
SOURCE_REFRESH_DEF: OperationDef[SourceRefreshInput, SourceRefreshResult] = OperationDef(
    Operation.SOURCE_REFRESH,
    CallPolicy.MUTATION,
    SourceRefreshInput,
    SourceRefreshResult,
)
SOURCE_CHECK_FRESHNESS_DEF: OperationDef[SourceFreshnessInput, SourceFreshnessResult] = (
    OperationDef(
        Operation.SOURCE_CHECK_FRESHNESS,
        CallPolicy.READ,
        SourceFreshnessInput,
        SourceFreshnessResult,
    )
)
SOURCE_GET_GUIDE_DEF: OperationDef[SourceGuideInput, SourceGuideResult] = OperationDef(
    Operation.SOURCE_GET_GUIDE,
    CallPolicy.STATEFUL_START,
    SourceGuideInput,
    SourceGuideResult,
)
SOURCE_GET_FULLTEXT_DEF: OperationDef[SourceFulltextInput, SourceFulltextResult] = OperationDef(
    Operation.SOURCE_GET_FULLTEXT,
    CallPolicy.READ,
    SourceFulltextInput,
    SourceFulltextResult,
)
SOURCE_WAIT_DEF: OperationDef[SourceWaitSnapshotInput, SourceWaitSnapshotResult] = OperationDef(
    Operation.SOURCE_WAIT,
    CallPolicy.MUTATION,
    SourceWaitSnapshotInput,
    SourceWaitSnapshotResult,
)
MIND_MAP_LIST_DEF: OperationDef[MindMapListInput, MindMapListResult] = OperationDef(
    Operation.MIND_MAP_LIST,
    CallPolicy.READ,
    MindMapListInput,
    MindMapListResult,
)
MIND_MAP_GET_DEF: OperationDef[MindMapGetInput, MindMapGetResult] = OperationDef(
    Operation.MIND_MAP_GET,
    CallPolicy.READ,
    MindMapGetInput,
    MindMapGetResult,
)
MIND_MAP_GENERATE_NOTE_DEF: OperationDef[MindMapGenerateNoteInput, MindMapGenerateNoteResult] = (
    OperationDef(
        Operation.MIND_MAP_GENERATE_NOTE,
        CallPolicy.STATEFUL_START,
        MindMapGenerateNoteInput,
        MindMapGenerateNoteResult,
    )
)
MIND_MAP_GENERATE_INTERACTIVE_DEF: OperationDef[
    MindMapGenerateInteractiveInput, MindMapGenerateInteractiveResult
] = OperationDef(
    Operation.MIND_MAP_GENERATE_INTERACTIVE,
    CallPolicy.STATEFUL_START,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
)
MIND_MAP_UPDATE_DEF: OperationDef[MindMapUpdateInput, MindMapUpdateResult] = OperationDef(
    Operation.MIND_MAP_UPDATE,
    CallPolicy.MUTATION,
    MindMapUpdateInput,
    MindMapUpdateResult,
)
MIND_MAP_DELETE_DEF: OperationDef[MindMapDeleteInput, MindMapDeleteResult] = OperationDef(
    Operation.MIND_MAP_DELETE,
    CallPolicy.MUTATION,
    MindMapDeleteInput,
    MindMapDeleteResult,
)


__all__ = [
    "ARTIFACT_CATALOG_DEF",
    "ARTIFACT_DELETE_DEF",
    "ARTIFACT_DOWNLOAD_DEF",
    "CHAT_ASK_DEF",
    "CHAT_CONFIGURE_DEF",
    "CHAT_DELETE_HISTORY_DEF",
    "CHAT_GET_CONVERSATION_DEF",
    "CHAT_GET_HISTORY_DEF",
    "CHAT_SAVE_NOTE_DEF",
    "ARTIFACT_EXPORT_DEF",
    "ARTIFACT_GET_DEF",
    "ARTIFACT_GENERATE_DATA_TABLE_DEF",
    "ARTIFACT_GENERATE_AUDIO_DEF",
    "ARTIFACT_GENERATE_FLASHCARDS_DEF",
    "ARTIFACT_GENERATE_INFOGRAPHIC_DEF",
    "ARTIFACT_GENERATE_MIND_MAP_DEF",
    "ARTIFACT_GENERATE_QUIZ_DEF",
    "ARTIFACT_GENERATE_REPORT_DEF",
    "ARTIFACT_GENERATE_SLIDE_DECK_DEF",
    "ARTIFACT_GENERATE_VIDEO_DEF",
    "ARTIFACT_LIST_DEF",
    "ARTIFACT_PATCH_TITLE_DEF",
    "COLLECTION_CREATE_DEF",
    "COLLECTION_DELETE_DEF",
    "COLLECTION_GET_DEF",
    "COLLECTION_LIST_DEF",
    "COLLECTION_UPDATE_DEF",
    "LABEL_CREATE_DEF",
    "LABEL_DELETE_DEF",
    "LABEL_GENERATE_DEF",
    "LABEL_GET_DEF",
    "LABEL_ALLOCATE_DEF",
    "LABEL_LIST_DEF",
    "LABEL_MUTATE_DEF",
    "LABEL_UPDATE_DEF",
    "LEGACY_SHARE_ARTIFACT_DEF",
    "ARTIFACT_RENAME_DEF",
    "ARTIFACT_RETRY_DEF",
    "ARTIFACT_REVISE_SLIDE_DEF",
    "ARTIFACT_SUGGEST_REPORTS_DEF",
    "ARTIFACT_WAIT_DEF",
    "NOTEBOOK_ALLOCATE_DEF",
    "NOTEBOOK_GET_DEF",
    "NOTEBOOK_LIST_DEF",
    "NOTEBOOK_PATCH_DEF",
    "NOTEBOOK_CREATE_DEF",
    "NOTEBOOK_DELETE_DEF",
    "NOTEBOOK_DESCRIBE_DEF",
    "NOTEBOOK_REMOVE_RECENT_DEF",
    "NOTEBOOK_SUGGEST_PROMPTS_DEF",
    "NOTEBOOK_SUMMARIZE_DEF",
    "NOTEBOOK_UPDATE_DEF",
    "RESEARCH_CANCEL_DEF",
    "RESEARCH_IMPORT_DEF",
    "RESEARCH_POLL_DEF",
    "RESEARCH_START_DEF",
    "SETTINGS_GET_DEF",
    "SETTINGS_GET_LIMITS_DEF",
    "SETTINGS_SET_LANGUAGE_DEF",
    "SOURCE_GET_DEF",
    "SOURCE_LIST_DEF",
    "SOURCE_ADD_URL_DEF",
    "SHARING_GET_DEF",
    "SHARING_MUTATE_DEF",
    "SHARING_PATCH_VIEW_LEVEL_DEF",
    "SHARING_SET_PUBLIC_DEF",
    "SHARING_SET_VIEW_LEVEL_DEF",
    "SHARING_UPDATE_USERS_DEF",
    "SharingGrants",
    "SharingPatchViewLevelInput",
    "SharingPatchViewLevelResult",
    "SharingVisibility",
    "SOURCE_ADD_URL_BATCH_DEF",
    "SOURCE_ADD_TEXT_DEF",
    "SOURCE_ADD_DRIVE_DEF",
    "SOURCE_ADD_FILE_DEF",
    "SOURCE_DELETE_DEF",
    "SOURCE_UPDATE_DEF",
    "SOURCE_PATCH_TITLE_DEF",
    "SOURCE_REFRESH_DEF",
    "SOURCE_CHECK_FRESHNESS_DEF",
    "SOURCE_GET_GUIDE_DEF",
    "SOURCE_GET_FULLTEXT_DEF",
    "SOURCE_WAIT_DEF",
    "NOTE_CREATE_DEF",
    "NOTE_DELETE_DEF",
    "NOTE_GET_DEF",
    "NOTE_LIST_DEF",
    "NOTE_UPDATE_DEF",
    "MIND_MAP_DELETE_DEF",
    "MIND_MAP_GENERATE_INTERACTIVE_DEF",
    "MIND_MAP_GENERATE_NOTE_DEF",
    "MIND_MAP_GET_DEF",
    "MIND_MAP_LIST_DEF",
    "MIND_MAP_UPDATE_DEF",
    "AccountLimitsRecord",
    "ArtifactSuggestReportsInput",
    "ArtifactCatalogInput",
    "ArtifactCatalogResult",
    "ArtifactGetInput",
    "ArtifactGetResult",
    "ArtifactDeleteInput",
    "ArtifactDeleteResult",
    "ArtifactDownloadInput",
    "ArtifactDownloadResult",
    "ArtifactInfographicRecord",
    "ArtifactListInput",
    "ArtifactListResult",
    "ArtifactMediaRecord",
    "ArtifactPollInput",
    "ArtifactPollResult",
    "ArtifactPatchTitleInput",
    "ArtifactPatchTitleResult",
    "ArtifactRecord",
    "ArtifactRenameInput",
    "ArtifactRenameResult",
    "ArtifactRepresentationRecord",
    "ArtifactParseFailureKind",
    "ArtifactParseFailureRecord",
    "sanitize_artifact_parse_text",
    "ArtifactRetryInput",
    "ArtifactRetryResult",
    "ArtifactReviseSlideInput",
    "ArtifactReviseSlideResult",
    "ArtifactSlideRecord",
    "ArtifactUserStateRecord",
    "ArtifactSuggestReportsResult",
    "ChatAskInput",
    "ChatAskResultRecord",
    "ChatConfigureAction",
    "ChatConfigureInput",
    "ChatConfigureResult",
    "ChatConversationTurnRecord",
    "ChatDeleteHistoryInput",
    "ChatDeleteHistoryResult",
    "ChatGetConversationInput",
    "ChatGetConversationResult",
    "ChatGetHistoryInput",
    "ChatGetHistoryResult",
    "ChatHistoryPairRecord",
    "ChatLegacyMappingRecord",
    "ChatLegacyScalar",
    "ChatLegacySequenceRecord",
    "ChatLegacyValue",
    "ChatNextStepRecord",
    "ChatReferenceRecord",
    "ChatSavedNoteRecord",
    "ChatSaveNoteInput",
    "ChatSaveNoteResult",
    "ChatSettingsRecord",
    "ChatStreamAnswerRecord",
    "ChatTurnDecodeErrorRecord",
    "ChatTurnKeyRecord",
    "DataTableGenerateInput",
    "DataTableGenerateResult",
    "DriveExportInput",
    "DriveExportResult",
    "AudioGenerateInput",
    "AudioGenerateResult",
    "AudioMetadataRecord",
    "CollectionRecord",
    "GenerationStatusRecord",
    "InteractiveGenerateInput",
    "InteractiveGenerateResult",
    "InteractiveMetadataRecord",
    "InfographicGenerateInput",
    "LabelCreateInput",
    "LabelAllocateInput",
    "LabelAllocateResult",
    "LabelCreateResult",
    "LabelDeleteInput",
    "LabelDeleteResult",
    "LabelGenerateInput",
    "LabelGenerateResult",
    "LabelGetInput",
    "LabelGetResult",
    "LabelKind",
    "LabelListInput",
    "LabelListResult",
    "LabelMutateInput",
    "LabelMutateResult",
    "LabelRecord",
    "LabelUpdateInput",
    "LabelUpdateResult",
    "NotebookAllocateInput",
    "NotebookAllocateResult",
    "NotebookChatSessionRecord",
    "NotebookChatSettingsRecord",
    "NotebookCreateInput",
    "NotebookCreateResult",
    "NotebookDeleteInput",
    "NotebookDeleteResult",
    "NotebookGetInput",
    "NotebookGetResult",
    "NotebookGuideInput",
    "NotebookGuideResult",
    "NotebookListInput",
    "NotebookListResult",
    "NotebookPatchInput",
    "NotebookPatchResult",
    "NotebookPremiumFeaturesRecord",
    "NotebookRecord",
    "NotebookDescriptionRecord",
    "NotebookRemoveRecentInput",
    "NotebookRemoveRecentResult",
    "NotebookSuggestPromptsInput",
    "NotebookSuggestPromptsResult",
    "NotebookUpdateInput",
    "NotebookUpdateResult",
    "MindMapGenerateInput",
    "MindMapGenerateResult",
    "MindMapDeleteInput",
    "MindMapDeleteResult",
    "MindMapGenerateInteractiveInput",
    "MindMapGenerateInteractiveResult",
    "MindMapGenerateNoteInput",
    "MindMapGenerateNoteResult",
    "MindMapGetInput",
    "MindMapGetResult",
    "MindMapListInput",
    "MindMapListResult",
    "MindMapRecord",
    "MindMapUpdateInput",
    "MindMapUpdateResult",
    "MindMapRepresentationRecord",
    "NoteCreateInput",
    "NoteCreateResult",
    "NoteDeleteInput",
    "NoteDeleteResult",
    "NoteGetInput",
    "NoteGetResult",
    "NoteListInput",
    "NoteListResult",
    "NoteRecord",
    "NoteUpdateInput",
    "NoteUpdateResult",
    "ReportGenerateInput",
    "ReportGenerateResult",
    "ReportMetadataRecord",
    "ResearchCancelInput",
    "ResearchCancelResult",
    "ResearchImportEntry",
    "ResearchImportEntryKind",
    "ResearchImportInput",
    "ResearchImportResult",
    "ResearchImportedSourceRecord",
    "ResearchMode",
    "ResearchPollInput",
    "ResearchPollResult",
    "ResearchSearchSource",
    "ResearchSourceRecord",
    "ResearchStartInput",
    "ResearchStartResult",
    "ResearchTaskRecord",
    "PromptSuggestionRecord",
    "SourceGetInput",
    "SourceGetResult",
    "SourceAddCommitState",
    "SourceAddFailureKind",
    "SourceAddFailureRecord",
    "SourceAddTitleState",
    "SourceAddUrlInput",
    "SourceAddUrlReceipt",
    "SourceAddUrlResult",
    "SourceAddUrlBatchInput",
    "SourceAddUrlBatchResult",
    "SourceUrlBatchItemRecord",
    "SourceAddTextInput",
    "SourceAddTextResult",
    "SourceAddDriveInput",
    "SourceAddDriveResult",
    "SourceAddFileInput",
    "SourceAddFileResult",
    "SourceFileInputKind",
    "SourceFileRegistrationRecord",
    "SourceProgressCallback",
    "SourceDeleteInput",
    "SourceDeleteResult",
    "SourceUpdateInput",
    "SourceUpdateResult",
    "SourcePatchTitleInput",
    "SourcePatchTitleResult",
    "SourceRefreshInput",
    "SourceRefreshResult",
    "SourceFreshnessInput",
    "SourceFreshnessResult",
    "SourceGuideInput",
    "SourceGuideRecord",
    "SourceGuideResult",
    "SourceFulltextInput",
    "SourceFulltextRecord",
    "SourceFulltextResult",
    "SourceWaitSnapshotInput",
    "SourceWaitSnapshotResult",
    "SourceListInput",
    "SourceListResult",
    "SourceRecord",
    "SlideDeckGenerateInput",
    "LegacyShareArtifactInput",
    "LegacyShareArtifactResult",
    "ReportSuggestionRecord",
    "ShareAccessLevel",
    "SharePermissionLevel",
    "SettingsGetInput",
    "SettingsGetLimitsInput",
    "SettingsGetLimitsResult",
    "SettingsGetResult",
    "SettingsSetLanguageInput",
    "SettingsSetLanguageResult",
    "ShareStatusRecord",
    "ShareViewScope",
    "SharedUserRecord",
    "SharingGetInput",
    "SharingGetResult",
    "SharingSetPublicInput",
    "SharingSetPublicResult",
    "SharingSetViewLevelInput",
    "SharingSetViewLevelResult",
    "SharingUpdateUsersInput",
    "SharingUpdateUsersResult",
    "SharingMutateInput",
    "SharingMutateResult",
    "SharingUserGrant",
    "SuggestedTopicRecord",
    "VideoGenerateInput",
    "VideoGenerateResult",
    "VideoMetadataRecord",
    "VisualGenerateResult",
    "VisualMetadataRecord",
    "UserSettingsRecord",
]
