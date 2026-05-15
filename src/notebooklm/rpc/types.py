"""RPC types and constants for NotebookLM API."""

import json
import logging
import os
from enum import Enum
from functools import lru_cache
from typing import Final

from .._env import DEFAULT_BASE_URL, get_base_url

logger = logging.getLogger(__name__)

# Track which override dicts we've already logged at INFO level, keyed by the
# hash of the canonical (sorted) item tuple. This dedupes multi-client tests
# while still emitting one INFO line per *distinct* override set in a process.
_logged_override_hashes: set[int] = set()


@lru_cache(maxsize=8)
def _parse_rpc_overrides(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse and validate the raw env-var string, returning an immutable mapping.

    Cached on the raw string so JSON parsing — and the WARNING emitted for
    malformed input — happens once per distinct env value rather than once
    per RPC call. Returns a tuple-of-pairs (immutable, hashable) so the
    caller can rebuild a fresh dict without mutating cached state.
    """
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("NOTEBOOKLM_RPC_OVERRIDES is not valid JSON: %s", exc)
        return ()
    if not isinstance(data, dict):
        logger.warning(
            "NOTEBOOKLM_RPC_OVERRIDES must be a JSON object mapping "
            "method names to RPC IDs, got %s",
            type(data).__name__,
        )
        return ()
    # Reject keys that don't match an RPCMethod enum member. Without this
    # gate, a typo like ``"LIST_NOTEBOOK"`` would silently no-op (resolver
    # only matches exact enum names) while the INFO log line proudly claims
    # the override was applied — making the escape hatch look live while
    # calls keep using canonical IDs. ``RPCMethod`` is defined later in this
    # module; the lookup is deferred to call time so the forward reference
    # is resolved by the time any caller invokes us.
    valid_methods = set(RPCMethod.__members__)
    normalized: list[tuple[str, str]] = []
    null_keys: list[str] = []
    for k, v in data.items():
        if v is None:
            # ``json.loads('{"X": null}')`` would coerce to ``str(None) ==
            # "None"`` — a literal four-character string on the wire, almost
            # certainly not what the user meant. Drop and warn loudly.
            null_keys.append(str(k))
            continue
        normalized.append((str(k), str(v)))
    if null_keys:
        logger.warning(
            "Ignoring NOTEBOOKLM_RPC_OVERRIDES entries with null values "
            "(provide a non-null RPC id string): %s",
            ", ".join(sorted(null_keys)),
        )
    unknown = sorted(k for k, _ in normalized if k not in valid_methods)
    if unknown:
        logger.warning(
            "Ignoring unknown NOTEBOOKLM_RPC_OVERRIDES method names (not in RPCMethod): %s",
            ", ".join(unknown),
        )
    return tuple((k, v) for k, v in normalized if k in valid_methods)


def _load_rpc_overrides() -> dict[str, str]:
    """Parse ``NOTEBOOKLM_RPC_OVERRIDES`` into a ``{method_name: rpc_id}`` map.

    The env var is a JSON object mapping :class:`RPCMethod` member names
    (e.g. ``"LIST_NOTEBOOKS"``) to override RPC ID strings. Any malformed
    input — invalid JSON, non-object top-level (array, string, etc.) —
    is logged at WARNING and treated as no overrides.

    Returns an empty dict when the env var is unset or invalid.
    """
    return dict(_parse_rpc_overrides(os.environ.get("NOTEBOOKLM_RPC_OVERRIDES")))


def resolve_rpc_id(method_name: str, canonical_id: str) -> str:
    """Return the override RPC id for ``method_name`` when applicable, else ``canonical_id``.

    Overrides are sourced from the ``NOTEBOOKLM_RPC_OVERRIDES`` env var and
    are gated on the configured base host being on the allowlist
    (:data:`notebooklm._env._ALLOWED_BASE_HOSTS`). When the host is off the
    allowlist — which the strict ``get_base_url()`` validator already
    enforces, but we re-check here as defense in depth — overrides are
    ignored to avoid leaking custom RPC IDs to untrusted endpoints.

    The first time a distinct override set is consulted in a process, the
    full mapping is logged at INFO level so operators can confirm the
    config they intended is live. Subsequent calls with the same set are
    silent to avoid spamming multi-client tests / long-running daemons.

    Args:
        method_name: The :class:`RPCMethod` enum member name
            (e.g. ``"LIST_NOTEBOOKS"``).
        canonical_id: The fallback RPC ID — usually
            ``RPCMethod.<member>.value`` — returned when no override applies.

    Returns:
        Either the override RPC id (when an override is configured AND the
        host is on the allowlist) or ``canonical_id``.
    """
    # Local import to avoid a circular import at module-load time —
    # ``_env`` is dependency-free, but the public package ``notebooklm``
    # imports ``rpc.types`` during init, and ``_env`` ships from the same
    # package.
    from .._env import _ALLOWED_BASE_HOSTS, get_base_host

    try:
        host = get_base_host()
    except ValueError:
        # ``get_base_host()`` raises ``ValueError`` for a malformed
        # ``NOTEBOOKLM_BASE_URL`` (the only failure mode it documents).
        # Treat overrides as disabled in that case rather than crashing the
        # resolver — the URL builder itself will surface the real error to
        # the caller. A broader ``except Exception`` would mask unrelated
        # bugs in ``get_base_host`` during development.
        return canonical_id
    if host not in _ALLOWED_BASE_HOSTS:
        return canonical_id

    overrides = _load_rpc_overrides()
    if not overrides:
        return canonical_id

    key = hash(tuple(sorted(overrides.items())))
    if key not in _logged_override_hashes:
        _logged_override_hashes.add(key)
        logger.info(
            "NOTEBOOKLM_RPC_OVERRIDES applied: %s",
            ", ".join(f"{k}={v}" for k, v in sorted(overrides.items())),
        )

    return overrides.get(method_name, canonical_id)


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

    # Summary and query
    SUMMARIZE = "VfAZjd"
    GET_SOURCE_GUIDE = "tr032e"
    GET_SUGGESTED_REPORTS = "ciyUvf"  # AI-suggested report formats

    # Artifact operations
    CREATE_ARTIFACT = "R7cb6c"  # Generate any artifact (audio, video, report, quiz, etc.)
    LIST_ARTIFACTS = "gArtLc"  # List all artifacts in a notebook
    DELETE_ARTIFACT = "V5N4be"
    RENAME_ARTIFACT = "rc3d8d"
    EXPORT_ARTIFACT = "Krh3pd"
    SHARE_ARTIFACT = "RGP97b"
    GET_INTERACTIVE_HTML = "v9rmvd"  # Fetch quiz/flashcard HTML content
    REVISE_SLIDE = "KmcKPe"  # Revise individual slide with prompt

    # Research
    START_FAST_RESEARCH = "Ljjv0c"
    START_DEEP_RESEARCH = "QA9ei"
    POLL_RESEARCH = "e3bVqc"
    IMPORT_RESEARCH = "LBwxtb"

    # Note and mind map operations
    GENERATE_MIND_MAP = "yyryJe"  # Generate mind map from sources
    CREATE_NOTE = "CYK0Xb"
    GET_NOTES_AND_MIND_MAPS = "cFji9"  # Returns both notes and mind maps
    UPDATE_NOTE = "cYAfTb"
    DELETE_NOTE = "AH0mwd"

    # Conversation
    GET_LAST_CONVERSATION_ID = "hPTbtc"  # Returns only the most recent conversation ID
    GET_CONVERSATION_TURNS = "khqZz"  # Returns full Q&A turns for a conversation

    # Sharing operations (notebook-level)
    SHARE_NOTEBOOK = "QDyure"  # Set notebook visibility (restricted/anyone with link)
    GET_SHARE_STATUS = "JFMDGd"  # Get notebook share settings
    # Note: SET_SHARE_ACCESS uses RENAME_NOTEBOOK (s0tc2d) with different params

    # Additional notebook operations
    REMOVE_RECENTLY_VIEWED = "fejl7e"

    # User settings
    GET_USER_SETTINGS = "ZwVcOc"  # Get user settings including output language
    SET_USER_SETTINGS = "hT54vc"  # Set user settings (e.g., output language)
    GET_USER_TIER = "ozz5Z"  # Get NotebookLM subscription tier from homepage context


class ArtifactTypeCode(int, Enum):
    """Integer codes for artifact types used in RPC calls.

    These are the raw codes used in the CREATE_ARTIFACT (R7cb6c) RPC call.
    Values correspond to artifact_data[2] in API responses.

    Note: This is an internal enum. Users should use ArtifactType (str enum)
    from notebooklm.types for a cleaner API.
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


# Deprecated alias for backward compatibility
StudioContentType = ArtifactTypeCode


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
        String status: "processing", "ready", "error", or "unknown".
        Returns "unknown" for unrecognized codes (future-proofing).
    """
    return _SOURCE_STATUS_MAP.get(status_code, "unknown")
