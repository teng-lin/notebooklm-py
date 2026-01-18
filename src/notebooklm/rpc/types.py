"""RPC types and constants for NotebookLM API."""

from enum import Enum

# NotebookLM API endpoints
BATCHEXECUTE_URL = "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
QUERY_URL = "https://notebooklm.google.com/_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateFreeFormStreamed"
UPLOAD_URL = "https://notebooklm.google.com/upload/_/"


class RPCMethod(str, Enum):
    """RPC method IDs for NotebookLM operations.

    These are obfuscated method identifiers used by the batchexecute API.
    Reverse-engineered from network traffic analysis.
    """

    # Notebook operations
    LIST_NOTEBOOKS = "wXbhsf"
    CREATE_NOTEBOOK = "CCqFvf"
    GET_NOTEBOOK = "rLM1Ne"
    RENAME_NOTEBOOK = "s0tc2d"
    DELETE_NOTEBOOK = "WWINqb"

    # Source operations
    ADD_SOURCE = "izAoDd"
    ADD_SOURCE_FILE = "o4cbdc"  # Register uploaded file as source
    DELETE_SOURCE = "tGMBJ"
    GET_SOURCE = "hizoJc"
    REFRESH_SOURCE = "FLmJqe"
    CHECK_SOURCE_FRESHNESS = "yR9Yof"
    UPDATE_SOURCE = "b7Wfje"
    DISCOVER_SOURCES = "qXyaNe"

    # Summary and query
    SUMMARIZE = "VfAZjd"
    GET_SOURCE_GUIDE = "tr032e"
    GET_SUGGESTED_REPORTS = "ciyUvf"  # AI-suggested report formats

    # Query endpoint (not a batchexecute RPC ID)
    QUERY_ENDPOINT = "/_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GenerateFreeFormStreamed"

    # Studio content generation
    CREATE_AUDIO = "AHyHrd"
    GET_AUDIO = "VUsiyb"
    DELETE_AUDIO = "sJDbic"
    CREATE_VIDEO = "R7cb6c"
    POLL_STUDIO = "gArtLc"
    DELETE_STUDIO = "V5N4be"
    CREATE_ARTIFACT = "xpWGLf"
    GET_ARTIFACT = "BnLyuf"
    LIST_ARTIFACTS = "gArtLc"

    # Research
    START_FAST_RESEARCH = "Ljjv0c"
    START_DEEP_RESEARCH = "QA9ei"
    POLL_RESEARCH = "e3bVqc"
    IMPORT_RESEARCH = "LBwxtb"

    ACT_ON_SOURCES = "yyryJe"
    GENERATE_MIND_MAP = "yyryJe"
    CREATE_NOTE = "CYK0Xb"
    GET_NOTES_AND_MIND_MAPS = "cFji9"  # Returns both notes and mind maps

    # Note operations
    UPDATE_NOTE = "cYAfTb"
    DELETE_NOTE = "AH0mwd"

    # Artifact management
    RENAME_ARTIFACT = "rc3d8d"
    EXPORT_ARTIFACT = "Krh3pd"
    LIST_ARTIFACTS_ALT = "LfTXoe"
    GET_INTERACTIVE_HTML = "v9rmvd"  # Fetch quiz/flashcard HTML content

    # Conversation
    GET_CONVERSATION_HISTORY = "hPTbtc"

    # Sharing operations
    SHARE_ARTIFACT = "RGP97b"  # Share any artifact (audio, video, report, quiz, flashcards)
    SHARE_NOTEBOOK = "QDyure"  # Set notebook visibility (restricted/anyone with link)
    GET_SHARE_STATUS = "JFMDGd"  # Get notebook share settings
    # Note: SET_SHARE_ACCESS uses RENAME_NOTEBOOK (s0tc2d) with different params

    # Additional notebook operations
    REMOVE_RECENTLY_VIEWED = "fejl7e"

    # User settings
    SET_OUTPUT_LANGUAGE = "hT54vc"  # Set/get output language for artifact generation


class StudioContentType(int, Enum):
    """Types of studio content that can be generated.

    These are integer codes used in the R7cb6c RPC call.
    Values correspond to artifact_data[2] in API responses.
    """

    AUDIO = 1
    REPORT = (
        2  # Includes: Briefing Doc, Study Guide, Blog Post, White Paper, Research Proposal, etc.
    )
    VIDEO = 3
    QUIZ = 4  # Also used for flashcards
    QUIZ_FLASHCARD = 4  # Alias for backward compatibility
    MIND_MAP = 5
    # Note: Type 6 appears unused in current API
    INFOGRAPHIC = 7
    SLIDE_DECK = 8
    DATA_TABLE = 9


class ArtifactStatus(int, Enum):
    """Processing status of an artifact.

    Values correspond to artifact_data[4] in API responses.
    """

    PROCESSING = 1  # Artifact is being generated
    PENDING = 2  # Artifact is queued
    COMPLETED = 3  # Artifact is ready for use/download


_ARTIFACT_STATUS_MAP: dict[int, str] = {
    ArtifactStatus.PROCESSING: "in_progress",
    ArtifactStatus.PENDING: "pending",
    ArtifactStatus.COMPLETED: "completed",
}


