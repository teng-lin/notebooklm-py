"""Producer/consumer agreement for canonical commit evidence (plan P2)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.util import find_spec

import pytest

from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._idempotency import (
    OperationJournal,
    attach_journal_entry,
    attach_operation_journal,
)
from notebooklm.cli.error_handler import handle_errors
from notebooklm.exceptions import DecodingError, NetworkError, RateLimitError, RPCError
from notebooklm.outcomes import CommitState, RecoveryAction


@dataclass(frozen=True)
class _Expected:
    state: CommitState
    recovery: RecoveryAction
    category: ErrorCategory
    retriable: bool
    unconfirmed: bool
    cli_code: str
    mcp_code: str
    rest_category: str


_UNKNOWN = _Expected(
    CommitState.UNKNOWN,
    RecoveryAction.INSPECT_AND_RECONCILE,
    ErrorCategory.RPC,
    False,
    True,
    "UNCONFIRMED_WRITE",
    "RPC",
    "rpc",
)
_CONFIRMED_READBACK = _Expected(
    CommitState.CONFIRMED,
    RecoveryAction.INSPECT_AND_RECONCILE,
    ErrorCategory.RPC,
    False,
    True,
    "UNCONFIRMED_WRITE",
    "RPC",
    "rpc",
)
_REJECTED = _Expected(
    CommitState.REJECTED,
    RecoveryAction.NONE,
    ErrorCategory.RATE_LIMITED,
    True,
    False,
    "RATE_LIMITED",
    "RATE_LIMITED",
    "rate_limited",
)
_NOT_SENT = _Expected(
    CommitState.NOT_SENT,
    RecoveryAction.RETRY,
    ErrorCategory.NETWORK,
    True,
    False,
    "NETWORK_ERROR",
    "NETWORK",
    "network",
)


def _produce(backend: str, producer: str) -> tuple[RPCError, _Expected]:
    journal = OperationJournal(f"{backend}.matrix")
    mutation = journal.new_entry(method=f"{backend}.Create", phase="mutation")

    if producer == "decoded_refusal":
        mutation.mark_dispatched()
        mutation.record(CommitState.REJECTED, "decoded refusal")
        error = RateLimitError("request refused")
        return attach_journal_entry(error, mutation), _REJECTED

    if producer == "pre_send_failure":
        mutation.record(CommitState.NOT_SENT, "verified local connect failure")
        error = NetworkError("connection failed before send")
        return attach_journal_entry(
            error, mutation, recovery_action=RecoveryAction.RETRY
        ), _NOT_SENT

    if producer == "decoded_success_then_readback_loss":
        mutation.mark_dispatched()
        mutation.record(
            CommitState.CONFIRMED,
            "decoded success",
            known_resource_ids=(f"{backend}-resource",),
        )
        readback = journal.new_entry(method=f"{backend}.Read", phase="readback")
        readback.mark_dispatched()
        error = RPCError("required readback was lost")
        return (
            attach_operation_journal(
                error,
                journal,
                primary=mutation,
                recovery_action=RecoveryAction.INSPECT_AND_RECONCILE,
            ),
            _CONFIRMED_READBACK,
        )

    mutation.mark_dispatched()
    error = (
        DecodingError("accepted response could not be decoded")
        if producer == "decoder_failure_after_acceptance"
        else NetworkError("response lost")
        if producer == "lost_response"
        else RPCError("workflow expired after dispatch")
    )
    return (
        attach_journal_entry(
            error,
            mutation,
            recovery_action=RecoveryAction.INSPECT_AND_RECONCILE,
        ),
        _UNKNOWN,
    )


@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.parametrize(
    "producer",
    [
        "decoded_refusal",
        "decoded_success_then_readback_loss",
        "decoder_failure_after_acceptance",
        "lost_response",
        "pre_send_failure",
        "workflow_expiry",
    ],
)
def test_commit_evidence_producer_consumer_matrix(
    backend: str,
    producer: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error, expected = _produce(backend, producer)

    assert error.commit_state is expected.state
    assert error.operation_metadata is not None
    assert error.operation_metadata.recovery_action is expected.recovery
    assert error.unconfirmed is expected.unconfirmed
    classified = classify(error)
    assert (classified.category, classified.retriable) == (
        expected.category,
        expected.retriable,
    )

    with pytest.raises(SystemExit) as cli_exit, handle_errors(json_output=True):
        raise error
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_exit.value.code == 1
    assert cli_payload["code"] == expected.cli_code
    assert cli_payload.get("unconfirmed", False) is expected.unconfirmed

    if find_spec("fastmcp") is not None:
        from notebooklm.mcp._errors import to_tool_error, tool_error_payload

        mcp_payload = tool_error_payload(error)
        assert mcp_payload["code"] == expected.mcp_code
        assert mcp_payload["retriable"] is expected.retriable
        assert mcp_payload.get("unconfirmed", False) is expected.unconfirmed
        mcp_wire = str(to_tool_error(error))
        assert mcp_wire.startswith(f"{expected.mcp_code}:")
        assert ("unconfirmed=true" in mcp_wire) is expected.unconfirmed

    if find_spec("fastapi") is not None:
        from notebooklm.server._errors import error_response

        rest = error_response(error)
        rest_payload = json.loads(rest.body)["error"]
        assert rest_payload["category"] == expected.rest_category
        assert rest_payload["retriable"] is expected.retriable
        assert rest_payload.get("unconfirmed", False) is expected.unconfirmed

    if producer == "decoded_success_then_readback_loss":
        assert error.operation_metadata.known_resource_ids == (f"{backend}-resource",)
        assert tuple(entry.phase for entry in error.operation_metadata.entries) == (
            "mutation",
            "readback",
        )
