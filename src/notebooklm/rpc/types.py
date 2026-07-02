"""RPC types and constants for NotebookLM API."""

from enum import Enum
from typing import Final

from .._env import DEFAULT_BASE_URL, get_base_url
from .overrides import (
    _load_rpc_overrides as _load_rpc_overrides,
)
from .overrides import (
    _logged_override_hashes as _logged_override_hashes,
)
from .overrides import (
    _parse_rpc_overrides as _parse_rpc_overrides,
)
from .overrides import (
    resolve_rpc_id as resolve_rpc_id,
)

# URL path for the streamed-chat endpoint. Not a batchexecute RPC ID — kept
# as a module-level constant rather than an ``RPCMethod`` member so the enum
# only contains real RPC IDs that ``scripts/check_rpc_health.py`` can probe.
_QUERY_ENDPOINT_PATH: Final[str] = (
    "/_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1."
    "LabsTailwindOrchestrationService/GenerateFreeFormStreamed"
)

# Backward-compatible default-host endpoint constants. Runtime code should use
# the lazy get_* helpers below so NOTEBOOKLM_BASE_URL is honored after import.
BATCHEXECUTE_URL = f"{DEFAULT_BASE_URL}/_/LabsTailwindUi/data/batchexecute"
QUERY_URL = f"{DEFAULT_BASE_URL}{_QUERY_ENDPOINT_PATH}"
UPLOAD_URL = f"{DEFAULT_BASE_URL}/upload/_/"


def get_batchexecute_url() -> str:
    """Return the NotebookLM batchexecute endpoint for the configured host."""
    return f"{get_base_url()}/_/LabsTailwindUi/data/batchexecute"


def get_query_url() -> str:
    """Return the NotebookLM streamed chat endpoint for the configured host."""
    return f"{get_base_url()}{_QUERY_ENDPOINT_PATH}"


def get_upload_url() -> str:
    """Return the NotebookLM upload endpoint for the configured host."""
    return f"{get_base_url()}/upload/_/"


