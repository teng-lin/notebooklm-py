"""Transport-neutral positional result for source URL batch creation."""

from __future__ import annotations

from dataclasses import dataclass

from ..exceptions import SourceAddError
from ..outcomes import BatchItemOutcome, CommitState, ReconciliationReport
from ..types import Source


@dataclass(frozen=True)
class SourceUrlBatchItem:
    """One positional outcome from a backend-owned URL-add workflow."""

    url: str
    source: Source | None = None
    error: SourceAddError | None = None
    member: int = 0
    outcome: BatchItemOutcome | None = None

    def __post_init__(self) -> None:
        if (self.source is None) == (self.error is None):
            raise ValueError("exactly one of source or error must be set")
        if self.member < 0:
            raise ValueError("member must be non-negative")
        if self.outcome is None:
            metadata = None if self.error is None else self.error.operation_metadata
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


__all__ = ["SourceUrlBatchItem"]
