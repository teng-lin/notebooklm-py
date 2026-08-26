"""Transport-neutral records and operation definitions for mind maps."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..operations import CallPolicy, Operation, OperationDef, OperationTier


@dataclass(frozen=True, slots=True)
class MindMapRecord:
    """One backend-neutral mind map with its optional JSON tree payload."""

    id: str
    notebook_id: str
    title: str
    kind: str
    created_at: datetime | None = None
    tree_json: str | None = None


#: Diagnostics key the supplemental catalog read's partial-availability net
#: matches on.  ``mind_map.list``'s ``map_error`` stamps it on the one failure
#: it translates, so the swallowing service recognises that failure without
#: widening its reason set to every network failure.
SUPPLEMENTAL_TRANSPORT_FAILURE = "supplemental_transport_failure"

#: ``MindMapListInput.raw_rows``: return the mind-map rows only.
RAW_MIND_MAP_ROWS = "mind_maps"
#: ``MindMapListInput.raw_rows``: return the whole normalized row collection.
RAW_ALL_NOTE_ROWS = "all"


@dataclass(frozen=True, slots=True)
class MindMapListInput:
    """Notebook whose active note-backed mind maps are requested.

    ``raw_rows`` selects the undecoded compatibility branch that
    ``NotesAPI.list_mind_maps`` and ``NotesAPI._get_all_notes_and_mind_maps``
    publish: :data:`RAW_MIND_MAP_ROWS` yields the normalized mind-map rows and
    :data:`RAW_ALL_NOTE_ROWS` the whole normalized collection, both exactly as
    the wire produced them.  ``None`` is the record branch every semantic
    caller uses.

    ``supplemental`` marks the optional read the Studio catalog merges into a
    complete listing.  It selects nothing about the request or the decode; it
    tells the row only that this one caller applies ADR-0019 Rule 3's
    partial-availability policy, so the row may translate the raw transport
    leaf that policy has always swallowed.  Every other consumer leaves it
    ``False`` and keeps observing that leaf unchanged.
    """

    notebook_id: str
    raw_rows: str | None = None
    supplemental: bool = False


@dataclass(frozen=True, slots=True)
class MindMapListResult:
    """Active note-backed mind maps in backend order.

    ``rows`` carries the undecoded payload for ``raw_rows`` requests only and
    stays empty on the record branch, which in turn leaves ``mind_maps`` empty.
    """

    mind_maps: tuple[MindMapRecord, ...]
    rows: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class MindMapGetInput:
    """Interactive mind-map identity whose tree is requested."""

    notebook_id: str
    mind_map_id: str


@dataclass(frozen=True, slots=True)
class MindMapGetResult:
    """Interactive tree JSON, or ``None`` while absent/not populated."""

    tree_json: str | None


@dataclass(frozen=True, slots=True)
class MindMapGenerateNoteInput:
    """Pre-resolved note-backed generation input: the port defaults nothing.

    ``source_ids`` and ``language`` are both required.  "No scope given means
    every source in the notebook" and "no language given means the environment
    default" are service-level defaults (P10 R5.1b, ADR-0035 addendum D1(a)):
    :class:`~notebooklm._semantic.services.note.NoteService` and
    :class:`~notebooklm._studio.NoteBackedMindMapFamilyService` resolve both
    above the port, so the row never re-derives them below it.
    """

    notebook_id: str
    source_ids: tuple[str, ...]
    language: str
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MindMapGenerateNoteResult:
    """Generated note-backed tree before semantic note persistence."""

    tree_json: str | None


@dataclass(frozen=True, slots=True)
class MindMapGenerateTreeInput:
    """Resolved note-backed generation request for the ``mind_map.generate`` leaf.

    ``source_ids`` is required: a primitive never resolves a default source set
    of its own, so the sequencing service supplies the exact set the native is
    called with.
    """

    notebook_id: str
    source_ids: tuple[str, ...]
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MindMapGenerateTreeResult:
    """Serialized tree the generation native produced, or ``None`` when absent.

    Only the JSON text crosses the port. The parsed tree and its display title
    are derived above the port so no public value is decoded below it.
    """

    tree_json: str | None


@dataclass(frozen=True, slots=True)
class MindMapGenerateInteractiveInput:
    """Interactive Studio mind-map generation options with a resolved scope.

    ``source_ids`` is required: "no scope given means every source" is a
    service-level default (:class:`~notebooklm._studio.MindMapFamilyService`
    resolves the notebook's full source set through ``NOTEBOOK_GET`` before
    invoking), not something the backend re-derives below the port.
    """

    notebook_id: str
    source_ids: tuple[str, ...]
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MindMapGenerateInteractiveResult:
    """Allocated interactive Studio mind-map identity."""

    mind_map_id: str


@dataclass(frozen=True, slots=True)
class MindMapUpdateInput:
    """Interactive mind-map title replacement."""

    notebook_id: str
    mind_map_id: str
    title: str


@dataclass(frozen=True, slots=True)
class MindMapUpdateResult:
    """Successful interactive mind-map rename."""


@dataclass(frozen=True, slots=True)
class MindMapDeleteInput:
    """Interactive mind-map identity to delete idempotently."""

    notebook_id: str
    mind_map_id: str


@dataclass(frozen=True, slots=True)
class MindMapDeleteResult:
    """Successful idempotent interactive mind-map deletion."""


@dataclass(frozen=True, slots=True)
class MindMapRepresentationRecord:
    """One note-backed mind-map identity and serialized tree."""

    id: str
    title: str
    content: str | None = field(default=None, repr=False)
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MindMapGenerateInput:
    """Note-backed mind-map generation options."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
    instructions: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class MindMapGenerateResult:
    """Generated mind-map tree plus its persisted note identity."""

    mind_map: object | None = field(default=None, repr=False)
    note_id: str | None = None
    created_at: datetime | None = None


