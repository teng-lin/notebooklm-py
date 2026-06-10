"""Unit tests for the layer-3 headless re-auth wiring in ``refresh_auth_session``.

Covers the L3 hook on the dead-cookie path:

* default-unchanged: dead cookies + no opt-in + no profile → the original
  ``ValueError`` ("Run 'notebooklm login'") stands, and L3 is NOT attempted.
* opt-in success: a successful headless re-mint reloads cookies and the
  homepage GET is retried once, yielding fresh tokens.
* opt-in failure: a FAILED/UNAVAILABLE L3 outcome leaves the dead-cookie
  ``ValueError`` intact.
* coalescing: N concurrent failing refreshes routed through the real
  ``AuthRefreshCoordinator.await_refresh`` single-flight spawn at most ONE
  browser drive.

The browser drive (``attempt_headless_reauth``) is faked; no Playwright /
network is touched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._auth.headless_reauth import HeadlessReauthResult, HeadlessReauthStatus
from notebooklm._auth.session import refresh_auth_session
from notebooklm._runtime.auth import AuthRefreshCoordinator
from notebooklm.auth import AuthTokens

REFRESH_HTML = '"SNlM0e":"new_csrf_token_123" "FdrFJe":"new_session_id_456"'
LOGIN_REDIRECT = "https://accounts.google.com/signin/v2/identifier"


def _auth(storage_path: Path | None = None) -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "dead_sid", "__Secure-1PSIDTS": "dead", "HSID": "h"},
        csrf_token="old_csrf",
        session_id="old_session",
        storage_path=storage_path,
    )


class _RecordingKernel:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    def get_http_client(self) -> httpx.AsyncClient:
        return self._http_client


class _RecordingLifecycle:
    def __init__(self) -> None:
        self.saved = 0

    async def save_cookies(self, cookie_persistence: Any, jar: httpx.Cookies, path=None) -> None:
        self.saved += 1


class _RecordingAuthCoord:
    def __init__(self) -> None:
        self.ops: list[str] = []

    async def update_auth_tokens(self, *, auth: AuthTokens, csrf: str, session_id: str) -> None:
        self.ops.append("update")
        auth.csrf_token = csrf
        auth.session_id = session_id

    def update_auth_headers(self, *, auth: AuthTokens, kernel: Any) -> None:
        self.ops.append("headers")


def _bundle(http_client: httpx.AsyncClient, auth: AuthTokens) -> dict[str, Any]:
    return {
        "auth": auth,
        "kernel": _RecordingKernel(http_client),
        "auth_coord": _RecordingAuthCoord(),
        "lifecycle": _RecordingLifecycle(),
        "cookie_persistence": object(),
    }


def _redirect_then_ok_handler(state: dict[str, int]):
    """Homepage 302s to login until L3 'heals', then serves tokens.

    ``state['healed']`` is flipped by the faked re-mint; before that the
    homepage redirects to the Google login page (dead cookies).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.google.com":
            return httpx.Response(200, text="<html>sign in</html>", request=request)
        if state.get("healed"):
            return httpx.Response(200, text=REFRESH_HTML, request=request)
        return httpx.Response(302, headers={"Location": LOGIN_REDIRECT}, request=request)

    return handler


# ---------------------------------------------------------------------------
# Default-unchanged: dead cookies, no opt-in, no profile → ValueError, no L3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_cookies_no_optin_raises_and_skips_l3(monkeypatch) -> None:
    state: dict[str, int] = {}
    called = {"l3": 0}

    def _spy(**kwargs):  # pragma: no cover - must not be called
        called["l3"] += 1
        return HeadlessReauthResult(HeadlessReauthStatus.UNAVAILABLE, "no")

    # storage_path=None → ``_try_headless_reauth`` declines BEFORE reaching
    # ``attempt_headless_reauth`` (env-var auth has no writeable backing store).
    import notebooklm._auth.headless_reauth as hr

    monkeypatch.setattr(hr, "attempt_headless_reauth", _spy)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_redirect_then_ok_handler(state)),
        follow_redirects=True,
    ) as http_client:
        b = _bundle(http_client, _auth(storage_path=None))
        with pytest.raises(ValueError, match="Authentication expired"):
            await refresh_auth_session(allow_headless=False, **b)

    assert called["l3"] == 0  # env-var auth with no storage path never drives L3