class RPCMethod(str, Enum):
    """RPC method IDs for NotebookLM operations.

    These are obfuscated method identifiers used by the batchexecute API.
    Reverse-engineered from network traffic analysis.

    Many members carry a ``-> <Method>`` comment (trailing, or on the line
    above when it would overflow) naming the live
    ``/LabsTailwindOrchestrationService.<Method>`` endpoint the obfuscated id
    resolves to (recovered from Google's own id registry; entries on a
    *different* service spell out the service prefix). That backend name is the
    source of truth for what the RPC *actually does* — our enum member name is a
    reverse-engineered label that is sometimes narrower or older than the real
    method. Where the two diverge (e.g. ``LIST_NOTEBOOKS -> ListRecentlyViewedProjects``,
    ``GENERATE_MIND_MAP -> ActOnSources``), the comment is the authoritative semantics and
    the divergence is documented at the call site too. The member names
    themselves are part of the internal RPC contract and are intentionally left
    unchanged; only the clarifying comments were corrected.
    """

    # Notebook operations
    # -> ListRecentlyViewedProjects. Recency-ordered (most-recently-viewed first,
    # live-observed); whether it can omit an owned notebook (full vs recents) is
    # not independently confirmed.
    LIST_NOTEBOOKS = "wXbhsf"
    CREATE_NOTEBOOK = "CCqFvf"  # -> CreateProject
    GET_NOTEBOOK = "rLM1Ne"  # -> GetProject
    RENAME_NOTEBOOK = "s0tc2d"  # -> MutateProject (generic notebook mutator; see note below)
    DELETE_NOTEBOOK = "WWINqb"  # -> DeleteProjects (batch-capable; we send a single id)

    # Source operations
    ADD_SOURCE = "izAoDd"  # -> AddSources (batch-capable; we send a single source)
    # NOTE: the live registry path-extractor paired o4cbdc with ``AddTentativeSources``,
    # but that is almost certainly a mis-pairing — this id is the upload-register
    # step for a file already staged via the upload endpoint, whereas
    # AddTentativeSources is a discover-sources op. Treat the real method as
    # unconfirmed; do not relabel from the suspect pairing.
    ADD_SOURCE_FILE = "o4cbdc"  # Register uploaded file as source (live /Method unconfirmed)
    DELETE_SOURCE = "tGMBJ"  # -> DeleteSources (batch-capable; we send a single source)
    GET_SOURCE = "hizoJc"  # -> LoadSource
    REFRESH_SOURCE = "FLmJqe"  # -> RefreshSource
    CHECK_SOURCE_FRESHNESS = "yR9Yof"  # -> CheckSourceFreshness
    UPDATE_SOURCE = "b7Wfje"  # -> MutateSource

    # Source label operations (AI topic grouping)
    # -> CreateLabel. Multi-mode: AI auto-group (generate) AND manual create
    CREATE_LABEL = "agX4Bc"
    LIST_LABELS = "I3xc3c"  # -> GetLabels
    UPDATE_LABEL = "le8sX"  # -> MutateLabel. Rename / set emoji / add sources (fieldmask)
    DELETE_LABEL = "GyzE7e"  # -> DeleteLabels. Batch delete by id

    # Summary and query
    SUMMARIZE = "VfAZjd"  # -> GenerateNotebookGuide (guide w/ summary + suggested questions)
    GET_SOURCE_GUIDE = "tr032e"  # -> GenerateDocumentGuides
    GET_SUGGESTED_REPORTS = "ciyUvf"  # -> GenerateReportSuggestions. AI-suggested report formats

    # Artifact operations
    # -> CreateArtifact. Generate any artifact (audio, video, report, quiz, etc.)
    CREATE_ARTIFACT = "R7cb6c"
    LIST_ARTIFACTS = "gArtLc"  # -> ListArtifacts. List all artifacts in a notebook
    DELETE_ARTIFACT = "V5N4be"  # -> DeleteArtifact
    # -> UpdateArtifact (generic artifact updater; we only set the title)
    RENAME_ARTIFACT = "rc3d8d"
    # -> ExportToDrive (Google Drive only; Docs/Sheets are Drive destinations)
    EXPORT_ARTIFACT = "Krh3pd"
    SHARE_ARTIFACT = "RGP97b"  # -> LabsTailwindSharingService.ShareAudio
    # -> GetArtifact. A GENERIC single-artifact getter, not interactive-HTML-specific:
    # for type-4 artifacts it returns the quiz/flashcard HTML at [0][9][0] and the
    # interactive mind map node tree at [0][9][3]. The enum name reflects only the
    # interactive-HTML use we expose.
    GET_INTERACTIVE_HTML = "v9rmvd"
    REVISE_SLIDE = "KmcKPe"  # -> DeriveArtifact (generic derive op; we use it to revise a slide)
    # -> GenerateArtifact. Retry a failed Studio artifact in place (UI "Retry")
    RETRY_ARTIFACT = "Rytqqe"

    # Research — the whole family is backed by Google's "DiscoverSources" pipeline
    START_FAST_RESEARCH = "Ljjv0c"  # -> DiscoverSourcesManifold
    START_DEEP_RESEARCH = "QA9ei"  # -> DiscoverSourcesAsync
    POLL_RESEARCH = "e3bVqc"  # -> ListDiscoverSourcesJob
    IMPORT_RESEARCH = "LBwxtb"  # -> FinishDiscoverSourcesRun
    CANCEL_RESEARCH = "Zbrupe"  # -> CancelDiscoverSourcesJob

    # Note and mind map operations
    # -> ActOnSources (generic source-action op; we use it to generate a mind map)
    GENERATE_MIND_MAP = "yyryJe"
    CREATE_NOTE = "CYK0Xb"  # -> CreateNote
    # -> GetNotes (mind maps come back as JSON-bodied notes; see _note_service)
    GET_NOTES_AND_MIND_MAPS = "cFji9"
    UPDATE_NOTE = "cYAfTb"  # -> MutateNote
    DELETE_NOTE = "AH0mwd"  # -> DeleteNotes (batch-capable; we send a single note)

    # Conversation
    # -> ListChatSessions (we read only the most recent session id)
    GET_LAST_CONVERSATION_ID = "hPTbtc"
    GET_CONVERSATION_TURNS = "khqZz"  # -> ListChatTurns. Returns full Q&A turns for a conversation
    # -> DeleteChatTurns (deletes the chat turns; web UI's "Delete history")
    DELETE_CONVERSATION = "J7Gthc"
    # -> GeneratePromptSuggestions. AI-suggested questions/prompts to ask a notebook
    SUGGEST_PROMPTS = "otmP3b"

    # Sharing operations (notebook-level)
    SHARE_NOTEBOOK = "QDyure"  # -> LabsTailwindSharingService.ShareProject. Set notebook visibility
    # -> LabsTailwindSharingService.GetProjectDetails. Get notebook share settings
    GET_SHARE_STATUS = "JFMDGd"
    # Note: SET_SHARE_ACCESS uses RENAME_NOTEBOOK (s0tc2d) with different params

    # Additional notebook operations
    REMOVE_RECENTLY_VIEWED = "fejl7e"  # -> RemoveRecentlyViewedProject

    # User settings
    # -> GetOrCreateAccount (account-level; may create the account on first call)
    GET_USER_SETTINGS = "ZwVcOc"
    # -> MutateAccount (generic account mutator; we only set the output language)
    SET_USER_SETTINGS = "hT54vc"