def artifact_status_to_str(status_code: int) -> str:
    """Convert artifact status code to human-readable string.

    This is the single source of truth for status code to string mapping.
    Use this helper instead of inline conditionals to ensure consistency.

    Args:
        status_code: Numeric status from API response (artifact_data[4]).

    Returns:
        String status: "in_progress", "pending", "completed", or "unknown".
        Returns "unknown" for unrecognized codes (future-proofing).
    """
    return _ARTIFACT_STATUS_MAP.get(status_code, "unknown")


class AudioFormat(int, Enum):
    """Audio overview format options."""

    DEEP_DIVE = 1
    BRIEF = 2
    CRITIQUE = 3
    DEBATE = 4


class AudioLength(int, Enum):
    """Audio overview length options."""

    SHORT = 1
    DEFAULT = 2
    LONG = 3


class VideoFormat(int, Enum):
    """Video overview format options."""

    EXPLAINER = 1
    BRIEF = 2


class VideoStyle(int, Enum):
    """Video visual style options."""

    AUTO_SELECT = 1
    CUSTOM = 2
    CLASSIC = 3
    WHITEBOARD = 4
    KAWAII = 5
    ANIME = 6
    WATERCOLOR = 7
    RETRO_PRINT = 8
    HERITAGE = 9
    PAPER_CRAFT = 10


class QuizQuantity(int, Enum):
    """Quiz/Flashcards quantity options.

    Note: Google's API only distinguishes between FEWER (1) and STANDARD (2).
    MORE is an alias for STANDARD - the API treats them identically.
    This matches the observed behavior from NotebookLM's web interface.
    """

    FEWER = 1
    STANDARD = 2
    MORE = 2  # Alias for STANDARD - API limitation


class QuizDifficulty(int, Enum):
    """Quiz/Flashcards difficulty options."""

    EASY = 1
    MEDIUM = 2
    HARD = 3


class InfographicOrientation(int, Enum):
    """Infographic orientation options."""

    LANDSCAPE = 1
    PORTRAIT = 2
    SQUARE = 3


class InfographicDetail(int, Enum):
    """Infographic detail level options."""

    CONCISE = 1
    STANDARD = 2
    DETAILED = 3


class SlideDeckFormat(int, Enum):
    """Slide deck format options."""

    DETAILED_DECK = 1
    PRESENTER_SLIDES = 2


class SlideDeckLength(int, Enum):
    """Slide deck length options."""

    DEFAULT = 1
    SHORT = 2


class ReportFormat(str, Enum):
    """Report format options for type 2 artifacts.

    All reports use StudioContentType.REPORT (2) but are differentiated
    by the title/description/prompt configuration.
    """

    BRIEFING_DOC = "briefing_doc"
    STUDY_GUIDE = "study_guide"
    BLOG_POST = "blog_post"
    CUSTOM = "custom"


class ChatGoal(int, Enum):
    """Chat persona/goal options for notebook configuration.

    Used with the s0tc2d RPC to configure chat behavior.
    """

    DEFAULT = 1  # General purpose research and brainstorming
    CUSTOM = 2  # Custom prompt (up to 10,000 characters)
    LEARNING_GUIDE = 3  # Educational focus with learning-oriented responses


class ChatResponseLength(int, Enum):
    """Chat response length options for notebook configuration.

    Used with the s0tc2d RPC to configure response verbosity.
    """

    DEFAULT = 1  # Standard response length
    LONGER = 4  # Verbose, detailed responses
    SHORTER = 5  # Concise, brief responses


class DriveMimeType(str, Enum):
    """Google Drive MIME types for source integration."""

    GOOGLE_DOC = "application/vnd.google-apps.document"
    GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
    GOOGLE_SHEETS = "application/vnd.google-apps.spreadsheet"
    PDF = "application/pdf"


class ExportType(int, Enum):
    """Export destination types for artifacts.

    Used when exporting artifacts to Google Docs or Sheets.
    """

    DOCS = 1  # Export to Google Docs
    SHEETS = 2  # Export to Google Sheets


class SourceStatus(int, Enum):
    """Processing status of a source.

    After adding a source to a notebook, it goes through processing
    before it can be used for chat or artifact generation.

    Values discovered from GET_NOTEBOOK API response at source[3][1].
    """

    PROCESSING = 1  # Source is being processed (indexing content)
    READY = 2  # Source is ready for use
    ERROR = 3  # Source processing failed


# Source status code to string mapping (uses int keys for mypy compatibility)
_SOURCE_STATUS_MAP: dict[int, str] = {
    SourceStatus.PROCESSING: "processing",
    SourceStatus.READY: "ready",
    SourceStatus.ERROR: "error",
}


def source_status_to_str(status_code: int | SourceStatus) -> str:
    """Convert source status code to human-readable string.

    This is the single source of truth for source status code to string mapping.
    Use this helper instead of inline conditionals to ensure consistency.

    Args:
        status_code: Status code as int or SourceStatus enum.

    Returns:
        String status: "processing", "ready", "error", or "unknown".
        Returns "unknown" for unrecognized codes (future-proofing).
    """
    return _SOURCE_STATUS_MAP.get(status_code, "unknown")
