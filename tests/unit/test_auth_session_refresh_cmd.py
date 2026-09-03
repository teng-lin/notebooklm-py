"""Tests for the L2.5 mid-session refresh-cmd rung (c-PR4).

The refresh-command rung used to be reachable only at COLD START; a configured
``NOTEBOOKLM_REFRESH_CMD`` did nothing when cookies died mid-session (audit
refresh-4). c-PR4 promotes it into ``refresh_auth_session``'s ladder between L2
(RotateCookies) and L3 (headless re-mint), OPT-IN for one release via
``NOTEBOOKLM_REFRESH_CMD_MIDSESSION=1`` and reusing the SAME cold-start
machinery (the single-flight-coalesced ``_coalesced_run_refresh_cmd`` + the
per-path refresh flock).

Pins:

* the rung is reachable from the mid-session ladder WHEN the opt-in is set
  (finding-4 regression);
* WITHOUT the opt-in the rung is a no-op (default off);
* the gate declines when no command / no storage path is configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

import notebooklm._auth.refresh as refresh_mod
import notebooklm._auth.session as session_mod
from notebooklm._auth.session import refresh_auth_session
from notebooklm._browser.headless_reauth import HeadlessReauthResult, HeadlessReauthStatus
from notebooklm.auth import AuthTokens

REFRESH_HTML = '"SNlM0e":"new_csrf_token_123" "FdrFJe":"new_session_id_456"'
LOGIN_REDIRECT = "https://accounts.google.com/signin/v2/identifier"
TEST_EPOCH = 1


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

    def assert_epoch(self, expected_epoch: int) -> None:
        assert expected_epoch == TEST_EPOCH

    def get_http_client(self, *, expected_epoch: int | None = None) -> httpx.AsyncClient:
        assert expected_epoch == TEST_EPOCH
        return self._http_client


class _RecordingLifecycle:
    def __init__(self) -> None:
        self.saved = 0

    async def save_cookies(
        self,
        jar: httpx.Cookies,
        path=None,
        *,
        expected_epoch: int | None = None,
    ) -> None:
        assert expected_epoch == TEST_EPOCH
        self.saved += 1


class _RecordingAuthCoord:
    def __init__(self) -> None:
        self.ops: list[str] = []

    def assert_epoch(self, expected_epoch: int) -> None:
        assert expected_epoch == TEST_EPOCH

    async def update_auth_tokens(
        self,
        *,
        auth: AuthTokens,
        csrf: str,
        session_id: str,
        expected_epoch: int | None = None,
    ) -> None:
        assert expected_epoch == TEST_EPOCH
        self.ops.append("update")
        auth.csrf_token = csrf
        auth.session_id = session_id

    def update_auth_headers(
        self,
        *,
        auth: AuthTokens,
        kernel: Any,
        expected_epoch: int | None = None,
    ) -> None:
        assert expected_epoch == TEST_EPOCH
        self.ops.append("headers")


class _RecordingCookiePersistence:
    async def _adopt_reloaded_baseline(self, path: Path, expected: Any, *, to_thread: Any) -> None:
        del path, expected, to_thread


def _bundle(http_client: httpx.AsyncClient, auth: AuthTokens) -> dict[str, Any]:
    return {
        "auth": auth,
        "kernel": _RecordingKernel(http_client),
        "auth_coord": _RecordingAuthCoord(),
        "web_transport": _RecordingLifecycle(),
        "cookie_persistence": _RecordingCookiePersistence(),
        "expected_epoch": TEST_EPOCH,
    }


def _magic_kernel() -> _RecordingKernel:
    """Return a kernel spy that rejects calls outside the pinned generation."""
    http_client = MagicMock()
    http_client.cookies = httpx.Cookies()
    return _RecordingKernel(http_client)  # type: ignore[arg-type]


def _redirect_then_ok_handler(state: dict[str, int]):
    """Homepage 302s to login until a rung 'heals', then serves tokens."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.google.com":
            return httpx.Response(200, text="<html>sign in</html>", request=request)
        if state.get("healed"):
            return httpx.Response(200, text=REFRESH_HTML, request=request)
        return httpx.Response(302, headers={"Location": LOGIN_REDIRECT}, request=request)

    return handler


