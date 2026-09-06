"""Safety checks for the test-only logical-host socket router."""

from __future__ import annotations

import httpx
import pytest

import tests._fault_server.web_scenarios as web_scenarios
from tests._fault_server.common import ScenarioFailure, ScenarioResult
from tests._fault_server.http import HttpFaultServer, LogicalHostTransport


class _FakeLifecycle:
    def __init__(self) -> None:
        self.open = False

    def is_open(self) -> bool:
        return self.open


class _FakeClient:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self._lifecycle = _FakeLifecycle()
        self._close_error = close_error

    async def __aenter__(self) -> _FakeClient:
        self._lifecycle.open = True
        return self

    async def close(self, *, drain: bool) -> None:
        if self._close_error is not None:
            raise self._close_error
        self._lifecycle.open = False


async def test_logical_transport_refuses_unmapped_host_and_http_downgrade() -> None:
    async with HttpFaultServer() as server:
        transport = LogicalHostTransport({"notebook.google.com": server.address})
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ConnectError, match="unmapped logical host"):
                await client.get("https://example.com/")
            with pytest.raises(httpx.ConnectError, match="non-HTTPS logical URL"):
                await client.get("http://notebook.google.com/")

    assert server.journal == []


def test_logical_transport_refuses_non_loopback_target() -> None:
    with pytest.raises(ValueError, match="numeric loopback"):
        LogicalHostTransport({"notebook.google.com": ("192.0.2.1", 80)})


async def test_unexpected_request_is_visible_in_server_errors() -> None:
    async with HttpFaultServer() as server:
        transport = LogicalHostTransport({"notebook.google.com": server.address})
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://notebook.google.com/unplanned")

    assert response.status_code == 500
    assert len(server.errors) == 1
    assert "unexpected request" in server.errors[0]


async def test_cohort_records_client_cleanup_failure_and_still_closes_server(monkeypatch) -> None:
    result = ScenarioResult("web", "cleanup", "client-close")
    server = HttpFaultServer()
    fake = _FakeClient(close_error=RuntimeError("client close failed"))
    monkeypatch.setattr(web_scenarios, "build_fault_client", lambda *_args, **_kwargs: fake)

    with pytest.raises(RuntimeError, match="client close failed"):
        async with web_scenarios._cohort(result, server):
            pass

    cleanup = result.events[-1]
    assert cleanup["kind"] == "cleanup"
    assert cleanup["close_error"] == "RuntimeError"
    assert cleanup["server_close_error"] is None
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.address


async def test_cohort_records_server_cleanup_failure(monkeypatch) -> None:
    result = ScenarioResult("web", "cleanup", "server-close")
    server = HttpFaultServer()
    fake = _FakeClient()
    monkeypatch.setattr(web_scenarios, "build_fault_client", lambda *_args, **_kwargs: fake)
    original_close = server.aclose

    async def fail_close() -> None:
        raise RuntimeError("server close failed")

    monkeypatch.setattr(server, "aclose", fail_close)
    with pytest.raises(RuntimeError, match="server close failed"):
        async with web_scenarios._cohort(result, server):
            pass

    cleanup = result.events[-1]
    assert cleanup["kind"] == "cleanup"
    assert cleanup["close_error"] is None
    assert cleanup["server_close_error"] == "RuntimeError"

    await original_close()


async def test_cohort_rejects_client_that_silently_remains_open(monkeypatch) -> None:
    class UnclosedClient(_FakeClient):
        async def close(self, *, drain: bool) -> None:
            pass

    result = ScenarioResult("web", "cleanup", "silent-close")
    server = HttpFaultServer()
    monkeypatch.setattr(web_scenarios, "build_fault_client", lambda *_a, **_kw: UnclosedClient())
    with pytest.raises(ScenarioFailure, match="client_closed"):
        async with web_scenarios._cohort(result, server):
            pass
    assert result.checks["client_closed"] is False
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.address
