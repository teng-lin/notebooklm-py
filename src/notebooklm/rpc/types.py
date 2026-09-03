"""RPC wire types, constants, and compatibility re-exports."""

from enum import Enum
from typing import Final

from .._env import DEFAULT_BASE_URL, get_base_url
from .._types.enums import (  # noqa: F401 - compatibility re-exports
    _ARTIFACT_STATUS_MAP,
    _DISCOVERY_MODE_MAP,
    _DRIVE_SOURCE_STATUS_MAP,
    _SHARE_PERMISSION_MAP,
    _SOURCE_STATUS_MAP,
    ARTIFACT_STATUS_SUGGESTED_WIRE_NAME,
    FLASHCARDS_VARIANT,
    INTERACTIVE_MIND_MAP_VARIANT,
    QUIZ_VARIANT,
    SOURCE_STATUS_LABELS,
    ArtifactStatus,
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
    ChatGoal,
    ChatResponseLength,
    DiscoveryMode,
    DriveMimeType,
    DriveSourceStatus,
    ExportType,
    GrpcStatusCode,
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
    normalize_grpc_status,
    normalize_rpc_code,
    share_permission_to_str,
    source_status_to_str,
)
from .._web.wire.overrides import (
    _load_rpc_overrides as _load_rpc_overrides,
)
from .._web.wire.overrides import (
    _logged_override_hashes as _logged_override_hashes,
)
from .._web.wire.overrides import (
    _parse_rpc_overrides as _parse_rpc_overrides,
)
from .._web.wire.overrides import (
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
    COPY_NOTEBOOK = "te3DCe"  # -> CopyProject
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
    DELETE_SOURCE = "tGMBJ"  # -> DeleteSources (batch: [[[id1], [id2], ...]])
    GET_SOURCE = "hizoJc"  # -> LoadSource
    REFRESH_SOURCE = "FLmJqe"  # -> RefreshSource
    CHECK_SOURCE_FRESHNESS = "yR9Yof"  # -> CheckSourceFreshness
    UPDATE_SOURCE = "b7Wfje"  # -> MutateSource
    # -> RetrieveRelevantChunks. Ranked passage retrieval across all notebook
    # sources or an explicit source-id subset. The registration lives in a
    # lazy Web module rather than the entry bundle (#2283).
    RETRIEVE_RELEVANT_CHUNKS = "ASU5Oe"
    # -> AddSourcesAsync. Same request as AddSources; returns the queued stub
    # rows plus a per-source acknowledgement list without waiting for ingest
    # (#2283). Served to both front doors (live-verified 2026-09-01).
    ADD_SOURCES_ASYNC = "X1snv"
    # -> AppendSource. Appends a plain-text block to an existing source in place.
    APPEND_SOURCE = "QsNTEd"
    # -> CopySourcesAsync. Copies sources into another notebook; the response
    # maps each original id to its new Source row. The sync twin CopySources
    # (Z8UXi) is dead on both front doors — never model it (#2283).
    COPY_SOURCES = "R27wvc"
    # -> ListExpertIntelligenceContent. Lists the account's Google Play Books
    # library eligible to be added as sources ("Expert Intelligence", US/18+).
    # Web-verified live 2026-09-01; the Android tier serves the same method over
    # gRPC. Adding a listed book rides ADD_SOURCE / ADD_SOURCES_ASYNC on Web
    # and AddSources on Android with an ExpertIntelligenceContent spec — no add
    # method of its own. Android obtains a per-account Phenotype experiment
    # token and sends it in the required gRPC metadata (#2292).
    LIST_EXPERT_INTELLIGENCE_CONTENT = "mVtEUb"

    # Source label operations (AI topic grouping).
    # NOTE: account-level *collections* (notebook grouping) reuse these four
    # methods verbatim — a collection is a type-3 label with a null notebook
    # parent. See notebooklm._web.params.collections for the collection wire shapes.
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
    # -> CopyArtifactsAsync. Copies Studio artifacts into another notebook; the
    # response carries each new artifact row inline. The sync twin CopyArtifacts
    # (zVGIdd) is a no-op stub that reports success and copies nothing — never
    # model it (#2283).
    COPY_ARTIFACTS = "mKDdke"
    # -> GetArtifactCustomizationChoices. The Studio "Customize" option tables
    # (audio / video / slide-deck formats plus report presets). Account-level:
    # the server ignores the notebook id on both front doors.
    GET_CUSTOMIZATION_CHOICES = "sqTeoe"

    # Research — the whole family is backed by Google's "DiscoverSources" pipeline
    # -> DiscoverSources. The synchronous "Discover sources" dialog call: answers
    # in one round trip and also records a completed job the poll RPC lists (#2283).
    DISCOVER_SOURCES = "Es3dTe"
    START_FAST_RESEARCH = "Ljjv0c"  # -> DiscoverSourcesManifold
    START_DEEP_RESEARCH = "QA9ei"  # -> DiscoverSourcesAsync
    POLL_RESEARCH = "e3bVqc"  # -> ListDiscoverSourcesJob
    IMPORT_RESEARCH = "LBwxtb"  # -> FinishDiscoverSourcesRun
    CANCEL_RESEARCH = "Zbrupe"  # -> CancelDiscoverSourcesJob

    # Note and mind map operations
    # -> ActOnSources (generic source-action op; we use it to generate a mind map)
    GENERATE_MIND_MAP = "yyryJe"
    CREATE_NOTE = "CYK0Xb"  # -> CreateNote
    # -> GetNotes (mind maps come back as JSON-bodied notes; see _web.notes)
    GET_NOTES_AND_MIND_MAPS = "cFji9"
    UPDATE_NOTE = "cYAfTb"  # -> MutateNote
    DELETE_NOTE = "AH0mwd"  # -> DeleteNotes (batch-capable; we send a single note)

    # Conversation
    # -> ListChatSessions (we read only the most recent session id)
    GET_LAST_CONVERSATION_ID = "hPTbtc"
    GET_CONVERSATION_TURNS = "khqZz"  # -> ListChatTurns. Returns full Q&A turns for a conversation
    # -> GetChatSessionStatus. Returns an opaque generation token plus 1=idle / 2=active.
    GET_CHAT_SESSION_STATUS = "oXwmh"
    # -> CancelGeneration. Stops the active turn for a chat session; idempotent.
    CANCEL_GENERATION = "XgrPMd"
    # -> DeleteChatTurns (deletes the chat turns; web UI's "Delete history")
    DELETE_CONVERSATION = "J7Gthc"
    # -> GeneratePromptSuggestions. AI-suggested questions/prompts to ask a notebook
    SUGGEST_PROMPTS = "otmP3b"
    # -> NextStepSuggestions. Grounded follow-up questions for a notebook — the
    # standalone form of the block chat answers carry at index 5 (#2283).
    SUGGEST_NEXT_STEPS = "OcvKNc"

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
