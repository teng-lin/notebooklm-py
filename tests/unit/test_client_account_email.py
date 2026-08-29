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

import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest
from pytest_httpx import HTTPXMock

import notebooklm.client as client_module
from notebooklm._auth import account_email as account_email_module
from notebooklm._auth.cookie_types import CookieJar
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
    cookie_jar: httpx.Cookies | None = None,
) -> AuthTokens:
    """Build minimal AuthTokens with the identity fields under test."""
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y", "HSID": "z"},
        csrf_token="csrf",
        session_id="sess",
        account_email=account_email,
        authuser=authuser,
        storage_path=storage_path,
        cookie_jar=cookie_jar,
    )


def _install_probe_client(client: NotebookLMClient) -> httpx.AsyncClient:
    """Give the kernel a real ``httpx.AsyncClient`` (intercepted by pytest-httpx).

    ``follow_redirects=True`` mirrors the production kernel client (_web/transport/kernel.py) so
    the login-redirect path exercises the real ``is_google_auth_redirect`` branch
    (a followed 302 lands on a 200 signin page), not just the ``status != 200`` guard.
    """
    http_client = httpx.AsyncClient(cookies=client.auth.cookie_jar, follow_redirects=True)
    install_http_client_for_test(client._collaborators.kernel, http_client)
    return http_client


async def _open_probe_client(client: NotebookLMClient) -> httpx.AsyncClient:
    """Open through the root lifecycle, then install the pytest-httpx client."""
    await client.__aenter__()
    kernel = client._collaborators.kernel
    original = kernel.get_http_client()
    await original.aclose()
    return _install_probe_client(client)


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
_PROBE_URL = "https://notebook.google.com/?authuser=0"


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

    client = NotebookLMClient(_make_auth(storage_path=storage, authuser=1))
    assert await client.get_account_email() == "bob@gmail.com"
    assert httpx_mock.get_requests() == []


async def test_pre_open_persisted_email_uses_kernel_seed_not_public_shadow(
    httpx_mock: HTTPXMock,
    tmp_path,
) -> None:
    """The network-free pre-open lookup is owned by Kernel after bootstrap."""
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    write_account_metadata(storage, authuser=0, email="kernel@example.com")
    client = NotebookLMClient(_make_auth(storage_path=storage))

    divergent_shadow = httpx.Cookies()
    divergent_shadow.set("SID", "shadow", domain=".google.com", path="/")
    divergent_shadow.set("__Secure-1PSIDTS", "shadow-ts", domain=".google.com", path="/")
    client.auth.replace_cookie_jar(divergent_shadow)

    assert await client.get_account_email(live_fallback=False) == "kernel@example.com"
    assert httpx_mock.get_requests() == []


async def test_post_close_persisted_email_uses_retained_kernel_jar(
    httpx_mock: HTTPXMock,
    tmp_path,
) -> None:
    """Closing transport retains its exact jar for network-free identity reads."""
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    write_account_metadata(storage, authuser=0, email="closed@example.com")
    client = NotebookLMClient(_make_auth(storage_path=storage))
    http_client = _install_probe_client(client)
    await client._collaborators.kernel.aclose()
    assert http_client.is_closed

    divergent_shadow = httpx.Cookies()
    divergent_shadow.set("SID", "shadow", domain=".google.com", path="/")
    divergent_shadow.set("__Secure-1PSIDTS", "shadow-ts", domain=".google.com", path="/")
    client.auth.replace_cookie_jar(divergent_shadow)

    assert await client.get_account_email(live_fallback=False) == "closed@example.com"
    assert httpx_mock.get_requests() == []


async def test_live_probe_persists_back_and_memoizes(httpx_mock: HTTPXMock, tmp_path) -> None:
    """Both sources empty + live_fallback → probe hit is returned, persisted, memoized."""
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    httpx_mock.add_response(
        url=_PROBE_URL, content=_wiz_html_with_email("carol@example.com").encode()
    )

    client = NotebookLMClient(_make_auth(storage_path=storage))
    await _open_probe_client(client)
    try:
        assert await client.get_account_email() == "carol@example.com"
        # Self-heal wrote the email back to storage for the next process.
        assert read_account_metadata(storage)["email"] == "carol@example.com"
        # A second call is served from the memo — no new HTTP.
        assert await client.get_account_email() == "carol@example.com"
    finally:
        await client.close(drain=False)

    assert len(httpx_mock.get_requests()) == 1


