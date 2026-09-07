"""Live diagnostics must precede completion and preserve the checker's verdict."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest
from scripts._ci_rpc_progress import live_progress, progress_phase, safe_reason

from scripts import check_rpc_health as health


@pytest.mark.asyncio
async def test_real_probe_streams_before_response_and_keeps_payload_private(monkeypatch, tmp_path):
    live = tmp_path / "live.log"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    entered = asyncio.Event()
    release = asyncio.Event()
    method = health.RPCMethod.LIST_NOTEBOOKS

    async def blocked_call(*args, **kwargs):
        entered.set()
        await release.wait()
        return [method.value], None

    monkeypatch.setattr(health, "make_rpc_call", blocked_call)
    with live.open("w") as stream, live_progress(stream.fileno()):
        progress_phase("method inventory")
        task = asyncio.create_task(health.check_method(None, None, method, "private-notebook"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            started = live.read_text()
            assert f"START RPC-ID LIST_NOTEBOOKS ({method.value})" in started
            assert " OK " not in started
        finally:
            release.set()
            result = await task
        assert result.status is health.CheckStatus.OK
        skipped = await health.check_method(
            None, None, health.RPCMethod.CREATE_NOTEBOOK, None, True
        )
        assert skipped.status is health.CheckStatus.SKIPPED

    output = live.read_text() + summary.read_text()
    assert "expected RPC ID observed" in output
    assert "handled in setup/cleanup; see those phase results" in output
    assert "private-notebook" not in output
    assert "elapsed=" in output
    assert "| method inventory |" in summary.read_text()


@pytest.mark.asyncio
async def test_successful_string_payload_is_not_treated_as_an_error(monkeypatch, tmp_path):
    method = health.RPCMethod.CREATE_NOTE

    async def rpc_request(*args, **kwargs):
        return json.dumps([["wrb.fr", method.value, json.dumps("private payload")]]), None

    monkeypatch.setattr(health, "make_rpc_request", rpc_request)
    live = tmp_path / "live.log"
    with live.open("w") as stream, live_progress(stream.fileno()):
        result, data = await health.test_rpc_method_with_data(None, None, method, [])
    assert result.status is health.CheckStatus.OK
    assert data == "private payload"
    output = live.read_text()
    assert "expected RPC ID observed" in output
    assert "rejected" not in output
    assert "private payload" not in output


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [
        (health.CheckStatus.ERROR, "HTTP 429 SID=private", "quota/rate limit (transient)"),
        (health.CheckStatus.ERROR, "ReadTimeout private", "RPC read timeout (transient)"),
        (health.CheckStatus.ERROR, "HTTP 503 private", "HTTP 503; see report"),
        (health.CheckStatus.ERROR, "ConnectTimeout private", "ConnectTimeout"),
        (
            health.CheckStatus.ERROR,
            "Parse error: ValueError private",
            "response decoding failed; see report",
        ),
        (
            health.CheckStatus.OK,
            "Call failed but ID found: private",
            "expected RPC ID observed, but operation rejected; see report",
        ),
    ],
)
def test_live_error_classification_never_copies_upstream_text(status, error, reason):
    result = health.CheckResult(health.RPCMethod.LIST_NOTEBOOKS, status, "wXbhsf", [], error)
    assert safe_reason(result, "ID observed") == reason
    assert "private" not in safe_reason(result, "ID observed")


def test_mismatch_reports_only_rpc_shaped_ids():
    result = health.CheckResult(
        health.RPCMethod.LIST_NOTEBOOKS,
        health.CheckStatus.MISMATCH,
        "wXbhsf",
        ["abcdef", "private-notebook-id", "bad|\n::error::injected"],
    )
    reason = safe_reason(result, "ID observed")
    assert "observed IDs=abcdef" in reason
    assert "<unexpected ID shape>" in reason
    assert "private" not in reason
    assert "::error::" not in reason


@pytest.mark.parametrize(
    ("status", "expected_exit"),
    [(health.CheckStatus.OK, 0), (health.CheckStatus.MISMATCH, 1), (health.CheckStatus.ERROR, 3)],
)
def test_cli_progress_preserves_verdict_and_full_report(
    monkeypatch, tmp_path, capsys, status, expected_exit
):
    method = health.RPCMethod.LIST_NOTEBOOKS
    result = health.CheckResult(method, status, method.value, [], "HTTP 503")

    async def run(**kwargs):
        return [result], health.CustomizationStatus.MATCH, None, None

    monkeypatch.setattr(health, "run_health_check", run)
    live = tmp_path / "live.log"
    with live.open("w") as stream:
        monkeypatch.setattr(
            sys, "argv", ["check_rpc_health.py", "--progress-fd", str(stream.fileno())]
        )
        assert health.main() == expected_exit
        # Closing the duplicate leaves the caller's descriptor usable.
        stream.write("caller still owns descriptor\n")
    assert "RPC health finished: exit=" in live.read_text()
    assert "RESULT:" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cancelled_probe_keeps_failure_and_partial_summary(monkeypatch, tmp_path):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(health, "make_rpc_call", cancelled)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with (tmp_path / "live.log").open("w") as stream, live_progress(stream.fileno()):
        progress_phase("cleanup")
        with pytest.raises(asyncio.CancelledError):
            await health.test_rpc_method(None, None, health.RPCMethod.DELETE_NOTEBOOK, [])
    assert "| cleanup | `RPC-ID DELETE_NOTEBOOK" in summary.read_text()
    assert "| ERROR |" in summary.read_text()


@pytest.mark.parametrize("invalid_descriptor", [False, True])
def test_unusable_progress_preserves_cli_failure_and_summary(
    monkeypatch, tmp_path, capsys, invalid_descriptor
):
    async def failed_call(*args, **kwargs):
        return [], "HTTP 503"

    async def run(**kwargs):
        progress_phase("method inventory")
        result = await health.check_method(None, None, health.RPCMethod.LIST_NOTEBOOKS, None)
        return [result], health.CustomizationStatus.MATCH, None, None

    monkeypatch.setattr(health, "make_rpc_call", failed_call)
    monkeypatch.setattr(health, "run_health_check", run)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    if invalid_descriptor:
        writer = -1
    else:
        reader, writer = os.pipe()
        os.close(reader)
    try:
        monkeypatch.setattr(sys, "argv", ["check_rpc_health.py", "--progress-fd", str(writer)])
        assert health.main() == 3
    finally:
        if writer >= 0:
            os.close(writer)
    assert "| ERROR |" in summary.read_text()
    assert "HTTP 503; see report" in summary.read_text()
    warning = (
        "could not open CI progress stream"
        if invalid_descriptor
        else "could not write live RPC progress"
    )
    assert capsys.readouterr().err.count(warning) == 1