@pytest.mark.asyncio
async def test_dead_cookies_optin_but_unavailable_raises(monkeypatch, tmp_path: Path) -> None:
    state: dict[str, int] = {}
    import notebooklm._auth.headless_reauth as hr

    monkeypatch.setattr(
        hr,
        "attempt_headless_reauth",
        lambda **k: HeadlessReauthResult(HeadlessReauthStatus.UNAVAILABLE, "no profile"),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_redirect_then_ok_handler(state)),
        follow_redirects=True,
    ) as http_client:
        b = _bundle(http_client, _auth(storage_path=tmp_path / "s.json"))
        with pytest.raises(ValueError, match="Authentication expired"):
            await refresh_auth_session(allow_headless=True, **b)


# ---------------------------------------------------------------------------
# Opt-in success: re-mint heals, retry yields fresh tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_cookies_optin_success_retries_and_refreshes(
    monkeypatch, tmp_path: Path
) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    state: dict[str, int] = {}
    import notebooklm._auth.headless_reauth as hr

    def _fake_attempt(**kwargs):
        # Simulate the headless re-mint "healing" the dead-cookie homepage.
        state["healed"] = 1
        assert kwargs["storage_path"] == storage
        return HeadlessReauthResult(HeadlessReauthStatus.SUCCESS, "ok", storage_path=storage)

    monkeypatch.setattr(hr, "attempt_headless_reauth", _fake_attempt)
    # Avoid touching the real cookie reload (no real cookies on disk).
    # ``_try_headless_reauth`` imports this function-locally from ``.cookies``,
    # so patch the owning module, not ``session_mod``.
    import notebooklm._auth.cookies as cookies_mod

    monkeypatch.setattr(cookies_mod, "build_httpx_cookies_from_storage", lambda p: httpx.Cookies())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_redirect_then_ok_handler(state)),
        follow_redirects=True,
    ) as http_client:
        auth = _auth(storage_path=storage)
        b = _bundle(http_client, auth)
        result = await refresh_auth_session(allow_headless=True, **b)

    assert result is auth
    assert auth.csrf_token == "new_csrf_token_123"
    assert auth.session_id == "new_session_id_456"


# ---------------------------------------------------------------------------
# Coalescing: N concurrent failing refreshes → at most ONE browser
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_refreshes_coalesce_to_one_browser(monkeypatch, tmp_path: Path) -> None:
    """N concurrent refreshes via the real single-flight spawn ONE re-mint.

    The mid-RPC cascade reaches ``refresh_auth_session`` through
    ``AuthRefreshCoordinator.await_refresh``; its single-flight task creation
    means concurrent failing callers join ONE refresh task — and therefore one
    headless browser drive.
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    state: dict[str, int] = {}
    drives = {"count": 0}
    import notebooklm._auth.headless_reauth as hr

    def _fake_attempt(**kwargs):
        drives["count"] += 1
        state["healed"] = 1
        return HeadlessReauthResult(HeadlessReauthStatus.SUCCESS, "ok", storage_path=storage)

    monkeypatch.setattr(hr, "attempt_headless_reauth", _fake_attempt)
    import notebooklm._auth.cookies as cookies_mod

    monkeypatch.setattr(cookies_mod, "build_httpx_cookies_from_storage", lambda p: httpx.Cookies())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_redirect_then_ok_handler(state)),
        follow_redirects=True,
    ) as http_client:
        auth = _auth(storage_path=storage)
        b = _bundle(http_client, auth)

        async def _do_refresh() -> AuthTokens:
            return await refresh_auth_session(allow_headless=True, **b)

        # Route N concurrent callers through ONE coordinator single-flight.
        coord = AuthRefreshCoordinator(refresh_callback=_do_refresh)
        coord.set_bound_loop(asyncio.get_running_loop())
        await asyncio.gather(*[coord.await_refresh() for _ in range(8)])

    assert drives["count"] == 1