async def test_no_live_fallback_returns_none_without_http(httpx_mock: HTTPXMock) -> None:
    """Both sources empty + ``live_fallback=False`` → None, no probe."""
    client = NotebookLMClient(_make_auth())
    assert await client.get_account_email(live_fallback=False) is None
    assert httpx_mock.get_requests() == []


async def test_drained_client_rejects_live_probe_before_http(monkeypatch) -> None:
    """The account probe uses the same top-level admission fence as other I/O."""
    client = NotebookLMClient(_make_auth())
    await client.__aenter__()
    probe_called = False

    async def probe(_http_client, _authuser):  # type: ignore[no-untyped-def]
        nonlocal probe_called
        probe_called = True
        return "should-not-run@example.com"

    monkeypatch.setattr(client_module, "_probe_authuser", probe)
    try:
        await client.drain()
        with pytest.raises(RuntimeError, match="not accepting new operations"):
            await client.get_account_email()
    finally:
        await client.close(drain=False)

    assert probe_called is False


async def test_drained_client_keeps_network_free_account_identity_available() -> None:
    client = NotebookLMClient(_make_auth(account_email="cached@example.com"))
    await client.__aenter__()
    try:
        await client.drain()
        assert await client.get_account_email() == "cached@example.com"
    finally:
        await client.close(drain=False)


async def test_old_live_probe_cannot_publish_after_forced_close_reopen(monkeypatch) -> None:
    """A retired probe neither reaches epoch 2 resources nor caches its result."""
    client = NotebookLMClient(_make_auth())
    old_http_client = await _open_probe_client(client)
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    seen_clients: list[httpx.AsyncClient] = []

    async def blocked_probe(http_client, _authuser):  # type: ignore[no-untyped-def]
        seen_clients.append(http_client)
        probe_started.set()
        await release_probe.wait()
        return "retired@example.com"

    monkeypatch.setattr(client_module, "_probe_authuser", blocked_probe)
    task = asyncio.create_task(client.get_account_email())
    await probe_started.wait()

    await client.close(drain=False)
    await client.__aenter__()
    new_http_client = client._collaborators.kernel.get_http_client()
    release_probe.set()
    try:
        with pytest.raises(RuntimeError, match="resource generation is retired"):
            await task
    finally:
        await client.close(drain=False)

    assert seen_clients == [old_http_client]
    assert new_http_client is not old_http_client
    assert client._account_email_cache is None


async def test_probe_transport_error_returns_none(httpx_mock: HTTPXMock) -> None:
    """A probe transport blip degrades to None, never raises."""
    httpx_mock.add_exception(httpx.ConnectError("boom"), url=_PROBE_URL)

    client = NotebookLMClient(_make_auth())
    await _open_probe_client(client)
    try:
        assert await client.get_account_email() is None
    finally:
        await client.close(drain=False)


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
    await _open_probe_client(client)
    try:
        assert await client.get_account_email() is None
    finally:
        await client.close(drain=False)


async def test_inline_auth_probe_hit_does_not_persist(httpx_mock: HTTPXMock, monkeypatch) -> None:
    """Inline auth (``storage_path=None``): probe hit is returned but never persisted."""
    httpx_mock.add_response(
        url=_PROBE_URL, content=_wiz_html_with_email("dave@example.com").encode()
    )
    write_spy = MagicMock()
    monkeypatch.setattr(
        account_email_module,
        "_write_account_metadata_if_document_unchanged",
        write_spy,
    )

    client = NotebookLMClient(_make_auth())  # storage_path is None
    await _open_probe_client(client)
    try:
        assert await client.get_account_email() == "dave@example.com"
    finally:
        await client.close(drain=False)

    # No path → nothing to persist to; the write branch must be skipped.
    write_spy.assert_not_called()


