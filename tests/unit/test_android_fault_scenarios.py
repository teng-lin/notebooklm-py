"""Contract checks for the Android fault scenario entry point."""

from __future__ import annotations

import pytest

from tests._fault_server.android_scenarios import SCENARIOS, run_scenario
from tests._fault_server.common import ScenarioResult


@pytest.mark.asyncio
async def test_android_fault_scenarios_reject_unknown_name_before_opening_resources() -> None:
    with pytest.raises(ValueError, match="unknown Android fault scenario"):
        await run_scenario("not-a-scenario", operation_id="unit-unknown")


@pytest.mark.asyncio
async def test_android_fault_scenarios_require_matching_supplied_result_identity() -> None:
    result = ScenarioResult("android", SCENARIOS[0], "different-operation")

    with pytest.raises(ValueError, match="identity"):
        await run_scenario(SCENARIOS[0], operation_id="unit-operation", result=result)