MIND_MAP_LIST_DEF: OperationDef[MindMapListInput, MindMapListResult] = OperationDef(
    Operation.MIND_MAP_LIST,
    CallPolicy.READ,
    MindMapListInput,
    MindMapListResult,
)


MIND_MAP_GET_DEF: OperationDef[MindMapGetInput, MindMapGetResult] = OperationDef(
    Operation.MIND_MAP_GET,
    CallPolicy.READ,
    MindMapGetInput,
    MindMapGetResult,
)


MIND_MAP_GENERATE_NOTE_DEF: OperationDef[MindMapGenerateNoteInput, MindMapGenerateNoteResult] = (
    OperationDef(
        Operation.MIND_MAP_GENERATE_NOTE,
        CallPolicy.STATEFUL_START,
        MindMapGenerateNoteInput,
        MindMapGenerateNoteResult,
    )
)


MIND_MAP_GENERATE_DEF: OperationDef[MindMapGenerateTreeInput, MindMapGenerateTreeResult] = (
    OperationDef(
        Operation.MIND_MAP_GENERATE,
        CallPolicy.STATEFUL_START,
        MindMapGenerateTreeInput,
        MindMapGenerateTreeResult,
        tier=OperationTier.PRIMITIVE,
    )
)


MIND_MAP_GENERATE_INTERACTIVE_DEF: OperationDef[
    MindMapGenerateInteractiveInput, MindMapGenerateInteractiveResult
] = OperationDef(
    Operation.MIND_MAP_GENERATE_INTERACTIVE,
    CallPolicy.STATEFUL_START,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
)


MIND_MAP_UPDATE_DEF: OperationDef[MindMapUpdateInput, MindMapUpdateResult] = OperationDef(
    Operation.MIND_MAP_UPDATE,
    CallPolicy.MUTATION,
    MindMapUpdateInput,
    MindMapUpdateResult,
)


MIND_MAP_DELETE_DEF: OperationDef[MindMapDeleteInput, MindMapDeleteResult] = OperationDef(
    Operation.MIND_MAP_DELETE,
    CallPolicy.MUTATION,
    MindMapDeleteInput,
    MindMapDeleteResult,
)


__all__ = [
    "MIND_MAP_DELETE_DEF",
    "MIND_MAP_GENERATE_DEF",
    "MIND_MAP_GENERATE_INTERACTIVE_DEF",
    "MIND_MAP_GENERATE_NOTE_DEF",
    "MIND_MAP_GET_DEF",
    "MIND_MAP_LIST_DEF",
    "MIND_MAP_UPDATE_DEF",
    "MindMapDeleteInput",
    "MindMapDeleteResult",
    "MindMapGenerateInput",
    "MindMapGenerateInteractiveInput",
    "MindMapGenerateInteractiveResult",
    "MindMapGenerateNoteInput",
    "MindMapGenerateNoteResult",
    "MindMapGenerateResult",
    "MindMapGenerateTreeInput",
    "MindMapGenerateTreeResult",
    "MindMapGetInput",
    "MindMapGetResult",
    "MindMapListInput",
    "MindMapListResult",
    "MindMapRecord",
    "MindMapRepresentationRecord",
    "MindMapUpdateInput",
    "MindMapUpdateResult",
    "RAW_ALL_NOTE_ROWS",
    "RAW_MIND_MAP_ROWS",
    "SUPPLEMENTAL_TRANSPORT_FAILURE",
]
