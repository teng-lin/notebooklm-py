"""Hand-authored semantic operation catalog specifications and dispositions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from notebooklm._operations import CallPolicy, Operation
from notebooklm.rpc import RPCMethod

NativeKey = tuple[RPCMethod, str | None]


class Disposition(str, Enum):
    """Reviewed current disposition for a semantic operation."""

    SEMANTIC = "semantic"
    COMPOSITE = "composite"
    LEGACY_PRIVATE = "legacy_private"


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """The hand-authored portion of one operation catalog row."""

    operation: Operation
    policy: CallPolicy
    owner: str
    route_context: str
    composite_behavior: str
    public_methods: tuple[str, ...] = ()
    native_bindings: tuple[NativeKey, ...] = ()
    web_paths: tuple[str, ...] = ()
    disposition: Disposition = Disposition.SEMANTIC
    app_authorities: tuple[str, ...] = ()
    known_divergence: str | None = None
    recency_effect: str = "none"


def _b(method: RPCMethod, variant: str | None = None) -> NativeKey:
    return method, variant


def _p(namespace: str, *methods: str) -> tuple[str, ...]:
    return tuple(f"{namespace}.{method}" for method in methods)


_CREATE_ARTIFACT = (_b(RPCMethod.CREATE_ARTIFACT),)
_APP_DOWNLOAD_DIVERGENCE = (
    "_app/download.py owns selection/conflict/filesystem choreography while the facade owns "
    "network reads. P4.2 starts a separate budget at each facade list/download operation; "
    "P5 keeps one backend execution path."
)

# This table is the catalog's reviewed source.  Do not add copied RPC ids,
# idempotency values, golden pointers, or source locations here; those belong to
# the derivation below.  A spec may share a native binding with another spec
# where one web RPC is genuinely polymorphic (labels/collections are the main
# example).
OPERATION_SPECS: tuple[OperationSpec, ...] = (
    OperationSpec(
        Operation.NOTEBOOK_LIST,
        CallPolicy.READ,
        "NotebookService",
        "account",
        "Reads the recency-ordered notebook collection without touching recency.",
        _p("notebooks", "list"),
        (_b(RPCMethod.LIST_NOTEBOOKS),),
    ),
    OperationSpec(
        Operation.NOTEBOOK_GET,
        CallPolicy.MUTATION,
        "NotebookService",
        "notebook",
        "GET_NOTEBOOK reads a notebook and writes lastViewedTime as a user-visible side effect.",
        _p("notebooks", "get", "get_or_none", "get_raw", "get_source_ids"),
        (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="exactly one GET_NOTEBOOK per direct call",
    ),
    OperationSpec(
        Operation.NOTEBOOK_CREATE,
        CallPolicy.MUTATION,
        "NotebookService",
        "account",
        "Takes an unconditional LIST_NOTEBOOKS baseline, creates once, probes on ambiguity, and "
        "may read account settings and list again only to diagnose quota failures.",
        _p("notebooks", "create"),
        (
            _b(RPCMethod.CREATE_NOTEBOOK),
            _b(RPCMethod.LIST_NOTEBOOKS),
            _b(RPCMethod.GET_USER_SETTINGS),
        ),
    ),
    OperationSpec(
        Operation.NOTEBOOK_UPDATE,
        CallPolicy.MUTATION,
        "NotebookService",
        "notebook",
        "Title and emoji updates share MutateProject; the facade always re-reads the model.",
        _p("notebooks", "update", "rename", "set_emoji"),
        (_b(RPCMethod.RENAME_NOTEBOOK), _b(RPCMethod.GET_NOTEBOOK)),
        recency_effect="exactly one GET_NOTEBOOK after every successful mutation",
    ),
    OperationSpec(
        Operation.NOTEBOOK_DELETE,
        CallPolicy.MUTATION,
        "NotebookService",
        "notebook",
        "Deletes exactly one notebook although the native method is plural.",
        _p("notebooks", "delete"),
        (_b(RPCMethod.DELETE_NOTEBOOK),),
    ),
    OperationSpec(
        Operation.NOTEBOOK_REMOVE_RECENT,
        CallPolicy.MUTATION,
        "NotebookMutationService",
        "notebook",
        "Removes the notebook from the account's Recent list.",
        _p("notebooks", "remove_from_recent"),
        (_b(RPCMethod.REMOVE_RECENTLY_VIEWED),),
    ),
    OperationSpec(
        Operation.NOTEBOOK_SUMMARIZE,
        CallPolicy.STATEFUL_START,
        "NotebookGuideService",
        "notebook",
        "Generates the notebook guide and projects its summary field.",
        _p("notebooks", "get_summary"),
        (_b(RPCMethod.SUMMARIZE),),
    ),
    OperationSpec(
        Operation.NOTEBOOK_DESCRIBE,
        CallPolicy.STATEFUL_START,
        "NotebookGuideService",
        "notebook",
        "Uses the same guide generation response and projects description/topics.",
        _p("notebooks", "get_description"),
        (_b(RPCMethod.SUMMARIZE),),
    ),
    OperationSpec(
        Operation.NOTEBOOK_METADATA,
        CallPolicy.READ,
        "NotebookService",
        "notebook",
        "Combines notebook and source views concurrently through public facades.",
        _p("notebooks", "get_metadata"),
        (_b(RPCMethod.GET_NOTEBOOK),),
        disposition=Disposition.COMPOSITE,
        recency_effect="exactly two GET_NOTEBOOK calls (notebook.get plus sources.list)",
    ),
    OperationSpec(
        Operation.NOTEBOOK_SUGGEST_PROMPTS,
        CallPolicy.STATEFUL_START,
        "NotebookService",
        "notebook",
        "Resolves default sources through GET_NOTEBOOK, then generates prompt suggestions.",
        _p("notebooks", "suggest_prompts"),
        (_b(RPCMethod.SUGGEST_PROMPTS), _b(RPCMethod.GET_NOTEBOOK)),
        recency_effect="one GET_NOTEBOOK only when source_ids is omitted",
    ),
    OperationSpec(
        Operation.SOURCE_LIST,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook",
        "Projects source rows embedded in GET_NOTEBOOK; strict/filter behavior stays in facade.",
        _p("sources", "list"),
        (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="exactly one GET_NOTEBOOK per list call",
    ),
    OperationSpec(
        Operation.SOURCE_GET,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook+source",
        "Selects an exact source from the notebook source snapshot.",
        _p("sources", "get", "get_or_none"),
        (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="exactly one GET_NOTEBOOK per get/get_or_none call",
    ),
    OperationSpec(
        Operation.SOURCE_ADD_URL,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook",
        "Takes an unconditional source-id baseline, routes YouTube URLs to their wire shape, "
        "uses exact new-row reconciliation, and applies an optional title afterward.",
        _p("sources", "add_url"),
        (
            _b(RPCMethod.ADD_SOURCE, "url"),
            _b(RPCMethod.GET_NOTEBOOK),
            _b(RPCMethod.UPDATE_SOURCE),
        ),
        recency_effect=(
            "one unconditional pre-create GET_NOTEBOOK; ambiguity probes add reads, and "
            "wait=True adds one source snapshot per facade-owned readiness poll tick"
        ),
    ),
    OperationSpec(
        Operation.SOURCE_ADD_URL_BATCH,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook",
        "Sends validated URL/YouTube entries once, never blindly replays an uncertain write, "
        "and reconciles omitted positions against exact URL identities.",
        native_bindings=(
            _b(RPCMethod.ADD_SOURCE, "url"),
            _b(RPCMethod.GET_NOTEBOOK),
        ),
        recency_effect=(
            "zero GET_NOTEBOOK calls for complete responses; exactly one reconciliation snapshot "
            "when response positions are omitted"
        ),
    ),
    OperationSpec(
        Operation.SOURCE_ADD_TEXT,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook",
        "Creates text without a safe probe key; idempotent=True is rejected up front.",
        _p("sources", "add_text"),
        (_b(RPCMethod.ADD_SOURCE, "text"),),
        recency_effect=(
            "source.add_text itself issues no GET_NOTEBOOK; wait=True composes source.wait, "
            "which reads one snapshot per facade-owned readiness tick"
        ),
    ),
    OperationSpec(
        Operation.SOURCE_ADD_DRIVE,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook",
        "Takes an unconditional source-id baseline and reconciles by new exact Drive document id.",
        _p("sources", "add_drive"),
        (
            _b(RPCMethod.ADD_SOURCE, "drive"),
            _b(RPCMethod.GET_NOTEBOOK),
            _b(RPCMethod.UPDATE_SOURCE),
        ),
        recency_effect=(
            "one unconditional pre-create GET_NOTEBOOK; ambiguity probes add reads, and "
            "wait=True adds one source snapshot per facade-owned readiness poll tick"
        ),
    ),
    OperationSpec(
        Operation.SOURCE_ADD_FILE,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook+upload-session",
        "Stages local bytes or first downloads a Drive binary, registers the staged file, "
        "reconciles ambiguity, and may rename the resulting source.",
        _p("sources", "add_file", "add_drive_file"),
        (
            _b(RPCMethod.ADD_SOURCE_FILE),
            _b(RPCMethod.GET_NOTEBOOK),
            _b(RPCMethod.GET_USER_SETTINGS),
            _b(RPCMethod.UPDATE_SOURCE),
        ),
        ("resumable_upload", "drive_https_download"),
        recency_effect=(
            "one unconditional pre-create GET_NOTEBOOK; registration/reconciliation probes add "
            "reads, a custom title may poll registration even when wait=False, and wait=True "
            "adds one source snapshot per facade-owned readiness poll tick"
        ),
    ),
    OperationSpec(
        Operation.SOURCE_DELETE,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook+source",
        "Deletes one source through a batch-capable native method.",
        _p("sources", "delete"),
        (_b(RPCMethod.DELETE_SOURCE),),
    ),
    OperationSpec(
        Operation.SOURCE_UPDATE,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook+source",
        "Updates the source title and optionally re-reads the source.",
        _p("sources", "rename"),
        (_b(RPCMethod.UPDATE_SOURCE), _b(RPCMethod.GET_NOTEBOOK)),
        recency_effect="zero GET_NOTEBOOK calls on an echo; exactly one on a null echo",
    ),
    OperationSpec(
        Operation.SOURCE_PATCH_TITLE,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook+source",
        "P9.2 primitive: one UPDATE_SOURCE title set-op whose optional echo is returned to the "
        "service-owned source.update workflow.",
        (),
        (_b(RPCMethod.UPDATE_SOURCE),),
    ),
    OperationSpec(
        Operation.SOURCE_REFRESH,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook+source",
        "Requests refresh; native policy currently accepts at-least-once replay.",
        _p("sources", "refresh"),
        (_b(RPCMethod.REFRESH_SOURCE),),
        known_divergence="Semantic mutation is backed by AT_LEAST_ONCE_ACCEPTED native retry; "
        "P4 records parity but must not change behavior.",
    ),
    OperationSpec(
        Operation.SOURCE_CHECK_FRESHNESS,
        CallPolicy.READ,
        "SourceService",
        "notebook+source",
        "Decodes both web-source empty and Drive-source freshness response shapes.",
        _p("sources", "check_freshness"),
        (_b(RPCMethod.CHECK_SOURCE_FRESHNESS),),
    ),
    OperationSpec(
        Operation.SOURCE_GET_GUIDE,
        CallPolicy.STATEFUL_START,
        "SourceService",
        "notebook+source",
        "Generates and decodes a source guide.",
        _p("sources", "get_guide"),
        (_b(RPCMethod.GET_SOURCE_GUIDE),),
    ),
    OperationSpec(
        Operation.SOURCE_GET_FULLTEXT,
        CallPolicy.READ,
        "SourceService",
        "notebook+source",
        "Loads source content and projects text or markdown.",
        _p("sources", "get_fulltext"),
        (_b(RPCMethod.GET_SOURCE),),
    ),
    OperationSpec(
        Operation.SOURCE_WAIT,
        CallPolicy.MUTATION,
        "SourceService",
        "notebook+source-set",
        "Fetches one semantic notebook source snapshot per invocation; SourcesAPI/SourcePoller "
        "owns the loop and shares each tick across multi-source waits.",
        _p(
            "sources",
            "add_drive",
            "add_drive_file",
            "add_file",
            "add_text",
            "add_url",
            "wait_until_ready",
            "wait_all_until_ready",
            "wait_until_registered",
            "wait_for_sources",
        ),
        (_b(RPCMethod.GET_NOTEBOOK),),
        disposition=Disposition.COMPOSITE,
        recency_effect=(
            "one GET_NOTEBOOK per facade-owned poll tick; multi-source waits share it across "
            "inputs and wait=True add methods compose the same snapshot operation"
        ),
    ),
    OperationSpec(
        Operation.ARTIFACT_LIST,
        CallPolicy.READ,
        "StudioCatalog",
        "notebook",
        "Lists heterogeneous Studio artifacts and preserves partial mind-map availability rules.",
        _p(
            "artifacts",
            "list",
            "list_audio",
            "list_video",
            "list_reports",
            "list_quizzes",
            "list_flashcards",
            "list_infographics",
            "list_slide_decks",
            "list_data_tables",
        ),
        (_b(RPCMethod.LIST_ARTIFACTS), _b(RPCMethod.GET_NOTES_AND_MIND_MAPS)),
    ),
    OperationSpec(
        Operation.ARTIFACT_GET,
        CallPolicy.READ,
        "StudioCatalog",
        "notebook+artifact",
        "Selects one artifact or its prompt from the heterogeneous catalog.",
        _p("artifacts", "get", "get_or_none", "get_prompt"),
        (_b(RPCMethod.LIST_ARTIFACTS), _b(RPCMethod.GET_NOTES_AND_MIND_MAPS)),
    ),
    OperationSpec(
        Operation.ARTIFACT_GENERATE_AUDIO,
        CallPolicy.STATEFUL_START,
        "AudioFamilyService",
        "notebook+source-set",
        "Creates audio with format/length/instruction variants.",
        _p("artifacts", "generate_audio"),
        _CREATE_ARTIFACT + (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.ARTIFACT_GENERATE_VIDEO,
        CallPolicy.STATEFUL_START,
        "VideoFamilyService",
        "notebook+source-set",
        "Creates standard or cinematic video while preserving their option shapes.",
        _p("artifacts", "generate_video", "generate_cinematic_video"),
        _CREATE_ARTIFACT + (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.ARTIFACT_GENERATE_REPORT,
        CallPolicy.STATEFUL_START,
        "ReportFamilyService",
        "notebook+source-set",
        "Creates report or study-guide variants.",
        _p("artifacts", "generate_report", "generate_study_guide"),
        _CREATE_ARTIFACT + (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.ARTIFACT_GENERATE_QUIZ,
        CallPolicy.STATEFUL_START,
        "QuizFamilyService",
        "notebook+source-set",
        "Creates quiz variant 2.",
        _p("artifacts", "generate_quiz"),
        _CREATE_ARTIFACT + (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.ARTIFACT_GENERATE_FLASHCARDS,
        CallPolicy.STATEFUL_START,
        "QuizFamilyService",
        "notebook+source-set",
        "Creates flashcard variant 1.",
        _p("artifacts", "generate_flashcards"),
        _CREATE_ARTIFACT + (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC,
        CallPolicy.STATEFUL_START,
        "InfographicFamilyService",
        "notebook+source-set",
        "Creates an infographic with orientation/detail/style variants.",
        _p("artifacts", "generate_infographic"),
        _CREATE_ARTIFACT + (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.ARTIFACT_GENERATE_SLIDE_DECK,
        CallPolicy.STATEFUL_START,
        "VisualFamilyService",
        "notebook+source-set",
        "Creates a slide deck with format and length variants.",
        _p("artifacts", "generate_slide_deck"),
        _CREATE_ARTIFACT + (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.ARTIFACT_GENERATE_DATA_TABLE,
        CallPolicy.STATEFUL_START,
        "DataTableFamilyService",
        "notebook+source-set",
        "Creates a data-table artifact.",
        _p("artifacts", "generate_data_table"),
        _CREATE_ARTIFACT + (_b(RPCMethod.GET_NOTEBOOK),),
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.ARTIFACT_GENERATE_MIND_MAP,
        CallPolicy.STATEFUL_START,
        "MindMapFamilyService",
        "notebook+source-set",
        "Generates note-backed JSON and persists it through the plain-note variant.",
        _p("artifacts", "generate_mind_map"),
        (
            _b(RPCMethod.GENERATE_MIND_MAP),
            _b(RPCMethod.CREATE_NOTE, "plain"),
            _b(RPCMethod.UPDATE_NOTE),
            _b(RPCMethod.DELETE_NOTE),
            _b(RPCMethod.GET_NOTEBOOK),
        ),
        disposition=Disposition.COMPOSITE,
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.ARTIFACT_REVISE_SLIDE,
        CallPolicy.MUTATION,
        "StudioManagementService",
        "notebook+artifact",
        "Derives one revised slide from an existing deck.",
        _p("artifacts", "revise_slide"),
        (_b(RPCMethod.REVISE_SLIDE),),
    ),
    OperationSpec(
        Operation.ARTIFACT_RETRY,
        CallPolicy.STATEFUL_START,
        "StudioManagementService",
        "notebook+artifact",
        "Retries a failed artifact in place.",
        _p("artifacts", "retry_failed"),
        (_b(RPCMethod.RETRY_ARTIFACT),),
    ),
    OperationSpec(
        Operation.ARTIFACT_DELETE,
        CallPolicy.MUTATION,
        "StudioManagementService",
        "notebook+artifact",
        "Deletes one artifact.",
        _p("artifacts", "delete"),
        (_b(RPCMethod.DELETE_ARTIFACT),),
    ),
    OperationSpec(
        Operation.ARTIFACT_RENAME,
        CallPolicy.MUTATION,
        "StudioManagementService",
        "notebook+artifact",
        "Updates title and optionally re-lists to return the artifact.",
        _p("artifacts", "rename"),
        (_b(RPCMethod.RENAME_ARTIFACT), _b(RPCMethod.LIST_ARTIFACTS)),
    ),
    OperationSpec(
        Operation.ARTIFACT_EXPORT,
        CallPolicy.MUTATION,
        "DriveExportService",
        "notebook+artifact",
        "Exports report/data-table representations to the supported Drive destination.",
        _p("artifacts", "export", "export_report", "export_data_table"),
        (_b(RPCMethod.EXPORT_ARTIFACT),),
    ),
    OperationSpec(
        Operation.ARTIFACT_DOWNLOAD,
        CallPolicy.READ,
        "ArtifactRepresentationService",
        "notebook+artifact",
        "Selects a representation, obtains its URL/content, and writes the requested format.",
        _p(
            "artifacts",
            "download_audio",
            "download_video",
            "download_infographic",
            "download_slide_deck",
            "download_report",
            "download_mind_map",
            "download_data_table",
            "download_quiz",
            "download_flashcards",
        ),
        (
            _b(RPCMethod.LIST_ARTIFACTS),
            _b(RPCMethod.GET_NOTES_AND_MIND_MAPS),
            _b(RPCMethod.GET_INTERACTIVE_HTML),
        ),
        ("artifact_https_download",),
        Disposition.COMPOSITE,
        ("_app/download.py:execute_download",),
        _APP_DOWNLOAD_DIVERGENCE,
    ),
    OperationSpec(
        Operation.ARTIFACT_WAIT,
        CallPolicy.READ,
        "ArtifactLifecycleService",
        "notebook+artifact",
        "Polls the artifact catalog through the shared-leader polling registry.",
        _p("artifacts", "poll_status", "wait_for_completion"),
        (_b(RPCMethod.LIST_ARTIFACTS),),
        disposition=Disposition.COMPOSITE,
    ),
    OperationSpec(
        Operation.ARTIFACT_SUGGEST_REPORTS,
        CallPolicy.STATEFUL_START,
        "ReportSuggestionService",
        "notebook",
        "Generates report-format suggestions.",
        _p("artifacts", "suggest_reports"),
        (_b(RPCMethod.GET_SUGGESTED_REPORTS),),
    ),
    OperationSpec(
        Operation.CHAT_ASK,
        CallPolicy.STREAM,
        "ChatService",
        "notebook+conversation+source-set",
        "Two-phase all-or-nothing operation: streamed query first, then conversation-id RPC. "
        "Citation anchors index answer_document.text, raw_response stays truncated, the byte cap "
        "fires pre-decode, the loop guard precedes the lock, and missing conversation id logs the "
        "full answer before ChatError. Cancellation between phases may leave an undiscoverable turn.",
        _p("chat", "ask"),
        (_b(RPCMethod.GET_LAST_CONVERSATION_ID), _b(RPCMethod.GET_NOTEBOOK)),
        ("streamed_query",),
        recency_effect="one GET_NOTEBOOK on every ask where source_ids is omitted",
        known_divergence=(
            "GET_NOTEBOOK is the facade's NOTEBOOK_GET recency read, not a native the "
            "CHAT_ASK row dispatches"
        ),
    ),
    OperationSpec(
        Operation.CHAT_GET_CONVERSATION,
        CallPolicy.READ,
        "ChatService",
        "notebook",
        "Gets the most recent server conversation id.",
        _p("chat", "get_conversation_id"),
        (_b(RPCMethod.GET_LAST_CONVERSATION_ID),),
    ),
    OperationSpec(
        Operation.CHAT_GET_HISTORY,
        CallPolicy.READ,
        "ChatService",
        "notebook+conversation",
        "Loads turns and exposes raw or question/answer history projections.",
        _p("chat", "get_conversation_turns", "get_history"),
        (_b(RPCMethod.GET_CONVERSATION_TURNS),),
    ),
    OperationSpec(
        Operation.CHAT_DELETE_HISTORY,
        CallPolicy.MUTATION,
        "ChatService",
        "notebook+conversation",
        "Deletes the conversation turns.",
        _p("chat", "delete_conversation"),
        (_b(RPCMethod.DELETE_CONVERSATION),),
    ),
    OperationSpec(
        Operation.CHAT_CONFIGURE,
        CallPolicy.MUTATION,
        "ChatService",
        "notebook",
        "Reads or mutates chat settings embedded in the notebook payload.",
        _p("chat", "configure", "set_mode", "get_settings"),
        (_b(RPCMethod.RENAME_NOTEBOOK), _b(RPCMethod.GET_NOTEBOOK)),
        recency_effect="get_settings performs exactly one GET_NOTEBOOK",
    ),
    OperationSpec(
        Operation.CHAT_SAVE_NOTE,
        CallPolicy.MUTATION,
        "ChatService",
        "notebook+conversation-turn",
        "Saves an answer through the seven-element saved_from_chat note variant.",
        _p("chat", "save_answer_as_note"),
        (_b(RPCMethod.CREATE_NOTE, "saved_from_chat"),),
    ),
    OperationSpec(
        Operation.NOTE_LIST,
        CallPolicy.READ,
        "NoteService",
        "notebook",
        "Lists note rows while excluding note-backed mind maps.",
        _p("notes", "list"),
        (_b(RPCMethod.GET_NOTES_AND_MIND_MAPS),),
    ),
    OperationSpec(
        Operation.NOTE_GET,
        CallPolicy.READ,
        "NoteService",
        "notebook+note",
        "Selects an exact note from the mixed note/mind-map response.",
        _p("notes", "get", "get_or_none"),
        (_b(RPCMethod.GET_NOTES_AND_MIND_MAPS),),
    ),
    OperationSpec(
        Operation.NOTE_CREATE,
        CallPolicy.MUTATION,
        "NoteService",
        "notebook",
        "Creates a plain five-element note row without blind retry.",
        _p("notes", "create"),
        (
            _b(RPCMethod.CREATE_NOTE, "plain"),
            _b(RPCMethod.UPDATE_NOTE),
            _b(RPCMethod.DELETE_NOTE),
        ),
    ),
    OperationSpec(
        Operation.NOTE_UPDATE,
        CallPolicy.MUTATION,
        "NoteService",
        "notebook+note",
        "Updates note title/content and discards the native echo.",
        _p("notes", "update"),
        (_b(RPCMethod.UPDATE_NOTE),),
    ),
    OperationSpec(
        Operation.NOTE_DELETE,
        CallPolicy.MUTATION,
        "NoteService",
        "notebook+note",
        "Deletes one note through the batch-capable native method.",
        _p("notes", "delete"),
        (_b(RPCMethod.DELETE_NOTE),),
    ),
    OperationSpec(
        Operation.MIND_MAP_LIST,
        CallPolicy.READ,
        "MindMapService",
        "notebook",
        "Combines note-backed JSON mind maps and interactive Studio mind maps.",
        _p("mind_maps", "list", "list_note_backed") + _p("notes", "list_mind_maps"),
        (_b(RPCMethod.GET_NOTES_AND_MIND_MAPS), _b(RPCMethod.LIST_ARTIFACTS)),
        disposition=Disposition.COMPOSITE,
    ),
    OperationSpec(
        Operation.MIND_MAP_GET,
        CallPolicy.READ,
        "MindMapService",
        "notebook+mind-map",
        "Auto-detects note-backed versus Studio representation and optionally loads a tree.",
        _p("mind_maps", "get", "get_or_none", "get_tree"),
        (
            _b(RPCMethod.GET_NOTES_AND_MIND_MAPS),
            _b(RPCMethod.LIST_ARTIFACTS),
            _b(RPCMethod.GET_INTERACTIVE_HTML),
        ),
        disposition=Disposition.COMPOSITE,
    ),
    OperationSpec(
        Operation.MIND_MAP_GENERATE_NOTE,
        CallPolicy.STATEFUL_START,
        "MindMapService",
        "notebook+source-set",
        "Generates JSON, then persists it as a plain note and returns a MindMap.",
        (_p("mind_maps", "generate")),
        (
            _b(RPCMethod.GENERATE_MIND_MAP),
            _b(RPCMethod.CREATE_NOTE, "plain"),
            _b(RPCMethod.UPDATE_NOTE),
            _b(RPCMethod.GET_NOTEBOOK),
        ),
        disposition=Disposition.COMPOSITE,
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.MIND_MAP_GENERATE_INTERACTIVE,
        CallPolicy.STATEFUL_START,
        "MindMapFamilyService",
        "notebook+source-set",
        "Creates and optionally waits for an interactive Studio mind map.",
        _p("mind_maps", "generate"),
        (
            _b(RPCMethod.CREATE_ARTIFACT),
            _b(RPCMethod.GET_NOTEBOOK),
            _b(RPCMethod.LIST_ARTIFACTS),
            _b(RPCMethod.GET_INTERACTIVE_HTML),
        ),
        disposition=Disposition.COMPOSITE,
        recency_effect="one GET_NOTEBOOK when source_ids is omitted",
    ),
    OperationSpec(
        Operation.MIND_MAP_UPDATE,
        CallPolicy.MUTATION,
        "MindMapService",
        "notebook+mind-map",
        "Auto-detects representation and routes title update to note or artifact mutation.",
        _p("mind_maps", "rename"),
        (_b(RPCMethod.UPDATE_NOTE), _b(RPCMethod.RENAME_ARTIFACT)),
        disposition=Disposition.COMPOSITE,
    ),
    OperationSpec(
        Operation.MIND_MAP_DELETE,
        CallPolicy.MUTATION,
        "MindMapService",
        "notebook+mind-map",
        "Auto-detects representation and routes delete to note or artifact mutation.",
        _p("mind_maps", "delete") + _p("notes", "delete_mind_map"),
        (_b(RPCMethod.DELETE_NOTE), _b(RPCMethod.DELETE_ARTIFACT)),
        disposition=Disposition.COMPOSITE,
    ),
    OperationSpec(
        Operation.RESEARCH_START,
        CallPolicy.STATEFUL_START,
        "ResearchService",
        "notebook",
        "Selects fast or deep discovery; neither start shape has a safe client token.",
        _p("research", "start"),
        (_b(RPCMethod.START_FAST_RESEARCH), _b(RPCMethod.START_DEEP_RESEARCH)),
    ),
    OperationSpec(
        Operation.RESEARCH_POLL,
        CallPolicy.READ,
        "ResearchService",
        "notebook+research-task",
        "Lists discovery jobs and resolves task aliases.",
        _p("research", "poll"),
        (_b(RPCMethod.POLL_RESEARCH),),
    ),
    OperationSpec(
        Operation.RESEARCH_WAIT,
        CallPolicy.READ,
        "ResearchService",
        "notebook+research-task",
        "Polls one research task under a bounded total timeout.",
        _p("research", "wait_for_completion"),
        (_b(RPCMethod.POLL_RESEARCH),),
        disposition=Disposition.COMPOSITE,
    ),
    OperationSpec(
        Operation.RESEARCH_CANCEL,
        CallPolicy.MUTATION,
        "ResearchService",
        "notebook+research-run",
        "Sets a research run to its terminal cancelled state.",
        _p("research", "cancel"),
        (_b(RPCMethod.CANCEL_RESEARCH),),
    ),
    OperationSpec(
        Operation.RESEARCH_IMPORT,
        CallPolicy.MUTATION,
        "ResearchService",
        "notebook+research-task",
        "Imports selected result rows without a safe dedupe token.",
        _p("research", "import_sources"),
        (_b(RPCMethod.IMPORT_RESEARCH),),
    ),
    OperationSpec(
        Operation.RESEARCH_IMPORT_VERIFY,
        CallPolicy.MUTATION,
        "ResearchService",
        "notebook+research-task",
        "Imports then verifies source appearance within one max_elapsed budget.",
        _p("research", "import_sources_with_verification"),
        (_b(RPCMethod.IMPORT_RESEARCH), _b(RPCMethod.GET_NOTEBOOK)),
        disposition=Disposition.COMPOSITE,
    ),
    OperationSpec(
        Operation.LABEL_LIST,
        CallPolicy.READ,
        "LabelService",
        "notebook",
        "Lists type-discriminated source labels.",
        _p("labels", "list"),
        (_b(RPCMethod.LIST_LABELS),),
    ),
    OperationSpec(
        Operation.LABEL_GET,
        CallPolicy.READ,
        "LabelService",
        "notebook+label",
        "Selects one label from the list response.",
        _p("labels", "get", "get_or_none"),
        (_b(RPCMethod.LIST_LABELS),),
    ),
    OperationSpec(
        Operation.LABEL_SOURCES,
        CallPolicy.READ,
        "LabelService",
        "notebook+label",
        "Resolves the label membership ids against the notebook source snapshot.",
        _p("labels", "sources"),
        (_b(RPCMethod.LIST_LABELS), _b(RPCMethod.GET_NOTEBOOK)),
        disposition=Disposition.COMPOSITE,
    ),
    OperationSpec(
        Operation.LABEL_GENERATE,
        CallPolicy.STATEFUL_START,
        "LabelService",
        "notebook",
        "Runs the auto-group mode of CreateLabel.",
        _p("labels", "generate"),
        (_b(RPCMethod.CREATE_LABEL),),
    ),
    OperationSpec(
        Operation.LABEL_CREATE,
        CallPolicy.MUTATION,
        "LabelService",
        "notebook",
        "Runs the manual-create mode of CreateLabel.",
        _p("labels", "create"),
        (_b(RPCMethod.LIST_LABELS), _b(RPCMethod.CREATE_LABEL)),
    ),
    OperationSpec(
        Operation.LABEL_UPDATE,
        CallPolicy.MUTATION,
        "LabelService",
        "notebook+label",
        "Updates fields or source membership through field-mask variants.",
        _p("labels", "update", "rename", "set_emoji", "add_sources", "remove_sources"),
        (
            _b(RPCMethod.LIST_LABELS),
            _b(RPCMethod.UPDATE_LABEL),
            _b(RPCMethod.UPDATE_LABEL, "add_sources"),
            _b(RPCMethod.UPDATE_LABEL, "remove_sources"),
        ),
    ),
    OperationSpec(
        Operation.LABEL_MUTATE,
        CallPolicy.MUTATION,
        "LabelService",
        "notebook+label",
        "P9.2 primitive: one UPDATE_LABEL set-op (field mask, one member append, or one "
        "member removal) whose variant is chosen from the request kind and form; the hoisted "
        "update workflows issue one call per member.",
        (),
        (
            _b(RPCMethod.UPDATE_LABEL),
            _b(RPCMethod.UPDATE_LABEL, "add_sources"),
            _b(RPCMethod.UPDATE_LABEL, "remove_sources"),
            _b(RPCMethod.UPDATE_LABEL, "add_notebooks"),
            _b(RPCMethod.UPDATE_LABEL, "remove_notebooks"),
        ),
    ),
    OperationSpec(
        Operation.LABEL_ALLOCATE,
        CallPolicy.MUTATION,
        "LabelService",
        "notebook+label-set",
        "P9.2 primitive: one manual CREATE_LABEL allocation for either dialect; the "
        "source-label echo is decoded, the collection dialect returns no echo.",
        (),
        (_b(RPCMethod.CREATE_LABEL),),
    ),
    OperationSpec(
        Operation.LABEL_DELETE,
        CallPolicy.MUTATION,
        "LabelService",
        "notebook+label-set",
        "Deletes one or more labels.",
        _p("labels", "delete"),
        (_b(RPCMethod.DELETE_LABEL),),
    ),
    OperationSpec(
        Operation.COLLECTION_LIST,
        CallPolicy.READ,
        "CollectionService",
        "account",
        "Lists type-3 account labels as collections.",
        _p("collections", "list"),
        (_b(RPCMethod.LIST_LABELS),),
    ),
    OperationSpec(
        Operation.COLLECTION_GET,
        CallPolicy.READ,
        "CollectionService",
        "collection",
        "Selects one type-3 account label.",
        _p("collections", "get", "get_or_none"),
        (_b(RPCMethod.LIST_LABELS),),
    ),
    OperationSpec(
        Operation.COLLECTION_NOTEBOOKS,
        CallPolicy.READ,
        "CollectionService",
        "collection",
        "Resolves collection membership ids against notebook listing.",
        _p("collections", "notebooks"),
        (_b(RPCMethod.LIST_LABELS), _b(RPCMethod.LIST_NOTEBOOKS)),
        disposition=Disposition.COMPOSITE,
    ),
    OperationSpec(
        Operation.COLLECTION_CREATE,
        CallPolicy.MUTATION,
        "CollectionService",
        "account",
        "Creates a type-3 label with a null notebook parent.",
        _p("collections", "create"),
        (_b(RPCMethod.LIST_LABELS), _b(RPCMethod.CREATE_LABEL)),
    ),
    OperationSpec(
        Operation.COLLECTION_UPDATE,
        CallPolicy.MUTATION,
        "CollectionService",
        "collection",
        "Renames or changes notebook membership through shared label variants.",
        _p("collections", "rename", "add_notebooks", "remove_notebooks"),
        (
            _b(RPCMethod.LIST_LABELS),
            _b(RPCMethod.UPDATE_LABEL),
            _b(RPCMethod.UPDATE_LABEL, "add_notebooks"),
            _b(RPCMethod.UPDATE_LABEL, "remove_notebooks"),
        ),
    ),
    OperationSpec(
        Operation.COLLECTION_DELETE,
        CallPolicy.MUTATION,
        "CollectionService",
        "collection-set",
        "Deletes one or more type-3 labels.",
        _p("collections", "delete"),
        (_b(RPCMethod.DELETE_LABEL),),
    ),
    OperationSpec(
        Operation.SHARING_GET,
        CallPolicy.READ,
        "SharingService",
        "notebook",
        "Reads visibility and individual-user grants.",
        _p("sharing", "get_status"),
        (_b(RPCMethod.GET_SHARE_STATUS),),
    ),
    OperationSpec(
        Operation.SHARING_SET_PUBLIC,
        CallPolicy.MUTATION,
        "SharingService",
        "notebook",
        "Sets notebook visibility, then re-reads status.",
        _p("sharing", "set_public"),
        (_b(RPCMethod.SHARE_NOTEBOOK), _b(RPCMethod.GET_SHARE_STATUS)),
    ),
    OperationSpec(
        Operation.SHARING_SET_VIEW_LEVEL,
        CallPolicy.MUTATION,
        "SharingService",
        "notebook",
        "Uses MutateProject's share-access shape, then re-reads status.",
        _p("sharing", "set_view_level"),
        (_b(RPCMethod.RENAME_NOTEBOOK), _b(RPCMethod.GET_SHARE_STATUS)),
    ),
    OperationSpec(
        Operation.SHARING_UPDATE_USERS,
        CallPolicy.MUTATION,
        "SharingService",
        "notebook+user-grants",
        "Adds, replaces, updates, or removes individual grants and re-reads status.",
        _p("sharing", "add_user", "set_users", "update_user", "remove_user"),
        (_b(RPCMethod.SHARE_NOTEBOOK), _b(RPCMethod.GET_SHARE_STATUS)),
    ),
    OperationSpec(
        Operation.SHARING_MUTATE,
        CallPolicy.MUTATION,
        "SharingService",
        "notebook",
        "P9.2 primitive: one SHARE_NOTEBOOK envelope setting link visibility or one "
        "individual-user grant set; the hoisted sharing workflows read status back.",
        (),
        (_b(RPCMethod.SHARE_NOTEBOOK),),
    ),
    OperationSpec(
        Operation.LEGACY_SHARE_ARTIFACT,
        CallPolicy.MUTATION,
        "ShareManager",
        "notebook+artifact?",
        "Sets legacy notebook or artifact share-link state through the semantic backend.",
        native_bindings=(_b(RPCMethod.SHARE_ARTIFACT),),
    ),
    OperationSpec(
        Operation.SETTINGS_GET,
        CallPolicy.READ,
        "SettingsService",
        "account",
        "Gets the account settings row.",
        _p("settings", "get_user_settings", "get_output_language"),
        (_b(RPCMethod.GET_USER_SETTINGS),),
    ),
    OperationSpec(
        Operation.SETTINGS_SET_LANGUAGE,
        CallPolicy.MUTATION,
        "SettingsService",
        "account",
        "Sets output language and returns the server projection.",
        _p("settings", "set_output_language"),
        (_b(RPCMethod.SET_USER_SETTINGS),),
    ),
    OperationSpec(
        Operation.SETTINGS_GET_LIMITS,
        CallPolicy.READ,
        "SettingsService",
        "account",
        "Projects account quota and rollout limits from the settings row.",
        _p("settings", "get_account_limits"),
        (_b(RPCMethod.GET_USER_SETTINGS),),
    ),
)

DIVERGENCE_KINDS: Mapping[Operation, str] = {
    Operation.ARTIFACT_DOWNLOAD: "authority",
    Operation.CHAT_ASK: "authority",
    Operation.SOURCE_REFRESH: "policy",
}


# Public methods with no backend operation.  This is a reviewed disposition,
# not an ignore list: stale entries and newly discovered methods fail audit.
LOCAL_PUBLIC_METHODS: Mapping[str, str] = {
    "chat.cache_size": "local conversation-cache inspection",
    "chat.clear_cache": "local conversation-cache mutation",
    "chat.get_cached_turns": "local conversation-cache read",
    "chat.reset_after_open": "client lifecycle helper; resets loop-bound local state",
    "chat.set_bound_loop": "client lifecycle helper; binds loop-affine local state",
    "notebooks.get_share_url": "pure URL composition; performs no share mutation",
    "research.extract_report_urls": "pure report-markdown URL extraction helper",
    "research.select_cited_sources": "pure cited-source selection helper",
}

# Reviewed private facade seams invoked only from transport-neutral application
# orchestration. These are intentionally absent from the public namespace
# inventory, but must stay exact and catalogued rather than being silently
# ignored by the app-call scan.
APP_PRIVATE_FACADE_DISPOSITIONS: Mapping[str, str] = {
    "sources._add_urls_batch": (
        "typed true-batch URL seam; _app/source_batch.py is the sole adapter-neutral caller"
    ),
}


# Public members on the root client do not become semantic operations merely
# because they are stable API.  They still need a reviewed, fail-closed
# disposition: lifecycle/auth/observability/local/raw behavior is part of the
# compatibility boundary just as namespace methods are.
CLIENT_PUBLIC_MEMBER_DISPOSITIONS: Mapping[str, tuple[str, str]] = {
    "auth": ("auth", "live AuthTokens identity; local property read"),
    "close": ("lifecycle", "drain-aware transport shutdown"),
    "drain": ("lifecycle", "stop admission and await in-flight operations"),
    "from_storage": ("auth", "storage-backed client factory/context manager"),
    "get_account_authuser": ("auth", "network-free account route lookup"),
    "get_account_email": ("auth", "cached identity lookup with optional live probe"),
    "is_connected": ("lifecycle", "local lifecycle-state property read"),
    "metrics_snapshot": ("observability", "local cumulative metrics snapshot"),
    "refresh_auth": ("auth", "explicit credential refresh and propagation"),
    "rpc_call": ("raw", "documented web-only legacy RPC escape hatch"),
}


# Registry default rows retained as compatibility fallbacks even though every
# production caller supplies a literal variant.  They are dispositions, not
# semantic bindings, and must remain callsite-free or the audit fails.
NATIVE_BINDING_DISPOSITIONS: Mapping[NativeKey, str] = {
    _b(RPCMethod.ADD_SOURCE): "compatibility default; every active caller selects url/drive/text",
    _b(RPCMethod.CREATE_NOTE): (
        "compatibility default; every active caller selects plain/saved_from_chat"
    ),
}


# The greenfield v0 omissions called out by the plan.  Pinning these names to
# catalog operations prevents a narrowed semantic inventory from silently
# deleting current behavior merely because it was absent from the greenfield.
GREENFIELD_OMISSION_COVERAGE: Mapping[str, tuple[Operation, ...]] = {
    "source listing": (Operation.SOURCE_LIST,),
    "settings and account limits": (Operation.SETTINGS_GET, Operation.SETTINGS_GET_LIMITS),
    "individual sharing": (Operation.SHARING_UPDATE_USERS,),
    "prompt suggestions": (Operation.NOTEBOOK_SUGGEST_PROMPTS,),
    "report suggestions": (Operation.ARTIFACT_SUGGEST_REPORTS,),
    "generic artifact actions": (
        Operation.ARTIFACT_GET,
        Operation.ARTIFACT_DELETE,
        Operation.ARTIFACT_RENAME,
    ),
    "artifact retry": (Operation.ARTIFACT_RETRY,),
    "mind maps": (Operation.MIND_MAP_LIST, Operation.MIND_MAP_GENERATE_NOTE),
    "data tables": (Operation.ARTIFACT_GENERATE_DATA_TABLE,),
    "exports and download formats": (Operation.ARTIFACT_EXPORT, Operation.ARTIFACT_DOWNLOAD),
}


APP_ORCHESTRATOR_DISPOSITIONS: Mapping[str, str] = {
    "_app/generate_retry.py": (
        "Keep adapter-neutral command composition, optional wait dispatch, progress, and outcome "
        "projection. P4.2 passes one scalar budget and the public kickoff/wait callables through "
        "the underscore-private notebooklm.artifacts workflow entry; the exported retry helper "
        "remains available only for external callers."
    ),
    "_app/source_wait.py": (
        "Keep validation and typed outcome mapping as ordinary application callers. The public "
        "source facade is already the sole polling authority and receives the caller budget once."
    ),
    "_app/download.py": (
        "Keep selection/conflict/filesystem choreography; P4.2 starts a separate budget for "
        "each facade list/download operation (no command-wide outer budget), and P5 makes "
        "family services the sole network execution authority."
    ),
    "_app/pagination.py": (
        "Keep the pure bounded slice as local-only application behavior; move the web fact that "
        "batchexecute does not paginate into web binding evidence when domains migrate."
    ),
}


def native_key_text(binding: NativeKey) -> str:
    """Return the stable display key for a native method/variant binding."""
    method, variant = binding
    return f"{method.name}:{variant if variant is not None else '<default>'}"