class ArtifactTypeCode(int, Enum):
    """Integer codes for artifact types used in RPC calls.

    Codes 1, 2, 3, 4, 7, 8, and 9 are raw CREATE_ARTIFACT / LIST_ARTIFACTS
    values. MIND_MAP (5) is the library's synthetic code for note-backed mind
    maps returned by GET_NOTES_AND_MIND_MAPS; interactive mind maps are type 4 /
    variant 4.

    Note: This is an internal enum. Users should use ArtifactType (str enum)
    from notebooklm.types for a cleaner API.
    """

    AUDIO = 1
    REPORT = (
        2  # Includes: Briefing Doc, Study Guide, Blog Post, White Paper, Research Proposal, etc.
    )
    VIDEO = 3
    QUIZ = 4  # Also used for flashcards and interactive mind maps (variant 4)
    QUIZ_FLASHCARD = 4  # Alias for backward compatibility
    MIND_MAP = 5
    # Note: Type 6 appears unused in current API
    INFOGRAPHIC = 7
    SLIDE_DECK = 8
    DATA_TABLE = 9


# Variant codes at artifact_data[9][1][0], distinguishing sub-kinds within the
# type-4 (QUIZ) family. The interactive mind map is a studio artifact
# (type 4 / variant 4) created via CREATE_ARTIFACT, distinct from the
# note-backed mind map (surfaced with the synthetic type code 5).
FLASHCARDS_VARIANT: Final[int] = 1
QUIZ_VARIANT: Final[int] = 2
INTERACTIVE_MIND_MAP_VARIANT: Final[int] = 4


class ArtifactStatus(int, Enum):
    """Processing status of an artifact.

    Values correspond to artifact_data[4] in API responses.
    """

    PROCESSING = 1  # Artifact is being generated
    PENDING = 2  # Artifact is queued
    COMPLETED = 3  # Artifact is ready for use/download
    FAILED = 4  # Generation failed


_ARTIFACT_STATUS_MAP: dict[int, str] = {
    ArtifactStatus.PROCESSING: "in_progress",
    ArtifactStatus.PENDING: "pending",
    ArtifactStatus.COMPLETED: "completed",
    ArtifactStatus.FAILED: "failed",
}


def artifact_status_to_str(status_code: int) -> str:
    """Convert artifact status code to human-readable string.

    This is the single source of truth for status code to string mapping.
    Use this helper instead of inline conditionals to ensure consistency.

    Args:
        status_code: Numeric status from API response (artifact_data[4]).

    Returns:
        String status: "in_progress", "pending", "completed", "failed", or "unknown".
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
    CINEMATIC = 3


class VideoStyle(int, Enum):
    """Video visual style options."""

    AUTO_SELECT = 1
    CUSTOM = 0
    CLASSIC = 2
    WHITEBOARD = 3
    KAWAII = 9
    ANIME = 7
    WATERCOLOR = 6
    RETRO_PRINT = 8
    HERITAGE = 4
    PAPER_CRAFT = 5


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


class InfographicStyle(int, Enum):
    """Infographic visual style options.

    Values differ from VideoStyle — shared names (ANIME, KAWAII) have different codes.
    """

    AUTO_SELECT = 1
    SKETCH_NOTE = 2
    PROFESSIONAL = 3
    BENTO_GRID = 4
    EDITORIAL = 5
    INSTRUCTIONAL = 6
    BRICKS = 7
    CLAY = 8
    ANIME = 9
    KAWAII = 10
    SCIENTIFIC = 11


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

    All reports use ArtifactTypeCode.REPORT (2) but are differentiated
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


class ShareAccess(int, Enum):
    """Notebook access level for public sharing."""

    RESTRICTED = 0  # Only explicitly shared users
    ANYONE_WITH_LINK = 1  # Public link access


class ShareViewLevel(int, Enum):
    """What viewers can access when shared."""

    FULL_NOTEBOOK = 0  # Chat + sources + notes
    CHAT_ONLY = 1  # Chat interface only


class SharePermission(int, Enum):
    """User permission level for sharing."""

    OWNER = 1  # Full control (read-only, cannot assign)
    EDITOR = 2  # Can edit notebook
    VIEWER = 3  # Read-only access
    _REMOVE = 4  # Internal: remove user from share list


class SourceStatus(int, Enum):
    """Processing status of a source.

    After adding a source to a notebook, it goes through processing
    before it can be used for chat or artifact generation.

    Values discovered from GET_NOTEBOOK API response at source[3][1].
    """

    PROCESSING = 1  # Source is being processed (indexing content)
    READY = 2  # Source is ready for use
    ERROR = 3  # Source processing failed
    PREPARING = 5  # Source is being prepared/uploaded (pre-processing stage)


# Source status code to string mapping (uses int keys for mypy compatibility)
_SOURCE_STATUS_MAP: dict[int, str] = {
    SourceStatus.PROCESSING: "processing",
    SourceStatus.READY: "ready",
    SourceStatus.ERROR: "error",
    SourceStatus.PREPARING: "preparing",
}


def source_status_to_str(status_code: int | SourceStatus) -> str:
    """Convert source status code to human-readable string.

    This is the single source of truth for source status code to string mapping.
    Use this helper instead of inline conditionals to ensure consistency.

    Args:
        status_code: Status code as int or SourceStatus enum.

    Returns:
        String status: "processing", "ready", "error", "preparing", or "unknown".
        Returns "unknown" for unrecognized codes (future-proofing).
    """
    return _SOURCE_STATUS_MAP.get(status_code, "unknown")
