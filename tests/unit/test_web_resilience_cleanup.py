"""Failure-path coverage for the Web resilience scenario owners."""

from __future__ import annotations

import asyncio
import json

import pytest

import tests._fault_server.web_resilience_scenarios as resilience
from tests._fault_server.common import ScenarioResult
from tests._fault_server.http import HttpFaultServer


class _FakeLifecycle:
    def __init__(self) -> None:
        self.open = False

    def is_open(self) -> bool:
        return self.open


class _FakeClient:
    def __init__(self, close_error: BaseException) -> None:
        self._lifecycle = _FakeLifecycle()
        self._close_error = close_error
        self.close_calls = 0

    async def __aenter__(self) -> _FakeClient:
        self._lifecycle.open = True
        return self

    async def close(self, *, drain: bool) -> None:
        self.close_calls += 1
        raise self._close_error


@pytest.mark.parametrize("primary_type", [ValueError, asyncio.CancelledError])
async def test_resilience_cohort_preserves_primary_and_settles_both_failed_owners(
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
) -> None:
    result = ScenarioResult("web", "cleanup-negative", "dual-owner-failure")
    server = HttpFaultServer()
    primary = primary_type("private-primary")
    client_failure = RuntimeError("private-client-close")
    server_failure = RuntimeError("private-server-close")
    client = _FakeClient(client_failure)
    monkeypatch.setattr(resilience, "build_fault_client", lambda *_args, **_kwargs: client)
    real_server_close = server.aclose

    async def close_server_then_fail() -> None:
        await real_server_close()
        raise server_failure

    monkeypatch.setattr(server, "aclose", close_server_then_fail)

    with pytest.raises(primary_type) as raised:
        async with resilience._cohort(result, server):
            raise primary

    assert raised.value is primary
    assert client.close_calls == 1
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.address
    cleanup = next(event for event in result.events if event["kind"] == "cleanup")
    assert cleanup["primary_error"] == primary_type.__name__
    assert cleanup["cleanup_error_types"] == ["RuntimeError", "RuntimeError"]
    assert "private" not in json.dumps(result.events)


async def test_old_generation_build_failure_still_closes_started_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = ScenarioResult("web", "auth_refresh_old_generation", "build-failure")
    server = HttpFaultServer()
    primary = ValueError("private-build-failure")
    monkeypatch.setattr(resilience, "HttpFaultServer", lambda: server)

    def fail_build(*_args: object, **_kwargs: object) -> None:
        raise primary

    monkeypatch.setattr(resilience, "build_fault_client", fail_build)

    with pytest.raises(ValueError) as raised:
        await resilience._auth_refresh_old_generation(result)

    assert raised.value is primary
    with pytest.raises(RuntimeError, match="not running"):
        _ = server.address
    cleanup = next(event for event in result.events if event["kind"] == "cleanup")
    assert cleanup["client_closed"] is True
    assert cleanup["primary_error"] == "ValueError"
    assert cleanup["cleanup_error_types"] == []
    assert any(event["kind"] == "http_trace" for event in result.events)
    assert "private" not in json.dumps(result.events)
