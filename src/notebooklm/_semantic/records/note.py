"""Transport-neutral records and operation definitions for plain notes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..._operations import CallPolicy, Operation, OperationDef


@dataclass(frozen=True, slots=True)
class NoteRecord:
    """Neutral note value returned by note semantic operations."""

    id: str
    notebook_id: str
    title: str
    content: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NoteListInput:
    """Notebook whose active plain notes should be listed."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class NoteListResult:
    """Active plain notes in backend order."""

    notes: tuple[NoteRecord, ...]


@dataclass(frozen=True, slots=True)
class NoteGetInput:
    """Notebook and exact note identity requested by note get."""

    notebook_id: str
    note_id: str


@dataclass(frozen=True, slots=True)
class NoteGetResult:
    """Exact note lookup result; ``None`` is a genuine miss."""

    note: NoteRecord | None


@dataclass(frozen=True, slots=True)
class NoteCreateInput:
    """Requested plain-note value."""

    notebook_id: str
    title: str
    content: str


@dataclass(frozen=True, slots=True)
class NoteCreateResult:
    """Created note identity and creation metadata before finalization."""

    note: NoteRecord


@dataclass(frozen=True, slots=True)
class NoteUpdateInput:
    """Exact note identity and replacement content/title."""

    notebook_id: str
    note_id: str
    content: str
    title: str


@dataclass(frozen=True, slots=True)
class NoteUpdateResult:
    """Successful in-place note update."""


@dataclass(frozen=True, slots=True)
class NoteDeleteInput:
    """Exact note identity to soft-delete idempotently."""

    notebook_id: str
    note_id: str


@dataclass(frozen=True, slots=True)
class NoteDeleteResult:
    """Successful idempotent plain-note deletion."""


NOTE_LIST_DEF: OperationDef[NoteListInput, NoteListResult] = OperationDef(
    Operation.NOTE_LIST,
    CallPolicy.READ,
    NoteListInput,
    NoteListResult,
)
NOTE_GET_DEF: OperationDef[NoteGetInput, NoteGetResult] = OperationDef(
    Operation.NOTE_GET,
    CallPolicy.READ,
    NoteGetInput,
    NoteGetResult,
)
NOTE_CREATE_DEF: OperationDef[NoteCreateInput, NoteCreateResult] = OperationDef(
    Operation.NOTE_CREATE,
    CallPolicy.MUTATION,
    NoteCreateInput,
    NoteCreateResult,
)
NOTE_UPDATE_DEF: OperationDef[NoteUpdateInput, NoteUpdateResult] = OperationDef(
    Operation.NOTE_UPDATE,
    CallPolicy.MUTATION,
    NoteUpdateInput,
    NoteUpdateResult,
)
NOTE_DELETE_DEF: OperationDef[NoteDeleteInput, NoteDeleteResult] = OperationDef(
    Operation.NOTE_DELETE,
    CallPolicy.MUTATION,
    NoteDeleteInput,
    NoteDeleteResult,
)


__all__ = [
    "NOTE_CREATE_DEF",
    "NOTE_DELETE_DEF",
    "NOTE_GET_DEF",
    "NOTE_LIST_DEF",
    "NOTE_UPDATE_DEF",
    "NoteCreateInput",
    "NoteCreateResult",
    "NoteDeleteInput",
    "NoteDeleteResult",
    "NoteGetInput",
    "NoteGetResult",
    "NoteListInput",
    "NoteListResult",
    "NoteRecord",
    "NoteUpdateInput",
    "NoteUpdateResult",
]