@pytest.fixture
def _clean_refresh_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the refresh recursion guards and env between tests."""
    monkeypatch.delenv(refresh_mod._REFRESH_ATTEMPTED_ENV, raising=False)
    monkeypatch.delenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, raising=False)
    monkeypatch.delenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV, raising=False)


def _patch_refresh_cmd_machinery(
    monkeypatch: pytest.MonkeyPatch, calls: list[str], *, heal: dict[str, int] | None = None
) -> None:
    """Patch the shared cold-start machinery the rung reuses (no subprocess)."""

    async def _fake_coalesced(
        refresh_key: str,
        resolved_path: Path,
        profile: str | None,
        *,
        deps: refresh_mod.RefreshCmdDeps | None = None,
    ) -> None:
        calls.append(refresh_key)
        if heal is not None:
            heal["healed"] = 1

    import notebooklm._auth.cookies as cookies_mod

    monkeypatch.setattr(refresh_mod, "_coalesced_run_refresh_cmd", _fake_coalesced)
    # The rung reload resolves ``build_httpx_cookies_from_storage`` via the
    # refresh-module alias; ``AuthTokens`` construction (and the L3 reload) resolve
    # it via the cookies module. Patch BOTH so an empty on-disk storage file does
    # not trip cookie validation.
    monkeypatch.setattr(refresh_mod, "build_httpx_cookies_from_storage", lambda p: httpx.Cookies())
    monkeypatch.setattr(cookies_mod, "build_httpx_cookies_from_storage", lambda p: httpx.Cookies())


# ---------------------------------------------------------------------------
# Gate: the wrapper adapter (_try_refresh_cmd_reauth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rung_declines_without_opt_in(
    _clean_refresh_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Command configured but MIDSESSION opt-in absent → rung is a no-op."""
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh.sh")
    # NOTEBOOKLM_REFRESH_CMD_MIDSESSION deliberately unset.
    calls: list[str] = []
    _patch_refresh_cmd_machinery(monkeypatch, calls)

    kernel = _magic_kernel()
    ok = await session_mod._try_refresh_cmd_reauth(
        auth=_auth(storage_path=tmp_path / "storage_state.json"),
        kernel=kernel,
        expected_epoch=TEST_EPOCH,
    )
    assert ok is False
    assert calls == [], "cold-start machinery must NOT run without the opt-in"


@pytest.mark.asyncio
async def test_rung_invoked_with_opt_in(
    _clean_refresh_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Command + MIDSESSION=1 → rung runs the shared machinery and reloads."""
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh.sh")
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV, "1")
    calls: list[str] = []
    _patch_refresh_cmd_machinery(monkeypatch, calls)

    kernel = _magic_kernel()
    storage = tmp_path / "storage_state.json"
    ok = await session_mod._try_refresh_cmd_reauth(
        auth=_auth(storage_path=storage), kernel=kernel, expected_epoch=TEST_EPOCH
    )
    assert ok is True
    assert len(calls) == 1, "the coalesced refresh-cmd machinery must run once"
    assert calls[0] == str(storage.expanduser().resolve())


@pytest.mark.asyncio
async def test_rung_declines_without_command(
    _clean_refresh_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Opt-in set but no NOTEBOOKLM_REFRESH_CMD → nothing to run."""
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV, "1")
    calls: list[str] = []
    _patch_refresh_cmd_machinery(monkeypatch, calls)

    kernel = _magic_kernel()
    ok = await session_mod._try_refresh_cmd_reauth(
        auth=_auth(storage_path=tmp_path / "storage_state.json"),
        kernel=kernel,
        expected_epoch=TEST_EPOCH,
    )
    assert ok is False
    assert calls == []


@pytest.mark.asyncio
async def test_rung_declines_without_storage_path(
    _clean_refresh_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env-var auth (no on-disk file) has nothing to reload → decline."""
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh.sh")
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV, "1")
    calls: list[str] = []
    _patch_refresh_cmd_machinery(monkeypatch, calls)

    ok = await session_mod._try_refresh_cmd_reauth(
        auth=_auth(storage_path=None), kernel=_magic_kernel(), expected_epoch=TEST_EPOCH
    )
    assert ok is False
    assert calls == []


@pytest.mark.asyncio
async def test_rung_declines_under_recursion_guard(
    _clean_refresh_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refresh command re-invoking this library must not re-spawn a subprocess."""
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh.sh")
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV, "1")
    monkeypatch.setenv(refresh_mod._REFRESH_ATTEMPTED_ENV, "1")  # child-process guard
    calls: list[str] = []
    _patch_refresh_cmd_machinery(monkeypatch, calls)

    kernel = _magic_kernel()
    ok = await session_mod._try_refresh_cmd_reauth(
        auth=_auth(storage_path=tmp_path / "storage_state.json"),
        kernel=kernel,
        expected_epoch=TEST_EPOCH,
    )
    assert ok is False
    assert calls == []


