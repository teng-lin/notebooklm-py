"""Public local persistence faults; portable scenarios also join the stress deck."""

import asyncio

import pytest

from tests._fault_server.common import ScenarioResult
from tests._fault_server.web_persistence import IMPLEMENTATIONS, REQUIRED_CHECKS

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", IMPLEMENTATIONS)
async def test_download_local_persistence(scenario: str) -> None:
    result = ScenarioResult("web", scenario, f"pytest-{scenario}")
    result.record(
        "plan",
        required_checks=REQUIRED_CHECKS[scenario],
        operation_timeout=5,
        cleanup_timeout=2,
        request_limit=4,
        commit_limit=0,
        gates=[],
    )
    await asyncio.wait_for(IMPLEMENTATIONS[scenario](result), 8)
    assert all(result.checks.get(check) for check in REQUIRED_CHECKS[scenario])
