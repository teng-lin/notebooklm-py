"""Public source-batch outcome contract and adapter projection preservation."""

from __future__ import annotations

import asyncio

import pytest

from notebooklm._source.batch import preserve_batch_projection_failure
from notebooklm.exceptions import SourceAddError
from notebooklm.outcomes import (
    BatchItemOutcome,
    CommitState,
    ReconciliationReport,
    SourceBatchItemOutcome,
    operation_metadata_payload,
)
from notebooklm.types import Source


def _failed(state: CommitState, member: int) -> SourceBatchItemOutcome:
    url = f"https://{member}.example.com"
    error = SourceAddError(url)
    report = (
        ReconciliationReport(unresolved_inputs=(url,), reason="readback inconclusive")
        if state is CommitState.UNKNOWN
        else None
    )
    return SourceBatchItemOutcome(
        url=url,
        error=error,
        member=member,
        outcome=BatchItemOutcome(
            member=member,
            input=url,
            commit_state=state,
            error=error,
            reconciliation=report,
        ),
    )


def test_public_source_batch_contract_represents_all_four_states() -> None:
    confirmed = SourceBatchItemOutcome(
        url="https://ok.example.com",
        source=Source(id="source-ok"),
        outcome=BatchItemOutcome(
            member=0,
            input="https://ok.example.com",
            commit_state=CommitState.CONFIRMED,
            resource_id="source-ok",
        ),
    )
    items = [
        confirmed,
        _failed(CommitState.REJECTED, 1),
        _failed(CommitState.UNKNOWN, 2),
        _failed(CommitState.NOT_SENT, 3),
    ]

    assert [item.outcome.commit_state for item in items if item.outcome] == [
        CommitState.CONFIRMED,
        CommitState.REJECTED,
        CommitState.UNKNOWN,
        CommitState.NOT_SENT,
    ]


@pytest.mark.parametrize("error", [RuntimeError("projection failed"), asyncio.CancelledError()])
def test_projection_failure_retains_every_settled_member(error: BaseException) -> None:
    confirmed = SourceBatchItemOutcome(
        url="https://ok.example.com",
        source=Source(id="source-ok"),
        outcome=BatchItemOutcome(
            member=0,
            input="https://ok.example.com",
            commit_state=CommitState.CONFIRMED,
            resource_id="source-ok",
        ),
    )
    unknown = _failed(CommitState.UNKNOWN, 1)

    assert preserve_batch_projection_failure(error, [confirmed, unknown]) is error
    batch = operation_metadata_payload(error)["batch_outcome"]
    assert [item["commit_state"] for item in batch["items"]] == ["confirmed", "unknown"]
    assert batch["items"][0]["resource_id"] == "source-ok"
    assert batch["whole_request_retriable"] is False
