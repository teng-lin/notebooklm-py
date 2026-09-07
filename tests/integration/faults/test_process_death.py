"""Pytest-only abrupt process death lane, separate from socket timing stress."""

import asyncio

import pytest

from tests._fault_server.process_death import REQUIRED_CHECKS, SCENARIOS, run_scenario

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_process_death_recovery(scenario: str) -> None:
    result = await asyncio.wait_for(run_scenario(scenario), 25)
    assert all(result.checks.get(name) for name in REQUIRED_CHECKS[scenario])
