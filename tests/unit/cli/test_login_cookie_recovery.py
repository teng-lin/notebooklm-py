"""Tests for browser-cookies extraction edge cases (issue #990).

Covers the in-memory ``__Secure-1PSIDTS`` recovery wired into
``_enumerate_one_jar`` and ``_write_extracted_cookies``, plus the
scenario-specific diagnostic hints emitted when recovery declines.

Scenarios:

- Browser cookies arrive with ``SID`` + secondary binding but no PSIDTS
  (cold session, RotateCookies not yet fired): in-memory recovery should
  mint PSIDTS so ``notebooklm login --browser-cookies`` succeeds.
- No ``SID`` at all: user isn't signed in to Google. CLI emits the
  "Sign in to a Google account" hint without firing RotateCookies.
- No secondary binding (no ``OSID``, no ``APISID+SAPISID``): RotateCookies
  would 401. CLI emits the "Open https://notebooklm.google.com" hint.
- RotateCookies POST returns 4xx: recovery declines, hint surfaces.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm.notebooklm_cli import cli
from tests._fixtures import patch_session_login_dual

_ROTATE_URL_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies$")


def _rookiepy_mock(cookies: list[dict]) -> MagicMock:
    mock = MagicMock()
    mock.chrome = MagicMock(return_value=cookies)
    mock.load = MagicMock(return_value=cookies)
    return mock


def _account_enum(email: str = "alice@example.com"):
    async def _enum(*args, **kwargs):
        from notebooklm.auth import Account

        return [Account(authuser=0, email=email, is_default=True)]

    return _enum


def _profile_storage_path(target_root):
    def fake_get_storage_path(profile=None):
        return target_root / (profile or "default") / "storage_state.json"

    return fake_get_storage_path


def _make_rotate_response(*, with_psidts: bool = True, status_code: int = 200):
    headers: list[tuple[str, str]] = []
    if with_psidts:
        headers.append(
            (
                "Set-Cookie",
                "__Secure-1PSIDTS=fresh_psidts; Domain=.google.com; Path=/; "
                "Secure; HttpOnly; SameSite=Lax",
            )
        )
    return {
        "status_code": status_code,
        "headers": headers,
        "content": b'["identity.hfcr",600]',
    }


# Rookiepy cookies that have everything Google needs EXCEPT the rotating
# PSIDTS — the in-memory recovery should be able to mint it via RotateCookies.
_RECOVERABLE_BROWSER_COOKIES = [
    {
        "domain": ".google.com",
        "name": "SID",
        "value": "browser_sid",
        "path": "/",
        "secure": True,
        "http_only": False,
    },
    {
        "domain": ".google.com",
        "name": "HSID",
        "value": "browser_hsid",
        "path": "/",
        "secure": True,
        "http_only": True,
    },
    {
        "domain": ".google.com",
        "name": "SSID",
        "value": "browser_ssid",
        "path": "/",
        "secure": True,
        "http_only": True,
    },
    {
        "domain": ".google.com",
        "name": "APISID",
        "value": "browser_apisid",
        "path": "/",
        "secure": False,
        "http_only": False,
    },
    {
        "domain": ".google.com",
        "name": "SAPISID",
        "value": "browser_sapisid",
        "path": "/",
        "secure": True,
        "http_only": True,
    },
]


@pytest.mark.no_default_keepalive_mock
class TestPsidtsRecoveryDuringExtraction:
    """``login --browser-cookies`` recovers PSIDTS when Google can mint it."""

    def test_recovers_missing_psidts_and_completes_login(self, runner, tmp_path, httpx_mock):
        """SID + secondary binding present, no PSIDTS → RotateCookies + persist."""
        mock_rk = _rookiepy_mock(list(_RECOVERABLE_BROWSER_COOKIES))
        target_root = tmp_path / "profiles"
        httpx_mock.add_response(url=_ROTATE_URL_RE, **_make_rotate_response())

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch_session_login_dual("_sync_server_language_to_config"),
            patch("notebooklm.auth.enumerate_accounts", new=_account_enum()),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome"])

        assert result.exit_code == 0, result.output

        # RotateCookies must have been called exactly once (in-memory recovery).
        rotate_calls = [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))]
        assert len(rotate_calls) == 1

        # And the persisted storage_state must include the rotated PSIDTS.
        import json

        storage_file = target_root / "default" / "storage_state.json"
        saved = json.loads(storage_file.read_text())
        names = {c["name"] for c in saved["cookies"]}
        assert "__Secure-1PSIDTS" in names
        psidts = next(c for c in saved["cookies"] if c["name"] == "__Secure-1PSIDTS")
        assert psidts["value"] == "fresh_psidts"


@pytest.mark.no_default_keepalive_mock
class TestMissingCookiesDiagnostics:
    """When recovery cannot help, the CLI emits a scenario-specific hint."""

    def test_no_sid_emits_signin_hint(self, runner, tmp_path, httpx_mock):
        """No SID → recovery declines → "not signed in to Google" hint."""
        cookies = [c for c in _RECOVERABLE_BROWSER_COOKIES if c["name"] != "SID"]
        mock_rk = _rookiepy_mock(cookies)
        target_root = tmp_path / "profiles"

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch_session_login_dual("_sync_server_language_to_config"),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome"])

        assert result.exit_code == 1
        assert "not signed in" in result.output
        assert "chrome" in result.output

        # Crucially: no RotateCookies POST (recovery preconditions failed).
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    def test_missing_secondary_binding_emits_visit_notebooklm_hint(
        self, runner, tmp_path, httpx_mock
    ):
        """No OSID / no APISID+SAPISID → RotateCookies would 401 → visit-page hint."""
        # SID alone — no binding at all.
        cookies = [c for c in _RECOVERABLE_BROWSER_COOKIES if c["name"] == "SID"]
        mock_rk = _rookiepy_mock(cookies)
        target_root = tmp_path / "profiles"

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch_session_login_dual("_sync_server_language_to_config"),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome"])

        assert result.exit_code == 1
        # Hint nudges the user to open NotebookLM. We assert on a non-URL
        # substring so CodeQL doesn't flag this as incomplete URL sanitization
        # (the hint itself contains the canonical URL). Rich line-wraps the
        # output, so we normalize whitespace before checking.
        output_normalized = " ".join(result.output.split())
        assert "reload the page" in output_normalized

        # No POST: recovery short-circuits on the secondary-binding precondition.
        assert [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))] == []

    def test_rotate_cookies_4xx_emits_recovery_failed_hint(self, runner, tmp_path, httpx_mock):
        """Recovery attempts the POST but Google returns 401 → visit-page hint."""
        mock_rk = _rookiepy_mock(list(_RECOVERABLE_BROWSER_COOKIES))
        target_root = tmp_path / "profiles"
        # 401 means recovery declines (psidts_present check fails).
        httpx_mock.add_response(url=_ROTATE_URL_RE, status_code=401)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual(
                "get_storage_path",
                side_effect=_profile_storage_path(target_root),
            ),
            patch_session_login_dual("_sync_server_language_to_config"),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome"])

        assert result.exit_code == 1
        # The 4xx-recovery-failed hint surfaces the rotation diagnosis. Checked
        # on a non-URL substring to keep CodeQL's URL-sanitization rule happy.
        # Normalize whitespace because Rich may line-wrap the message.
        output_normalized = " ".join(result.output.split())
        assert "RotateCookies recovery" in output_normalized

        # POST was attempted exactly once (and declined).
        rotate_calls = [r for r in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(r.url))]
        assert len(rotate_calls) == 1
