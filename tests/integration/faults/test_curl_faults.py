"""Optional real curl lane: TLS, sessions, streaming uploads and publication."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("curl_cffi", reason="requires the optional [impersonate] extra")

from tests._fault_server.curl_scenarios import SCENARIOS, run_scenario  # noqa: E402

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_curl_fault_scenario(scenario: str) -> None:
    result = await asyncio.wait_for(run_scenario(scenario, operation_id=f"pytest-{scenario}"), 8)
    assert result.checks and all(result.checks.values())
    transport = next(event for event in result.events if event["kind"] == "transport")
    assert transport["selected"] == "curl_cffi"
    assert transport["tls_peer_verified"] and transport["tls_hostname_verified"]
    if scenario.startswith("curl_upload_"):
        assert transport["upload_handles"] == 2
        assert transport["body_descriptors"] == 2
