"""Internal source-batch compatibility and settlement helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .._idempotency import attach_batch_outcome
from ..exceptions import ValidationError
from ..outcomes import (
    _MAX_BATCH_OUTCOME_ITEMS,
    BatchItemOutcome,
    BatchOutcome,
    CommitState,
    ReconciliationReport,
    SourceBatchItemOutcome,
)

SourceUrlBatchItem = SourceBatchItemOutcome


def validate_source_batch_occurrences(urls: Sequence[str]) -> None:
    """Reject an oversized public batch before any backend mutation can run.

    The bound applies to input occurrences, not unique URL values, because each
    occurrence owns a distinct ordered outcome and operation-journal member.
    """

    if len(urls) > _MAX_BATCH_OUTCOME_ITEMS:
        raise ValidationError(
            f"urls must contain at most {_MAX_BATCH_OUTCOME_ITEMS} entries; got {len(urls)}"
        )


def preserve_batch_projection_failure(
    error: BaseException,
    items: list[SourceBatchItemOutcome],
) -> BaseException:
    """Attach every settled member when an adapter projection subsequently fails."""

    outcomes = tuple(
        replace(item.outcome, member=index)
        for index, item in enumerate(items)
        if item.outcome is not None
    )
    if len(outcomes) != len(items):  # pragma: no cover - public outcome invariant
        raise AssertionError("source batch item missing canonical outcome")
    attach_batch_outcome(error, BatchOutcome(items=outcomes))
    return error


def preserve_batch_call_failure(
    error: BaseException,
    *,
    local_items: Sequence[SourceBatchItemOutcome | None],
    valid_positions: Sequence[int],
    valid_inputs: Sequence[str],
) -> BaseException:
    """Merge local and facade settlements onto an escaping batch exception.

    Facade batch metadata uses indexes relative to the filtered list of valid
    inputs.  Adapters additionally settle locally rejected inputs as NOT_SENT,
    so those relative indexes must be mapped back to the original request before
    the same exception is re-raised.  Any valid member absent from the facade's
    attachment remains UNKNOWN rather than disappearing.
    """

    if len(valid_positions) != len(valid_inputs):  # pragma: no cover - caller invariant
        raise AssertionError("valid source positions and inputs must align")

    merged: list[BatchItemOutcome | None] = [None] * len(local_items)
    for member, item in enumerate(local_items):
        if item is not None:
            assert item.outcome is not None
            merged[member] = replace(item.outcome, member=member)

    metadata = getattr(error, "operation_metadata", None) or getattr(
        error, "_operation_metadata", None
    )
    facade_batch = None if metadata is None else metadata.batch_outcome
    if facade_batch is not None:
        for item in facade_batch.items:
            if 0 <= item.member < len(valid_positions):
                merged[valid_positions[item.member]] = replace(
                    item,
                    member=valid_positions[item.member],
                )

    synthesized = False
    for relative_member, (member, input_value) in enumerate(
        zip(valid_positions, valid_inputs, strict=True)
    ):
        if merged[member] is not None:
            continue
        synthesized = True
        merged[member] = BatchItemOutcome(
            member=member,
            input=input_value,
            commit_state=CommitState.UNKNOWN,
            error=error,
            reconciliation=ReconciliationReport(
                unresolved_inputs=(input_value,),
                reason=f"batch result member {relative_member} could not be correlated",
            ),
        )

    if any(item is None for item in merged):  # pragma: no cover - caller invariant
        raise AssertionError("source batch failure lost positional outcomes")
    complete = tuple(item for item in merged if item is not None)
    attach_batch_outcome(
        error,
        BatchOutcome(
            items=complete,
            whole_request_retriable=(
                facade_batch is not None
                and facade_batch.whole_request_retriable
                and not synthesized
            ),
        ),
    )
    return error


__all__ = [
    "SourceUrlBatchItem",
    "preserve_batch_call_failure",
    "preserve_batch_projection_failure",
    "validate_source_batch_occurrences",
]
