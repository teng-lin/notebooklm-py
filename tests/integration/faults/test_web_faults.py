"""Real-loopback Web resilience scenarios (intentionally outside VCR)."""

from __future__ import annotations

import asyncio

import pytest

from tests._fault_server.common import ScenarioFailure
from tests._fault_server.http import Action, HttpFaultServer, Reply, RequestRecord, Route
from tests._fault_server.web_scenarios import SCENARIOS, run_scenario
from tests._fault_server.web_transfers import BASE_FINAL, FINAL, UPLOAD

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_web_fault_scenario(scenario: str) -> None:
    result = await asyncio.wait_for(
        run_scenario(scenario, operation_id=f"pytest-{scenario}"),
        timeout=20.0,
    )

    assert result.checks
    assert all(result.checks.values())
    required = result.events[0]["required_checks"]
    assert required
    assert set(required) <= result.checks.keys()
    assert all(result.checks.get(check) is True for check in required)
    assert result.events[0]["kind"] == "plan"
    assert result.events[0]["faults"]
    assert all(
        cohort_id.startswith(f"pytest-{scenario}:") for cohort_id in result.events[0]["cohort_ids"]
    )
    assert any(event["kind"] == "http_trace" for event in result.events)


@pytest.mark.parametrize(
    ("scenario", "delayed_route"),
    [
        pytest.param("upload_success", UPLOAD, id="success-session-start"),
        pytest.param("upload_success", BASE_FINAL, id="success-baseline-finalize"),
        pytest.param("upload_success", FINAL, id="success-finalize"),
        pytest.param("upload_body_stall", UPLOAD, id="body-stall-session-start"),
        pytest.param("upload_body_stall", BASE_FINAL, id="body-stall-baseline-finalize"),
    ],
)
async def test_web_upload_tolerates_delayed_unstalled_reply(
    monkeypatch: pytest.MonkeyPatch, scenario: str, delayed_route: Route
) -> None:
    run_action = HttpFaultServer._run_action

    async def delayed_reply(
        server: HttpFaultServer,
        action: Action,
        writer: asyncio.StreamWriter,
        record: RequestRecord,
    ) -> None:
        if record.route == delayed_route:
            # A finite response delay exceeds the old 300 ms stall deadline.
            await asyncio.sleep(0.6)
        await run_action(server, action, writer, record)

    monkeypatch.setattr(HttpFaultServer, "_run_action", delayed_reply)
    result = await asyncio.wait_for(run_scenario(scenario, operation_id="delayed-upload"), 20)

    assert result.checks["successful_upload_baseline"]
    assert all(result.checks.values())
    if scenario == "upload_body_stall":
        assert result.checks["stage_specific_error"]
        assert result.checks["actual_partial_request"]
    else:
        assert result.checks["uploaded_identity"]


async def test_web_upload_failure_records_outcome_before_failed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_action = HttpFaultServer._run_action

    async def rejected_finalize(
        server: HttpFaultServer,
        action: Action,
        writer: asyncio.StreamWriter,
        record: RequestRecord,
    ) -> None:
        if record.route == FINAL:
            action = Reply(503)
        await run_action(server, action, writer, record)

    monkeypatch.setattr(HttpFaultServer, "_run_action", rejected_finalize)
    with pytest.raises(ScenarioFailure, match="uploaded_identity") as raised:
        await asyncio.wait_for(run_scenario("upload_success", operation_id="rejected-upload"), 20)

    events = raised.value.result.events
    outcome = next(event for event in events if event["kind"] == "outcome")
    failure = next(event for event in events if event.get("passed") is False)
    assert outcome["error"] == "ServerError"
    assert events.index(outcome) < events.index(failure)
