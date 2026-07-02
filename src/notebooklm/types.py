"""Data types for NotebookLM API client.

This module contains all dataclasses and re-exports enums from rpc/types.py
for convenient access.

Usage:
    from notebooklm.types import Notebook, Source, Artifact, GenerationStatus
    from notebooklm.types import AudioFormat, VideoFormat
    from notebooklm.types import SourceType, ArtifactType  # str enums for .kind
"""

from ._types import artifacts as _artifact_types
from ._types import common as _common_types
from ._types import sources as _source_types
from ._types.artifacts import (
    Artifact,
    ArtifactType,
    GenerationState,
    GenerationStatus,
    ReportSuggestion,
)
from ._types.chat import (
    AskResult,
    ChatMode,
    ChatReference,
    ConversationTurn,
)
from ._types.common import (
    AccountLimits,
    CitedSourceSelection,
    ClientMetricsSnapshot,
    ConnectionLimits,
    RpcTelemetryEvent,
    UnknownTypeWarning,
)
from ._types.labels import Label
from ._types.mind_maps import MindMap, MindMapKind
from ._types.notebooks import (
    Notebook,
    NotebookDescription,
    NotebookMetadata,
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
    SourceGuide,
)
from ._types.sharing import SharedUser, ShareStatus
from ._types.sources import (
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
    LabelError,
    LabelNotFoundError,
    SourceAddError,
    SourceError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)

# Re-export enums from rpc/types.py for convenience
from .rpc.types import (
    ArtifactStatus,
    AudioFormat,
    AudioLength,
    ChatGoal,
    ChatResponseLength,
    DriveMimeType,
    ExportType,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
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
    source_status_to_str,
)
from .rpc.types import (
    ArtifactTypeCode as _ArtifactTypeCode,
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

# Guards the ``ResearchSourceInput`` import from being removed as unused:
# ``typing.get_type_hints(CitedSourceSelection)`` needs it in this facade's
# globals after ``CitedSourceSelection.__module__`` is rewritten below.
# Intentionally absent from ``__all__``.
_CITED_SOURCE_SELECTION_TYPE_HINT_GLOBALS = (ResearchSourceInput,)


__all__ = [
    # Dataclasses
    "CitedSourceSelection",
    "ConnectionLimits",
    "ClientMetricsSnapshot",
    "RpcTelemetryEvent",
    "Notebook",
    "NotebookDescription",
    "NotebookMetadata",
    "SuggestedTopic",
    "Source",
    "SourceFulltext",
    "SourceSummary",
    "Artifact",
    "GenerationState",
    "GenerationStatus",
    "ReportSuggestion",
    "Note",
    "Label",
    "ConversationTurn",
    "ChatReference",
    "AskResult",
    "ChatMode",
    "PromptSuggestion",
    "SharedUser",
    "ShareStatus",
    # Research / mind-map / source-guide typed returns
    "ResearchStatus",
    "ResearchSource",
    "ResearchTask",
    "ResearchStart",
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
    "DriveMimeType",
    "ExportType",
    "SourceStatus",
    "ShareAccess",
    "ShareViewLevel",
    "SharePermission",
    # Helper functions
    "artifact_status_to_str",
    "source_status_to_str",
]


for _public_common_type in (
    AccountLimits,
    CitedSourceSelection,
    ClientMetricsSnapshot,
    ConnectionLimits,
    RpcTelemetryEvent,
    UnknownTypeWarning,
):
    _public_common_type.__module__ = __name__
del _public_common_type


for _public_moved_type in (
    Artifact,
    ArtifactType,
    AskResult,
    ChatMode,
    ChatReference,
    ConversationTurn,
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
    PromptSuggestion,
    ReportSuggestion,
    ResearchSource,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
    SharedUser,
    ShareStatus,
    Source,
    SourceFulltext,
    SourceGuide,
    SourceSummary,
    SourceType,
    SuggestedTopic,
):
    _public_moved_type.__module__ = __name__
del _public_moved_type
