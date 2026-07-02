"""Tests for ``NotebookLMClient.get_account_email`` / ``get_account_authuser``.

Identity resolution has three sources, the first two network-free: the in-memory
:class:`AuthTokens`, the persisted profile metadata, and (opt-in) a single live
``WIZ_global_data`` probe of the active ``authuser`` page. The probe is exercised
through pytest-httpx by installing a real ``httpx.AsyncClient`` on the kernel (the
seam ``client._collaborators.kernel.get_http_client()`` reads) so mocked page GETs
are intercepted without opening a live session. The WIZ HTML shape mirrors
``tests/unit/test_auth_account.py``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
from pytest_httpx import HTTPXMock

import notebooklm.client as client_module
from notebooklm.auth import AuthTokens, read_account_metadata, write_account_metadata
from notebooklm.client import NotebookLMClient
from tests._fixtures.kernel_test_helpers import install_http_client_for_test


def _wiz_html_with_email(email: str) -> str:
    """Build a minimal NotebookLM-style page that embeds a user email."""
    return f'<script>window.WIZ_global_data = {{"oM1Kwf":"{email}"}};</script>'


def _make_auth(
    *,
    account_email: str | None = None,
    authuser: int = 0,
    storage_path=None,
) -> AuthTokens:
    """Build minimal AuthTokens with the identity fields under test."""
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y", "HSID": "z"},
        csrf_token="csrf",
        session_id="sess",
        account_email=account_email,
        authuser=authuser,
        storage_path=storage_path,
    )


def _install_probe_client(client: NotebookLMClient) -> httpx.AsyncClient:
    """Give the kernel a real ``httpx.AsyncClient`` (intercepted by pytest-httpx).

    ``follow_redirects=True`` mirrors the production kernel client (_kernel.py) so
    the login-redirect path exercises the real ``is_google_auth_redirect`` branch
    (a followed 302 lands on a 200 signin page), not just the ``status != 200`` guard.
    """
    http_client = httpx.AsyncClient(follow_redirects=True)
    install_http_client_for_test(client._collaborators.kernel, http_client)
    return http_client


def _write_storage_state(path) -> None:
    """Write a minimal SID-bearing storage_state.json (required-cookie policy)."""
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "y",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )


# The probe hits authuser=0 by default (the AuthTokens default index).
_PROBE_URL = "https://notebooklm.google.com/?authuser=0"


async def test_in_memory_email_returned_without_http(httpx_mock: HTTPXMock) -> None:
    """An in-memory ``account_email`` short-circuits: returned, memoized, no HTTP."""
    client = NotebookLMClient(_make_auth(account_email="alice@example.com"))
    assert await client.get_account_email() == "alice@example.com"
    assert httpx_mock.get_requests() == []
    assert client._account_email_cache == "alice@example.com"


async def test_persisted_email_returned_without_http(httpx_mock: HTTPXMock, tmp_path) -> None:
    """No in-memory email, but persisted metadata carries one → returned, no HTTP."""
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    write_account_metadata(storage, authuser=1, email="bob@gmail.com")

    client = NotebookLMClient(_make_auth(storage_path=storage))
    assert await client.get_account_email() == "bob@gmail.com"
    assert httpx_mock.get_requests() == []


async def test_live_probe_persists_back_and_memoizes(httpx_mock: HTTPXMock, tmp_path) -> None:
    """Both sources empty + live_fallback → probe hit is returned, persisted, memoized."""
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    httpx_mock.add_response(
        url=_PROBE_URL, content=_wiz_html_with_email("carol@example.com").encode()
    )

    client = NotebookLMClient(_make_auth(storage_path=storage))
    http_client = _install_probe_client(client)
    try:
        assert await client.get_account_email() == "carol@example.com"
        # Self-heal wrote the email back to storage for the next process.
        assert read_account_metadata(storage)["email"] == "carol@example.com"
        # A second call is served from the memo — no new HTTP.
        assert await client.get_account_email() == "carol@example.com"
    finally:
        await http_client.aclose()

    assert len(httpx_mock.get_requests()) == 1


async def test_no_live_fallback_returns_none_without_http(httpx_mock: HTTPXMock) -> None:
    """Both sources empty + ``live_fallback=False`` → None, no probe."""
    client = NotebookLMClient(_make_auth())
    assert await client.get_account_email(live_fallback=False) is None
    assert httpx_mock.get_requests() == []


async def test_probe_transport_error_returns_none(httpx_mock: HTTPXMock) -> None:
    """A probe transport blip degrades to None, never raises."""
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=_PROBE_URL)

    client = NotebookLMClient(_make_auth())
    http_client = _install_probe_client(client)
    try:
        assert await client.get_account_email() is None
    finally:
        await http_client.aclose()


async def test_probe_login_redirect_returns_none(httpx_mock: HTTPXMock) -> None:
    """A login redirect from the probe → None (not an error).

    The probe client follows redirects (like production), so the 302 lands on a
    200 signin page; ``None`` must come from the ``is_google_auth_redirect`` final-URL
    check, NOT the ``status != 200`` guard — this exercises the real branch.
    """
    httpx_mock.add_response(
        url=_PROBE_URL,
        status_code=302,
        headers={"Location": "https://accounts.google.com/v3/signin/identifier"},
    )
    # The followed signin page returns 200 — extraction would run if the redirect
    # guard were missing, so this proves the guard is what returns None.
    httpx_mock.add_response(
        url="https://accounts.google.com/v3/signin/identifier",
        status_code=200,
        html="<html><body>Sign in - Google Accounts</body></html>",
    )

    client = NotebookLMClient(_make_auth())
    http_client = _install_probe_client(client)
    try:
        assert await client.get_account_email() is None
    finally:
        await http_client.aclose()


async def test_inline_auth_probe_hit_does_not_persist(httpx_mock: HTTPXMock, monkeypatch) -> None:
    """Inline auth (``storage_path=None``): probe hit is returned but never persisted."""
    httpx_mock.add_response(
        url=_PROBE_URL, content=_wiz_html_with_email("dave@example.com").encode()
    )
    write_spy = MagicMock()
    monkeypatch.setattr(client_module, "write_account_metadata", write_spy)

    client = NotebookLMClient(_make_auth())  # storage_path is None
    http_client = _install_probe_client(client)
    try:
        assert await client.get_account_email() == "dave@example.com"
    finally:
        await http_client.aclose()

    # No path → nothing to persist to; the write branch must be skipped.
    write_spy.assert_not_called()


async def test_get_account_authuser_returns_auth_index() -> None:
    """``get_account_authuser`` surfaces the in-memory ``authuser`` (network-free)."""
    client = NotebookLMClient(_make_auth(authuser=2))
    assert client.get_account_authuser() == 2
