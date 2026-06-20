"""NotebookLM Automation - RPC-based automation for Google NotebookLM.

Example usage:
    from notebooklm import NotebookLMClient

    async with NotebookLMClient.from_storage() as client:
        notebooks = await client.notebooks.list()
        await client.sources.add_url(notebook_id, "https://example.com")
        result = await client.chat.ask(notebook_id, "What is this about?")

Note:
    This library uses undocumented Google APIs that can change without notice.
    See docs/troubleshooting.md for guidance on handling API changes.
"""

# Runtime Python version guard (must run before any PEP 604 syntax is evaluated)
from ._version_check import check_python_version as _check_python_version  # noqa: E402

_check_python_version()
del _check_python_version

# Configure logging (must run before other imports that create loggers)
from ._logging import (
    configure_logging,
    correlation_id,
    get_request_id,
    reset_request_id,
    set_request_id,
)

configure_logging()

# Version sourced from pyproject.toml via importlib.metadata
import logging
from importlib.metadata import PackageNotFoundError, version

_logger = logging.getLogger(__name__)

try:
    __version__ = version("notebooklm-py")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"  # Fallback when package is not installed
    _logger.debug(
        "Package 'notebooklm-py' not found in metadata. "
        "Using fallback version '%s'. This is normal during development.",
        __version__,
    )

# Public API: Authentication
from .auth import AuthTokens

# Public API: Client
from .client import NotebookLMClient

# Public API: Exceptions (centralized in exceptions.py)
from .exceptions import (
    AmbiguousResearchTaskError,  # Domain: Research
    # Domain: Artifacts
    ArtifactDownloadError,
    ArtifactError,
    ArtifactFeatureUnavailableError,
    ArtifactInProgressTimeoutError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    ArtifactPendingTimeoutError,
    ArtifactTimeoutError,
    # RPC Protocol
    AuthError,
    AuthExtractionError,
    # Domain: Chat
    ChatError,
    ChatResponseParseError,
    ClientError,
    # Validation/Config
    ConfigurationError,
    DecodingError,
    # Domain: Source labels
    LabelError,
    LabelNotFoundError,
    MindMapError,
    MindMapNotFoundError,
    # Network
    NetworkError,
    # Idempotency
    NonIdempotentRetryError,
    # Domain: Notebooks
    NotebookError,
    NotebookLimitError,
    # Base
    NotebookLMError,
    NotebookNotFoundError,
    NoteError,
    NoteNotFoundError,
    # Cross-domain umbrellas
    NotFoundError,
    RateLimitError,
    ResearchError,
    ResearchTaskMismatchError,
    ResearchTimeoutError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
    # Domain: Sources
    SourceAddError,
    SourceError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
    UnknownRPCMethodError,
    ValidationError,
    # Cross-domain umbrellas (wait/poll timeouts)
    WaitTimeoutError,
)

# Public API: Types and dataclasses
from .types import (
    AccountLimits,
    AccountTier,
    Artifact,
    ArtifactType,
    AskResult,
    AudioFormat,
    AudioLength,
    ChatGoal,
    ChatMode,
    ChatReference,
    ChatResponseLength,
    CitedSourceSelection,
    ClientMetricsSnapshot,
    ConnectionLimits,
    ConversationTurn,
    DriveMimeType,
    ExportType,
    GenerationState,
    GenerationStatus,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    Label,
    MindMap,
    MindMapKind,
    MindMapResult,
    Note,
    Notebook,
    NotebookDescription,
    NotebookMetadata,
    PromptSuggestion,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    ReportSuggestion,
    ResearchSource,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
    RpcTelemetryEvent,
    ShareAccess,
    SharedUser,
    SharePermission,
    ShareStatus,
    ShareViewLevel,
    SlideDeckFormat,
    SlideDeckLength,
    Source,
    SourceFulltext,
    SourceGuide,
    SourceStatus,
    SourceSummary,
    SourceType,
    # Enums for configuration
    SuggestedTopic,
    # Warnings
    UnknownTypeWarning,
    VideoFormat,
    VideoStyle,
)

# Public API: Utility helpers
from .utils import resolve_chat_reference_passage

__all__ = [
    "__version__",
    # Client (main entry point)
    "NotebookLMClient",
    # Auth
    "AuthTokens",
    # Observability
    "correlation_id",
    "get_request_id",
    "set_request_id",
    "reset_request_id",
    # Types
    "AccountLimits",
    "AccountTier",
    "ConnectionLimits",
    "ClientMetricsSnapshot",
    "RpcTelemetryEvent",
    "Notebook",
    "NotebookDescription",
    "NotebookMetadata",
    "SuggestedTopic",
    "Source",
    "SourceFulltext",
    "SourceGuide",
    "SourceSummary",
    "Artifact",
    "GenerationState",
    "GenerationStatus",
    "ReportSuggestion",
    "MindMap",
    "MindMapKind",
    "MindMapResult",
    "Note",
    "Label",
    "ConversationTurn",
    "ChatReference",
    "AskResult",
    "ChatMode",
    "PromptSuggestion",
    "CitedSourceSelection",
    "ResearchStatus",
    "ResearchSource",
    "ResearchTask",
    "ResearchStart",
    "SharedUser",
    "ShareStatus",
    # Utility helpers
    "resolve_chat_reference_passage",
    # Base Exceptions
    "NotebookLMError",
    "ValidationError",
    "ConfigurationError",
    # Cross-domain umbrellas
    "NotFoundError",
    # RPC/Network Exceptions
    "RPCError",
    "DecodingError",
    "UnknownRPCMethodError",
    "AuthError",
    "AuthExtractionError",
    "NetworkError",
    "RPCTimeoutError",
    "RPCResponseTooLargeError",
    "RateLimitError",
    "ServerError",
    "ClientError",
    # Idempotency
    "NonIdempotentRetryError",
    # Domain Exceptions: Notebooks
    "NotebookError",
    "NotebookNotFoundError",
    "NotebookLimitError",
    # Domain Exceptions: Chat
    "ChatError",
    "ChatResponseParseError",
    # Domain Exceptions: Sources
    "SourceError",
    "SourceAddError",
    "SourceProcessingError",
    "SourceTimeoutError",
    "SourceNotFoundError",
    # Domain Exceptions: Artifacts
    "ArtifactError",
    "ArtifactFeatureUnavailableError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "ArtifactParseError",
    "ArtifactDownloadError",
    "ArtifactTimeoutError",
    "ArtifactPendingTimeoutError",
    "ArtifactInProgressTimeoutError",
    # Domain Exceptions: Research
    "AmbiguousResearchTaskError",
    "ResearchError",
    "ResearchTimeoutError",
    "ResearchTaskMismatchError",
    # Domain Exceptions: Notes
    "NoteError",
    "NoteNotFoundError",
    # Domain Exceptions: Mind maps
    "MindMapError",
    "MindMapNotFoundError",
    # Domain Exceptions: Source labels
    "LabelError",
    "LabelNotFoundError",
    # Cross-domain umbrella: wait/poll timeouts
    "WaitTimeoutError",
    # Warnings
    "UnknownTypeWarning",
    # User-facing type enums (str enums for .kind property)
    "SourceType",
    "ArtifactType",
    # Configuration enums
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
]
