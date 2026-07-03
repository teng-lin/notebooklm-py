"""Phase 4: GET /v1/server/info — version + auth health (mirrors MCP server_info)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from notebooklm.server.routes import meta as meta_route

from .fakes import FakeClient


class _FakeAuthResult:
    def __init__(self, *, all_passed: bool) -> None:
        self.all_passed = all_passed
        self.checks = {
            "storage_exists": True,
            "json_valid": True,
            "cookies_present": True,
            "sid_cookie": all_passed,
        }


def _patch_auth(monkeypatch: pytest.MonkeyPatch, *, all_passed: bool) -> None:
    async def _fake_run(plan: Any, *, read_env_auth_json: Any) -> _FakeAuthResult:
        return _FakeAuthResult(all_passed=all_passed)

    monkeypatch.setattr(meta_route, "run_auth_check", _fake_run)


def test_server_info_reports_version_and_auth(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_auth(monkeypatch, all_passed=True)
    resp = authed_client.get("/v1/server/info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["server"] == "notebooklm-server"
    assert isinstance(body["version"], str) and body["version"]
    assert body["auth"]["authenticated"] is True
    assert body["auth"]["sid_cookie"] is True
    # Default call does not include the account block.
    assert "account" not in body


def test_server_info_does_not_leak_storage_path(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_auth(monkeypatch, all_passed=True)
    body = authed_client.get("/v1/server/info").json()
    # No absolute on-disk storage path anywhere in the response (MCP scrubs it too).
    assert "storage_path" not in body["auth"]
    assert "/" not in str(body["auth"].get("profile", ""))


def test_server_info_include_account(
    authed_client: TestClient, fake_client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_auth(monkeypatch, all_passed=True)
    resp = authed_client.get("/v1/server/info", params={"include_account": True})
    assert resp.status_code == 200
    account = resp.json()["account"]
    assert account["email"] == "user@example.com"
    assert account["available"] is True
    assert account["notebook_limit"] == 100
    assert account["source_limit"] == 50
    assert account["output_language"] == "en"


def test_server_info_include_account_unauthenticated_degrades(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_auth(monkeypatch, all_passed=False)
    account = authed_client.get("/v1/server/info", params={"include_account": True}).json()[
        "account"
    ]
    assert account["available"] is False
    assert account["reason"] == "not authenticated"
