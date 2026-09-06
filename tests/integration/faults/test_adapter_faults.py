"""R14 public-adapter projections over the local Web fault service."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("fastmcp")

from notebooklm.server._auth import SERVER_TOKEN_ENV
from tests._fault_server.adapter_scenarios import SCENARIOS, run_scenario

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_adapter_fault_scenario(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    # The outer pytest fixture isolates NOTEBOOKLM_* once, before this coroutine
    # starts. The REST dependency reads this token at request time, so set it at
    # test scope rather than mutating environment inside a concurrent cohort.
    monkeypatch.setenv(SERVER_TOKEN_ENV, "adapter-fault-token")
    result = await asyncio.wait_for(
        run_scenario(scenario, operation_id=f"pytest-{scenario}"),
        timeout=8.0,
    )

    assert result.checks
    assert all(result.checks.values())
    assert result.events[0]["kind"] == "plan"
    assert any(event["kind"] == "http_trace" for event in result.events)
