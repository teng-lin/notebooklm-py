"""Canonical operation metadata projection across every public adapter."""

from __future__ import annotations

import json

import pytest

from notebooklm._idempotency import attach_operation_metadata, reconciliation_report
from notebooklm._source.batch import SourceUrlBatchItem
from notebooklm.cli.error_handler import handle_errors
from notebooklm.exceptions import NetworkError, RPCError, SourceAddError
from notebooklm.mcp._errors import to_tool_error, tool_error_payload
from notebooklm.outcomes import (
    BatchItemOutcome,
    BatchOutcome,
    CommitState,
    OperationMetadata,
    ReconciliationCandidate,
    ReconciliationReport,
    RecoveryAction,
    operation_metadata_payload,
)
from notebooklm.server._errors import error_response


def _long_userinfo_url() -> str:
    return (
        "https://userinfo-must-not-leak-"
        + "x" * 220
        + ":password-must-not-leak@unknown.test/path?access_token=query-must-not-leak"
    )


def _rich_error() -> RPCError:
    secret = "ya29." + "this-must-not-leak" * 8
    report = ReconciliationReport(
        candidates=tuple(
            ReconciliationCandidate(
                f"candidate-{index}-{secret}",
                title=f"/Users/alice/private/{index}",
            )
            for index in range(30)
        ),
        unresolved_inputs=(
            f"https://example.test/item?access_token={secret}",
            "/Users/alice/private/source.txt",
        ),
        reason=f"readback Authorization: Bearer {secret} " + "x" * 500,
    )
    batch = BatchOutcome(
        (
            BatchItemOutcome(0, "https://ok.test", CommitState.CONFIRMED, "source-ok"),
            BatchItemOutcome(
                1,
                "https://bad.test",
                CommitState.REJECTED,
                error=SourceAddError("https://bad.test"),
            ),
            BatchItemOutcome(
                2,
                f"https://userinfo-secret:password-secret@unknown.test/path?access_token={secret}",
                CommitState.UNKNOWN,
                error=NetworkError(f"lost {secret}"),
                reconciliation=report,
            ),
            BatchItemOutcome(
                3,
                "https://unattempted.test",
                CommitState.NOT_SENT,
                error=NetworkError("blocked before dispatch"),
            ),
        )
    )
    error = RPCError(f"lost Authorization: Bearer {secret}")
    return attach_operation_metadata(
        error,
        OperationMetadata(
            commit_state=CommitState.UNKNOWN,
            operation="sources.add_urls",
            known_resource_ids=tuple(f"resource-{index}-{secret}" for index in range(30)),
            recovery_action=RecoveryAction.INSPECT_AND_RECONCILE,
            source_id="source-known",
            stage="readback",
            reconciliation=report,
            prerequisite_ids=("drive-file", "/Users/alice/private/staged"),
            batch_outcome=batch,
        ),
    )


def test_shared_metadata_projection_is_full_bounded_and_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = _rich_error()
    expected = operation_metadata_payload(error)
    assert error.batch_outcome is not None
    assert error.batch_outcome.items[2].input.startswith("https://***@unknown.test/")

    with pytest.raises(SystemExit), handle_errors(json_output=True):
        raise error
    cli = json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit), handle_errors(json_output=False):
        raise error
    cli_text = capsys.readouterr().err
    mcp = tool_error_payload(error)
    rest = json.loads(error_response(error).body)["error"]

    for payload in (cli, mcp, rest):
        for key, value in expected.items():
            assert payload[key] == value
    wire = str(to_tool_error(error))
    assert "operation_metadata=" in wire
    assert "known_resource_ids" in wire
    assert "batch_outcome" in wire

    rendered = json.dumps(
        {"metadata": expected, "cli": cli, "mcp": mcp, "rest": rest, "wire": wire}
    )
    assert "this-must-not-leak" not in rendered
    assert "userinfo-secret" not in rendered
    assert "password-secret" not in rendered
    assert "/Users/alice/" not in rendered
    assert "Operation metadata:" in cli_text
    assert "this-must-not-leak" not in cli_text
    assert "userinfo-secret" not in cli_text
    assert "password-secret" not in cli_text
    assert "/Users/alice/" not in cli_text
    assert len(expected["known_resource_ids"]) == 20  # type: ignore[arg-type]
    reconciliation = expected["reconciliation"]
    assert isinstance(reconciliation, dict)
    assert len(reconciliation["candidates"]) == 20  # type: ignore[arg-type]
    unknown = expected["batch_outcome"]
    assert isinstance(unknown, dict)
    items = unknown["items"]
    assert isinstance(items, list)
    assert items[2]["reconciliation"]["unresolved_inputs"]  # type: ignore[index]


