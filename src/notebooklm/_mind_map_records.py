"""Transport-neutral records and operation definitions for mind maps."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ._operations import CallPolicy, Operation, OperationDef, OperationTier


@dataclass(frozen=True, slots=True)
class MindMapRecord:
    """One backend-neutral mind map with its optional JSON tree payload."""

    id: str
    notebook_id: str
    title: str
    kind: str
    created_at: datetime | None = None
    tree_json: str | None = None


@dataclass(frozen=True, slots=True)
class MindMapListInput:
    """Notebook whose active note-backed mind maps are requested."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class MindMapListResult:
    """Active note-backed mind maps in backend order."""

    mind_maps: tuple[MindMapRecord, ...]


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
    """Note-backed mind-map generation options."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
    language: str | None = "en"
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
    """Interactive Studio mind-map generation options."""

    notebook_id: str
    source_ids: tuple[str, ...] | None = None
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
]
