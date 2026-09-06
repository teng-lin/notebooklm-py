"""Canonical typed outcome and private journal contracts (plan P2)."""

import httpx
import pytest

from notebooklm._idempotency import (
    OperationJournal,
    attach_batch_outcome,
    attach_journal_entry,
    attach_reconciliation_report,
    call_unconfirmed_on_transport_loss,
    mark_commit_state,
    mark_unconfirmed,
    reconciliation_report,
)
from notebooklm._web.transport.errors import TransportServerError
from notebooklm._web.transport.middleware.context import RPC_CONTEXT_JOURNAL
from notebooklm._web.transport.middleware.core import RpcRequest
from notebooklm.auth import AuthTokens
from notebooklm.exceptions import NetworkError, RPCError, SourceProcessingError, SourceTimeoutError
from notebooklm.outcomes import BatchItemOutcome, BatchOutcome, CommitState, RecoveryAction
from tests._helpers.client_factory import build_client_shell_for_tests


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


def test_member_batch_attachment_preserves_state_and_targeted_recovery() -> None:
    error = mark_commit_state(
        NetworkError("member never sent"),
        CommitState.NOT_SENT,
        recovery_action=RecoveryAction.RETRY,
    )
    unknown_report = reconciliation_report([], ["https://unknown.test"])
    batch = BatchOutcome(
        (
            BatchItemOutcome(0, "https://not-sent.test", CommitState.NOT_SENT),
            BatchItemOutcome(
                1,
                "https://unknown.test",
                CommitState.UNKNOWN,
                reconciliation=unknown_report,
            ),
        ),
        whole_request_retriable=False,
    )

    attach_batch_outcome(error, batch, preserve_commit_state=True)

    assert error.commit_state is CommitState.NOT_SENT
    assert error.operation_metadata is not None
    assert error.operation_metadata.recovery_action is RecoveryAction.RETRY
    assert error.batch_outcome is batch
    assert error.batch_outcome.whole_request_retriable is False


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


@pytest.mark.asyncio
async def test_verified_terminal_not_sent_survives_unconfirmed_wrapper() -> None:
    entry = _entry()
    attempt = entry.mark_dispatched()
    entry.record(CommitState.NOT_SENT, "verified zero-send", attempt=attempt)
    failure = NetworkError("connect failed before write")

    async def send() -> None:
        raise failure

    with pytest.raises(NetworkError) as raised:
        await call_unconfirmed_on_transport_loss(
            send,
            method="ADD_SOURCE",
            what="URL source",
            operation="sources.add_url",
            journal_entry=entry,
        )

    assert raised.value is failure
    assert failure.commit_state is CommitState.NOT_SENT
    assert failure.unconfirmed is False
    assert failure.operation_metadata is not None
    assert failure.operation_metadata.recovery_action is RecoveryAction.NONE


@pytest.mark.asyncio
async def test_missing_fake_dispatch_handoff_defaults_transport_loss_to_unknown() -> None:
    entry = _entry()
    failure = NetworkError("response lost")

    async def send() -> None:
        raise failure

    with pytest.raises(NetworkError) as raised:
        await call_unconfirmed_on_transport_loss(
            send,
            method="ADD_SOURCE",
            what="URL source",
            journal_entry=entry,
        )

    assert raised.value is failure
    assert entry.commit_state is CommitState.UNKNOWN
    assert [attempt.commit_state for attempt in entry.attempts] == [CommitState.UNKNOWN]
    assert failure.commit_state is CommitState.UNKNOWN
    assert failure.unconfirmed is True
    assert failure.operation_metadata is not None
    assert failure.operation_metadata.recovery_action is RecoveryAction.INSPECT_AND_RECONCILE


@pytest.mark.asyncio
async def test_authoritative_predispatch_not_sent_survives_without_a_dispatch_record() -> None:
    entry = _entry()
    failure = mark_commit_state(
        NetworkError("verified zero send"),
        CommitState.NOT_SENT,
        recovery_action=RecoveryAction.RETRY,
    )

    async def send() -> None:
        raise failure

    with pytest.raises(NetworkError) as raised:
        await call_unconfirmed_on_transport_loss(
            send,
            method="ADD_SOURCE",
            what="URL source",
            journal_entry=entry,
        )

    assert raised.value is failure
    assert entry.commit_state is CommitState.NOT_SENT
    assert entry.attempts == ()
    assert failure.commit_state is CommitState.NOT_SENT
    assert failure.unconfirmed is False
    assert failure.operation_metadata is not None
    assert failure.operation_metadata.recovery_action is RecoveryAction.RETRY


@pytest.mark.asyncio
async def test_journal_predispatch_not_sent_evidence_survives_an_unmarked_error() -> None:
    entry = _entry()
    entry.record(CommitState.NOT_SENT, "verified before dispatch")
    failure = NetworkError("connection unavailable")

    async def send() -> None:
        raise failure

    with pytest.raises(NetworkError):
        await call_unconfirmed_on_transport_loss(
            send,
            method="ADD_SOURCE",
            what="URL source",
            journal_entry=entry,
        )

    assert entry.commit_state is CommitState.NOT_SENT
    assert entry.attempts == ()
    assert failure.commit_state is CommitState.NOT_SENT
    assert failure.unconfirmed is False


@pytest.mark.asyncio
async def test_real_web_terminal_records_verified_connect_failure_as_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_client_shell_for_tests(
        auth=AuthTokens(csrf_token="csrf", session_id="session", cookies={})
    )
    terminal = client._web_runtime.composed.transport
    entry = _entry()

    async def connect_failure(*args: object, **kwargs: object) -> httpx.Response:
        del args, kwargs
        request = httpx.Request("POST", "https://example.test")
        raise httpx.ConnectError("connect failed", request=request)

    monkeypatch.setattr(terminal._kernel, "post", connect_failure)
    request = RpcRequest(
        "https://example.test",
        {},
        b"payload",
        {RPC_CONTEXT_JOURNAL: entry},
    )

    with pytest.raises(TransportServerError):
        await terminal.terminal(request)

    assert entry.commit_state is CommitState.NOT_SENT
    assert [attempt.commit_state for attempt in entry.attempts] == [CommitState.NOT_SENT]
