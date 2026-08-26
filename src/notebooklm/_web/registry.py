"""Closed web dispositions for the semantic operation vocabulary.

Direct P2 notebook/source operations, P5 Studio family operations, and P6.1–P6.7 domain
workflows have executable rows from ``_web.bindings``. P9.2 service-owned workflows keep their
canonical definition but no direct web binding. Every other operation has an unsupported
disposition, and the count assertions force a deliberate registry update when the closed
:class:`Operation` enum changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from .._binding import Binding, OperationDisposition
from .._operations import Operation, OperationDef
from .._records import (
    ARTIFACT_CATALOG_DEF,
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
    ARTIFACT_PATCH_TITLE_DEF,
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
    LABEL_ALLOCATE_DEF,
    LABEL_CREATE_DEF,
    LABEL_DELETE_DEF,
    LABEL_GENERATE_DEF,
    LABEL_GET_DEF,
    LABEL_LIST_DEF,
    LABEL_MUTATE_DEF,
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
    NOTEBOOK_ALLOCATE_DEF,
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_PATCH_DEF,
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
    SHARING_MUTATE_DEF,
    SHARING_PATCH_VIEW_LEVEL_DEF,
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
    SOURCE_PATCH_TITLE_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_REGISTER_DEF,
    SOURCE_UPDATE_DEF,
    SOURCE_WAIT_DEF,
)
from .bindings import WEB_BINDING_ROWS
from .policy import audit_web_call_policy_bindings


@dataclass(frozen=True, slots=True)
class WebOperationBinding:
    """One direct row, service-owned workflow, or reviewed unsupported disposition."""

    definition: OperationDef[Any, Any] | None
    unsupported_reason: str | None
    row: Binding | None = None
    #: P9.2: the workflow is sequenced by a semantic service from leaf
    #: operations; the backend refuses to invoke it directly.
    service_owned: bool = False

    def __post_init__(self) -> None:
        has_definition = self.definition is not None
        has_row = self.row is not None
        if has_definition != has_row and not self.service_owned:
            raise ValueError("direct web definitions and rows must be present together")
        if self.row is not None and self.row.definition is not self.definition:
            raise ValueError("a web binding row must carry the registry's canonical definition")
        if has_row and self.unsupported_reason is not None:
            raise ValueError("direct web binding rows cannot carry an unsupported reason")
        if not has_row and self.unsupported_reason is None:
            raise ValueError("unsupported web bindings require a reason")
        if self.service_owned and (has_row or not has_definition):
            raise ValueError("a service-owned workflow keeps its definition and has no direct row")

    @property
    def is_supported(self) -> bool:
        """Whether this binding names an executable row."""
        return self.definition is not None and self.unsupported_reason is None

    @property
    def disposition(self) -> OperationDisposition:
        """Three-way disposition: direct row, service-owned workflow, or unsupported."""
        if self.service_owned:
            return OperationDisposition.SERVICE_OWNED
        if self.is_supported:
            return OperationDisposition.SUPPORTED_DIRECT
        return OperationDisposition.UNSUPPORTED


_SUPPORTED_DEFINITIONS: Final[Mapping[Operation, OperationDef[Any, Any]]] = MappingProxyType(
    {
        Operation.NOTEBOOK_LIST: NOTEBOOK_LIST_DEF,
        Operation.NOTEBOOK_GET: NOTEBOOK_GET_DEF,
        Operation.NOTEBOOK_ALLOCATE: NOTEBOOK_ALLOCATE_DEF,
        Operation.NOTEBOOK_PATCH: NOTEBOOK_PATCH_DEF,
        Operation.NOTEBOOK_DELETE: NOTEBOOK_DELETE_DEF,
        Operation.NOTEBOOK_REMOVE_RECENT: NOTEBOOK_REMOVE_RECENT_DEF,
        Operation.NOTEBOOK_SUMMARIZE: NOTEBOOK_SUMMARIZE_DEF,
        Operation.NOTEBOOK_DESCRIBE: NOTEBOOK_DESCRIBE_DEF,
        Operation.SOURCE_ADD_URL_BATCH: SOURCE_ADD_URL_BATCH_DEF,
        Operation.SOURCE_ADD_FILE: SOURCE_ADD_FILE_DEF,
        Operation.SOURCE_DELETE: SOURCE_DELETE_DEF,
        Operation.SOURCE_PATCH_TITLE: SOURCE_PATCH_TITLE_DEF,
        Operation.SOURCE_REGISTER: SOURCE_REGISTER_DEF,
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
        Operation.ARTIFACT_CATALOG: ARTIFACT_CATALOG_DEF,
        Operation.ARTIFACT_PATCH_TITLE: ARTIFACT_PATCH_TITLE_DEF,
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
        Operation.LABEL_DELETE: LABEL_DELETE_DEF,
        Operation.LABEL_MUTATE: LABEL_MUTATE_DEF,
        Operation.LABEL_ALLOCATE: LABEL_ALLOCATE_DEF,
        Operation.COLLECTION_LIST: COLLECTION_LIST_DEF,
        Operation.COLLECTION_GET: COLLECTION_GET_DEF,
        Operation.COLLECTION_DELETE: COLLECTION_DELETE_DEF,
        Operation.SHARING_GET: SHARING_GET_DEF,
        Operation.LEGACY_SHARE_ARTIFACT: LEGACY_SHARE_ARTIFACT_DEF,
        Operation.SHARING_MUTATE: SHARING_MUTATE_DEF,
        Operation.SHARING_PATCH_VIEW_LEVEL: SHARING_PATCH_VIEW_LEVEL_DEF,
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
        Operation.ARTIFACT_DOWNLOAD: ARTIFACT_DOWNLOAD_DEF,
        Operation.ARTIFACT_WAIT: ARTIFACT_WAIT_DEF,
    }
)

# ``_web.bindings`` assembles every directly supported row; the registry checks
# that this key set is exactly the directly executable definition set.
_ROW_BACKED_OPERATIONS: Final[frozenset[Operation]] = frozenset(WEB_BINDING_ROWS)

# P9.2 service-owned workflows: the semantic service sequences the leaves named
# in ``_web/policy.py``'s workflow ledger; ``capabilities.supports()`` reports
# ``False`` because ``invoke()`` refuses them (the port's ``supports`` means
# invokable). Each entry names the owning service call site.
_SERVICE_OWNED_DEFINITIONS: Final[Mapping[Operation, OperationDef[Any, Any]]] = MappingProxyType(
    {
        Operation.LABEL_CREATE: LABEL_CREATE_DEF,
        Operation.LABEL_UPDATE: LABEL_UPDATE_DEF,
        Operation.COLLECTION_CREATE: COLLECTION_CREATE_DEF,
        Operation.COLLECTION_UPDATE: COLLECTION_UPDATE_DEF,
        Operation.SOURCE_ADD_URL: SOURCE_ADD_URL_DEF,
        Operation.SOURCE_ADD_TEXT: SOURCE_ADD_TEXT_DEF,
        Operation.SOURCE_ADD_DRIVE: SOURCE_ADD_DRIVE_DEF,
        Operation.SOURCE_UPDATE: SOURCE_UPDATE_DEF,
        Operation.SHARING_SET_PUBLIC: SHARING_SET_PUBLIC_DEF,
        Operation.SHARING_UPDATE_USERS: SHARING_UPDATE_USERS_DEF,
        Operation.SHARING_SET_VIEW_LEVEL: SHARING_SET_VIEW_LEVEL_DEF,
        Operation.ARTIFACT_RENAME: ARTIFACT_RENAME_DEF,
        Operation.NOTEBOOK_CREATE: NOTEBOOK_CREATE_DEF,
        Operation.NOTEBOOK_UPDATE: NOTEBOOK_UPDATE_DEF,
    }
)
_SERVICE_OWNED_REASONS: Final[Mapping[Operation, str]] = MappingProxyType(
    {
        Operation.LABEL_CREATE: (
            "service-owned since P9.2-8: LabelSetService.create sequences label.list and "
            "label.allocate"
        ),
        Operation.LABEL_UPDATE: (
            "service-owned since P9.2-2: LabelSetService.update sequences label.get and "
            "label.mutate"
        ),
        Operation.COLLECTION_CREATE: (
            "service-owned since P9.2-9: LabelSetService.create sequences collection.list, "
            "label.allocate and collection.list"
        ),
        Operation.COLLECTION_UPDATE: (
            "service-owned since P9.2-3: LabelSetService.update sequences collection.get and "
            "label.mutate"
        ),
        Operation.SOURCE_ADD_URL: (
            "service-owned since P10 R3.3: SourceService.add_url sequences the source.list "
            "baseline, one source.register url allocation, the reconciling source.list probe "
            "and the source.patch_title finalise"
        ),
        Operation.SOURCE_ADD_TEXT: (
            "service-owned since P10 R3.2: SourceService.add_text refuses a non-idempotent "
            "replay and runs one source.register text allocation"
        ),
        Operation.SOURCE_ADD_DRIVE: (
            "service-owned since P10 R3.4: SourceService.add_drive sequences the source.list "
            "baseline, one source.register drive allocation, the reconciling source.list probe "
            "matched on drive_document_id and the source.patch_title finalise"
        ),
        Operation.SOURCE_UPDATE: (
            "service-owned since P9.2-4: SourceService.update sequences source.patch_title and "
            "source.get"
        ),
        Operation.SHARING_SET_PUBLIC: (
            "service-owned since P9.2-5: SharingService.set_public sequences sharing.mutate and "
            "sharing.get"
        ),
        Operation.SHARING_UPDATE_USERS: (
            "service-owned since P9.2-6: SharingService.set_users/remove_user sequence "
            "sharing.mutate and sharing.get"
        ),
        Operation.SHARING_SET_VIEW_LEVEL: (
            "service-owned since P9.2-7: SharingService.set_view_level sequences "
            "sharing.patch_view_level and sharing.get"
        ),
        Operation.ARTIFACT_RENAME: (
            "service-owned since P9.2-10: StudioManagementService.rename sequences "
            "artifact.patch_title and artifact.catalog"
        ),
        Operation.NOTEBOOK_CREATE: (
            "service-owned since P9.2-12: NotebookMutationService.create sequences "
            "notebook.list, notebook.allocate, and settings.get_limits"
        ),
        Operation.NOTEBOOK_UPDATE: (
            "service-owned since P9.2-11: NotebookMutationService.update sequences "
            "notebook.patch and notebook.get"
        ),
    }
)

# The remaining operations have no typed ``OperationDef`` at all: each is a
# composition a facade or a semantic service performs over *public* methods, so
# there is nothing for ``invoke`` to accept and nothing for ``capabilities`` to
# advertise. A generic "not migrated" string hid why, which is exactly the
# distinction P10/N2 asks the registry to make: each entry now says what runs
# the operation today and, where a later slice changes that, which one.
_UNSUPPORTED_REASONS: Final[Mapping[Operation, str]] = MappingProxyType(
    {
        Operation.NOTEBOOK_METADATA: (
            "facade composition without a typed def: NotebooksAPI.get_metadata delegates to "
            "NotebookMetadataService, which gathers the public notebooks.get and sources.list "
            "reads concurrently (_notebook_metadata.py); R6.2 decides whether it earns a typed "
            "NOTEBOOK_METADATA_DEF workflow"
        ),
        Operation.LABEL_SOURCES: (
            "facade composition without a typed def: LabelsAPI.sources joins the label's "
            "membership ids against the public source listing client-side and issues no native "
            "call of its own"
        ),
        Operation.COLLECTION_NOTEBOOKS: (
            "facade composition without a typed def: CollectionsAPI.notebooks joins the "
            "collection's membership ids against the public notebook listing client-side and "
            "issues no native call of its own"
        ),
        Operation.RESEARCH_WAIT: (
            "facade/service workflow without a typed def: ResearchService polls research.poll "
            "under its own total budget; R6.4 adds typed inputs/results and flips it to "
            "service-owned"
        ),
        Operation.RESEARCH_IMPORT_VERIFY: (
            "facade/service workflow without a typed def: ResearchService sequences "
            "research.import and a source-listing probe within one budget; R6.4 adds typed "
            "inputs/results and flips it to service-owned"
        ),
    }
)

# The frozen catalog currently contains 97 operations (87 product members plus ten
# decomposition primitives). This assertion is repeated at
# the runtime registry boundary: a new enum member must not silently inherit an
# unsupported disposition without a web-registry review.
_EXPECTED_OPERATION_COUNT: Final = 97
_EXPECTED_SUPPORTED_COUNT: Final = 78
_EXPECTED_SERVICE_OWNED_COUNT: Final = 14


def _build_web_operation_registry() -> Mapping[Operation, WebOperationBinding]:
    if set(_SUPPORTED_DEFINITIONS) != _ROW_BACKED_OPERATIONS:
        raise RuntimeError("web definitions and binding rows disagree")
    for operation, row in WEB_BINDING_ROWS.items():
        if row.definition is not _SUPPORTED_DEFINITIONS[operation]:
            raise RuntimeError(
                f"{operation.value} binding row does not carry its canonical definition"
            )
    if set(_SERVICE_OWNED_DEFINITIONS) != set(_SERVICE_OWNED_REASONS):
        raise RuntimeError("service-owned web definitions and reasons disagree")
    if set(_SUPPORTED_DEFINITIONS) & set(_SERVICE_OWNED_DEFINITIONS):
        raise RuntimeError("a web operation cannot be both directly supported and service-owned")
    if len(_SERVICE_OWNED_DEFINITIONS) != _EXPECTED_SERVICE_OWNED_COUNT:
        raise RuntimeError(
            "the service-owned workflow set changed; update the reviewed service-owned count"
        )
    if len(Operation) != _EXPECTED_OPERATION_COUNT:
        raise RuntimeError(
            "the semantic operation vocabulary changed; review every web disposition "
            f"(expected {_EXPECTED_OPERATION_COUNT}, found {len(Operation)})"
        )
    if len(_SUPPORTED_DEFINITIONS) != _EXPECTED_SUPPORTED_COUNT:
        raise RuntimeError(
            "the web handler set changed; update the reviewed supported-operation count"
        )
    if set(_UNSUPPORTED_REASONS) != (
        set(Operation) - set(_SUPPORTED_DEFINITIONS) - set(_SERVICE_OWNED_DEFINITIONS)
    ):
        raise RuntimeError(
            "every operation without a web row or a service-owned workflow needs its own "
            "reviewed unsupported reason"
        )
    if policy_errors := audit_web_call_policy_bindings(
        _SUPPORTED_DEFINITIONS, workflows=_SERVICE_OWNED_DEFINITIONS
    ):
        raise RuntimeError("web call-policy binding drift:\n- " + "\n- ".join(policy_errors))

    registry: dict[Operation, WebOperationBinding] = {}
    for operation in Operation:
        definition = _SUPPORTED_DEFINITIONS.get(operation)
        service_owned_definition = _SERVICE_OWNED_DEFINITIONS.get(operation)
        if service_owned_definition is not None:
            registry[operation] = WebOperationBinding(
                definition=service_owned_definition,
                unsupported_reason=_SERVICE_OWNED_REASONS[operation],
                service_owned=True,
            )
        elif definition is not None:
            registry[operation] = WebOperationBinding(
                definition=definition,
                unsupported_reason=None,
                row=WEB_BINDING_ROWS[operation],
            )
        else:
            registry[operation] = WebOperationBinding(
                definition=None,
                unsupported_reason=_UNSUPPORTED_REASONS[operation],
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

WEB_SERVICE_OWNED_OPERATIONS: Final = frozenset(
    operation
    for operation, binding in WEB_OPERATION_REGISTRY.items()
    if binding.disposition is OperationDisposition.SERVICE_OWNED
)

__all__ = [
    "WEB_OPERATION_REGISTRY",
    "WEB_SERVICE_OWNED_OPERATIONS",
    "WEB_SUPPORTED_OPERATIONS",
    "WebOperationBinding",
]
