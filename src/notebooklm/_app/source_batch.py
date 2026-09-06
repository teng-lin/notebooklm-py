"""Transport-neutral source batch admission limits.

Continuation policy belongs to the public typed outcome returned by
``SourcesAPI.add_urls_batch``. Adapters must not infer it from an HTTP status or
exception category.
"""

from __future__ import annotations

from dataclasses import replace

from ..outcomes import (
    _MAX_BATCH_OUTCOME_ITEMS,
    BatchItemOutcome,
    CommitState,
    ReconciliationReport,
    SourceBatchItemOutcome,
)

__all__ = [
    "MAX_BATCH_URLS",
    "remap_source_batch_item",
    "unattempted_source_batch_item",
    "unknown_source_batch_item",
]

#: Max URL entries accepted by one batch add. It aliases the capacity of the
#: canonical public ``BatchOutcome`` so admission and evidence cannot diverge.
MAX_BATCH_URLS = _MAX_BATCH_OUTCOME_ITEMS


def unattempted_source_batch_item(
    url: str, error: BaseException, *, member: int
) -> SourceBatchItemOutcome:
    """Represent a locally rejected member with positive zero-send evidence."""

    return SourceBatchItemOutcome(
        url=url,
        error=error,
        member=member,
        outcome=BatchItemOutcome(
            member=member,
            input=url,
            commit_state=CommitState.NOT_SENT,
            error=error,
        ),
    )


def unknown_source_batch_item(
    url: str, error: BaseException, *, member: int
) -> SourceBatchItemOutcome:
    """Represent a sent member whose adapter contract lost its settlement."""

    return SourceBatchItemOutcome(
        url=url,
        error=error,
        member=member,
        outcome=BatchItemOutcome(
            member=member,
            input=url,
            commit_state=CommitState.UNKNOWN,
            error=error,
            reconciliation=ReconciliationReport(
                unresolved_inputs=(url,),
                reason="batch result could not be correlated",
            ),
        ),
    )


def remap_source_batch_item(item: SourceBatchItemOutcome, *, member: int) -> SourceBatchItemOutcome:
    """Place a facade outcome back at its adapter request occurrence index."""

    assert item.outcome is not None
    return SourceBatchItemOutcome(
        url=item.outcome.input,
        source=item.source,
        error=item.error,
        member=member,
        outcome=replace(item.outcome, member=member),
    )
