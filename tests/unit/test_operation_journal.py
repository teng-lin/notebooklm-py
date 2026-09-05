"""Canonical typed outcome and private journal contracts (plan P2)."""

from notebooklm._idempotency import (
    OperationJournal,
    attach_journal_entry,
    attach_reconciliation_report,
    mark_commit_state,
    mark_unconfirmed,
    reconciliation_report,
)
from notebooklm.exceptions import RPCError, SourceProcessingError, SourceTimeoutError
from notebooklm.outcomes import CommitState, RecoveryAction


def _entry():
    journal = OperationJournal("sources.add_url")
    return journal.new_entry(method="ADD_SOURCE")


def test_journal_aggregates_unknown_before_later_refusal() -> None:
    entry = _entry()
    entry.mark_dispatched()
    entry.mark_dispatched()
    entry.record(CommitState.REJECTED, "decoded refusal")

    assert entry.commit_state is CommitState.UNKNOWN
    assert tuple(attempt.commit_state for attempt in entry.attempts) == (
        CommitState.UNKNOWN,
        CommitState.REJECTED,
    )


def test_journal_settles_verified_zero_send_then_confirmed_retry() -> None:
    entry = _entry()
    first = entry.mark_dispatched()
    entry.record(CommitState.NOT_SENT, "connect failure", attempt=first)
    entry.mark_dispatched()
    entry.record(CommitState.CONFIRMED, "decoded success", known_resource_ids=("source-1",))

    snapshot = entry.snapshot()
    assert snapshot.commit_state is CommitState.CONFIRMED
    assert snapshot.known_resource_ids == ("source-1",)
    assert tuple(attempt.ordinal for attempt in snapshot.attempts) == (1, 2)


def test_later_success_does_not_settle_earlier_unknown() -> None:
    entry = _entry()
    entry.mark_dispatched()
    entry.mark_dispatched()
    entry.record(CommitState.CONFIRMED, "decoded success", known_resource_ids=("source-2",))

    assert entry.commit_state is CommitState.UNKNOWN
    assert entry.known_resource_ids == ("source-2",)


def test_semantic_identity_keeps_duplicate_members_distinct() -> None:
    journal = OperationJournal("sources.add_urls")
    invocation_id = journal.invocation_id()
    first = journal.new_entry(method="ADD_SOURCE", member=0, invocation_id=invocation_id)
    second = journal.new_entry(method="ADD_SOURCE", member=1, invocation_id=invocation_id)

    assert first is not second
    assert first.identity.member == 0
    assert second.identity.member == 1


def test_exception_projections_are_derived_from_one_carrier() -> None:
    error = RPCError("lost")
    mark_unconfirmed(error, operation="chat", source_id="source-1", stage="readback")

    assert error.commit_state is CommitState.UNKNOWN
    assert error.unconfirmed is True
    assert error.operation_metadata is not None
    assert error.operation_metadata.operation == "chat"
    assert error.source_id == "source-1"
    assert error.stage == "readback"
    assert error.batch_outcome is None


def test_positive_evidence_is_not_downgraded_by_outer_wrapper() -> None:
    error = mark_commit_state(RPCError("refused"), CommitState.REJECTED)
    mark_unconfirmed(error)

    assert error.commit_state is CommitState.REJECTED
    assert error.unconfirmed is False


def test_reconciliation_candidates_are_not_known_resource_ids() -> None:
    error = RPCError("lost")
    report = reconciliation_report(["candidate-1"], ["https://example.test"])
    attach_reconciliation_report(error, report, operation="sources.add_url")

    assert error.operation_metadata is not None
    assert error.operation_metadata.known_resource_ids == ()
    assert error.operation_metadata.reconciliation == report
    assert error.reconciliation_candidates == ("candidate-1",)  # type: ignore[attr-defined]
    assert error.unconfirmed is True


def test_journal_snapshot_is_authoritative_on_exception() -> None:
    entry = _entry()
    entry.mark_dispatched()
    entry.record(CommitState.CONFIRMED, "decoded", known_resource_ids=("source-1",))
    attach_journal_entry(error := RPCError("readback failed"), entry)

    assert error.commit_state is CommitState.CONFIRMED
    assert error.operation_metadata is not None
    assert error.operation_metadata.known_resource_ids == ("source-1",)


def test_workflow_snapshot_aggregates_all_semantic_entries_and_ids() -> None:
    journal = OperationJournal("sources.add_url")
    first = journal.new_entry(method="REGISTER", phase="registration")
    first.mark_dispatched()
    first.record(CommitState.CONFIRMED, "registered", known_resource_ids=("source-1",))
    second = journal.new_entry(method="COMMIT")
    second.mark_dispatched()

    snapshot = journal.snapshot(primary=second)

    assert snapshot.commit_state is CommitState.UNKNOWN
    assert snapshot.known_resource_ids == ("source-1",)
    assert tuple(entry.method for entry in snapshot.entries) == ("REGISTER", "COMMIT")
    assert tuple(entry.commit_state for entry in snapshot.entries) == (
        CommitState.CONFIRMED,
        CommitState.UNKNOWN,
    )


def test_positive_metadata_accepts_missing_identity_without_changing_state() -> None:
    error = mark_commit_state(RPCError("refused"), CommitState.REJECTED)

    mark_unconfirmed(
        error,
        operation="sources.add_file",
        source_id="source-1",
        stage="register",
    )

    assert error.commit_state is CommitState.REJECTED
    assert error.unconfirmed is False
    assert error.operation_metadata is not None
    assert error.operation_metadata.operation == "sources.add_file"
    assert error.source_id == "source-1"
    assert error.stage == "register"


def test_legacy_source_identity_setters_create_evidence_free_metadata() -> None:
    for error in (
        SourceProcessingError("source-1"),
        SourceTimeoutError("source-2", 1.0),
    ):
        assert error.operation_metadata is not None
        assert error.commit_state is None
        assert error.unconfirmed is False
        assert error.source_id in {"source-1", "source-2"}
        assert error.operation_metadata.recovery_action is RecoveryAction.NONE
