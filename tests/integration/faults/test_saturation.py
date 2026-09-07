"""Real pool/admission saturation; registered portable stress scenarios."""

import asyncio

import pytest

from tests._fault_server.web_saturation import SCENARIOS, run_scenario

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_saturation_recovers(scenario: str) -> None:
    result = await asyncio.wait_for(run_scenario(scenario, operation_id=f"pytest-{scenario}"), 15)
    assert all(result.checks.values())
    assert set(result.events[0]["required_checks"]) <= result.checks.keys()
