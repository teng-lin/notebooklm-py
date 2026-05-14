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
from .auth import AuthTokens

# Public API: Client
from .client import NotebookLMClient

# Public API: Exceptions (centralized in exceptions.py)
from .exceptions import (
    # Domain: Artifacts
    ArtifactDownloadError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    # RPC Protocol
    AuthError,
    # Domain: Chat
    ChatError,
    ClientError,
    # Validation/Config
    ConfigurationError,
    DecodingError,
    # Network
    NetworkError,
    # Domain: Notebooks
    NotebookError,
    NotebookLimitError,
    # Base
    NotebookLMError,
    NotebookNotFoundError,
    RateLimitError,
    RPCError,
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
    ConversationTurn,
    DriveMimeType,
    ExportType,
    GenerationStatus,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    Note,
    Notebook,
    NotebookDescription,
    NotebookMetadata,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    ReportSuggestion,
    ShareAccess,
    SharedUser,
    SharePermission,
    ShareStatus,
    ShareViewLevel,
    SlideDeckFormat,
    SlideDeckLength,
    Source,
    SourceFulltext,
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

__all__ = [
    "__version__",
    # Client (main entry point)
    "NotebookLMClient",
    # Auth
    "AuthTokens",
    # Types
    "AccountLimits",
    "AccountTier",
    "Notebook",
    "NotebookDescription",
    "NotebookMetadata",
    "SuggestedTopic",
    "Source",
    "SourceFulltext",
    "SourceSummary",
    "Artifact",
    "GenerationStatus",
    "ReportSuggestion",
    "Note",
    "ConversationTurn",
    "ChatReference",
    "AskResult",
    "ChatMode",
    "CitedSourceSelection",
    "SharedUser",
    "ShareStatus",
    # Base Exceptions
    "NotebookLMError",
    "ValidationError",
    "ConfigurationError",
    # RPC/Network Exceptions
    "RPCError",
    "DecodingError",
    "UnknownRPCMethodError",
    "AuthError",
    "NetworkError",
    "RPCTimeoutError",
    "RateLimitError",
    "ServerError",
    "ClientError",
    # Domain Exceptions: Notebooks
    "NotebookError",
    "NotebookNotFoundError",
    "NotebookLimitError",
    # Domain Exceptions: Chat
    "ChatError",
    # Domain Exceptions: Sources
    "SourceError",
    "SourceAddError",
    "SourceProcessingError",
    "SourceTimeoutError",
    "SourceNotFoundError",
    # Domain Exceptions: Artifacts
    "ArtifactError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "ArtifactParseError",
    "ArtifactDownloadError",
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
    # Deprecated (will be removed in v0.5.0)
    "StudioContentType",
]


def __getattr__(name: str):
    """Emit deprecation warnings for deprecated module-level names.

    This allows us to provide backward-compatible imports with warnings.
    Uses globals() caching to avoid duplicate warnings on repeated access.
    """
    import warnings

    if name == "DEFAULT_STORAGE_PATH":
        from .paths import get_storage_path

        warnings.warn(
            "DEFAULT_STORAGE_PATH is deprecated, use notebooklm.paths.get_storage_path() instead. "
            "Will be removed in v0.5.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        val = get_storage_path()
        globals()[name] = val
        return val

    if name == "StudioContentType":
        from .rpc.types import ArtifactTypeCode

        warnings.warn(
            "StudioContentType is deprecated, use ArtifactType instead. Will be removed in v0.5.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Cache to prevent duplicate warnings on repeated access
        globals()[name] = ArtifactTypeCode
        return ArtifactTypeCode

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