# ---------------------------------------------------------------------------
# Full ladder: the rung is REACHABLE from refresh_auth_session mid-session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_midsession_ladder_reaches_rung_with_opt_in(
    _clean_refresh_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """finding-4 regression: dead cookies mid-session + opt-in → rung heals + retry."""
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh.sh")
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV, "1")
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    state: dict[str, int] = {}
    calls: list[str] = []
    _patch_refresh_cmd_machinery(monkeypatch, calls, heal=state)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_redirect_then_ok_handler(state)),
        follow_redirects=True,
    ) as http_client:
        auth = _auth(storage_path=storage)
        result = await refresh_auth_session(allow_headless=False, **_bundle(http_client, auth))

    assert result is auth
    assert len(calls) == 1, "the L2.5 rung must fire mid-session under the opt-in"
    assert auth.csrf_token == "new_csrf_token_123"
    assert auth.session_id == "new_session_id_456"


@pytest.mark.asyncio
async def test_midsession_ladder_skips_rung_without_opt_in(
    _clean_refresh_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default off: command configured but no opt-in → rung never runs, ValueError."""
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh.sh")
    # NOTEBOOKLM_REFRESH_CMD_MIDSESSION deliberately unset (default off).
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    state: dict[str, int] = {}
    calls: list[str] = []
    _patch_refresh_cmd_machinery(monkeypatch, calls, heal=state)

    # L3/L4 are unavailable so the dead-cookie ValueError stands when L2.5 is off.
    import notebooklm._browser.headless_reauth as hr

    monkeypatch.setattr(
        hr,
        "attempt_headless_reauth",
        lambda **k: HeadlessReauthResult(HeadlessReauthStatus.UNAVAILABLE, "no profile"),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_redirect_then_ok_handler(state)),
        follow_redirects=True,
    ) as http_client:
        auth = _auth(storage_path=storage)
        with pytest.raises(ValueError, match="Authentication expired"):
            await refresh_auth_session(allow_headless=False, **_bundle(http_client, auth))

    assert calls == [], "the L2.5 rung must NOT fire without the opt-in (default off)"


@pytest.mark.asyncio
async def test_rung_threads_work_profile_from_storage_path(
    _clean_refresh_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The mid-session rung derives the profile from ``auth.storage_path`` (P2).

    A client built for the ``work`` profile (storage under
    ``<home>/profiles/work/storage_state.json``) must refresh the WORK profile —
    the adapter passes ``profile="work"`` to the coalesced machinery, so
    ``_run_refresh_cmd`` exports ``NOTEBOOKLM_REFRESH_PROFILE=work`` — not the
    process-wide "default".
    """
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "refresh.sh")
    monkeypatch.setenv(refresh_mod.NOTEBOOKLM_REFRESH_CMD_MIDSESSION_ENV, "1")
    # Point the home at tmp so the storage path resolves under profiles/work.
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    work_storage = tmp_path / "profiles" / "work" / "storage_state.json"
    work_storage.parent.mkdir(parents=True)
    work_storage.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    captured: dict[str, str | None] = {}

    async def _fake_coalesced(
        refresh_key: str,
        resolved_path: Path,
        profile: str | None,
        *,
        deps: refresh_mod.RefreshCmdDeps | None = None,
    ) -> None:
        captured["profile"] = profile

    import notebooklm._auth.cookies as cookies_mod

    monkeypatch.setattr(refresh_mod, "_coalesced_run_refresh_cmd", _fake_coalesced)
    monkeypatch.setattr(refresh_mod, "build_httpx_cookies_from_storage", lambda p: httpx.Cookies())
    monkeypatch.setattr(cookies_mod, "build_httpx_cookies_from_storage", lambda p: httpx.Cookies())

    kernel = _magic_kernel()
    ok = await session_mod._try_refresh_cmd_reauth(
        auth=_auth(storage_path=work_storage), kernel=kernel, expected_epoch=TEST_EPOCH
    )

    assert ok is True
    assert captured["profile"] == "work", (
        "mid-session rung must thread the work profile derived from storage_path, "
        f"got {captured['profile']!r}"
    )
