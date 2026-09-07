"""Saturation evidence retains the original failure if task settlement is interrupted."""

import asyncio

import pytest

from tests._fault_server import web_saturation
from tests._fault_server.common import ScenarioResult


async def test_failed_settlement_preserves_primary_and_releases_peers(monkeypatch):
    baseline_tasks = asyncio.all_tasks()
    primary = ValueError("queue observation failed")

    async def fail_observation(*args, **kwargs):
        raise primary

    async def fail_settlement(*args, **kwargs):
        await asyncio.sleep(0)
        raise OSError("secondary settlement failure")

    monkeypatch.setattr(web_saturation, "_until", fail_observation)
    monkeypatch.setattr(web_saturation.asyncio, "wait", fail_settlement)
    name = "saturation_transport_pool"
    result = ScenarioResult("web", name, "cleanup-failure")
    with pytest.raises(ValueError) as caught:
        await web_saturation.run_scenario(name, operation_id="cleanup-failure", result=result)
    assert caught.value is primary
    cleanup = next(event for event in result.events if event["kind"] == "saturation_cleanup")
    assert cleanup["primary_error"] == "ValueError"
    assert cleanup["error_type"] == "OSError"
    assert cleanup["pending_tasks"] >= 1
    cohort_cleanup = next(event for event in result.events if event["kind"] == "cleanup")
    assert cohort_cleanup["client_closed"]
    assert cohort_cleanup["active_handlers"] == 0
    assert not (asyncio.all_tasks() - baseline_tasks)
