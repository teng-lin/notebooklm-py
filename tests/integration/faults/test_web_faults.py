"""Real-loopback Web resilience scenarios (intentionally outside VCR)."""

from __future__ import annotations

import asyncio

import pytest

from tests._fault_server.web_scenarios import SCENARIOS, run_scenario

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_web_fault_scenario(scenario: str) -> None:
    result = await asyncio.wait_for(
        run_scenario(scenario, operation_id=f"pytest-{scenario}"),
        timeout=8.0,
    )

    assert result.checks
    assert all(result.checks.values())
    assert result.events[0]["kind"] == "plan"
    assert result.events[0]["faults"]
    assert all(
        cohort_id.startswith(f"pytest-{scenario}:") for cohort_id in result.events[0]["cohort_ids"]
    )
    assert any(event["kind"] == "http_trace" for event in result.events)
