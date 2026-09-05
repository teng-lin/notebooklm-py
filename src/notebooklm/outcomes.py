"""Public operation-outcome evidence shared across backends.

Commit certainty is deliberately independent from an exception's category.
Callers may use this enum to distinguish a verified refusal from a response
loss without relying on HTTP/gRPC status codes or exception classes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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


@dataclass(frozen=True)
class ReconciliationCandidate:
    """A resource worth inspecting, without a claim of ownership."""

    id: str
    title: str | None = None
    role: str = "candidate"


@dataclass(frozen=True)
class ReconciliationReport:
    """Conservative result of inspecting state after an uncertain send."""

    candidates: tuple[ReconciliationCandidate, ...] = ()
    unresolved_inputs: tuple[str, ...] = ()
    reason: str = "outcome could not be correlated"


@dataclass(frozen=True)
class _AttemptMetadata:
    """Immutable evidence for one physical attempt of a semantic send."""

    ordinal: int
    commit_state: CommitState
    evidence: str | None = None
    known_resource_ids: tuple[str, ...] = ()


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
        if self.commit_state is CommitState.CONFIRMED and self.resource_id is None:
            raise ValueError("confirmed batch outcomes require resource_id")


@dataclass(frozen=True)
class BatchOutcome:
    """Ordered batch settlement retained when a top-level call fails."""

    items: tuple[BatchItemOutcome, ...]
    whole_request_retriable: bool = False

    def __post_init__(self) -> None:
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


__all__ = [
    "BatchItemOutcome",
    "BatchOutcome",
    "CommitState",
    "LookupSuggestion",
    "OperationMetadata",
    "ReconciliationCandidate",
    "ReconciliationReport",
    "RecoveryAction",
]
