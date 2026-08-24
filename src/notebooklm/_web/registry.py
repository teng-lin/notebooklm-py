"""Closed web dispositions for the semantic operation vocabulary.

P2 notebook/source operations, P5 Studio family operations, and P6.1–P6.7 domain workflows have
executable bindings: a legacy handler name resolved at construction, or — since P9.3 — a binding
row from ``_web.bindings``. Every other operation has an unsupported disposition, and the count
assertions force a deliberate registry update when the closed :class:`Operation` enum changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from .._binding import Binding, OperationDisposition
from .._operations import Operation, OperationDef
from .._records import (
    ARTIFACT_DELETE_DEF,
    ARTIFACT_DOWNLOAD_DEF,
    ARTIFACT_EXPORT_DEF,
    ARTIFACT_GENERATE_AUDIO_DEF,
    ARTIFACT_GENERATE_DATA_TABLE_DEF,
    ARTIFACT_GENERATE_FLASHCARDS_DEF,
    ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
    ARTIFACT_GENERATE_MIND_MAP_DEF,
    ARTIFACT_GENERATE_QUIZ_DEF,
    ARTIFACT_GENERATE_REPORT_DEF,
    ARTIFACT_GENERATE_SLIDE_DECK_DEF,
    ARTIFACT_GENERATE_VIDEO_DEF,
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    ARTIFACT_RENAME_DEF,
    ARTIFACT_RETRY_DEF,
    ARTIFACT_REVISE_SLIDE_DEF,
    ARTIFACT_SUGGEST_REPORTS_DEF,
    ARTIFACT_WAIT_DEF,
    CHAT_ASK_DEF,
    CHAT_CONFIGURE_DEF,
    CHAT_DELETE_HISTORY_DEF,
    CHAT_GET_CONVERSATION_DEF,
    CHAT_GET_HISTORY_DEF,
    CHAT_SAVE_NOTE_DEF,
    COLLECTION_CREATE_DEF,
    COLLECTION_DELETE_DEF,
    COLLECTION_GET_DEF,
    COLLECTION_LIST_DEF,
    COLLECTION_UPDATE_DEF,
    LABEL_CREATE_DEF,
    LABEL_DELETE_DEF,
    LABEL_GENERATE_DEF,
    LABEL_GET_DEF,
    LABEL_LIST_DEF,
    LABEL_UPDATE_DEF,
    LEGACY_SHARE_ARTIFACT_DEF,
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GENERATE_NOTE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_LIST_DEF,
    MIND_MAP_UPDATE_DEF,
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    NOTE_UPDATE_DEF,
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_SUGGEST_PROMPTS_DEF,
    NOTEBOOK_SUMMARIZE_DEF,
    NOTEBOOK_UPDATE_DEF,
    RESEARCH_CANCEL_DEF,
    RESEARCH_IMPORT_DEF,
    RESEARCH_POLL_DEF,
    RESEARCH_START_DEF,
    SETTINGS_GET_DEF,
    SETTINGS_GET_LIMITS_DEF,
    SETTINGS_SET_LANGUAGE_DEF,
    SHARING_GET_DEF,
    SHARING_SET_PUBLIC_DEF,
    SHARING_SET_VIEW_LEVEL_DEF,
    SHARING_UPDATE_USERS_DEF,
    SOURCE_ADD_DRIVE_DEF,
    SOURCE_ADD_FILE_DEF,
    SOURCE_ADD_TEXT_DEF,
    SOURCE_ADD_URL_BATCH_DEF,
    SOURCE_ADD_URL_DEF,
    SOURCE_CHECK_FRESHNESS_DEF,
    SOURCE_DELETE_DEF,
    SOURCE_GET_DEF,
    SOURCE_GET_FULLTEXT_DEF,
    SOURCE_GET_GUIDE_DEF,
    SOURCE_LIST_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_UPDATE_DEF,
    SOURCE_WAIT_DEF,
)
from .bindings import WEB_BINDING_ROWS
from .policy import audit_web_call_policy_bindings


@dataclass(frozen=True, slots=True)
class WebOperationBinding:
    """One executable handler or row, or one reviewed unsupported disposition."""

    definition: OperationDef[Any, Any] | None
    handler_name: str | None
    unsupported_reason: str | None
    row: Binding | None = None

    def __post_init__(self) -> None:
        has_definition = self.definition is not None
        has_handler = self.handler_name is not None
        has_row = self.row is not None
        if has_handler and has_row:
            raise ValueError("a web operation is backed by a handler name or a row, never both")
        if has_definition != (has_handler or has_row):
            raise ValueError("web definitions and handler names or rows must be present together")
        if self.row is not None and self.row.definition is not self.definition:
            raise ValueError("a web binding row must carry the registry's canonical definition")
        if not (has_handler or has_row) and self.unsupported_reason is None:
            raise ValueError("unsupported web bindings require a reason")

    @property
    def is_supported(self) -> bool:
        """Whether this binding names an executable P1 handler."""
        return self.definition is not None and self.unsupported_reason is None

    @property
    def is_staged(self) -> bool:
        """Whether a reviewed handler exists but production dispatch rejects it."""
        return self.definition is not None and self.unsupported_reason is not None

    @property
    def disposition(self) -> OperationDisposition:
        """Three-way disposition; ``SERVICE_OWNED`` arrives with the P9.2 hoists."""
        if self.is_supported:
            return OperationDisposition.SUPPORTED_DIRECT
        return OperationDisposition.UNSUPPORTED


_SUPPORTED_DEFINITIONS: Final[Mapping[Operation, OperationDef[Any, Any]]] = MappingProxyType(
    {
        Operation.NOTEBOOK_LIST: NOTEBOOK_LIST_DEF,
        Operation.NOTEBOOK_GET: NOTEBOOK_GET_DEF,
        Operation.NOTEBOOK_CREATE: NOTEBOOK_CREATE_DEF,
        Operation.NOTEBOOK_UPDATE: NOTEBOOK_UPDATE_DEF,
        Operation.NOTEBOOK_DELETE: NOTEBOOK_DELETE_DEF,
        Operation.NOTEBOOK_REMOVE_RECENT: NOTEBOOK_REMOVE_RECENT_DEF,
        Operation.NOTEBOOK_SUMMARIZE: NOTEBOOK_SUMMARIZE_DEF,
        Operation.NOTEBOOK_DESCRIBE: NOTEBOOK_DESCRIBE_DEF,
        Operation.SOURCE_ADD_URL: SOURCE_ADD_URL_DEF,
        Operation.SOURCE_ADD_URL_BATCH: SOURCE_ADD_URL_BATCH_DEF,
        Operation.SOURCE_ADD_TEXT: SOURCE_ADD_TEXT_DEF,
        Operation.SOURCE_ADD_DRIVE: SOURCE_ADD_DRIVE_DEF,
        Operation.SOURCE_ADD_FILE: SOURCE_ADD_FILE_DEF,
        Operation.SOURCE_DELETE: SOURCE_DELETE_DEF,
        Operation.SOURCE_UPDATE: SOURCE_UPDATE_DEF,
        Operation.SOURCE_REFRESH: SOURCE_REFRESH_DEF,
        Operation.SOURCE_CHECK_FRESHNESS: SOURCE_CHECK_FRESHNESS_DEF,
        Operation.SOURCE_GET_GUIDE: SOURCE_GET_GUIDE_DEF,
        Operation.SOURCE_GET_FULLTEXT: SOURCE_GET_FULLTEXT_DEF,
        Operation.SOURCE_LIST: SOURCE_LIST_DEF,
        Operation.SOURCE_GET: SOURCE_GET_DEF,
        Operation.CHAT_ASK: CHAT_ASK_DEF,
        Operation.CHAT_GET_CONVERSATION: CHAT_GET_CONVERSATION_DEF,
        Operation.CHAT_GET_HISTORY: CHAT_GET_HISTORY_DEF,
        Operation.CHAT_DELETE_HISTORY: CHAT_DELETE_HISTORY_DEF,
        Operation.CHAT_CONFIGURE: CHAT_CONFIGURE_DEF,
        Operation.CHAT_SAVE_NOTE: CHAT_SAVE_NOTE_DEF,
        Operation.SOURCE_WAIT: SOURCE_WAIT_DEF,
        Operation.NOTE_LIST: NOTE_LIST_DEF,
        Operation.NOTE_GET: NOTE_GET_DEF,
        Operation.NOTE_CREATE: NOTE_CREATE_DEF,
        Operation.NOTE_UPDATE: NOTE_UPDATE_DEF,
        Operation.NOTE_DELETE: NOTE_DELETE_DEF,
        Operation.MIND_MAP_LIST: MIND_MAP_LIST_DEF,
        Operation.MIND_MAP_GET: MIND_MAP_GET_DEF,
        Operation.MIND_MAP_GENERATE_NOTE: MIND_MAP_GENERATE_NOTE_DEF,
        Operation.MIND_MAP_GENERATE_INTERACTIVE: MIND_MAP_GENERATE_INTERACTIVE_DEF,
        Operation.MIND_MAP_UPDATE: MIND_MAP_UPDATE_DEF,
        Operation.MIND_MAP_DELETE: MIND_MAP_DELETE_DEF,
        Operation.ARTIFACT_LIST: ARTIFACT_LIST_DEF,
        Operation.ARTIFACT_GET: ARTIFACT_GET_DEF,
        Operation.ARTIFACT_GENERATE_AUDIO: ARTIFACT_GENERATE_AUDIO_DEF,
        Operation.ARTIFACT_GENERATE_QUIZ: ARTIFACT_GENERATE_QUIZ_DEF,
        Operation.ARTIFACT_GENERATE_FLASHCARDS: ARTIFACT_GENERATE_FLASHCARDS_DEF,
        Operation.ARTIFACT_GENERATE_REPORT: ARTIFACT_GENERATE_REPORT_DEF,
        Operation.ARTIFACT_GENERATE_VIDEO: ARTIFACT_GENERATE_VIDEO_DEF,
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC: ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
        Operation.ARTIFACT_GENERATE_SLIDE_DECK: ARTIFACT_GENERATE_SLIDE_DECK_DEF,
        Operation.ARTIFACT_GENERATE_DATA_TABLE: ARTIFACT_GENERATE_DATA_TABLE_DEF,
        Operation.ARTIFACT_GENERATE_MIND_MAP: ARTIFACT_GENERATE_MIND_MAP_DEF,
        Operation.ARTIFACT_EXPORT: ARTIFACT_EXPORT_DEF,
        Operation.LABEL_LIST: LABEL_LIST_DEF,
        Operation.LABEL_GET: LABEL_GET_DEF,
        Operation.LABEL_GENERATE: LABEL_GENERATE_DEF,
        Operation.LABEL_CREATE: LABEL_CREATE_DEF,
        Operation.LABEL_UPDATE: LABEL_UPDATE_DEF,
        Operation.LABEL_DELETE: LABEL_DELETE_DEF,
        Operation.COLLECTION_LIST: COLLECTION_LIST_DEF,
        Operation.COLLECTION_GET: COLLECTION_GET_DEF,
        Operation.COLLECTION_CREATE: COLLECTION_CREATE_DEF,
        Operation.COLLECTION_UPDATE: COLLECTION_UPDATE_DEF,
        Operation.COLLECTION_DELETE: COLLECTION_DELETE_DEF,
        Operation.SHARING_GET: SHARING_GET_DEF,
        Operation.SHARING_SET_PUBLIC: SHARING_SET_PUBLIC_DEF,
        Operation.SHARING_SET_VIEW_LEVEL: SHARING_SET_VIEW_LEVEL_DEF,
        Operation.SHARING_UPDATE_USERS: SHARING_UPDATE_USERS_DEF,
        Operation.LEGACY_SHARE_ARTIFACT: LEGACY_SHARE_ARTIFACT_DEF,
        Operation.RESEARCH_START: RESEARCH_START_DEF,
        Operation.RESEARCH_POLL: RESEARCH_POLL_DEF,
        Operation.RESEARCH_CANCEL: RESEARCH_CANCEL_DEF,
        Operation.RESEARCH_IMPORT: RESEARCH_IMPORT_DEF,
        Operation.NOTEBOOK_SUGGEST_PROMPTS: NOTEBOOK_SUGGEST_PROMPTS_DEF,
        Operation.ARTIFACT_SUGGEST_REPORTS: ARTIFACT_SUGGEST_REPORTS_DEF,
        Operation.SETTINGS_GET: SETTINGS_GET_DEF,
        Operation.SETTINGS_GET_LIMITS: SETTINGS_GET_LIMITS_DEF,
        Operation.SETTINGS_SET_LANGUAGE: SETTINGS_SET_LANGUAGE_DEF,
        Operation.ARTIFACT_REVISE_SLIDE: ARTIFACT_REVISE_SLIDE_DEF,
        Operation.ARTIFACT_RETRY: ARTIFACT_RETRY_DEF,
        Operation.ARTIFACT_DELETE: ARTIFACT_DELETE_DEF,
        Operation.ARTIFACT_RENAME: ARTIFACT_RENAME_DEF,
        Operation.ARTIFACT_DOWNLOAD: ARTIFACT_DOWNLOAD_DEF,
        Operation.ARTIFACT_WAIT: ARTIFACT_WAIT_DEF,
    }
)

_HANDLER_NAMES: Final[Mapping[Operation, str]] = MappingProxyType(
    {
        Operation.NOTEBOOK_LIST: "_notebook_list",
        Operation.NOTEBOOK_GET: "_notebook_get",
        Operation.NOTEBOOK_CREATE: "_notebook_create",
        Operation.NOTEBOOK_UPDATE: "_notebook_update",
        Operation.NOTEBOOK_DELETE: "_notebook_delete",
        Operation.NOTEBOOK_REMOVE_RECENT: "_notebook_remove_recent",
        Operation.NOTEBOOK_SUMMARIZE: "_notebook_summarize",
        Operation.NOTEBOOK_DESCRIBE: "_notebook_describe",
        Operation.SOURCE_ADD_URL: "_source_add_url",
        Operation.SOURCE_ADD_URL_BATCH: "_source_add_url_batch",
        Operation.SOURCE_ADD_TEXT: "_source_add_text",
        Operation.SOURCE_ADD_DRIVE: "_source_add_drive",
        Operation.SOURCE_ADD_FILE: "_source_add_file",
        Operation.SOURCE_DELETE: "_source_delete",
        Operation.SOURCE_UPDATE: "_source_update",
        Operation.SOURCE_REFRESH: "_source_refresh",
        Operation.SOURCE_CHECK_FRESHNESS: "_source_check_freshness",
        Operation.SOURCE_GET_GUIDE: "_source_get_guide",
        Operation.SOURCE_GET_FULLTEXT: "_source_get_fulltext",
        Operation.SOURCE_LIST: "_source_list",
        Operation.SOURCE_GET: "_source_get",
        Operation.CHAT_ASK: "_chat_ask",
        Operation.CHAT_GET_CONVERSATION: "_chat_get_conversation",
        Operation.CHAT_GET_HISTORY: "_chat_get_history",
        Operation.CHAT_DELETE_HISTORY: "_chat_delete_history",
        Operation.CHAT_CONFIGURE: "_chat_configure",
        Operation.CHAT_SAVE_NOTE: "_chat_save_note",
        Operation.SOURCE_WAIT: "_source_wait",
        Operation.NOTE_LIST: "_note_list",
        Operation.NOTE_GET: "_note_get",
        Operation.NOTE_CREATE: "_note_create",
        Operation.NOTE_UPDATE: "_note_update",
        Operation.NOTE_DELETE: "_note_delete",
        Operation.MIND_MAP_LIST: "_mind_map_list",
        Operation.MIND_MAP_GET: "_mind_map_get",
        Operation.MIND_MAP_GENERATE_NOTE: "_mind_map_generate_note",
        Operation.MIND_MAP_GENERATE_INTERACTIVE: "_mind_map_generate_interactive",
        Operation.MIND_MAP_UPDATE: "_mind_map_update",
        Operation.MIND_MAP_DELETE: "_mind_map_delete",
        Operation.ARTIFACT_LIST: "_artifact_list",
        Operation.ARTIFACT_GET: "_artifact_get",
        Operation.ARTIFACT_GENERATE_AUDIO: "_audio_generate",
        Operation.ARTIFACT_GENERATE_QUIZ: "_quiz_generate",
        Operation.ARTIFACT_GENERATE_FLASHCARDS: "_flashcards_generate",
        Operation.ARTIFACT_GENERATE_REPORT: "_report_generate",
        Operation.ARTIFACT_GENERATE_VIDEO: "_video_generate",
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC: "_infographic_generate",
        Operation.ARTIFACT_GENERATE_SLIDE_DECK: "_slide_deck_generate",
        Operation.ARTIFACT_GENERATE_DATA_TABLE: "_data_table_generate",
        Operation.ARTIFACT_GENERATE_MIND_MAP: "_mind_map_generate",
        Operation.ARTIFACT_EXPORT: "_artifact_export",
        Operation.LABEL_LIST: "_label_list",
        Operation.LABEL_GET: "_label_get",
        Operation.LABEL_GENERATE: "_label_generate",
        Operation.LABEL_CREATE: "_label_create",
        Operation.LABEL_UPDATE: "_label_update",
        Operation.LABEL_DELETE: "_label_delete",
        Operation.COLLECTION_LIST: "_collection_list",
        Operation.COLLECTION_GET: "_collection_get",
        Operation.COLLECTION_CREATE: "_collection_create",
        Operation.COLLECTION_UPDATE: "_collection_update",
        Operation.COLLECTION_DELETE: "_collection_delete",
        Operation.SHARING_SET_PUBLIC: "_sharing_set_public",
        Operation.SHARING_SET_VIEW_LEVEL: "_sharing_set_view_level",
        Operation.SHARING_UPDATE_USERS: "_sharing_update_users",
        Operation.NOTEBOOK_SUGGEST_PROMPTS: "_notebook_suggest_prompts",
        Operation.ARTIFACT_REVISE_SLIDE: "_artifact_revise_slide",
        Operation.ARTIFACT_RETRY: "_artifact_retry",
        Operation.ARTIFACT_DELETE: "_artifact_delete",
        Operation.ARTIFACT_RENAME: "_artifact_rename",
        Operation.ARTIFACT_DOWNLOAD: "_artifact_download",
        Operation.ARTIFACT_WAIT: "_artifact_wait",
    }
)

# Operations executed through a binding row rather than a resolved handler name.
# ``_web.bindings`` assembles the rows; the registry only checks the partition.
_ROW_BACKED_OPERATIONS: Final[frozenset[Operation]] = frozenset(WEB_BINDING_ROWS)

_STAGED_DEFINITIONS: Final[Mapping[Operation, OperationDef[Any, Any]]] = MappingProxyType({})

_STAGED_HANDLER_NAMES: Final[Mapping[Operation, str]] = MappingProxyType({})

# The frozen catalog currently contains 87 operations. This assertion is repeated at
# the runtime registry boundary: a new enum member must not silently inherit an
# unsupported disposition without a web-registry review.
_EXPECTED_OPERATION_COUNT: Final = 87
_EXPECTED_SUPPORTED_COUNT: Final = 82
_EXPECTED_STAGED_COUNT: Final = 0


def _build_web_operation_registry() -> Mapping[Operation, WebOperationBinding]:
    if set(_HANDLER_NAMES) & _ROW_BACKED_OPERATIONS:
        raise RuntimeError("a web operation cannot have both a handler name and a binding row")
    if set(_SUPPORTED_DEFINITIONS) != set(_HANDLER_NAMES) | _ROW_BACKED_OPERATIONS:
        raise RuntimeError("web definitions and handler names or binding rows disagree")
    for operation, row in WEB_BINDING_ROWS.items():
        if row.definition is not _SUPPORTED_DEFINITIONS[operation]:
            raise RuntimeError(
                f"{operation.value} binding row does not carry its canonical definition"
            )
    if set(_STAGED_DEFINITIONS) != set(_STAGED_HANDLER_NAMES):
        raise RuntimeError("staged web definitions and handler names disagree")
    if set(_SUPPORTED_DEFINITIONS) & set(_STAGED_DEFINITIONS):
        raise RuntimeError("a web operation cannot be active and staged")
    if len(Operation) != _EXPECTED_OPERATION_COUNT:
        raise RuntimeError(
            "the semantic operation vocabulary changed; review every web disposition "
            f"(expected {_EXPECTED_OPERATION_COUNT}, found {len(Operation)})"
        )
    if len(_SUPPORTED_DEFINITIONS) != _EXPECTED_SUPPORTED_COUNT:
        raise RuntimeError(
            "the web handler set changed; update the reviewed supported-operation count"
        )
    if len(_STAGED_DEFINITIONS) != _EXPECTED_STAGED_COUNT:
        raise RuntimeError(
            "the staged web handler set changed; update the reviewed staged-operation count"
        )
    if policy_errors := audit_web_call_policy_bindings(_SUPPORTED_DEFINITIONS):
        raise RuntimeError("web call-policy binding drift:\n- " + "\n- ".join(policy_errors))

    registry: dict[Operation, WebOperationBinding] = {}
    for operation in Operation:
        definition = _SUPPORTED_DEFINITIONS.get(operation)
        staged_definition = _STAGED_DEFINITIONS.get(operation)
        if definition is not None:
            registry[operation] = WebOperationBinding(
                definition=definition,
                handler_name=_HANDLER_NAMES.get(operation),
                unsupported_reason=None,
                row=WEB_BINDING_ROWS.get(operation),
            )
        elif staged_definition is not None:
            registry[operation] = WebOperationBinding(
                definition=staged_definition,
                handler_name=_STAGED_HANDLER_NAMES[operation],
                unsupported_reason="handler staged until the source facade delegates",
            )
        else:
            registry[operation] = WebOperationBinding(
                definition=None,
                handler_name=None,
                unsupported_reason="not migrated to the semantic backend",
            )
    if set(registry) != set(Operation):
        raise RuntimeError("web operation registry is not closed over Operation")
    return MappingProxyType(registry)


WEB_OPERATION_REGISTRY: Final = _build_web_operation_registry()

WEB_SUPPORTED_OPERATIONS: Final = frozenset(
    operation
    for operation, binding in WEB_OPERATION_REGISTRY.items()
    if binding.disposition is OperationDisposition.SUPPORTED_DIRECT
)

WEB_STAGED_OPERATIONS: Final = frozenset(
    operation for operation, binding in WEB_OPERATION_REGISTRY.items() if binding.is_staged
)


__all__ = [
    "WEB_OPERATION_REGISTRY",
    "WEB_SUPPORTED_OPERATIONS",
    "WEB_STAGED_OPERATIONS",
    "WebOperationBinding",
]
