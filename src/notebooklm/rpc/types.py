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
    DELETE_NOTEBOOK = "WWINqb"  # -> DeleteProjects (single id; batch shapes probed & rejected, see _notebooks.delete)

    # Source operations
    # -> AddSources. Single-item SDK methods send one source; the existing
    # MCP/REST batch endpoints deliberately send repeated URL entries (#2115).
    ADD_SOURCE = "izAoDd"
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

    # Source label operations (AI topic grouping).
    # NOTE: account-level *collections* (notebook grouping) reuse these four
    # methods verbatim — a collection is a type-3 label with a null notebook
    # parent. See notebooklm._collection.params for the collection wire shapes.
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
    DELETE_ARTIFACT = "V5N4be"  # -> DeleteArtifact (single id; batch shapes probed & rejected)
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


class GrpcStatusCode(int, Enum):
    """Canonical gRPC status codes (``google.rpc.Code``) seen on the wire.

    The batchexecute backend embeds one of these at index 5 of a ``wrb.fr``
    entry when an RPC returns null result data — the bare single-element form
    ``[code]`` observed in issues #114 and #294.

    Deliberately distinct from :class:`notebooklm.rpc.decoder.RPCErrorCode`,
    which is an HTTP-style namespace (``NOT_FOUND = 404``). The two share
    member names but not values, so code that compares a wire status must say
    which namespace it means: ``GrpcStatusCode.NOT_FOUND`` is ``5``.

    This is the single source of truth for those numbers. Before it existed the
    literals were spelled inline at three layers — the decoder's ``(5, 7)``
    routing, the neutral error classifier's ``== 5``, and the notebook
    not-found translation — so a reader had to know from context whether a bare
    ``5`` meant gRPC NOT_FOUND or something else.
    """

    OK = 0
    CANCELLED = 1
    UNKNOWN = 2
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    ABORTED = 10
    OUT_OF_RANGE = 11
    UNIMPLEMENTED = 12
    INTERNAL = 13
    UNAVAILABLE = 14
    DATA_LOSS = 15
    UNAUTHENTICATED = 16


def normalize_rpc_code(code: str | int | None) -> int | None:
    """Coerce an ``RPCError.rpc_code`` to an ``int``, or ``None``.

    ``rpc_code`` is typed ``str | int | None`` and carries three different
    kinds of value: a numeric status (gRPC on the decoder's wire path, or an
    HTTP status on the transport path), a non-numeric label such as
    ``"USER_DISPLAYABLE_ERROR"``, or ``None`` when the error came from a layer
    that never saw a status. A string ``"5"`` has to compare equal to the
    integer status, and a non-numeric label has to answer ``None`` rather than
    raise.

    Deliberately *not* restricted to :class:`GrpcStatusCode`: callers that
    range-check HTTP statuses (``500 <= code < 600``) need the raw integer
    through. Use :func:`normalize_grpc_status` when the question is
    specifically "which gRPC status is this?".

    ``bool`` is rejected explicitly: it is a subclass of ``int``, so ``True``
    would otherwise coerce to ``1``.
    """
    if code is None or isinstance(code, bool):
        return None
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def normalize_grpc_status(code: str | int | None) -> GrpcStatusCode | None:
    """Normalize an ``RPCError.rpc_code`` to a :class:`GrpcStatusCode`.

    The narrower companion to :func:`normalize_rpc_code`: it answers "is this
    gRPC status X?" for callers that want the semantic comparison, and returns
    ``None`` for anything outside the canonical table — including HTTP statuses
    such as ``500``, which are numeric but are not gRPC codes.

    Args:
        code: The raw ``rpc_code`` off an :class:`~notebooklm.exceptions.RPCError`.

    Returns:
        The matching :class:`GrpcStatusCode`, or ``None`` when the value is
        absent, non-numeric, or not a code this table knows.
    """
    as_int = normalize_rpc_code(code)
    if as_int is None:
        return None
    try:
        return GrpcStatusCode(as_int)
    except ValueError:
        return None


