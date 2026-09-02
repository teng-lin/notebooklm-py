"""Data types for NotebookLM API client.

This module contains all dataclasses and re-exports transport-neutral enums
for convenient access.

Usage:
    from notebooklm.types import Notebook, Source, Artifact, GenerationStatus
    from notebooklm.types import AudioFormat, VideoFormat
    from notebooklm.types import SourceType, ArtifactType  # str enums for .kind
"""

from ._types import artifacts as _artifact_types
from ._types import common as _common_types
from ._types import sources as _source_types
from ._types.artifact_content import (
    ArtifactInfographic,
    ArtifactMedia,
    ArtifactMediaType,
    ArtifactSlide,
    ArtifactUserState,
    AudioArtifactUserState,
    FlashcardArtifactUserState,
    UnknownArtifactUserState,
)
from ._types.artifacts import (
    Artifact,
    ArtifactCustomizationChoices,
    ArtifactType,
    CopiedArtifact,
    CustomizationChoice,
    GenerationState,
    GenerationStatus,
    ReportPreset,
    ReportSuggestion,
)
from ._types.chat import (
    AskResult,
    ChatMode,
    ChatReference,
    ChatSessionStatus,
    ChatSettings,
    ConversationTurn,
    ConversationTurnKey,
    NextStepSuggestion,
)
from ._types.collections import Collection
from ._types.common import (
    AccountLimits,
    CitedSourceSelection,
    ClientMetricsSnapshot,
    ConnectionLimits,
    RpcTelemetryEvent,
    UnknownTypeWarning,
    UserSettings,
)
from ._types.documents import (
    BlockKind,
    BlockStyle,
    DocumentAnnotation,
    DocumentBlock,
    ListInfo,
    ListStyle,
    StructuredDocument,
    TableCell,
    TextSpan,
    utf16_len,
)
from ._types.enums import (
    SOURCE_STATUS_LABELS,
    ArtifactStatus,
    AudioFormat,
    AudioLength,
    ChatGoal,
    ChatResponseLength,
    DiscoveryMode,
    DriveMimeType,
    DriveSourceStatus,
    ExportType,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    MagicArtifactType,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    ShareAccess,
    SharePermission,
    ShareViewLevel,
    SlideDeckFormat,
    SlideDeckLength,
    SourceStatus,
    VideoFormat,
    VideoStyle,
    artifact_status_to_str,
    discovery_mode_to_str,
    drive_source_status_to_str,
    share_permission_to_str,
    source_status_to_str,
)
from ._types.enums import (
    ArtifactTypeCode as _ArtifactTypeCode,
)
from ._types.enums import (
    GrpcStatusCode as _GrpcStatusCode,
)
from ._types.enums import (
    normalize_grpc_status as _normalize_grpc_status,
)
from ._types.enums import (
    normalize_rpc_code as _normalize_rpc_code,
)
from ._types.labels import Label
from ._types.mind_maps import MindMap, MindMapKind
from ._types.notebooks import (
    ChatSession,
    Notebook,
    NotebookDescription,
    NotebookMetadata,
    PremiumFeatureInfo,
    PromptSuggestion,
    SourceSummary,
    SuggestedTopic,
)
from ._types.notes import Note
from ._types.research import (
    MindMapResult,
    ResearchSource,
    ResearchSourceInput,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
    ResearchTerminationReason,
    SourceGuide,
)
from ._types.sharing import SharedUser, ShareStatus
from ._types.sources import (
    CopiedSource,
    ExpertIntelligenceSourceMetadata,
    PlayBook,
    PlayBookExportReason,
    RelevantChunk,
    Source,
    SourceFulltext,
    SourceType,
)

# Import exceptions from centralized module (re-export for backward compatibility)
from .exceptions import (
    ArtifactDownloadError,
    ArtifactError,
    ArtifactFeatureUnavailableError,
    ArtifactInProgressTimeoutError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    ArtifactPendingTimeoutError,
    ArtifactTimeoutError,
    CollectionError,
    CollectionNotFoundError,
    LabelError,
    LabelNotFoundError,
    SourceAddError,
    SourceError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)

