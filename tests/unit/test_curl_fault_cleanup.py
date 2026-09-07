"""Optional-lane cleanup evidence is required even when assembly/teardown fail."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests._fault_server import curl_scenarios
from tests._fault_server.common import ScenarioResult
from tests._fault_server.curl_routing import CurlFaultServer


class _Routing:
    def __init__(self, _server):
        self.sessions = []
        self.handles = []
        self.body_descriptors = []
        self._closed = False

    async def aclose(self):
        self._closed = True


@pytest.mark.parametrize(
    "primary_type", [ValueError, asyncio.CancelledError, KeyboardInterrupt, SystemExit]
)
async def test_curl_dual_failure_keeps_primary_and_partial_evidence(monkeypatch, primary_type):
    class Client:
        _lifecycle = type("Lifecycle", (), {"is_open": lambda self: False})()

        async def __aenter__(self):
            return self

        async def close(self, **_kwargs):
            raise RuntimeError("SENTINEL_CLEANUP_CAPABILITY")

    monkeypatch.setattr(curl_scenarios, "CurlRouting", _Routing)
    monkeypatch.setattr(curl_scenarios, "build_curl_client", lambda _routing: Client())
    result = ScenarioResult("web", "curl", "dual-failure")
    server = CurlFaultServer()
    primary = primary_type("SENTINEL_PRIMARY_CAPABILITY")
    with pytest.raises(primary_type) as caught:
        async with curl_scenarios._cohort(result, server):
            raise primary
    assert caught.value is primary
    cleanup = result.events[-1]
    assert cleanup["primary_error"] == primary_type.__name__
    assert cleanup["errors"] == {"client": "RuntimeError"}
    assert cleanup["routing_closed"]
    assert any(event["kind"] == "http_trace" for event in result.events)
    assert "SENTINEL" not in json.dumps(result.events)
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.address


async def test_curl_assembly_failure_closes_already_allocated_resources(monkeypatch):
    def fail(_routing):
        raise ValueError("SENTINEL_ASSEMBLY_CAPABILITY")

    monkeypatch.setattr(curl_scenarios, "CurlRouting", _Routing)
    monkeypatch.setattr(curl_scenarios, "build_curl_client", fail)
    result = ScenarioResult("web", "curl", "assembly-failure")
    server = CurlFaultServer()
    with pytest.raises(ValueError):
        async with curl_scenarios._cohort(result, server):
            pytest.fail("failed assembly cannot yield")
    cleanup = result.events[-1]
    assert cleanup["primary_error"] == "ValueError"
    assert cleanup["routing_closed"] and not cleanup["errors"]
    assert "SENTINEL" not in json.dumps(result.events)
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.address
