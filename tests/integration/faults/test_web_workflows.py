"""Socket-backed first-party Web workflow fault cohorts."""

from __future__ import annotations

import asyncio

import pytest

from tests._fault_server.web_scenarios import SCENARIOS as ALL_SCENARIOS
from tests._fault_server.web_workflows import SCENARIOS, run_scenario

pytestmark = pytest.mark.allow_no_vcr


def test_web_workflow_scenarios_registered_in_aggregate_registry() -> None:
    """All workflow scenarios are registered in the aggregate web fault suite."""
    assert SCENARIOS, "Workflow scenario registry must not be empty"
    assert set(SCENARIOS).issubset(set(ALL_SCENARIOS)), (
        f"Workflow scenarios missing from aggregate: {set(SCENARIOS) - set(ALL_SCENARIOS)}"
    )


async def test_web_workflow_dispatcher_routing() -> None:
    """Validate dispatcher routing for representative workflow scenario."""
    sample_scenario = SCENARIOS[0]
    result = await asyncio.wait_for(
        run_scenario(sample_scenario, operation_id=f"pytest-{sample_scenario}"),
        timeout=20.0,
    )
    assert result.checks
    assert all(result.checks.values())
    assert result.events[0]["kind"] == "plan"
