"""Phase 4: GET /v1/server/info — version + auth health (mirrors MCP server_info)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from notebooklm.server.app import create_app
from notebooklm.server.routes import meta as meta_route

from .conftest import TEST_TOKEN
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


def test_server_info_profile_is_resolved_not_null(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``auth.profile`` reports the resolved profile, never ``None`` (#1790).

    The server never sets a module-level active profile, so the field used to come
    back ``null`` even on a healthy session. It must name the profile the auth probe
    ran against (``"default"`` when no named profile is configured).
    """
    from notebooklm import paths

    _patch_auth(monkeypatch, all_passed=True)
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.setattr(paths, "_active_profile", None)
    body = authed_client.get("/v1/server/info").json()
    assert body["auth"]["profile"] == "default"


def test_server_info_uses_bound_profile_for_probe(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``create_app(profile=X)`` makes server_info probe that same profile (#1791)."""
    from notebooklm import paths

    seen: dict[str, Any] = {}

    async def _fake_run(plan: Any, *, read_env_auth_json: Any) -> _FakeAuthResult:
        seen["profile"] = plan.profile
        seen["storage_path"] = plan.storage_path
        return _FakeAuthResult(all_passed=True)

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        yield FakeClient()

    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.setattr(paths, "_active_profile", None)
    monkeypatch.setattr(meta_route, "run_auth_check", _fake_run)

    app = create_app(profile="work", client_factory=factory)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    with TestClient(app, headers=headers, client=("127.0.0.1", 5555)) as client:
        body = client.get("/v1/server/info").json()

    assert body["auth"]["profile"] == "work"
    assert seen == {"profile": "work", "storage_path": paths.get_storage_path("work")}
    assert paths.get_active_profile() is None


def test_server_info_locks_resolved_profile_for_lifespan(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``profile=None`` resolves once at startup, matching the lifespan-bound client."""
    from notebooklm import paths

    seen: dict[str, Any] = {}

    async def _fake_run(plan: Any, *, read_env_auth_json: Any) -> _FakeAuthResult:
        seen["profile"] = plan.profile
        seen["storage_path"] = plan.storage_path
        return _FakeAuthResult(all_passed=True)

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        yield FakeClient()

    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "work")
    monkeypatch.setattr(paths, "_active_profile", None)
    monkeypatch.setattr(meta_route, "run_auth_check", _fake_run)

    app = create_app(client_factory=factory)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    with TestClient(app, headers=headers, client=("127.0.0.1", 5555)) as client:
        monkeypatch.setenv("NOTEBOOKLM_PROFILE", "other")
        body = client.get("/v1/server/info").json()

    assert body["auth"]["profile"] == "work"
    assert seen == {"profile": "work", "storage_path": paths.get_storage_path("work")}
    assert paths.get_active_profile() is None


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