async def test_get_account_authuser_returns_auth_index() -> None:
    """``get_account_authuser`` surfaces the in-memory ``authuser`` (network-free)."""
    client = NotebookLMClient(_make_auth(authuser=2))
    assert client.get_account_authuser() == 2


async def test_account_email_cache_is_keyed_to_the_active_route() -> None:
    """A profile reload cannot leave REST/MCP identity on the previous account."""
    client = NotebookLMClient(_make_auth(account_email="old@example.com", authuser=1))
    assert await client.get_account_email(live_fallback=False) == "old@example.com"

    client.auth.authuser = 2
    client.auth.account_email = "new@example.com"
    assert await client.get_account_email(live_fallback=False) == "new@example.com"

    client.auth.authuser = 0
    client.auth.account_email = None
    assert await client.get_account_email(live_fallback=False) is None


async def test_account_email_cache_is_keyed_to_profile_session_generation(
    httpx_mock: HTTPXMock,
) -> None:
    """A same-route profile replacement invalidates an earlier live-probe memo."""
    httpx_mock.add_response(
        url=_PROBE_URL,
        content=_wiz_html_with_email("old@example.com").encode(),
    )
    client = NotebookLMClient(_make_auth())
    http_client = await _open_probe_client(client)
    try:
        assert await client.get_account_email() == "old@example.com"
        target = http_client.cookies
        target.set("SID", "old", domain=".google.com", path="/")
        source = httpx.Cookies()
        source.set("SID", "new", domain=".google.com", path="/")
        assert (
            client.auth._replace_profile_session(
                target_cookie_jar=target,
                source_cookie_jar=source,
                expected_cookie_jar=CookieJar.from_httpx(target),
                expected_authuser=0,
                expected_account_email=None,
                expected_generation=0,
                authuser=0,
                account_email=None,
            )
            is False
        )
        assert await client.get_account_email(live_fallback=False) is None
    finally:
        await client.close(drain=False)


