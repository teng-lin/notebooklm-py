"""Public operation-outcome evidence shared across backends.

Commit certainty is deliberately independent from an exception's category.
Callers may use this enum to distinguish a verified refusal from a response
loss without relying on HTTP/gRPC status codes or exception classes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from ._redact import redact

if TYPE_CHECKING:
    from .types import Source

_MAX_TEXT = 200
_MAX_BATCH_OUTCOME_ITEMS = 20
_MAX_COLLECTION = _MAX_BATCH_OUTCOME_ITEMS
_MAX_JOURNAL_RECORDS = 64


def _safe(value: str | None) -> str | None:
    return None if value is None else redact(value, max_length=_MAX_TEXT)


def _safe_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(safe for value in values[:_MAX_COLLECTION] if (safe := _safe(value)))
    )


class CommitState(str, Enum):
    """Evidence about whether one mutation reached a committed outcome."""

    NOT_SENT = "not_sent"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"


class RecoveryAction(str, Enum):
    """Safe next action derived from canonical send evidence."""

    RETRY = "retry"
    INSPECT_AND_RECONCILE = "inspect_and_reconcile"
    WAIT = "wait"
    NONE = "none"


@dataclass(frozen=True)
class LookupSuggestion:
    """Bounded near-match from an ordinary name lookup."""

    id: str
    title: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _safe(self.id) or "")
        object.__setattr__(self, "title", _safe(self.title) or "")


@dataclass(frozen=True)
class ReconciliationCandidate:
    """A resource worth inspecting, without a claim of ownership."""

    id: str
    title: str | None = None
    role: str = "candidate"

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _safe(self.id) or "")
        object.__setattr__(self, "title", _safe(self.title))
        object.__setattr__(self, "role", _safe(self.role) or "candidate")


@dataclass(frozen=True)
class ReconciliationReport:
    """Conservative result of inspecting state after an uncertain send."""

    candidates: tuple[ReconciliationCandidate, ...] = ()
    unresolved_inputs: tuple[str, ...] = ()
    reason: str = "outcome could not be correlated"

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", self.candidates[:_MAX_COLLECTION])
        object.__setattr__(self, "unresolved_inputs", _safe_tuple(self.unresolved_inputs))
        object.__setattr__(
            self,
            "reason",
            _safe(self.reason) or "outcome could not be correlated",
        )


@dataclass(frozen=True)
class _AttemptMetadata:
    """Immutable evidence for one physical attempt of a semantic send."""

    ordinal: int
    commit_state: CommitState
    evidence: str | None = None
    known_resource_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValueError("attempt ordinal must be positive")
        object.__setattr__(self, "evidence", _safe(self.evidence))
        object.__setattr__(self, "known_resource_ids", _safe_tuple(self.known_resource_ids))


@dataclass(frozen=True)
class BatchItemOutcome:
    """One ordered member of a possibly partially committed batch."""

    member: int
    input: str
    commit_state: CommitState
    resource_id: str | None = None
    error: BaseException | None = field(default=None, repr=False, compare=False)
    reconciliation: ReconciliationReport | None = None

    def __post_init__(self) -> None:
        if self.member < 0:
            raise ValueError("member must be non-negative")
        object.__setattr__(self, "input", _safe(self.input) or "")
        object.__setattr__(self, "resource_id", _safe(self.resource_id))
        if self.commit_state is CommitState.CONFIRMED and self.resource_id is None:
            raise ValueError("confirmed batch outcomes require resource_id")
        if self.commit_state is CommitState.CONFIRMED and (
            self.error is not None or self.reconciliation is not None
        ):
            raise ValueError("confirmed batch outcomes cannot carry failure evidence")
        if self.commit_state is CommitState.UNKNOWN and self.reconciliation is None:
            raise ValueError("unknown batch outcomes require a reconciliation report")
        if self.commit_state in (CommitState.REJECTED, CommitState.NOT_SENT) and (
            self.resource_id is not None or self.reconciliation is not None
        ):
            raise ValueError("rejected and unattempted outcomes cannot claim a resource")


@dataclass(frozen=True)
class SourceBatchItemOutcome:
    """One ordered result from :meth:`SourcesAPI.add_urls_batch`.

    ``outcome`` is the canonical continuation contract. ``source`` is present
    only when the backend returned a confirmed resource handle; failures retain
    their typed exception without asking an adapter to infer safety from an
    HTTP status or exception category.
    """

    url: str
    source: Source | None = field(default=None, repr=False)
    error: BaseException | None = field(default=None, repr=False, compare=False)
    member: int = 0
    outcome: BatchItemOutcome | None = None

    def __post_init__(self) -> None:
        if (self.source is None) == (self.error is None):
            raise ValueError("exactly one of source or error must be set")
        if self.member < 0:
            raise ValueError("member must be non-negative")
        if self.outcome is None:
            metadata = getattr(self.error, "operation_metadata", None)
            state = (
                CommitState.CONFIRMED
                if self.source is not None
                else metadata.commit_state
                if metadata is not None and metadata.commit_state is not None
                else CommitState.UNKNOWN
            )
            object.__setattr__(
                self,
                "outcome",
                BatchItemOutcome(
                    member=self.member,
                    input=self.url,
                    commit_state=state,
                    resource_id=(
                        self.source.id
                        if self.source is not None
                        else metadata.source_id
                        if metadata is not None and metadata.source_id is not None
                        else metadata.known_resource_ids[0]
                        if metadata is not None and metadata.known_resource_ids
                        else None
                    ),
                    error=self.error,
                    reconciliation=(
                        (
                            ReconciliationReport(
                                unresolved_inputs=(self.url,),
                                reason="batch member commit could not be correlated",
                            )
                            if metadata is None or metadata.reconciliation is None
                            else metadata.reconciliation
                        )
                        if state is CommitState.UNKNOWN
                        else None
                    ),
                ),
            )
        assert self.outcome is not None
        if self.outcome.member != self.member:
            raise ValueError("source batch item must match its canonical member outcome")
        if self.outcome.commit_state is CommitState.CONFIRMED:
            if self.source is None or self.error is not None:
                raise ValueError("confirmed source batch outcomes require only a source")
            if self.source.id != self.outcome.resource_id:
                raise ValueError("confirmed source id must match the canonical outcome")
        elif self.source is not None:
            raise ValueError("unconfirmed source batch outcomes cannot expose a source")
        # Retain only the canonical redacted/capped spelling.  Keeping the raw
        # caller URL here would leak credentials through this public dataclass's
        # ``url`` attribute and generated ``repr`` even when adapters correctly
        # projected ``outcome.input``.
        object.__setattr__(self, "url", self.outcome.input)

    @property
    def input(self) -> str:
        """Return the adapter-neutral input spelling."""

        assert self.outcome is not None
        return self.outcome.input


@dataclass(frozen=True)
class BatchOutcome:
    """Ordered batch settlement retained when a top-level call fails."""

    items: tuple[BatchItemOutcome, ...]
    whole_request_retriable: bool = False

    def __post_init__(self) -> None:
        if len(self.items) > _MAX_BATCH_OUTCOME_ITEMS:
            raise ValueError(f"batch outcomes are capped at {_MAX_BATCH_OUTCOME_ITEMS} items")
        if tuple(item.member for item in self.items) != tuple(range(len(self.items))):
            raise ValueError("batch outcome members must be ordered occurrence indexes")
        if self.whole_request_retriable and any(
            item.commit_state in (CommitState.CONFIRMED, CommitState.UNKNOWN) for item in self.items
        ):
            raise ValueError("a committed or unknown batch cannot be retried as a whole")


@dataclass(frozen=True)
class OperationMetadata:
    """Immutable, redaction-safe snapshot of one semantic send.

    ``commit_state=None`` means no mutation evidence was attached.  It is not
    a fifth commit state.  Candidate matches are deliberately separate from
    ``known_resource_ids`` because they do not prove provenance.
    """

    commit_state: CommitState | None = None
    operation: str | None = None
    invocation_id: str | None = None
    method: str | None = None
    phase: str | None = None
    member: int | None = None
    known_resource_ids: tuple[str, ...] = ()
    recovery_action: RecoveryAction = RecoveryAction.NONE
    source_id: str | None = None
    stage: str | None = None
    reconciliation: ReconciliationReport | None = None
    batch_outcome: BatchOutcome | None = None
    attempts: tuple[_AttemptMetadata, ...] = ()
    prerequisite_ids: tuple[str, ...] = ()
    entries: tuple[OperationMetadata, ...] = ()

    def __post_init__(self) -> None:
        for name in ("operation", "invocation_id", "method", "phase", "source_id", "stage"):
            object.__setattr__(self, name, _safe(getattr(self, name)))
        object.__setattr__(self, "known_resource_ids", _safe_tuple(self.known_resource_ids))
        object.__setattr__(self, "prerequisite_ids", _safe_tuple(self.prerequisite_ids))
        object.__setattr__(self, "attempts", self.attempts[:_MAX_JOURNAL_RECORDS])
        object.__setattr__(self, "entries", self.entries[:_MAX_JOURNAL_RECORDS])


def _wire_text(value: object) -> str:
    return redact(value, max_length=_MAX_TEXT)


def _wire_report(report: ReconciliationReport) -> dict[str, object]:
    return {
        "candidates": [
            {
                "id": _wire_text(item.id),
                **({"title": _wire_text(item.title)} if item.title is not None else {}),
                "role": _wire_text(item.role),
            }
            for item in report.candidates[:_MAX_COLLECTION]
        ],
        "unresolved_inputs": [
            _wire_text(item) for item in report.unresolved_inputs[:_MAX_COLLECTION]
        ],
        "reason": _wire_text(report.reason),
    }


def _wire_batch_item(item: BatchItemOutcome) -> dict[str, object]:
    projected: dict[str, object] = {
        "member": item.member,
        "input": _wire_text(item.input),
        "commit_state": item.commit_state.value,
    }
    if item.resource_id is not None:
        projected["resource_id"] = _wire_text(item.resource_id)
    if item.reconciliation is not None:
        projected["reconciliation"] = _wire_report(item.reconciliation)
    if item.error is not None:
        projected["error"] = {
            "type": type(item.error).__name__,
            "message": _wire_text(item.error),
        }
    return projected


def _wire_metadata(metadata: OperationMetadata) -> dict[str, object]:
    projected: dict[str, object] = {"recovery_action": metadata.recovery_action.value}
    if metadata.commit_state is not None:
        projected["commit_state"] = metadata.commit_state.value
    if metadata.operation is not None:
        projected["operation"] = _wire_text(metadata.operation)
    if metadata.known_resource_ids:
        projected["known_resource_ids"] = [
            _wire_text(item) for item in metadata.known_resource_ids[:_MAX_COLLECTION]
        ]
    if metadata.source_id is not None:
        projected["source_id"] = _wire_text(metadata.source_id)
    if metadata.stage is not None:
        projected["stage"] = _wire_text(metadata.stage)
    if metadata.prerequisite_ids:
        projected["prerequisite_ids"] = [
            _wire_text(item) for item in metadata.prerequisite_ids[:_MAX_COLLECTION]
        ]
    if metadata.reconciliation is not None:
        projected["reconciliation"] = _wire_report(metadata.reconciliation)
    if metadata.batch_outcome is not None:
        projected["batch_outcome"] = {
            "whole_request_retriable": metadata.batch_outcome.whole_request_retriable,
            "items": [
                _wire_batch_item(item) for item in metadata.batch_outcome.items[:_MAX_COLLECTION]
            ],
        }
    return projected


def operation_metadata_payload(exc: BaseException | None) -> dict[str, object]:
    """Return the bounded adapter projection for a library exception carrier."""

    metadata = getattr(exc, "operation_metadata", None) or getattr(exc, "_operation_metadata", None)
    return {} if metadata is None else _wire_metadata(metadata)


def format_operation_metadata(payload: dict[str, object]) -> str:
    """Flatten a projected payload without re-reading the exception carrier."""

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def redact_operation_text(value: object) -> str:
    """Return one bounded, redacted adapter-facing detail string."""

    return _wire_text(value)


__all__ = [
    "BatchItemOutcome",
    "BatchOutcome",
    "CommitState",
    "LookupSuggestion",
    "OperationMetadata",
    "ReconciliationCandidate",
    "ReconciliationReport",
    "RecoveryAction",
    "SourceBatchItemOutcome",
]
