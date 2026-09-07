"""Pytest-only abrupt process death lane, separate from socket timing stress."""

import asyncio
import json

import pytest

from tests._fault_server.common import ScenarioResult
from tests._fault_server.process_death import REQUIRED_CHECKS, SCENARIOS, run_scenario

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_process_death_recovery(scenario: str) -> None:
    result = ScenarioResult("web", scenario, f"pytest-process-{scenario}")
    try:
        await asyncio.wait_for(run_scenario(scenario, result=result), 25)
    except BaseException:
        # Pytest captures this sanitized evidence even on a startup/watchdog failure.
        print(json.dumps({"events": result.events, "checks": result.checks}))
        raise
    assert all(result.checks.get(name) for name in REQUIRED_CHECKS[scenario])
