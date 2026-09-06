"""Public source-batch outcome contract and adapter projection preservation."""

from __future__ import annotations

import asyncio

import pytest

from notebooklm._idempotency import attach_batch_outcome
from notebooklm._redact import redact
from notebooklm._source.batch import preserve_batch_call_failure, preserve_batch_projection_failure
from notebooklm.exceptions import SourceAddError
from notebooklm.outcomes import (
    BatchItemOutcome,
    BatchOutcome,
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


def test_public_source_batch_item_retains_only_canonical_input() -> None:
    raw = (
        "https://private-user:private-password@example.com/path?token=private-token&pad="
        + "x" * 300
    )
    item = SourceBatchItemOutcome(url=raw, source=Source(id="source-ok", url=raw))

    assert item.outcome is not None
    assert item.url == item.input == item.outcome.input == redact(raw, max_length=200)
    assert len(item.url) == 201
    assert item.source is not None and item.source.url == raw
    assert "resource_id='source-ok'" in repr(item)
    assert "private-user" not in repr(item)
    assert "private-password" not in repr(item)
    assert "private-token" not in repr(item)


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


@pytest.mark.parametrize("error", [RuntimeError("batch failed"), asyncio.CancelledError()])
def test_call_failure_merges_local_and_relative_facade_members(error: BaseException) -> None:
    raw_invalid = "ftp://local-user:local-password@example.com/path?token=local-token"
    raw_confirmed = "https://ok-user:ok-password@example.com/path?token=ok-token"
    raw_rejected = "https://no-user:no-password@example.com/path?token=no-token"
    local = _failed(CommitState.NOT_SENT, 0)
    # Model the adapter's locally rejected raw member, including its canonical input.
    local = SourceBatchItemOutcome(
        url=raw_invalid,
        error=local.error,
        outcome=BatchItemOutcome(
            member=0,
            input=raw_invalid,
            commit_state=CommitState.NOT_SENT,
            error=local.error,
        ),
    )
    rejected_error = SourceAddError(raw_rejected)
    attach_batch_outcome(
        error,
        BatchOutcome(
            items=(
                BatchItemOutcome(
                    member=0,
                    input=raw_confirmed,
                    commit_state=CommitState.CONFIRMED,
                    resource_id="source-ok",
                ),
                BatchItemOutcome(
                    member=1,
                    input=raw_rejected,
                    commit_state=CommitState.REJECTED,
                    error=rejected_error,
                ),
            )
        ),
    )

    assert (
        preserve_batch_call_failure(
            error,
            local_items=[local, None, None],
            valid_positions=[1, 2],
            valid_inputs=[raw_confirmed, raw_rejected],
        )
        is error
    )
    items = operation_metadata_payload(error)["batch_outcome"]["items"]
    assert [item["member"] for item in items] == [0, 1, 2]
    assert [item["commit_state"] for item in items] == ["not_sent", "confirmed", "rejected"]
    assert items[1]["resource_id"] == "source-ok"
    for item, raw in zip(items, [raw_invalid, raw_confirmed, raw_rejected], strict=True):
        assert item["input"] == redact(raw, max_length=200)


def test_call_failure_synthesizes_unknown_for_missing_valid_member() -> None:
    error = RuntimeError("batch failed before attaching all members")

    preserve_batch_call_failure(
        error,
        local_items=[None],
        valid_positions=[0],
        valid_inputs=["https://user:password@example.com/path?token=secret"],
    )

    item = operation_metadata_payload(error)["batch_outcome"]["items"][0]
    assert item["member"] == 0
    assert item["commit_state"] == "unknown"
    assert item["input"] == "https://***@example.com/path?token=***"