# Keep private facade names that first-party tests and external callers have
# historically imported while the implementation moves into _types modules.
_SOURCE_TYPE_COMPAT_MAP = _source_types._SOURCE_TYPE_COMPAT_MAP
_datetime_from_timestamp = _common_types._datetime_from_timestamp
_extract_artifact_url = _artifact_types._extract_artifact_url
_extract_audio_artifact_url = _artifact_types._extract_audio_artifact_url
_extract_infographic_artifact_url = _artifact_types._extract_infographic_artifact_url
_extract_slide_deck_artifact_url = _artifact_types._extract_slide_deck_artifact_url
_extract_source_created_at = _source_types._extract_source_created_at
_extract_source_url = _source_types._extract_source_url
_extract_video_artifact_url = _artifact_types._extract_video_artifact_url
_is_valid_artifact_url = _artifact_types._is_valid_artifact_url
_warned_artifact_types = _artifact_types._warned_artifact_types
_warned_source_types = _source_types._warned_source_types

# Imported for the historical ``notebooklm.types.ArtifactTypeCode`` attribute,
# but intentionally absent from ``__all__``.
ArtifactTypeCode = _ArtifactTypeCode

# The canonical gRPC status table and its two coercion helpers, routed through
# this facade for the ``_app`` layer: the boundary lint
# (``tests/_guardrails/test_app_boundary.py``) forbids ``_app`` from importing
# ``notebooklm.rpc.*`` directly, and the neutral error classifier needs both.
# Internal plumbing, so intentionally absent from ``__all__``.
GrpcStatusCode = _GrpcStatusCode
normalize_grpc_status = _normalize_grpc_status
normalize_rpc_code = _normalize_rpc_code

# The local-file extension policy, routed through this facade for the ``_app``
# layer for the same reason as the gRPC table above: the boundary lint
# (``tests/_guardrails/test_app_boundary.py``) forbids ``_app`` from importing
# private siblings such as ``notebooklm._types``, and the transport-neutral
# ``source add`` path heuristic needs the derived set. Internal plumbing, so
# intentionally absent from ``__all__``.
_PATH_SHAPED_FILE_EXTENSIONS = _source_types._PATH_SHAPED_FILE_EXTENSIONS

# Guards the ``ResearchSourceInput`` import from being removed as unused:
# ``typing.get_type_hints(CitedSourceSelection)`` needs it in this facade's
# globals after ``CitedSourceSelection.__module__`` is rewritten below.
# Intentionally absent from ``__all__``.
_CITED_SOURCE_SELECTION_TYPE_HINT_GLOBALS = (ResearchSourceInput,)


