"""Closed semantic operation vocabulary.

P0 introduced these types as an inert vocabulary. Later bounded slices activate
one typed backend binding at a time while retaining one execution authority per
operation; unsupported operations remain closed catalog dispositions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Generic, TypeVar

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


@unique
class Operation(str, Enum):
    """Backend-neutral product operations known to the current client."""

    NOTEBOOK_LIST = "notebook.list"
    NOTEBOOK_GET = "notebook.get"
    NOTEBOOK_CREATE = "notebook.create"
    NOTEBOOK_UPDATE = "notebook.update"
    NOTEBOOK_PATCH = "notebook.patch"
    NOTEBOOK_DELETE = "notebook.delete"
    NOTEBOOK_REMOVE_RECENT = "notebook.remove_recent"
    NOTEBOOK_SUMMARIZE = "notebook.summarize"
    NOTEBOOK_DESCRIBE = "notebook.describe"
    NOTEBOOK_METADATA = "notebook.metadata"
    NOTEBOOK_SUGGEST_PROMPTS = "notebook.suggest_prompts"

    SOURCE_LIST = "source.list"
    SOURCE_GET = "source.get"
    SOURCE_ADD_URL = "source.add_url"
    SOURCE_ADD_URL_BATCH = "source.add_url_batch"
    SOURCE_ADD_TEXT = "source.add_text"
    SOURCE_ADD_DRIVE = "source.add_drive"
    SOURCE_ADD_FILE = "source.add_file"
    SOURCE_DELETE = "source.delete"
    SOURCE_UPDATE = "source.update"
    # P9.2 primitive: one native title set-op consumed by source.update.
    SOURCE_PATCH_TITLE = "source.patch_title"
    SOURCE_REFRESH = "source.refresh"
    SOURCE_CHECK_FRESHNESS = "source.check_freshness"
    SOURCE_GET_GUIDE = "source.get_guide"
    SOURCE_GET_FULLTEXT = "source.get_fulltext"
    SOURCE_WAIT = "source.wait"

    ARTIFACT_LIST = "artifact.list"
    ARTIFACT_GET = "artifact.get"
    ARTIFACT_GENERATE_AUDIO = "artifact.generate_audio"
    ARTIFACT_GENERATE_VIDEO = "artifact.generate_video"
    ARTIFACT_GENERATE_REPORT = "artifact.generate_report"
    ARTIFACT_GENERATE_QUIZ = "artifact.generate_quiz"
    ARTIFACT_GENERATE_FLASHCARDS = "artifact.generate_flashcards"
    ARTIFACT_GENERATE_INFOGRAPHIC = "artifact.generate_infographic"
    ARTIFACT_GENERATE_SLIDE_DECK = "artifact.generate_slide_deck"
    ARTIFACT_GENERATE_DATA_TABLE = "artifact.generate_data_table"
    ARTIFACT_GENERATE_MIND_MAP = "artifact.generate_mind_map"
    ARTIFACT_REVISE_SLIDE = "artifact.revise_slide"
    ARTIFACT_RETRY = "artifact.retry"
    ARTIFACT_DELETE = "artifact.delete"
    ARTIFACT_RENAME = "artifact.rename"
    # P9.2 primitives: one native call each, sequenced by artifact.rename.
    ARTIFACT_PATCH_TITLE = "artifact.patch_title"
    ARTIFACT_CATALOG = "artifact.catalog"
    ARTIFACT_EXPORT = "artifact.export"
    ARTIFACT_DOWNLOAD = "artifact.download"
    ARTIFACT_WAIT = "artifact.wait"
    ARTIFACT_SUGGEST_REPORTS = "artifact.suggest_reports"

    CHAT_ASK = "chat.ask"
    CHAT_GET_CONVERSATION = "chat.get_conversation"
    CHAT_GET_HISTORY = "chat.get_history"
    CHAT_DELETE_HISTORY = "chat.delete_history"
    CHAT_CONFIGURE = "chat.configure"
    CHAT_SAVE_NOTE = "chat.save_note"

    NOTE_LIST = "note.list"
    NOTE_GET = "note.get"
    NOTE_CREATE = "note.create"
    NOTE_UPDATE = "note.update"
    NOTE_DELETE = "note.delete"

    MIND_MAP_LIST = "mind_map.list"
    MIND_MAP_GET = "mind_map.get"
    MIND_MAP_GENERATE_NOTE = "mind_map.generate_note"
    MIND_MAP_GENERATE_INTERACTIVE = "mind_map.generate_interactive"
    MIND_MAP_UPDATE = "mind_map.update"
    MIND_MAP_DELETE = "mind_map.delete"

    RESEARCH_START = "research.start"
    RESEARCH_POLL = "research.poll"
    RESEARCH_WAIT = "research.wait"
    RESEARCH_CANCEL = "research.cancel"
    RESEARCH_IMPORT = "research.import"
    RESEARCH_IMPORT_VERIFY = "research.import_verify"

    LABEL_LIST = "label.list"
    LABEL_GET = "label.get"
    LABEL_SOURCES = "label.sources"
    LABEL_GENERATE = "label.generate"
    LABEL_CREATE = "label.create"
    LABEL_UPDATE = "label.update"
    LABEL_DELETE = "label.delete"
    # P9.2 primitives: one native set-op each, consumed by the hoisted workflows.
    LABEL_MUTATE = "label.mutate"
    LABEL_ALLOCATE = "label.allocate"

    COLLECTION_LIST = "collection.list"
    COLLECTION_GET = "collection.get"
    COLLECTION_NOTEBOOKS = "collection.notebooks"
    COLLECTION_CREATE = "collection.create"
    COLLECTION_UPDATE = "collection.update"
    COLLECTION_DELETE = "collection.delete"

    SHARING_GET = "sharing.get"
    SHARING_SET_PUBLIC = "sharing.set_public"
    SHARING_SET_VIEW_LEVEL = "sharing.set_view_level"
    SHARING_UPDATE_USERS = "sharing.update_users"
    LEGACY_SHARE_ARTIFACT = "sharing.legacy_share_artifact"
    SHARING_MUTATE = "sharing.mutate"
    SHARING_PATCH_VIEW_LEVEL = "sharing.patch_view_level"

    SETTINGS_GET = "settings.get"
    SETTINGS_SET_LANGUAGE = "settings.set_language"
    SETTINGS_GET_LIMITS = "settings.get_limits"


class CallPolicy(str, Enum):
    """Semantic behavior relevant to retries, deadlines, and dispatch."""

    READ = "read"
    STATEFUL_START = "stateful_start"
    MUTATION = "mutation"
    STREAM = "stream"


@dataclass(frozen=True, slots=True)
class OperationDef(Generic[InputT, OutputT]):
    """Typed definition passed to a future semantic backend adapter.

    Concrete input/output types are introduced only when an operation is
    migrated.  Keeping this definition free of RPC, HTTP, CLI, MCP, and REST
    types is the dependency-direction invariant established in P0.
    """

    key: Operation
    policy: CallPolicy
    input_type: type[InputT]
    output_type: type[OutputT]


__all__ = ["CallPolicy", "Operation", "OperationDef"]
