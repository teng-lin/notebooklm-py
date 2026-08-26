"""Transport-neutral records and operation definitions for Studio artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, unique

from ._logging import scrub_secrets
from ._mind_map_records import (
    MindMapGenerateInput,
    MindMapGenerateResult,
    MindMapRepresentationRecord,
)
from ._operations import CallPolicy, Operation, OperationDef, OperationTier


def sanitize_artifact_parse_text(value: object) -> str:
    """Return credential-scrubbed artifact parse evidence for public replay."""

    return scrub_secrets(value)


@unique
class ArtifactParseFailureKind(str, Enum):
    """Closed exception vocabulary emitted while decoding representation rows."""

    UNKNOWN_RPC_METHOD = "unknown_rpc_method"
    INDEX = "index"
    KEY = "key"
    TYPE = "type"
    VALUE = "value"


@dataclass(frozen=True, slots=True)
class ArtifactParseFailureRecord:
    """Sanitized evidence needed to reconstruct one public parse-error cause."""

    kind: ArtifactParseFailureKind
    message: str = field(repr=False)
    method_id: str | int | None = None
    path: tuple[int, ...] | None = None
    source: str | None = None
    found_ids: tuple[str | int, ...] = ()
    raw_response: str | None = field(default=None, repr=False)
    data_at_failure: str | None = field(default=None, repr=False)
    rpc_code: str | int | None = None


@dataclass(frozen=True, slots=True)
class ReportSuggestionRecord:
    """Neutral suggested-report row."""

    title: str
    description: str
    prompt: str
    audience_level: object = 2


@dataclass(frozen=True, slots=True)
class ArtifactSuggestReportsInput:
    """Notebook identity requested by report-format suggestions."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class ArtifactSuggestReportsResult:
    """Report-format suggestions in backend order."""

    suggestions: tuple[ReportSuggestionRecord, ...]


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
class AudioGenerateRequest:
    """Audio generation as a caller states it; ``None`` means "resolve it".

    ``source_ids=None`` asks for the notebook's whole source set and
    ``language=None`` asks for the environment default — both are service-level
    defaults (ADR-0035 addendum D1(a)) resolved before the port is invoked.
    """

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)
    audio_format: str | None = None
    audio_length: str | None = None


@dataclass(frozen=True, slots=True)
class AudioGenerateInput:
    """Pre-resolved audio generation input: the port defaults nothing."""

    notebook_id: str
    source_ids: tuple[str, ...]
    language: str
    instructions: str | None = field(default=None, repr=False)
    audio_format: str | None = None
    audio_length: str | None = None


@dataclass(frozen=True, slots=True)
class AudioGenerateResult:
    """Audio generation kickoff result."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class VideoGenerateRequest:
    """Video generation as a caller states it; ``None`` means "resolve it"."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)
    video_format: str | None = None
    video_style: str | None = None
    style_prompt: str | None = field(default=None, repr=False)
    cinematic_route: bool = False


@dataclass(frozen=True, slots=True)
class VideoGenerateInput:
    """Pre-resolved video generation input: the port defaults nothing."""

    notebook_id: str
    source_ids: tuple[str, ...]
    language: str
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
class InteractiveGenerateRequest:
    """Quiz or flashcard generation as a caller states it."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    instructions: str | None = field(default=None, repr=False)
    quantity: str | None = None
    difficulty: str | None = None


@dataclass(frozen=True, slots=True)
class InteractiveGenerateInput:
    """Pre-resolved quiz or flashcard input: the port defaults nothing."""

    notebook_id: str
    source_ids: tuple[str, ...]
    instructions: str | None = field(default=None, repr=False)
    quantity: str | None = None
    difficulty: str | None = None


@dataclass(frozen=True, slots=True)
class InteractiveGenerateResult:
    """Quiz or flashcard generation kickoff result."""

    status: GenerationStatusRecord


@dataclass(frozen=True, slots=True)
class ReportGenerateRequest:
    """Report generation as a caller states it."""

    notebook_id: str
    report_format: str = "briefing_doc"
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    custom_prompt: str | None = field(default=None, repr=False)
    extra_instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ReportGenerateInput:
    """Pre-resolved report generation input: the port defaults nothing.

    The resolved fields lead, as they do in every generate family, so the two
    required ones precede the defaulted ``report_format``.
    """

    notebook_id: str
    source_ids: tuple[str, ...]
    language: str
    report_format: str = "briefing_doc"
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
class DataTableGenerateRequest:
    """Data-table generation as a caller states it."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DataTableGenerateInput:
    """Pre-resolved data-table generation input: the port defaults nothing."""

    notebook_id: str
    source_ids: tuple[str, ...]
    language: str
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DataTableGenerateResult:
    """Data-table generation kickoff result."""

    status: GenerationStatusRecord


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
class InfographicGenerateRequest:
    """Infographic generation as a caller states it."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)
    orientation: str | None = None
    detail_level: str | None = None
    style: str | None = None


@dataclass(frozen=True, slots=True)
class InfographicGenerateInput:
    """Pre-resolved infographic input: the port defaults nothing."""

    notebook_id: str
    source_ids: tuple[str, ...]
    language: str
    instructions: str | None = field(default=None, repr=False)
    orientation: str | None = None
    detail_level: str | None = None
    style: str | None = None


@dataclass(frozen=True, slots=True)
class SlideDeckGenerateRequest:
    """Slide-deck generation as a caller states it."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)
    slide_format: str | None = None
    slide_length: str | None = None