__all__ = [
    # Dataclasses
    "AccountLimits",
    "UserSettings",
    "CitedSourceSelection",
    "ConnectionLimits",
    "ClientMetricsSnapshot",
    "RpcTelemetryEvent",
    "Notebook",
    "PremiumFeatureInfo",
    "ChatSession",
    "NotebookDescription",
    "NotebookMetadata",
    "SuggestedTopic",
    "Source",
    "SourceFulltext",
    "RelevantChunk",
    "CopiedSource",
    "PlayBook",
    "PlayBookExportReason",
    "ExpertIntelligenceSourceMetadata",
    "SourceSummary",
    "Artifact",
    "ArtifactInfographic",
    "ArtifactMedia",
    "ArtifactMediaType",
    "ArtifactSlide",
    "ArtifactUserState",
    "AudioArtifactUserState",
    "FlashcardArtifactUserState",
    "UnknownArtifactUserState",
    "GenerationState",
    "GenerationStatus",
    "ReportSuggestion",
    "CopiedArtifact",
    "CustomizationChoice",
    "ReportPreset",
    "ArtifactCustomizationChoices",
    "Note",
    "Label",
    "Collection",
    "ConversationTurn",
    "ConversationTurnKey",
    "NextStepSuggestion",
    "ChatReference",
    "ChatSessionStatus",
    "BlockKind",
    "BlockStyle",
    "DocumentAnnotation",
    "DocumentBlock",
    "ListInfo",
    "ListStyle",
    "StructuredDocument",
    "TableCell",
    "TextSpan",
    "utf16_len",
    "AskResult",
    "ChatMode",
    "ChatSettings",
    "PromptSuggestion",
    "SharedUser",
    "ShareStatus",
    # Research / mind-map / source-guide typed returns
    "ResearchStatus",
    "ResearchSource",
    "ResearchTask",
    "ResearchStart",
    "ResearchTerminationReason",
    "MindMap",
    "MindMapKind",
    "MindMapResult",
    "SourceGuide",
    # Exceptions
    "SourceError",
    "SourceAddError",
    "SourceProcessingError",
    "SourceTimeoutError",
    "SourceNotFoundError",
    "ArtifactError",
    "ArtifactFeatureUnavailableError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "ArtifactParseError",
    "ArtifactDownloadError",
    "ArtifactTimeoutError",
    "ArtifactPendingTimeoutError",
    "ArtifactInProgressTimeoutError",
    "LabelError",
    "LabelNotFoundError",
    "CollectionError",
    "CollectionNotFoundError",
    # Warnings
    "UnknownTypeWarning",
    # User-facing type enums (str enums for .kind property)
    "SourceType",
    "ArtifactType",
    # Re-exported enums (configuration/RPC)
    "ArtifactStatus",
    # Note: ArtifactTypeCode is internal - not exported here
    "AudioFormat",
    "AudioLength",
    "VideoFormat",
    "VideoStyle",
    "QuizQuantity",
    "QuizDifficulty",
    "InfographicOrientation",
    "InfographicDetail",
    "InfographicStyle",
    "SlideDeckFormat",
    "SlideDeckLength",
    "ReportFormat",
    "ChatGoal",
    "ChatResponseLength",
    "MagicArtifactType",
    "DriveMimeType",
    "ExportType",
    "SourceStatus",
    "DriveSourceStatus",
    "DiscoveryMode",
    "ShareAccess",
    "ShareViewLevel",
    "SharePermission",
    # Helper functions
    "artifact_status_to_str",
    "discovery_mode_to_str",
    "drive_source_status_to_str",
    "share_permission_to_str",
    "SOURCE_STATUS_LABELS",
    "source_status_to_str",
]


for _public_common_type in (
    AccountLimits,
    CitedSourceSelection,
    ClientMetricsSnapshot,
    ConnectionLimits,
    RpcTelemetryEvent,
    UnknownTypeWarning,
    UserSettings,
):
    _public_common_type.__module__ = __name__
del _public_common_type


for _public_moved_type in (
    Artifact,
    ArtifactCustomizationChoices,
    ArtifactInfographic,
    ArtifactMedia,
    ArtifactMediaType,
    ArtifactSlide,
    AudioArtifactUserState,
    FlashcardArtifactUserState,
    UnknownArtifactUserState,
    ArtifactType,
    AskResult,
    ChatMode,
    ChatReference,
    ChatSessionStatus,
    ChatSettings,
    ChatSession,
    Collection,
    ConversationTurn,
    ConversationTurnKey,
    CopiedArtifact,
    CopiedSource,
    CustomizationChoice,
    NextStepSuggestion,
    GenerationState,
    GenerationStatus,
    Label,
    MindMap,
    MindMapKind,
    MindMapResult,
    Note,
    Notebook,
    NotebookDescription,
    NotebookMetadata,
    PremiumFeatureInfo,
    PromptSuggestion,
    ReportPreset,
    ReportSuggestion,
    ResearchSource,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
    ResearchTerminationReason,
    SharedUser,
    ShareStatus,
    ExpertIntelligenceSourceMetadata,
    PlayBook,
    PlayBookExportReason,
    RelevantChunk,
    Source,
    SourceFulltext,
    SourceGuide,
    SourceSummary,
    SourceType,
    SuggestedTopic,
):
    _public_moved_type.__module__ = __name__
del _public_moved_type
