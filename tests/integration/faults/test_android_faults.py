"""Real-loopback Android resilience scenarios without a VCR cassette."""

from __future__ import annotations

import asyncio

import pytest

from tests._fault_server.android_scenarios import SCENARIOS, run_scenario


@pytest.mark.allow_no_vcr(reason="real local grpc.aio fault server; no remote traffic")
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_android_fault_scenario(scenario: str) -> None:
    result = await asyncio.wait_for(
        run_scenario(scenario, operation_id=f"pytest-{scenario}"), timeout=20.0
    )

    assert result.events
    assert result.checks
    assert all(result.checks.values()), result.events
    required = result.events[0]["required_checks"]
    assert required
    assert all(result.checks.get(name) is True for name in required), result.events
