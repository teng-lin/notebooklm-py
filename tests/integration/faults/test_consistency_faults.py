"""Real-socket inconsistent successful-response regressions."""

import asyncio

import pytest

from tests._fault_server.common import ScenarioResult
from tests._fault_server.web_consistency import BUDGETS, IMPLEMENTATIONS, REQUIRED_CHECKS

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", IMPLEMENTATIONS)
async def test_web_consistency(scenario: str) -> None:
    result = ScenarioResult("web", scenario, f"pytest-{scenario}")
    result.record(
        "plan",
        required_checks=list(REQUIRED_CHECKS[scenario]),
        budgets=BUDGETS[scenario],
    )
    await asyncio.wait_for(IMPLEMENTATIONS[scenario](result), 8)
    assert all(result.checks.get(check) is True for check in REQUIRED_CHECKS[scenario])


@pytest.mark.parametrize(
    "scenario", ["consistency_stale_collection_readback", "consistency_repeated_chat_token"]
)
async def test_android_consistency(scenario: str) -> None:
    pytest.importorskip("grpc")
    from tests._fault_server.android_consistency import BUDGETS, IMPLEMENTATIONS, REQUIRED_CHECKS

    result = ScenarioResult("android", scenario, f"pytest-{scenario}")
    result.record(
        "plan",
        required_checks=list(REQUIRED_CHECKS[scenario]),
        budgets=BUDGETS[scenario],
    )
    await asyncio.wait_for(IMPLEMENTATIONS[scenario](result), 8)
    assert all(result.checks.get(check) is True for check in REQUIRED_CHECKS[scenario])