@dataclass(frozen=True, slots=True)
class SlideDeckGenerateInput:
    """Pre-resolved slide-deck input: the port defaults nothing."""

    notebook_id: str
    source_ids: tuple[str, ...]
    language: str
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
        tier=OperationTier.PRIMITIVE,
    )
)


ARTIFACT_CATALOG_DEF: OperationDef[ArtifactCatalogInput, ArtifactCatalogResult] = OperationDef(
    Operation.ARTIFACT_CATALOG,
    CallPolicy.READ,
    ArtifactCatalogInput,
    ArtifactCatalogResult,
    tier=OperationTier.PRIMITIVE,
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


__all__ = [
    "ARTIFACT_CATALOG_DEF",
    "ARTIFACT_DELETE_DEF",
    "ARTIFACT_DOWNLOAD_DEF",
    "ARTIFACT_EXPORT_DEF",
    "ARTIFACT_GENERATE_AUDIO_DEF",
    "ARTIFACT_GENERATE_DATA_TABLE_DEF",
    "ARTIFACT_GENERATE_FLASHCARDS_DEF",
    "ARTIFACT_GENERATE_INFOGRAPHIC_DEF",
    "ARTIFACT_GENERATE_MIND_MAP_DEF",
    "ARTIFACT_GENERATE_QUIZ_DEF",
    "ARTIFACT_GENERATE_REPORT_DEF",
    "ARTIFACT_GENERATE_SLIDE_DECK_DEF",
    "ARTIFACT_GENERATE_VIDEO_DEF",
    "ARTIFACT_GET_DEF",
    "ARTIFACT_LIST_DEF",
    "ARTIFACT_PATCH_TITLE_DEF",
    "ARTIFACT_RENAME_DEF",
    "ARTIFACT_RETRY_DEF",
    "ARTIFACT_REVISE_SLIDE_DEF",
    "ARTIFACT_SUGGEST_REPORTS_DEF",
    "ARTIFACT_WAIT_DEF",
    "ArtifactCatalogInput",
    "ArtifactCatalogResult",
    "ArtifactDeleteInput",
    "ArtifactDeleteResult",
    "ArtifactDownloadInput",
    "ArtifactDownloadResult",
    "ArtifactGetInput",
    "ArtifactGetResult",
    "ArtifactInfographicRecord",
    "ArtifactListInput",
    "ArtifactListResult",
    "ArtifactMediaRecord",
    "ArtifactParseFailureKind",
    "ArtifactParseFailureRecord",
    "ArtifactPatchTitleInput",
    "ArtifactPatchTitleResult",
    "ArtifactPollInput",
    "ArtifactPollResult",
    "ArtifactRecord",
    "ArtifactRenameInput",
    "ArtifactRenameResult",
    "ArtifactRepresentationRecord",
    "ArtifactRetryInput",
    "ArtifactRetryResult",
    "ArtifactReviseSlideInput",
    "ArtifactReviseSlideResult",
    "ArtifactSlideRecord",
    "ArtifactSuggestReportsInput",
    "ArtifactSuggestReportsResult",
    "ArtifactUserStateRecord",
    "AudioGenerateInput",
    "AudioGenerateRequest",
    "AudioGenerateResult",
    "AudioMetadataRecord",
    "DataTableGenerateInput",
    "DataTableGenerateRequest",
    "DataTableGenerateResult",
    "DriveExportInput",
    "DriveExportResult",
    "GenerationStatusRecord",
    "InfographicGenerateInput",
    "InfographicGenerateRequest",
    "InteractiveGenerateInput",
    "InteractiveGenerateRequest",
    "InteractiveGenerateResult",
    "InteractiveMetadataRecord",
    "ReportGenerateInput",
    "ReportGenerateRequest",
    "ReportGenerateResult",
    "ReportMetadataRecord",
    "ReportSuggestionRecord",
    "SlideDeckGenerateInput",
    "SlideDeckGenerateRequest",
    "VideoGenerateInput",
    "VideoGenerateRequest",
    "VideoGenerateResult",
    "VideoMetadataRecord",
    "VisualGenerateResult",
    "VisualMetadataRecord",
    "sanitize_artifact_parse_text",
]
