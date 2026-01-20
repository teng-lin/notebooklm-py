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

# Configure logging (must run before other imports that create loggers)
from ._logging import configure_logging

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
from .auth import DEFAULT_STORAGE_PATH, AuthTokens

# Public API: Client
from .client import NotebookLMClient

# Public API: RPC errors (needed for exception handling)
from .rpc import (
    AuthError,
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
)

# Public API: Types and dataclasses
from .types import (
    Artifact,
    ArtifactDownloadError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    ArtifactType,
    AskResult,
    AudioFormat,
    AudioLength,
    ChatGoal,
    ChatMode,
    ChatReference,
    ChatResponseLength,
    ConversationTurn,
    DriveMimeType,
    ExportType,
    GenerationStatus,
    InfographicDetail,
    InfographicOrientation,
    Note,
    Notebook,
    NotebookDescription,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    ReportSuggestion,
    SlideDeckFormat,
    SlideDeckLength,
    Source,
    # Exceptions
    SourceAddError,
    SourceError,
    SourceFulltext,
    SourceNotFoundError,
    SourceProcessingError,
    SourceStatus,
    SourceTimeoutError,
    SourceType,
    # Enums for configuration
    StudioContentType,
    SuggestedTopic,
    # Warnings
    UnknownTypeWarning,
    VideoFormat,
    VideoStyle,
)

__all__ = [
    "__version__",
    # Client (main entry point)
    "NotebookLMClient",
    # Auth
    "AuthTokens",
    "DEFAULT_STORAGE_PATH",
    # Types
    "Notebook",
    "NotebookDescription",
    "SuggestedTopic",
    "Source",
    "SourceFulltext",
    "Artifact",
    "GenerationStatus",
    "ReportSuggestion",
    "Note",
    "ConversationTurn",
    "ChatReference",
    "AskResult",
    "ChatMode",
    # Exceptions
    "SourceError",
    "SourceAddError",
    "SourceProcessingError",
    "SourceTimeoutError",
    "SourceNotFoundError",
    "ArtifactError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "ArtifactParseError",
    "ArtifactDownloadError",
    "RPCError",
    "AuthError",
    "NetworkError",
    "RPCTimeoutError",
    "RateLimitError",
    "ServerError",
    "ClientError",
    # Warnings
    "UnknownTypeWarning",
    # User-facing type enums (str enums for .kind property)
    "SourceType",
    "ArtifactType",
    # Configuration enums
    "StudioContentType",
    "AudioFormat",
    "AudioLength",
    "VideoFormat",
    "VideoStyle",
    "QuizQuantity",
    "QuizDifficulty",
    "InfographicOrientation",
    "InfographicDetail",
    "SlideDeckFormat",
    "SlideDeckLength",
    "ReportFormat",
    "ChatGoal",
    "ChatResponseLength",
    "DriveMimeType",
    "ExportType",
    "SourceStatus",
]
