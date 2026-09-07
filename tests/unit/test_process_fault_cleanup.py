"""Process-death startup failures retain the same caller-owned partial evidence."""

import pytest

from tests._fault_server import process_death
from tests._fault_server.common import ScenarioResult


async def test_startup_error_retains_trace_and_cleanup(monkeypatch):
    primary = OSError("child creation failed")

    async def failed_spawn(*args, **kwargs):
        raise primary

    monkeypatch.setattr(process_death, "_spawn", failed_spawn)
    result = ScenarioResult("web", "storage_before_replace", "startup-error")
    with pytest.raises(OSError) as caught:
        await process_death.run_scenario("storage_before_replace", result=result)
    assert caught.value is primary
    assert any(event["kind"] == "http_trace" for event in result.events)
    assert any(event["kind"] == "cleanup" for event in result.events)