class ArtifactTypeCode(int, Enum):
    """Integer codes for artifact types used in RPC calls.

    Codes 1 through 10 are backend ``ArtifactType`` values. The library also
    uses the genuine MIND_MAP code (5) when adapting note-backed mind maps from
    GET_NOTES_AND_MIND_MAPS into the public artifact listing; interactive mind
    maps are APP/QUIZ-family type 4 / variant 4.

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
    FANTASY_MAP = 6
    INFOGRAPHIC = 7
    SLIDE_DECK = 8
    DATA_TABLE = 9
    FILE = 10


# Variant codes at artifact_data[9][1][0], distinguishing sub-kinds within the
# type-4 (QUIZ) family. The interactive mind map is a studio artifact
# (type 4 / variant 4) created via CREATE_ARTIFACT, distinct from the
# note-backed mind map (adapted using the genuine backend mind-map code 5).
FLASHCARDS_VARIANT: Final[int] = 1
QUIZ_VARIANT: Final[int] = 2
INTERACTIVE_MIND_MAP_VARIANT: Final[int] = 4


class ArtifactStatus(int, Enum):
    """Processing status of an artifact.

    Values correspond to artifact_data[4] in API responses and mirror the
    backend's own ``ArtifactStatus`` enum (recovered in ``docs/mobile/enums.txt``
    and pinned by ``tests/_guardrails/_wire_contract.py``):

    * 0 — ``ARTIFACT_STATUS_UNKNOWN`` -> :attr:`UNKNOWN` -> ``"unknown"``
    * 1 — ``ARTIFACT_STATUS_INITIALIZED`` -> :attr:`PENDING` -> ``"pending"``
    * 2 — ``ARTIFACT_STATUS_PROCESSING`` -> :attr:`PROCESSING` -> ``"in_progress"``
    * 3 — ``ARTIFACT_STATUS_READY`` -> :attr:`COMPLETED` -> ``"completed"``
    * 4 — ``ARTIFACT_STATUS_FAILED`` -> :attr:`FAILED` -> ``"failed"``
    * 5 — ``ARTIFACT_STATUS_SUGGESTED`` -> :attr:`SUGGESTED` -> ``"suggested"``
    * 6 — ``ARTIFACT_PENDING_REVIEW`` -> :attr:`PENDING_REVIEW` -> ``"pending_review"``

    Member *names* describe the lifecycle phase, not the backend spelling, so
    they are stable across this correction: ``PENDING`` has always meant
    "queued, worker not started" and ``PROCESSING`` "actively generating". Only
    the wire integers behind them moved — until issue #2127 they were swapped
    (1 was read as PROCESSING and 2 as PENDING), which made
    :attr:`~notebooklm.types.Artifact.is_pending` and
    :attr:`~notebooklm.types.Artifact.is_processing` answer each other's
    question during the transitional window.
    """

    UNKNOWN = 0  # Status unset/unrecognized by the backend
    PENDING = 1  # ARTIFACT_STATUS_INITIALIZED — row created, worker not started
    PROCESSING = 2  # Artifact is actively being generated
    COMPLETED = 3  # Artifact is ready for use/download
    FAILED = 4  # Generation failed
    SUGGESTED = 5  # A suggestion, not a real artifact; LIST_ARTIFACTS filters these out
    # Backend spelling is ARTIFACT_PENDING_REVIEW (no ``ARTIFACT_STATUS_``
    # prefix). Semantics are UNCONFIRMED: the member exists in the backend enum
    # dump but has never been observed on this transport (0 of 42 live
    # artifacts, 0 of 301 recorded rows). It is modeled so a caller can *detect*
    # it distinctly instead of having it collapse into "unknown" — do not infer
    # a workflow from the name. Despite the shared prefix it is UNRELATED to
    # PENDING above; the collision is the backend's own spelling, kept so the
    # member stays greppable against the recovered dump.
    PENDING_REVIEW = 6


#: Wire spelling of :attr:`ArtifactStatus.SUGGESTED` for the server-side
#: LIST_ARTIFACTS filter expression. The backend filter grammar takes the
#: symbolic enum-value name, not the integer, so the string cannot be derived
#: from the member value. ``tests/_guardrails/test_wire_contract.py`` asserts
#: this equals the recovered dump's name for code 5, so a backend rename
#: cannot leave the filter stale (a stale filter matches nothing, and
#: suggestion rows would start appearing in every listing).
ARTIFACT_STATUS_SUGGESTED_WIRE_NAME: Final[str] = "ARTIFACT_STATUS_SUGGESTED"


_ARTIFACT_STATUS_MAP: dict[int, str] = {
    ArtifactStatus.UNKNOWN: "unknown",
    ArtifactStatus.PENDING: "pending",
    ArtifactStatus.PROCESSING: "in_progress",
    ArtifactStatus.COMPLETED: "completed",
    ArtifactStatus.FAILED: "failed",
    ArtifactStatus.SUGGESTED: "suggested",
    ArtifactStatus.PENDING_REVIEW: "pending_review",
}


def artifact_status_to_str(status_code: int) -> str:
    """Convert artifact status code to human-readable string.

    This is the single source of truth for status code to string mapping.
    Use this helper instead of inline conditionals to ensure consistency.

    Args:
        status_code: Numeric status from API response (artifact_data[4]).

    Returns:
        String status: "unknown", "pending", "in_progress", "completed",
        "failed", "suggested", or "pending_review". Returns "unknown" for
        unrecognized codes (future-proofing), which is also what code ``0``
        (``ARTIFACT_STATUS_UNKNOWN``) maps to.
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
    SHORT = 4  # vertical short-form video (bundle label "Short"); fixed style


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

    Values match the backend ``QuizGenerationOptions.QuestionQuantity`` /
    ``FlashcardsGenerationOptions.CardQuantity`` enums, both of which declare
    FEWER (1), STANDARD (2) and MORE (3). ``MORE`` was previously an alias for
    ``STANDARD`` on the (incorrect) belief that the API could not express the
    distinction; the backend accepts and persists ``cardQuantity = 3`` (#2117).
    """

    FEWER = 1
    STANDARD = 2
    MORE = 3


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
    # Returned by LIST_ARTIFACTS as a report kind. Read-only for now: unlike
    # the three static generation presets, its generation directive has not
    # been recovered, so build_report_artifact_params rejects it explicitly.
    CONCEPT_EXPLANATION = "concept_explanation"
    CUSTOM = "custom"


class MagicArtifactType(int, Enum):
    """Artifact/suggestion surface codes used by NextStepSuggestions."""

    UNSPECIFIED = 0
    MINDMAP = 1
    AUDIO_OVERVIEW = 2
    VIDEO_OVERVIEW = 3
    NOTE = 4
    TABLE = 5
    LINE_CHART = 6
    FLASHCARDS = 7
    REPORT = 8
    CONVERSATIONAL_TEXT_CHIP = 9
    VIDEO_OVERVIEW_TEXT_CHIP = 10
    AUDIO_OVERVIEW_TEXT_CHIP = 11
    REPORT_TEXT_CHIP = 12
    FLASHCARDS_TEXT_CHIP = 13
    QUIZ_TEXT_CHIP = 14
    SOURCE_DISCOVERY_TEXT_CHIP = 15


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
    """User permission level on a notebook.

    This is the wire enum the backend uses for *both* halves of the sharing
    story, under two different proto names:

    * ``SharedUser.permission`` — a collaborator's level, from
      ``GET_SHARE_STATUS`` (``SharePermission``).
    * ``Notebook.role`` — the *calling account's own* level, from
      ``ProjectMetadata.userRole`` (tag 1, ``meta[0]``) on every
      ``LIST_NOTEBOOKS`` / ``GET_NOTEBOOK`` row. The proto names its members
      ``OWNER``/``WRITER``/``READER``, but the integers are identical, so this
      one enum covers both rather than shipping a value-identical twin (#2125).
    """

    OWNER = 1  # Full control (read-only, cannot assign)
    EDITOR = 2  # Can edit notebook (proto: WRITER)
    VIEWER = 3  # Read-only access (proto: READER)
    _REMOVE = 4  # Internal: remove user from share list (proto: NOT_SHARED)


# Share permission code to string mapping (uses int keys for mypy compatibility).
# ``_REMOVE`` is deliberately absent: it is a write-only sentinel with no
# display meaning, so it degrades to "unknown" like any unmapped code.
_SHARE_PERMISSION_MAP: dict[int, str] = {
    SharePermission.OWNER: "owner",
    SharePermission.EDITOR: "editor",
    SharePermission.VIEWER: "viewer",
}


def share_permission_to_str(permission: int | SharePermission) -> str:
    """Convert a share permission / notebook role code to a human-readable string.

    This is the single source of truth for permission code to string mapping
    (the sibling of :func:`source_status_to_str`). Use this helper instead of
    inline conditionals so every adapter's label stays in lock-step.

    Args:
        permission: Permission code as int or SharePermission enum.

    Returns:
        String permission: ``"owner"``, ``"editor"``, or ``"viewer"``.
        Returns ``"unknown"`` for unrecognized codes (future-proofing).
    """
    return _SHARE_PERMISSION_MAP.get(permission, "unknown")


class SourceStatus(int, Enum):
    """Processing status of a source.

    After adding a source to a notebook, it goes through processing
    before it can be used for chat or artifact generation.

    Values discovered from GET_NOTEBOOK API response at source[3][1].
    """

    UNKNOWN = -1  # Status is absent, malformed, or not yet mapped
    PROCESSING = 1  # Source is being processed (indexing content)
    READY = 2  # Source is ready for use
    ERROR = 3  # Source processing failed
    PREPARING = 5  # Source is being prepared/uploaded (pre-processing stage)


# Source status code to string mapping (uses int keys for mypy compatibility)
_SOURCE_STATUS_MAP: dict[int, str] = {
    SourceStatus.UNKNOWN: "unknown",
    SourceStatus.PROCESSING: "processing",
    SourceStatus.READY: "ready",
    SourceStatus.ERROR: "error",
    SourceStatus.PREPARING: "preparing",
}


#: Every label :func:`source_status_to_str` can return, in enum order — the
#: canonical vocabulary for a status filter, so a CLI ``--status`` choice cannot
#: drift from the enum the rows are rendered from. Derived from the map rather
#: than restated, which is the whole point: a new ``SourceStatus`` member becomes
#: filterable the moment it is mapped. (The MCP tool's ``Literal`` cannot be
#: derived — ``Literal`` needs static values — and is guarded for parity by
#: ``tests/unit/mcp/test_sources.py::test_source_list_status_filter_enum_parity``.)
SOURCE_STATUS_LABELS: tuple[str, ...] = tuple(_SOURCE_STATUS_MAP.values())


def source_status_to_str(status_code: int | SourceStatus) -> str:
    """Convert source status code to human-readable string.

    This is the single source of truth for source status code to string mapping.
    Use this helper instead of inline conditionals to ensure consistency.

    Args:
        status_code: Status code as int or SourceStatus enum.

    Returns:
        String status: "unknown", "processing", "ready", "error", or "preparing".
        Returns "unknown" for unrecognized codes (future-proofing).
    """
    return _SOURCE_STATUS_MAP.get(status_code, "unknown")


class DriveSourceStatus(int, Enum):
    """Drive-side health of a Drive-backed source (``UserDriveSourceStatus``).

    The sibling of :class:`SourceStatus`, and a *different question*.
    :class:`SourceStatus` reports NotebookLM's own ingestion pipeline
    (``SourceSettings.status``, tag 2 → ``source[3][1]``), which completes —
    and stays complete — even after the underlying Drive file is deleted or
    unshared. Drive-side health lives only here
    (``SourceSettings.userDriveSourceStatus``, tag 4 → ``source[3][3]``), so a
    source can read ``READY`` while its Drive file is gone (#2111).

    Only Drive-backed sources carry the slot; every other source decodes to
    ``None`` (see :attr:`notebooklm.Source.drive_status`).

    The backend's ``DRIVE_SOURCE_STATUS_UNSPECIFIED`` (0) is deliberately NOT
    modelled here. proto3 omits zero-valued fields, so it normally arrives as
    an absent slot, and it means exactly what an absent slot means — "no claim".
    Modelling it would give that one state two representations (``None`` and a
    falsy member), so :attr:`notebooklm._row_adapters.sources.SourceRow.drive_status`
    normalizes an explicit ``0`` to ``None`` instead. Recorded in
    ``tests/_guardrails/_wire_contract.py::ENUM_GAPS``.

    Values are the backend ``UserDriveSourceStatus`` enum, recovered from the
    official Android app (``docs/mobile/enums.txt``) and pinned by
    ``tests/_guardrails/_wire_contract.py``.

    .. warning::
       Only :attr:`ACTIVE` has been observed on the wire (4 Drive rows in a
       409-row live capture; see
       ``docs/notes/web-rpc-vs-mobile-grpc-audit-2026-08-07.md`` §1.6).
       The degraded members below are read off the backend enum, **not**
       from an observed response — producing them requires deliberately
       breaking access to a real Drive file. Treat their exact wire behaviour
       as unverified.
    """

    #: Client-side sentinel — the slot carried a code this client does not
    #: model (or a non-integer). Never sent by the backend. ``None`` means
    #: "no Drive status on this row"; this means "present but unmappable".
    UNKNOWN = -1
    #: The account can no longer read the Drive file (e.g. unshared).
    INACCESSIBLE = 1
    #: The Drive file is being (re-)synced into the notebook.
    SYNCING = 2
    #: The Drive file is present and in sync. The only value observed live.
    ACTIVE = 3
    #: The Drive file has been deleted.
    DELETED = 4
    #: Gemini/AI access to the file is denied (e.g. a Workspace policy).
    GEN_AI_ACCESS_DENIED = 5


# Drive source status code to string mapping (uses int keys for mypy
# compatibility), the sibling of ``_SOURCE_STATUS_MAP``.
_DRIVE_SOURCE_STATUS_MAP: dict[int, str] = {
    DriveSourceStatus.UNKNOWN: "unknown",
    DriveSourceStatus.INACCESSIBLE: "inaccessible",
    DriveSourceStatus.SYNCING: "syncing",
    DriveSourceStatus.ACTIVE: "active",
    DriveSourceStatus.DELETED: "deleted",
    DriveSourceStatus.GEN_AI_ACCESS_DENIED: "gen_ai_access_denied",
}


def drive_source_status_to_str(status_code: int | DriveSourceStatus) -> str:
    """Convert a Drive source status code to a human-readable string.

    The single source of truth for Drive-status code to string mapping (the
    sibling of :func:`source_status_to_str`). Use this helper instead of
    inline conditionals so every adapter's label stays in lock-step.

    Args:
        status_code: Status code as int or :class:`DriveSourceStatus`.

    Returns:
        One of ``"inaccessible"``, ``"syncing"``, ``"active"``,
        ``"deleted"``, ``"gen_ai_access_denied"``. Returns ``"unknown"`` for
        the client sentinel and for unrecognized codes (future-proofing) —
        including the backend's unmodelled ``0``, which the decoder normalizes
        to ``None`` before it can reach this helper.
    """
    return _DRIVE_SOURCE_STATUS_MAP.get(status_code, "unknown")


class DiscoveryMode(int, Enum):
    """How a research run searched for sources (backend ``DiscoveryMode``).

    Echoed back by ``POLL_RESEARCH`` at ``task_info[2]`` — the same enum the
    ``START_FAST_RESEARCH`` / ``START_DEEP_RESEARCH`` request params carry, so
    the mode a run is executing under is confirmable from the response rather
    than only remembered from the request (#2122).

    The backend's ``DISCOVERY_MODE_UNSPECIFIED`` (0) is deliberately NOT
    modelled, for the same reason as
    :class:`DriveSourceStatus`: proto3 omits zero-valued fields, so it arrives
    as an absent slot and means what an absent slot means. The decoder
    normalizes an explicit ``0`` to ``None``. Recorded in
    ``tests/_guardrails/_wire_contract.py::ENUM_GAPS``.

    Values are the backend ``DiscoveryMode`` enum recovered from the official
    Android app (``docs/mobile/enums.txt``) and pinned by
    ``tests/_guardrails/_wire_contract.py``.

    .. warning::
       Only :attr:`DEFAULT_LLM_SEARCH` and :attr:`DEEP_RESEARCH` have been
       observed: ``1`` on every fast run (live probe #2122 plus 6/6 fast
       cassette rows) and ``5`` on every deep run (3/3 cassette rows). This
       client only ever *sends* those two. The remaining members come from the
       recovered enum and are decode-only forward compatibility.
    """

    #: Client-side sentinel — the slot carried a code this client does not
    #: model (or a non-integer). Never sent by the backend. ``None`` means
    #: "the poll made no mode claim"; this means "present but unmappable".
    UNKNOWN = -1
    #: The default LLM-driven search. Sent + observed for ``mode="fast"``.
    DEFAULT_LLM_SEARCH = 1
    #: Raw (non-LLM) search. Never sent by this client.
    RAW_SEARCH = 2
    #: "Curious" LLM search. Never sent by this client.
    CURIOUS_SEARCH = 3
    #: "Curious" raw search. Never sent by this client.
    CURIOUS_RAW_SEARCH = 4
    #: Multi-step deep research. Sent + observed for ``mode="deep"``.
    DEEP_RESEARCH = 5
    #: Lightweight LLM search. Never sent by this client.
    LITE_LLM_SEARCH = 6


# Discovery-mode code to string mapping (int keys for mypy compatibility),
# the sibling of ``_DRIVE_SOURCE_STATUS_MAP``.
_DISCOVERY_MODE_MAP: dict[int, str] = {
    DiscoveryMode.UNKNOWN: "unknown",
    DiscoveryMode.DEFAULT_LLM_SEARCH: "default_llm_search",
    DiscoveryMode.RAW_SEARCH: "raw_search",
    DiscoveryMode.CURIOUS_SEARCH: "curious_search",
    DiscoveryMode.CURIOUS_RAW_SEARCH: "curious_raw_search",
    DiscoveryMode.DEEP_RESEARCH: "deep_research",
    DiscoveryMode.LITE_LLM_SEARCH: "lite_llm_search",
}


def discovery_mode_to_str(mode: int | DiscoveryMode) -> str:
    """Convert a research discovery-mode code to a human-readable string.

    The single source of truth for discovery-mode code to string mapping (the
    sibling of :func:`drive_source_status_to_str`). Use this helper instead of
    inline conditionals so every adapter's label stays in lock-step.

    Args:
        mode: Discovery mode as int or :class:`DiscoveryMode`.

    Returns:
        The lower-snake-case member name (``"default_llm_search"`` /
        ``"deep_research"`` / …). Returns ``"unknown"`` for the client sentinel
        and for unrecognized codes — including the backend's unmodelled ``0``,
        which the decoder normalizes to ``None`` before it can reach here.
    """
    return _DISCOVERY_MODE_MAP.get(mode, "unknown")
