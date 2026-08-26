"""Closed web dispositions for the semantic operation vocabulary.

Direct P2 notebook/source operations, P5 Studio family operations, and P6.1–P6.7 domain
workflows have executable rows from ``_web.bindings``, and the directly supported definition set
is derived from that row table rather than re-listed here. Service-owned workflows (P9.2's
decomposition, P10 R2.2's chat ask, P10 R3.2-R3.5's source adds and P10 R6.4's two research
workflows) keep their canonical definition but no direct web binding. Every other operation has
an unsupported disposition, and the count assertions force a deliberate registry update when the
closed :class:`Operation` enum changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from .._binding import Binding, OperationDisposition
from .._operations import Operation, OperationDef
from .._records import (
    ARTIFACT_GENERATE_MIND_MAP_DEF,
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    ARTIFACT_RENAME_DEF,
    CHAT_ASK_DEF,
    COLLECTION_CREATE_DEF,
    COLLECTION_UPDATE_DEF,
    LABEL_CREATE_DEF,
    LABEL_UPDATE_DEF,
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_UPDATE_DEF,
    RESEARCH_IMPORT_VERIFY_DEF,
    RESEARCH_WAIT_DEF,
    SHARING_SET_PUBLIC_DEF,
    SHARING_SET_VIEW_LEVEL_DEF,
    SHARING_UPDATE_USERS_DEF,
    SOURCE_ADD_DRIVE_DEF,
    SOURCE_ADD_TEXT_DEF,
    SOURCE_ADD_URL_BATCH_DEF,
    SOURCE_ADD_URL_DEF,
    SOURCE_UPDATE_DEF,
)
from .bindings import WEB_BINDING_ROWS


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


# P10 R2.5 (invariant I7): the row table is the sole authority for what the web
# backend can execute directly, so the supported definitions are *derived* from
# it rather than re-listed here. ``_assemble_rows`` already rejects duplicate
# operations and rows that do not carry their canonical definition, which is
# what the two hand-maintained cross-checks used to prove.
_SUPPORTED_DEFINITIONS: Final[Mapping[Operation, OperationDef[Any, Any]]] = MappingProxyType(
    {operation: row.definition for operation, row in WEB_BINDING_ROWS.items()}
)

# Service-owned workflows: the semantic service sequences the workflow's
# leaf operations; ``capabilities.supports()`` reports
# ``False`` because ``invoke()`` refuses them (the port's ``supports`` means
# invokable), while ``capabilities.available()`` reports ``True``. Each entry
# names the owning service call site. Eleven arrived with P9.2's decomposition;
# P10 R2.2's chat.ask and R3.2-R3.5's four source adds hoisted their sequencing
# above the port; and the two research workflows joined in P10 R6.4, which gave
# them the typed definitions their UNSUPPORTED disposition was waiting on.
_SERVICE_OWNED_DEFINITIONS: Final[Mapping[Operation, OperationDef[Any, Any]]] = MappingProxyType(
    {
        Operation.LABEL_CREATE: LABEL_CREATE_DEF,
        Operation.LABEL_UPDATE: LABEL_UPDATE_DEF,
        Operation.COLLECTION_CREATE: COLLECTION_CREATE_DEF,
        Operation.COLLECTION_UPDATE: COLLECTION_UPDATE_DEF,
        Operation.SOURCE_ADD_URL: SOURCE_ADD_URL_DEF,
        Operation.SOURCE_ADD_URL_BATCH: SOURCE_ADD_URL_BATCH_DEF,
        Operation.SOURCE_ADD_TEXT: SOURCE_ADD_TEXT_DEF,
        Operation.SOURCE_ADD_DRIVE: SOURCE_ADD_DRIVE_DEF,
        Operation.SOURCE_UPDATE: SOURCE_UPDATE_DEF,
        Operation.SHARING_SET_PUBLIC: SHARING_SET_PUBLIC_DEF,
        Operation.SHARING_UPDATE_USERS: SHARING_UPDATE_USERS_DEF,
        Operation.SHARING_SET_VIEW_LEVEL: SHARING_SET_VIEW_LEVEL_DEF,
        Operation.ARTIFACT_RENAME: ARTIFACT_RENAME_DEF,
        Operation.ARTIFACT_LIST: ARTIFACT_LIST_DEF,
        Operation.ARTIFACT_GET: ARTIFACT_GET_DEF,
        Operation.ARTIFACT_GENERATE_MIND_MAP: ARTIFACT_GENERATE_MIND_MAP_DEF,
        Operation.NOTEBOOK_CREATE: NOTEBOOK_CREATE_DEF,
        Operation.NOTEBOOK_UPDATE: NOTEBOOK_UPDATE_DEF,
        Operation.CHAT_ASK: CHAT_ASK_DEF,
        Operation.RESEARCH_WAIT: RESEARCH_WAIT_DEF,
        Operation.RESEARCH_IMPORT_VERIFY: RESEARCH_IMPORT_VERIFY_DEF,
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
        Operation.SOURCE_ADD_URL_BATCH: (
            "service-owned since P10 R3.5: SourceService.add_urls_batch runs one non-replayed "
            "source.register url batch write and reconciles the entries its echo omitted "
            "against a source.list of ERROR rows"
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
        Operation.ARTIFACT_LIST: (
            "service-owned since P10 R4.2: StudioCatalog.list_records sequences "
            "artifact.catalog and the supplemental mind_map.list merge"
        ),
        Operation.ARTIFACT_GET: (
            "service-owned since P10 R4.2: StudioCatalog.get_record selects one identity "
            "from the artifact.catalog and mind_map.list merge"
        ),
        Operation.ARTIFACT_GENERATE_MIND_MAP: (
            "service-owned since P10 R4.2: NoteBackedMindMapFamilyService.generate sequences "
            "mind_map.generate_note and the note.create/note.update/note.delete persistence"
        ),
        Operation.NOTEBOOK_CREATE: (
            "service-owned since P9.2-12: NotebookMutationService.create sequences "
            "notebook.list, notebook.allocate, and settings.get_limits"
        ),
        Operation.NOTEBOOK_UPDATE: (
            "service-owned since P9.2-11: NotebookMutationService.update sequences "
            "notebook.patch and notebook.get"
        ),
        Operation.CHAT_ASK: (
            "service-owned since P10 R2.2: ChatWorkflowService.ask sequences chat.stream_answer "
            "and, "
            "only when the caller resolved no id, chat.get_conversation"
        ),
        Operation.RESEARCH_WAIT: (
            "service-owned since P10 R6.4: ResearchService.wait_for_completion polls "
            "research.poll under its own total budget and cadence"
        ),
        Operation.RESEARCH_IMPORT_VERIFY: (
            "service-owned since P10 R6.4: "
            "ResearchService.import_sources_with_verification sequences research.import "
            "and source.list within one max_elapsed"
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
            "NotebookMetadataService, which concurrently gathers the late-bound public "
            "notebooks.get and the injected NotebookSourceLister.list (_notebook_metadata.py); "
            "both collaborators are replaceable public-model seams rather than typed leaves, so "
            "the composition stays untyped and no NOTEBOOK_METADATA_DEF exists"
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
    }
)

# The frozen catalog currently contains 99 operations (87 product members plus the
# twelve decomposition primitives: the nine P9.2 leaves, P10 R2.2's streamed-answer
# leaf, P10 R3.2's source-registration leaf, and P10 R4.2's mind-map generation
# leaf). This assertion is repeated at
# the runtime registry boundary: a new enum member must not silently inherit an
# unsupported disposition without a web-registry review.
_EXPECTED_OPERATION_COUNT: Final = 99
_EXPECTED_SUPPORTED_COUNT: Final = 75
# 11 from P9.2, P10 R2.2's chat.ask, P10 R3.2-R3.5's four source adds, the two
# research workflows R6.4 typed, and P10 R4.2's three Studio/mind-map workflows.
# R6.4's flip leaves the vocabulary and the
# directly-supported row set alone: it moves two members from UNSUPPORTED to
# SERVICE_OWNED, which is a disposition change only.
_EXPECTED_SERVICE_OWNED_COUNT: Final = 21


def _build_web_operation_registry() -> Mapping[Operation, WebOperationBinding]:
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
