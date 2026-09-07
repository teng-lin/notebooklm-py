"""R14 public-adapter projections over the local Web fault service."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("fastmcp")

from tests._fault_server.adapter_scenarios import SCENARIOS, run_scenario

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_adapter_fault_scenario(scenario: str) -> None:
    result = await asyncio.wait_for(
        run_scenario(scenario, operation_id=f"pytest-{scenario}"),
        # Allow the child's 10s watchdog and 2s terminate/2s kill settlement.
        timeout=20.0,
    )

    assert result.checks
    assert all(result.checks.values())
    required = result.events[0]["required_checks"]
    assert required
    assert all(result.checks.get(check) is True for check in required)
    assert result.events[0]["kind"] == "plan"
    assert any(event["kind"] == "http_trace" for event in result.events)