def test_long_userinfo_is_redacted_before_outcome_cap_across_adapters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    url = _long_userinfo_url()
    report = reconciliation_report([url], [url], reason=url)
    source_error = SourceAddError(url)
    item = SourceUrlBatchItem(url=url, error=source_error)
    assert item.outcome is not None

    for stored in (
        report.candidates[0].id,
        report.unresolved_inputs[0],
        report.reason,
        item.outcome.input,
        item.outcome.reconciliation.unresolved_inputs[0],
    ):
        assert stored.startswith("https://***@unknown.test/")
        assert len(stored) <= 201
        assert "userinfo-must-not-leak" not in stored
        assert "password-must-not-leak" not in stored
        assert "query-must-not-leak" not in stored

    error = attach_operation_metadata(
        source_error,
        OperationMetadata(
            commit_state=CommitState.UNKNOWN,
            operation="sources.add_urls",
            recovery_action=RecoveryAction.INSPECT_AND_RECONCILE,
            reconciliation=report,
            batch_outcome=BatchOutcome((item.outcome,)),
        ),
    )
    with pytest.raises(SystemExit), handle_errors(json_output=True):
        raise error
    cli_json = json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit), handle_errors(json_output=False):
        raise error
    cli_text = capsys.readouterr().err
    payloads = (
        cli_json,
        tool_error_payload(error),
        json.loads(error_response(error).body)["error"],
    )
    rendered_adapters = tuple(
        json.dumps(payload) for payload in (*payloads, str(to_tool_error(error)), cli_text)
    )
    for rendered in rendered_adapters:
        assert "https://***@unknown.test/" in rendered
        assert "userinfo-must-not-leak" not in rendered
        assert "password-must-not-leak" not in rendered
        assert "query-must-not-leak" not in rendered


def test_confirmed_chat_projection_says_recorded_but_readback_unresolved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = attach_operation_metadata(
        RPCError("conversation id readback failed"),
        OperationMetadata(
            commit_state=CommitState.CONFIRMED,
            operation="chat",
            known_resource_ids=("conversation-1",),
            recovery_action=RecoveryAction.INSPECT_AND_RECONCILE,
        ),
    )

    payload = tool_error_payload(error)
    assert "was recorded" in payload["hint"]
    assert "conversation-1" in payload["hint"]
    assert "may or may not" not in payload["hint"]
    assert "was recorded" in str(to_tool_error(error))
    assert "was recorded" in json.loads(error_response(error).body)["error"]["hint"]

    with pytest.raises(SystemExit), handle_errors(json_output=True):
        raise error
    cli_json = json.loads(capsys.readouterr().out)
    assert "was recorded" in cli_json["hint"]
    assert "conversation-1" in cli_json["hint"]
    assert cli_json["known_resource_ids"] == ["conversation-1"]

    with pytest.raises(SystemExit), handle_errors(json_output=False):
        raise error
    cli_text = capsys.readouterr().err
    assert "was recorded" in cli_text
    assert "conversation-1" in cli_text
    assert "Operation metadata:" in cli_text
