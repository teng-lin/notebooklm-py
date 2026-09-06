"""Internal source-batch compatibility and settlement helpers."""

from __future__ import annotations

from dataclasses import replace

from .._idempotency import attach_batch_outcome
from ..outcomes import BatchOutcome, SourceBatchItemOutcome

SourceUrlBatchItem = SourceBatchItemOutcome


def preserve_batch_projection_failure(
    error: BaseException,
    items: list[SourceBatchItemOutcome],
) -> BaseException:
    """Attach every settled member when an adapter projection subsequently fails."""

    outcomes = tuple(
        replace(item.outcome, member=index, input=item.url)
        for index, item in enumerate(items)
        if item.outcome is not None
    )
    if len(outcomes) != len(items):  # pragma: no cover - public outcome invariant
        raise AssertionError("source batch item missing canonical outcome")
    attach_batch_outcome(error, BatchOutcome(items=outcomes))
    return error


__all__ = ["SourceUrlBatchItem", "preserve_batch_projection_failure"]