async def test_live_probe_restarts_after_profile_session_replacement(
    tmp_path,
    monkeypatch,
) -> None:
    """An old probe result cannot be returned or self-healed into a newer profile."""
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    client = NotebookLMClient(_make_auth(storage_path=storage))
    http_client = await _open_probe_client(client)
    http_client.cookies.set("SID", "x", domain=".google.com", path="/")
    http_client.cookies.set("__Secure-1PSIDTS", "y", domain=".google.com", path="/")
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def blocked_probe(_http_client, authuser):  # type: ignore[no-untyped-def]
        assert authuser == 0
        probe_started.set()
        await release_probe.wait()
        return "old@example.com"

    monkeypatch.setattr(client_module, "_probe_authuser", blocked_probe)
    task = asyncio.create_task(client.get_account_email())
    await probe_started.wait()
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "new", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "new-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "notebooklm": {
                    "account": {"authuser": 2, "email": "new@example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    source = httpx.Cookies()
    source.set("SID", "new", domain=".google.com", path="/")
    source.set("__Secure-1PSIDTS", "new-ts", domain=".google.com", path="/")
    assert (
        client.auth._replace_profile_session(
            target_cookie_jar=http_client.cookies,
            source_cookie_jar=source,
            expected_cookie_jar=CookieJar.from_httpx(http_client.cookies),
            expected_authuser=0,
            expected_account_email=None,
            expected_generation=0,
            authuser=2,
            account_email="new@example.com",
        )
        is True
    )
    release_probe.set()
    try:
        assert await task == "new@example.com"
    finally:
        await client.close(drain=False)

    assert read_account_metadata(storage) == {
        "authuser": 2,
        "email": "new@example.com",
    }


async def test_live_probe_self_heal_cannot_overwrite_advanced_profile(
    tmp_path,
    monkeypatch,
) -> None:
    """The durable heal is CASed against the exact pre-probe document."""
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    client = NotebookLMClient(_make_auth(storage_path=storage))
    await _open_probe_client(client)
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def blocked_probe(_http_client, authuser):  # type: ignore[no-untyped-def]
        assert authuser == 0
        probe_started.set()
        await release_probe.wait()
        return "old@example.com"

    monkeypatch.setattr(client_module, "_probe_authuser", blocked_probe)
    task = asyncio.create_task(client.get_account_email())
    await probe_started.wait()
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "new", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "new-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "notebooklm": {
                    "account": {"authuser": 2, "email": "new@example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    release_probe.set()
    try:
        assert await task == "old@example.com"
    finally:
        await client.close(drain=False)

    assert read_account_metadata(storage) == {
        "authuser": 2,
        "email": "new@example.com",
    }


async def test_live_probe_does_not_self_heal_into_previously_advanced_profile(
    tmp_path,
    monkeypatch,
) -> None:
    """Disk B sampled before the probe cannot receive live session A's identity."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "disk-b", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "disk-b-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "notebooklm": {"account": {"authuser": 2}},
            }
        ),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "x", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "y", domain=".google.com", path="/")
    client = NotebookLMClient(_make_auth(storage_path=storage, cookie_jar=live))
    await _open_probe_client(client)

    async def probe_old_session(_http_client, authuser):  # type: ignore[no-untyped-def]
        assert authuser == 0
        return "old@example.com"

    monkeypatch.setattr(client_module, "_probe_authuser", probe_old_session)
    try:
        assert await client.get_account_email() == "old@example.com"
    finally:
        await client.close(drain=False)

    assert read_account_metadata(storage) == {"authuser": 2}


async def test_persisted_email_from_a_different_live_session_is_ignored(tmp_path) -> None:
    """Network-free diagnostics never combine disk B's email with live route A."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "disk-b", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "disk-b-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "notebooklm": {
                    "account": {"authuser": 2, "email": "disk-b@example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "x", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "y", domain=".google.com", path="/")
    client = NotebookLMClient(_make_auth(storage_path=storage, cookie_jar=live))

    assert await client.get_account_email(live_fallback=False) is None
    assert client._account_email_cache is None


async def test_persisted_email_is_matched_against_authoritative_kernel_jar(tmp_path) -> None:
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    write_account_metadata(storage, authuser=0, email="disk-a@example.com")
    shadow = httpx.Cookies()
    shadow.set("SID", "x", domain=".google.com", path="/")
    shadow.set("__Secure-1PSIDTS", "y", domain=".google.com", path="/")
    client = NotebookLMClient(_make_auth(storage_path=storage, cookie_jar=shadow))
    http_client = _install_probe_client(client)
    http_client.cookies.set("SID", "kernel-b", domain=".google.com", path="/")
    http_client.cookies.set("__Secure-1PSIDTS", "kernel-b-ts", domain=".google.com", path="/")
    try:
        assert await client.get_account_email(live_fallback=False) is None
    finally:
        await http_client.aclose()


async def test_matching_legacy_profile_email_remains_network_free(tmp_path) -> None:
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    (tmp_path / "context.json").write_text(
        json.dumps({"account": {"authuser": 6, "email": "legacy@example.com"}}),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "x", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "y", domain=".google.com", path="/")
    client = NotebookLMClient(_make_auth(storage_path=storage, cookie_jar=live, authuser=6))

    assert await client.get_account_email(live_fallback=False) == "legacy@example.com"


@pytest.mark.parametrize("corruption", [b"\xff", b"[]"])
async def test_post_probe_storage_corruption_does_not_escape(
    tmp_path,
    monkeypatch,
    httpx_mock: HTTPXMock,
    corruption: bytes,
) -> None:
    storage = tmp_path / "storage_state.json"
    _write_storage_state(storage)
    httpx_mock.add_response(
        url=_PROBE_URL,
        content=_wiz_html_with_email("live@example.com").encode(),
    )
    original = account_email_module._write_account_metadata_if_document_unchanged

    def corrupt_then_write(path, **kwargs):  # type: ignore[no-untyped-def]
        path.write_bytes(corruption)
        return original(path, **kwargs)

    monkeypatch.setattr(
        account_email_module,
        "_write_account_metadata_if_document_unchanged",
        corrupt_then_write,
    )
    client = NotebookLMClient(_make_auth(storage_path=storage))
    await _open_probe_client(client)
    try:
        assert await client.get_account_email() == "live@example.com"
    finally:
        await client.close(drain=False)
