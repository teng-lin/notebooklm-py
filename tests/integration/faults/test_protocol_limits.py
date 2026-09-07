"""Socket-level protocol limits share their portable scenario implementations."""

import asyncio

import pytest

from tests._fault_server.web_protocol import SCENARIOS, run_scenario

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_web_protocol_limits(scenario: str) -> None:
    result = await asyncio.wait_for(run_scenario(scenario, operation_id=f"pytest-{scenario}"), 15)
    assert all(result.checks.values())
    assert set(result.events[0]["required_checks"]) <= result.checks.keys()


@pytest.mark.parametrize(
    "scenario",
    [
        "protocol_chat_cumulative_below",
        "protocol_chat_cumulative_at",
        "protocol_chat_cumulative_above",
    ],
)
async def test_android_protocol_limits(scenario: str) -> None:
    pytest.importorskip("grpc")
    from tests._fault_server.android_protocol import run_scenario as run_android

    result = await asyncio.wait_for(run_android(scenario, operation_id=f"pytest-{scenario}"), 15)
    assert all(result.checks.values())
    assert set(result.events[0]["required_checks"]) <= result.checks.keys()
