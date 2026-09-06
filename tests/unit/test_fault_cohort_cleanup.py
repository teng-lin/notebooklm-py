"""Cleanup failures remain evidence without replacing the triggering failure."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("fastmcp")

from tests._fault_server.adapter_scenarios import _fault_server
from tests._fault_server.common import ScenarioFailure, ScenarioResult
from tests._fault_server.http import HttpFaultServer, Reply, Route
from tests._fault_server.web_workflows import _cohort


def _scope(result: ScenarioResult, adapter: bool) -> Any:
    def setup(server: HttpFaultServer) -> None:
        # Intentionally unconsumed: cleanup must reject this required action.
        server.enqueue(Route.rpc("wXbhsf"), Reply())

    if adapter:
        return _fault_server(result, setup)
    server = HttpFaultServer()
    setup(server)
    return _cohort(result, server)


@pytest.mark.parametrize("adapter", [True, False], ids=["adapter", "workflow"])
@pytest.mark.parametrize("cancel", [True, False], ids=["cancel", "error"])
async def test_cleanup_failure_preserves_primary_identity_and_safe_evidence(
    adapter: bool,
    cancel: bool,
) -> None:
    result = ScenarioResult("web", "cleanup-regression", "cleanup")
    message = "fault-private-primary-sentinel"
    primary = asyncio.CancelledError(message) if cancel else RuntimeError(message)
    with pytest.raises(type(primary)) as raised:
        async with _scope(result, adapter):
            raise primary
    assert raised.value is primary
    assert result.checks["server_plan_consumed" if adapter else "server_clean"] is False
    cleanup = next(event for event in result.events if event["kind"] == "cleanup")
    assert cleanup["active_handlers"] == 0
    assert cleanup["remaining_actions"] == 1
    assert cleanup["primary_error"] == type(primary).__name__
    assert message not in json.dumps(result.events)


@pytest.mark.parametrize("adapter", [True, False], ids=["adapter", "workflow"])
async def test_unused_required_fault_cannot_pass_without_primary_failure(adapter: bool) -> None:
    result = ScenarioResult("web", "unused-regression", "cleanup")
    with pytest.raises(ScenarioFailure):
        async with _scope(result, adapter):
            pass
    assert not all(result.checks.values())
