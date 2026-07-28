"""Tests for the self-hosted OAuth authorization server (``notebooklm.mcp._oauth``).

Covers config resolution (off / partial / weak / non-https / ok), the password gate
(`/login` GET form, wrong→401+retry, right→302+code, throttle, pending bounds), the DCR
cap, `build_auth` composition, persistence round-trip, and an offline end-to-end
register→authorize→login→token→verify flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastmcp")

from fastmcp.server.auth import MultiAuth  # noqa: E402
from mcp.server.auth.provider import AuthorizationParams  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.datastructures import Headers  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from notebooklm.mcp._auth import McpBearerAuthProvider, build_auth  # noqa: E402
from notebooklm.mcp._oauth import (  # noqa: E402
    MAX_CLIENTS,
    MAX_LOGIN_ATTEMPTS,
    OAUTH_BASE_URL_ENV,
    OAUTH_PASSWORD_ENV,
    OAUTH_STATE_PATH_ENV,
    THROTTLE_MAX_FAILURES,
    TRUST_PROXY_ENV,
    OAuthConfig,
    SelfHostedOAuthProvider,
    _client_ip,
    _derived_state_path,
    _migrate_legacy_state,
    _slug_for_base_url,
    build_oauth_provider,
    get_oauth_config,
)
from notebooklm.mcp.server import create_server  # noqa: E402

_PW = "a-strong-random-password-1234567890"


@pytest.fixture
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        OAUTH_PASSWORD_ENV,
        OAUTH_BASE_URL_ENV,
        TRUST_PROXY_ENV,
        "NOTEBOOKLM_HOME",
        "NOTEBOOKLM_PROFILE",
    ):
        monkeypatch.delenv(k, raising=False)


class _FakeRequest:
    """Minimal Request stand-in for `_client_ip`: case-insensitive `.headers` (Starlette
    `Headers`) plus a `.client` with `.host` (or `None` to exercise the no-peer branch)."""

    def __init__(self, *, cf: str | None, peer: str | None) -> None:
        self.headers = Headers({"cf-connecting-ip": cf} if cf is not None else {})
        self.client = None if peer is None else type("C", (), {"host": peer})()


def _provider(tmp_path=None) -> SelfHostedOAuthProvider:
    state = (tmp_path / "oauth_state.json") if tmp_path else None
    return SelfHostedOAuthProvider(
        password=_PW, base_url="https://host.example.com", state_path=state
    )


def _client(cid: str = "c1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id=cid, redirect_uris=["https://claude.ai/cb"])


def _params() -> AuthorizationParams:
    return AuthorizationParams(
        state="st",
        scopes=[],
        code_challenge="cc",
        redirect_uri="https://claude.ai/cb",
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


# --------------------------------------------------------------------------- config
def test_config_off(_clear_env: None) -> None:
    assert get_oauth_config() is None


@pytest.mark.parametrize(
    ("pw", "base", "needle"),
    [
        (_PW, "", "BASE_URL"),  # partial
        ("", "https://h", "PASSWORD"),  # partial
        ("short", "https://h", "at least"),  # weak
        (_PW, "http://h", "https"),  # non-https
        (_PW, "https://", "https"),  # https but no host
        (_PW, "https://h?x=1", "https"),  # query not allowed
        (_PW, "https://h#f", "https"),  # fragment not allowed
        (_PW, "https://h/mcp", "/mcp"),  # the connector URL, not the bare origin
    ],
)
def test_config_fail_closed(
    _clear_env: None, monkeypatch: pytest.MonkeyPatch, pw: str, base: str, needle: str
) -> None:
    if pw:
        monkeypatch.setenv(OAUTH_PASSWORD_ENV, pw)
    if base:
        monkeypatch.setenv(OAUTH_BASE_URL_ENV, base)
    with pytest.raises(SystemExit) as e:
        get_oauth_config()
    assert needle in str(e.value)


def test_config_state_path_is_base_url_keyed_derived_default(
    _clear_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default state_path is deployment-scoped: <home>/oauth/<slug(base_url)>.json,
    NOT the served profile dir (#1765 reversal). legacy_state_path still points at the
    old profile-dir file so an existing install can be migrated once."""
    monkeypatch.setenv(OAUTH_PASSWORD_ENV, _PW)
    monkeypatch.setenv(OAUTH_BASE_URL_ENV, "https://host.example.com")
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "server")
    cfg = get_oauth_config()
    assert cfg is not None and cfg.state_path is not None
    assert cfg.state_path == _derived_state_path("https://host.example.com")
    assert cfg.state_path.parent == tmp_path / "oauth"
    assert cfg.state_path.name.startswith("host.example.com.")
    # legacy migration source still tracks the served profile
    assert cfg.legacy_state_path is not None
    assert cfg.legacy_state_path.parts[-3:] == ("profiles", "server", "oauth_state.json")


def test_config_state_path_env_override_wins(
    _clear_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """NOTEBOOKLM_MCP_OAUTH_STATE_PATH is used verbatim and wins over the derived default;
    an empty value is treated as unset (falls back to derived)."""
    monkeypatch.setenv(OAUTH_PASSWORD_ENV, _PW)
    monkeypatch.setenv(OAUTH_BASE_URL_ENV, "https://host.example.com")
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    custom = tmp_path / "custom" / "oauth.json"
    monkeypatch.setenv(OAUTH_STATE_PATH_ENV, str(custom))
    cfg = get_oauth_config()
    assert cfg is not None and cfg.state_path == custom
    # empty string → treated as unset → derived default
    monkeypatch.setenv(OAUTH_STATE_PATH_ENV, "")
    cfg2 = get_oauth_config()
    assert cfg2 is not None and cfg2.state_path == _derived_state_path("https://host.example.com")


def test_config_malformed_profile_yields_no_legacy_not_systemexit(
    _clear_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A path-traversal profile name no longer aborts OAuth startup: the derived
    (base_url-keyed) state_path is unaffected, and legacy_state_path degrades to None
    (nothing to migrate) rather than raising SystemExit."""
    monkeypatch.setenv(OAUTH_PASSWORD_ENV, _PW)
    monkeypatch.setenv(OAUTH_BASE_URL_ENV, "https://host.example.com")
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    cfg = get_oauth_config(profile="../escape")
    assert cfg is not None
    assert cfg.state_path == _derived_state_path("https://host.example.com")
    assert cfg.legacy_state_path is None


def test_slug_is_collision_resistant() -> None:
    """Distinct origins → distinct slug even when the sanitized readable prefix collides
    (the 64-bit hash of the normalized origin disambiguates); same origin differing only
    by case/trailing-slash → same slug."""
    # readable-prefix collisions that MUST still differ (hash disambiguates)
    for a, b in [
        ("https://bücher.example", "https://b_cher.example"),
        ("https://host:1", "https://host_1"),
        ("https://[::1]", "https://__1"),
    ]:
        assert _slug_for_base_url(a) != _slug_for_base_url(b), (a, b)
    # case- and trailing-slash-insensitive (same server)
    assert _slug_for_base_url("https://Host.Example.com") == _slug_for_base_url(
        "https://host.example.com/"
    )
    # output is filesystem-safe (single path component, no separators/traversal)
    slug = _slug_for_base_url("https://host.example.com")
    assert "/" not in slug and "\\" not in slug and slug not in (".", "..")


def test_derived_state_path_distinct_per_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    a = _derived_state_path("https://a.example.com")
    b = _derived_state_path("https://b.example.com")
    assert a != b
    assert a.parent == b.parent == tmp_path / "oauth"


def test_migrate_legacy_state_copies_renames_and_is_revocation_safe(tmp_path: Path) -> None:
    """Legacy oauth_state.json is migrated once into the new path (0600), the legacy file
    is renamed .migrated, re-runs are no-ops, and after a revocation (rm the new file) it
    is NOT re-created — the legacy source no longer exists as a fallback."""
    legacy = tmp_path / "profiles" / "server" / "oauth_state.json"
    legacy.parent.mkdir(parents=True)
    payload = {"clients": {"c1": {"redirect_uris": ["https://claude.ai/cb"]}}, "access_tokens": {}}
    legacy.write_text(json.dumps(payload), encoding="utf-8")
    new = tmp_path / "oauth" / "host.abcd.json"

    _migrate_legacy_state(new, legacy)

    assert json.loads(new.read_text()) == payload
    assert not legacy.exists()
    assert legacy.with_name("oauth_state.json.migrated").exists()
    if os.name == "posix":
        assert (new.stat().st_mode & 0o777) == 0o600
    # idempotent: a second run (new exists) is a no-op
    _migrate_legacy_state(new, legacy)
    # revocation-safe: delete the live state file → no legacy source remains to re-migrate
    new.unlink()
    _migrate_legacy_state(new, legacy)
    assert not new.exists()


def test_migrate_legacy_state_skips_malformed_and_missing(tmp_path: Path) -> None:
    """Corrupt legacy JSON is skipped without crashing; a None/missing legacy is a no-op."""
    new = tmp_path / "oauth" / "host.abcd.json"
    bad = tmp_path / "oauth_state.json"
    bad.write_text("not json at all", encoding="utf-8")
    _migrate_legacy_state(new, bad)  # must not raise
    assert not new.exists()
    _migrate_legacy_state(new, None)  # no-op
    _migrate_legacy_state(new, tmp_path / "does-not-exist.json")  # no-op
    assert not new.exists()


def test_migrate_legacy_state_rolls_back_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the state write fails after the legacy file is retired, the rename is rolled
    back so the next startup retries cleanly — no live state_path is left to trap a later
    revocation, and the data isn't stranded in .migrated."""
    import notebooklm.mcp._oauth as oauth_mod

    legacy = tmp_path / "profiles" / "server" / "oauth_state.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"clients": {}}), encoding="utf-8")
    new = tmp_path / "oauth" / "host.abcd.json"

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(oauth_mod, "atomic_update_json", _boom)
    _migrate_legacy_state(new, legacy)  # must not raise

    assert legacy.exists()  # rolled back — legacy restored for a clean retry
    assert not legacy.with_name("oauth_state.json.migrated").exists()
    assert not new.exists()


def test_migrate_legacy_state_retires_lingering_legacy_when_state_exists(tmp_path: Path) -> None:
    """Defensive cleanup: state_path already exists but a legacy file lingers (an earlier
    rename failed) — retire the legacy so it can't be re-migrated after a revocation,
    without touching the authoritative live state."""
    legacy = tmp_path / "oauth_state.json"
    legacy.write_text(json.dumps({"clients": {"old": {}}}), encoding="utf-8")
    new = tmp_path / "oauth" / "host.abcd.json"
    new.parent.mkdir(parents=True)
    new.write_text(json.dumps({"clients": {"live": {}}}), encoding="utf-8")

    _migrate_legacy_state(new, legacy)

    assert not legacy.exists()
    assert legacy.with_name("oauth_state.json.migrated").exists()
    assert json.loads(new.read_text()) == {"clients": {"live": {}}}  # live state untouched


def test_migrate_legacy_state_retires_non_dict_payload(tmp_path: Path) -> None:
    """A valid-JSON-but-non-object legacy file is retired (not re-parsed every startup)
    and never written as state."""
    legacy = tmp_path / "oauth_state.json"
    legacy.write_text("[1, 2, 3]", encoding="utf-8")
    new = tmp_path / "oauth" / "host.abcd.json"

    _migrate_legacy_state(new, legacy)

    assert not new.exists()
    assert not legacy.exists()
    assert legacy.with_name("oauth_state.json.migrated").exists()


def test_migrate_legacy_state_noop_when_override_equals_legacy(tmp_path: Path) -> None:
    """If NOTEBOOKLM_MCP_OAUTH_STATE_PATH points the live state AT the legacy file itself,
    migration is a no-op — the file must NOT be retired out from under the provider that
    then loads it (which would orphan every registered client)."""
    same = tmp_path / "oauth_state.json"
    same.write_text(json.dumps({"clients": {"c1": {}}}), encoding="utf-8")

    _migrate_legacy_state(same, same)

    assert same.exists()  # untouched — not renamed to .migrated
    assert not same.with_name("oauth_state.json.migrated").exists()
    assert json.loads(same.read_text()) == {"clients": {"c1": {}}}


def test_migrate_legacy_state_retires_invalid_utf8(tmp_path: Path) -> None:
    """A legacy file with invalid UTF-8 bytes is unusable → retired (not a startup crash;
    UnicodeDecodeError ⊂ ValueError is caught), never written as state."""
    legacy = tmp_path / "oauth_state.json"
    legacy.write_bytes(b"\xff\xfe not utf-8 at all")
    new = tmp_path / "oauth" / "host.abcd.json"

    _migrate_legacy_state(new, legacy)  # must not raise

    assert not new.exists()
    assert not legacy.exists()
    assert legacy.with_name("oauth_state.json.migrated").exists()


def test_migrate_legacy_state_ignores_reappeared_backup_when_marker_exists(tmp_path: Path) -> None:
    """After a one-time migration (``.migrated`` marker present) and a revocation (live
    state deleted), a restored/leftover oauth_state.json must NOT be re-imported — it is
    retired, not migrated, so revoked tokens can't be resurrected from a backup."""
    legacy = tmp_path / "oauth_state.json"
    legacy.write_text(json.dumps({"clients": {"revoked": {}}}), encoding="utf-8")
    (tmp_path / "oauth_state.json.migrated").write_text("{}", encoding="utf-8")  # prior marker
    new = tmp_path / "oauth" / "host.abcd.json"  # revoked → live state absent

    _migrate_legacy_state(new, legacy)

    assert not new.exists()  # NOT re-imported
    assert not legacy.exists()  # retired (folded into the marker)
    assert legacy.with_name("oauth_state.json.migrated").exists()


# --------------------------------------------------------------------------- routes / DCR
def test_provider_routes_include_login_and_register() -> None:
    p = _provider()
    paths = {getattr(r, "path", "") for r in p.get_routes()}
    assert "/login" in paths
    assert any("register" in x for x in paths)
    assert any("oauth-authorization-server" in x for x in paths)


def test_metadata_advertises_registration_endpoint() -> None:
    p = _provider()
    app = Starlette(routes=p.get_routes())
    with TestClient(app) as c:
        meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta.get("registration_endpoint")  # DCR enabled → claude.ai can register


# ------------------------------------------------- connector discovery (RFC 9728)
# These drive the FULL FastMCP ``http_app()`` — not just the AS provider routes —
# because the protected-resource-metadata endpoint is mounted by the MCP app, and
# that is exactly what fastmcp 3.4.3 regressed: its Host/Origin guard rejected any
# request whose Host wasn't its (localhost/``testserver``) allowlist — including
# the deployment's own public origin — so claude.ai's discovery fetch got a
# non-200 and dead-ended ("Couldn't connect to the server"). pyproject pins
# ``fastmcp==3.4.2``; these fail loudly (under a realistic Host) if a float breaks it.
_HOST = "host.example.com"


def _oauth_http_app():
    """The real FastMCP ``http_app()`` in OAuth mode, client bound to a stub."""

    @contextlib.asynccontextmanager
    async def factory():
        yield MagicMock()

    server = create_server(client_factory=factory, auth=build_auth(None, _provider()))
    return server.http_app()


def _path(url: str) -> str:
    """Strip the public origin → the route path the in-process TestClient hits."""
    return url.split(_HOST, 1)[-1]


# The connector reaches the server under its OWN public origin (behind the
# tunnel), NOT ``testserver``. This Host header is load-bearing: fastmcp 3.4.3's
# Host/Origin guard allowlisted ``testserver``/localhost but not the deployment
# origin, so it rejected the real request (→ 421) while a default TestClient Host
# sailed through. Every request below sends the realistic Host so the guard is
# actually exercised.
_H = {"host": _HOST}


def test_mcp_401_resource_metadata_is_fetchable() -> None:
    """The ``/mcp`` 401 must advertise a resource-metadata URL that returns 200
    when fetched under the deployment's own Host.

    Precise regression guard for fastmcp 3.4.3: its Host-protection returned a
    non-200 (421/404) for ``/.well-known/oauth-protected-resource/mcp`` under the
    real connector Host, so claude.ai could not discover the auth server
    ("Couldn't connect to the server"). pyproject pins ``fastmcp==3.4.2``.
    """
    with TestClient(_oauth_http_app()) as c:
        r = c.post(
            "/mcp",
            headers={**_H, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert r.status_code == 401, f"/mcp under Host={_HOST!r} -> {r.status_code}"
        m = re.search(r'resource_metadata="([^"]+)"', r.headers.get("www-authenticate", ""))
        assert m, f"no resource_metadata in WWW-Authenticate: {r.headers.get('www-authenticate')!r}"
        meta = c.get(_path(m.group(1)), headers=_H)
        assert meta.status_code == 200, (
            f"{_path(m.group(1))} under Host={_HOST!r} -> {meta.status_code}; the 401 "
            "points at an unreachable metadata doc (fastmcp host-guard regression?)"
        )
        assert meta.json().get("authorization_servers")


def test_dynamic_client_registration_succeeds() -> None:
    """claude.ai registers a client via DCR before connecting; ``/register`` → 201
    under the deployment's own Host (guards the same fastmcp 3.4.3 host regression)."""
    with TestClient(_oauth_http_app()) as c:
        meta = c.get("/.well-known/oauth-authorization-server", headers=_H)
        assert meta.status_code == 200, f"AS metadata under Host={_HOST!r} -> {meta.status_code}"
        reg = c.post(
            _path(meta.json()["registration_endpoint"]),
            headers=_H,
            json={
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
                "client_name": "test",
            },
        )
        assert reg.status_code == 201, (
            f"register under Host={_HOST!r} -> {reg.status_code}: {reg.text}"
        )
        assert reg.json().get("client_id")


# --------------------------------------------------------------------------- DCR cap
@pytest.mark.asyncio
async def test_register_client_cap_evicts_token_less_client() -> None:
    """At the DCR cap, a new registration evicts a TOKEN-LESS (never-used) client rather
    than rejecting — so an open-DCR flood can't permanently block the owner's onboarding."""
    p = _provider()
    for i in range(MAX_CLIENTS):
        await p.register_client(_client(f"c{i}"))
    await p.register_client(_client("newcomer"))  # evicts a token-less client, no raise
    assert len(p.clients) == MAX_CLIENTS  # still bounded
    assert "newcomer" in p.clients
    # updating an EXISTING client is still allowed at the cap (RFC 7591)
    await p.register_client(_client("newcomer"))


# --------------------------------------------------------------------------- authorize / pending bound
@pytest.mark.asyncio
async def test_authorize_stashes_and_returns_login() -> None:
    p = _provider()
    url = await p.authorize(_client(), _params())
    assert "/login?sid=" in url and len(p._pending) == 1


@pytest.mark.asyncio
async def test_pending_stash_bounded_by_eviction() -> None:
    """A flood of pre-password /authorize calls is bounded by evicting the oldest entry —
    NOT by rejecting new ones (so an attacker can't block the owner's login)."""
    from notebooklm.mcp._oauth import MAX_PENDING

    p = _provider()
    last = ""
    for _ in range(MAX_PENDING + 5):
        last = (await p.authorize(_client(), _params())).split("sid=")[1]
    assert len(p._pending) == MAX_PENDING  # bounded, never raised
    assert last in p._pending  # newest survives (oldest evicted)


# --------------------------------------------------------------------------- throttle
def test_throttle_per_ip() -> None:
    p = _provider()
    for _ in range(THROTTLE_MAX_FAILURES):
        assert p._throttled("1.2.3.4") is None
        p._record_failure("1.2.3.4")
    assert isinstance(p._throttled("1.2.3.4"), int)  # now throttled
    assert p._throttled("9.9.9.9") is None  # a different IP is unaffected


# --------------------------------------------------------------------------- /login via HTTP
def test_login_get_renders_form() -> None:
    p = _provider()
    with TestClient(Starlette(routes=p.get_routes())) as c:
        r = c.get("/login?sid=abc")
        assert r.status_code == 200 and "password" in r.text and "abc" in r.text


def test_login_post_wrong_then_right(tmp_path) -> None:
    p = _provider(tmp_path)
    client = _client()
    asyncio.run(p.register_client(client))
    sid = asyncio.run(p.authorize(client, _params())).split("sid=")[1]
    with TestClient(Starlette(routes=p.get_routes())) as c:
        # wrong password → 401, sid retained for retry
        r = c.post("/login", data={"sid": sid, "password": "nope"}, follow_redirects=False)
        assert r.status_code == 401 and sid in p._pending
        # right password → 302 to claude.ai redirect with a code
        r = c.post("/login", data={"sid": sid, "password": _PW}, follow_redirects=False)
        assert r.status_code == 302
        assert "code=" in r.headers["location"] and r.headers["location"].startswith(
            "https://claude.ai/cb"
        )
        assert sid not in p._pending  # single-use consumed


def test_login_post_locks_after_max_attempts() -> None:
    p = _provider()
    client = _client()
    asyncio.run(p.register_client(client))
    sid = asyncio.run(p.authorize(client, _params())).split("sid=")[1]
    with TestClient(Starlette(routes=p.get_routes())) as c:
        for _ in range(MAX_LOGIN_ATTEMPTS):
            c.post("/login", data={"sid": sid, "password": "nope"}, follow_redirects=False)
    assert sid not in p._pending  # sid burned after too many wrong attempts


# --------------------------------------------------------------------------- build_auth matrix
def test_build_auth_matrix() -> None:
    oauth = _provider()
    assert isinstance(build_auth("tok", oauth), MultiAuth)
    assert build_auth(None, oauth) is oauth
    assert isinstance(build_auth("tok", None), McpBearerAuthProvider)
    assert build_auth(None, None) is None


# --------------------------------------------------------------------------- persistence + e2e
def test_end_to_end_and_persistence(tmp_path) -> None:
    """register → authorize → /login(password) → code → token → verify, then reload
    the provider from disk and confirm the issued token still verifies."""

    async def run() -> str:
        p = _provider(tmp_path)
        client = _client()
        await p.register_client(client)
        sid = (await p.authorize(client, _params())).split("sid=")[1]
        with TestClient(Starlette(routes=p.get_routes())) as c:
            r = c.post("/login", data={"sid": sid, "password": _PW}, follow_redirects=False)
        code = r.headers["location"].split("code=")[1].split("&")[0]
        auth_code = p.auth_codes[code]
        token = await p.exchange_authorization_code(client, auth_code)
        assert await p.verify_token(token.access_token) is not None
        return token.access_token

    access_token = asyncio.run(run())

    # A fresh provider loading the same state file still recognizes the token + client.
    p2 = _provider(tmp_path)
    assert "c1" in p2.clients
    assert asyncio.run(p2.verify_token(access_token)) is not None


def test_save_state_runs_atomic_write_off_the_event_loop(tmp_path, monkeypatch) -> None:
    """Issue #1873(B): the blocking fsync/atomic-write must run OFF the loop thread.

    ``_save_state`` builds the snapshot on the loop, then offloads the mkdir +
    ``atomic_write_json`` (os.fsync under filelock) via ``anyio.to_thread.run_sync``.
    Capture the worker's thread id at write time and confirm it differs from the
    loop thread — AND that the file round-trips into a fresh provider.
    """
    import threading

    from notebooklm.mcp import _oauth as oauth_mod

    real_write = oauth_mod.atomic_write_json
    write_thread_ids: list[int] = []

    def _recording_write(path, data):
        write_thread_ids.append(threading.get_ident())
        return real_write(path, data)

    monkeypatch.setattr(oauth_mod, "atomic_write_json", _recording_write)

    async def run() -> int:
        p = _provider(tmp_path)
        await p.register_client(_client())
        return threading.get_ident()

    loop_thread_id = asyncio.run(run())

    # The atomic write ran (register_client persists) on a DIFFERENT thread.
    assert write_thread_ids, "atomic_write_json was never invoked"
    assert all(tid != loop_thread_id for tid in write_thread_ids)
    # And the persisted state round-trips into a fresh provider.
    p2 = _provider(tmp_path)
    assert "c1" in p2.clients


def test_all_save_state_callers_await_and_persist(tmp_path) -> None:
    """Issue #1873(B): every ``_save_state`` caller must AWAIT it.

    A missing ``await`` would leave a never-run coroutine and silently drop
    persistence (regressing #1765). Drive the flows that call
    ``exchange_authorization_code``, ``exchange_refresh_token`` and
    ``revoke_token`` and confirm each mutation reaches disk.
    """

    async def run() -> None:
        p = _provider(tmp_path)
        client = _client()
        await p.register_client(client)
        sid = (await p.authorize(client, _params())).split("sid=")[1]
        with TestClient(Starlette(routes=p.get_routes())) as c:
            r = c.post("/login", data={"sid": sid, "password": _PW}, follow_redirects=False)
        code = r.headers["location"].split("code=")[1].split("&")[0]
        token = await p.exchange_authorization_code(client, p.auth_codes[code])

        # exchange_refresh_token → persists the rotated token; the refresh
        # token must survive a reload done AFTER the exchange.
        assert token.refresh_token is not None
        refreshed = await p.exchange_refresh_token(
            client, p.refresh_tokens[token.refresh_token], scopes=[]
        )
        assert refreshed.access_token is not None
        p_after_refresh = _provider(tmp_path)
        assert await p_after_refresh.verify_token(refreshed.access_token) is not None

        # revoke_token → persists the removal; the revoked token must be gone
        # from a fresh reload.
        await p.revoke_token(p.access_tokens[refreshed.access_token])
        p_after_revoke = _provider(tmp_path)
        assert await p_after_revoke.verify_token(refreshed.access_token) is None

    asyncio.run(run())


def test_concurrent_save_state_serializes_last_write_wins(tmp_path, monkeypatch) -> None:
    """Issue #1873(B) follow-up: concurrent ``_save_state`` calls must serialize.

    Because ``_save_state`` snapshots on the loop then offloads the write, two
    concurrent saves could — absent serialization — snapshot in one order but
    have their fsyncs land in the opposite order, letting an OLDER snapshot
    clobber a newer one. The per-provider save lock forces a later save to
    snapshot only AFTER the earlier write has landed, so writes happen in
    start order and the last-started save wins.

    Drives the race deterministically: save1 snapshots ``{c1}`` and its (slow)
    write begins; only then is ``c2`` added and save2 started. With the lock the
    disk ends at ``{c1, c2}`` and the writes are ordered ``[{c1}, {c1, c2}]``;
    without it save2's fast write would land first and save1's ``{c1}`` write
    would clobber it (lost update).
    """
    import threading
    import time as _time

    from notebooklm.mcp import _oauth as oauth_mod

    real_write = oauth_mod.atomic_write_json
    write_order: list[tuple[str, ...]] = []
    first_write_started = threading.Event()

    def slow_write(path, data):
        markers = tuple(sorted(data["clients"]))
        write_order.append(markers)
        if len(markers) == 1:  # the first (save1) snapshot — hold the write open
            first_write_started.set()
            _time.sleep(0.2)
        return real_write(path, data)

    monkeypatch.setattr(oauth_mod, "atomic_write_json", slow_write)

    async def run() -> None:
        p = _provider(tmp_path)
        p.clients["c1"] = _client("c1")
        save1 = asyncio.create_task(p._save_state())
        # Wait until save1 holds the lock and its worker write is in progress.
        while not first_write_started.is_set():
            await asyncio.sleep(0.005)
        # Mutate AFTER save1 snapshotted; save2 must snapshot this newer state.
        p.clients["c2"] = _client("c2")
        save2 = asyncio.create_task(p._save_state())
        await asyncio.gather(save1, save2)

    asyncio.run(run())

    # Writes ran in start order: {c1} then {c1, c2} (no interleaving).
    assert write_order == [("c1",), ("c1", "c2")]
    # Last-started save wins on disk — no lost update.
    p2 = _provider(tmp_path)
    assert "c1" in p2.clients
    assert "c2" in p2.clients


# --------------------------------------------------------------------------- hardening (polish)
def test_oauth_config_repr_hides_password() -> None:
    cfg = OAuthConfig(password="super-secret-do-not-log", base_url="https://h", state_path=None)
    assert "super-secret-do-not-log" not in repr(cfg)


def test_login_form_escapes_reflected_sid() -> None:
    """`sid` on a GET comes from the URL (attacker-controllable) → must be escaped, and a
    strict CSP must be set, so /login?sid=<payload> is not a reflected XSS."""
    p = _provider()
    with TestClient(Starlette(routes=p.get_routes())) as c:
        r = c.get('/login?sid="><script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in r.text  # escaped, not injected
    csp = next(v for k, v in r.headers.items() if k.lower() == "content-security-policy")
    assert "default-src 'none'" in csp
    # MUST NOT set form-action: a correct password POST 302s to the client's redirect_uri
    # (e.g. claude.ai), and `form-action 'self'` would block that cross-origin callback.
    assert "form-action" not in csp


@pytest.mark.parametrize("blob", ["[1, 2, 3]", '"a string"', "not json at all", "{bad", ""])
def test_malformed_state_file_does_not_crash(tmp_path, blob: str) -> None:
    """A truncated / wrong-shape oauth_state.json must start empty, never crash startup."""
    (tmp_path / "oauth_state.json").write_text(blob, encoding="utf-8")
    p = _provider(tmp_path)  # must not raise
    assert p.clients == {}


def test_login_get_shows_escaped_consent_redirect() -> None:
    """The GET form shows the (escaped) redirect target for the sid's pending request,
    so a rogue registered client is visible before the password is entered."""
    p = _provider()
    client = OAuthClientInformationFull(client_id="c1", redirect_uris=["https://claude.ai/cb"])
    asyncio.run(p.register_client(client))
    sid = asyncio.run(p.authorize(client, _params())).split("sid=")[1]
    with TestClient(Starlette(routes=p.get_routes())) as c:
        r = c.get(f"/login?sid={sid}")
    assert "claude.ai/cb" in r.text  # consent line shows where the code returns


def test_fail_times_drops_empty_entries() -> None:
    """The per-IP throttle dict must not retain empty lists (bounded pre-auth memory)."""
    p = _provider()
    # a failure that ages out → the IP key is dropped, not kept as an empty list
    p._fail_times["1.2.3.4"] = [0.0]  # an ancient failure (epoch), outside the window
    assert p._throttled("1.2.3.4") is None
    assert "1.2.3.4" not in p._fail_times


# --------------------------------------------------------------------------- #1761 trusted-proxy
def test_client_ip_ignores_cf_header_when_untrusted() -> None:
    """Default (flag off): CF-Connecting-IP is NOT trusted — key on the socket peer, so a
    forged header can't dodge the throttle when the origin is exposed directly."""
    req = _FakeRequest(cf="9.9.9.9", peer="10.0.0.1")
    assert _client_ip(req, trust_proxy=False) == "10.0.0.1"


def test_client_ip_honors_cf_header_when_trusted() -> None:
    """Flag on (trusted proxy sets the header): key on CF-Connecting-IP."""
    req = _FakeRequest(cf="9.9.9.9", peer="10.0.0.1")
    assert _client_ip(req, trust_proxy=True) == "9.9.9.9"


def test_client_ip_empty_cf_header_falls_back_to_peer() -> None:
    """Even when trusted, a present-but-blank CF-Connecting-IP must NOT become a '' key —
    fall back to the socket peer."""
    assert _client_ip(_FakeRequest(cf="   ", peer="10.0.0.1"), trust_proxy=True) == "10.0.0.1"


def test_client_ip_no_peer_untrusted_cf_returns_unknown() -> None:
    """No socket peer, and the CF header present but UNTRUSTED (flag off, so ignored) →
    the bounded 'unknown' fallback rather than the attacker-controlled header value."""
    assert _client_ip(_FakeRequest(cf="9.9.9.9", peer=None), trust_proxy=False) == "unknown"


def test_config_resolves_trust_proxy_flag(
    _clear_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_oauth_config reads NOTEBOOKLM_MCP_TRUST_PROXY (=='1' → True; unset → False)."""
    monkeypatch.setenv(OAUTH_PASSWORD_ENV, _PW)
    monkeypatch.setenv(OAUTH_BASE_URL_ENV, "https://host.example.com")
    off = get_oauth_config()
    assert off is not None and off.trust_proxy is False
    monkeypatch.setenv(TRUST_PROXY_ENV, "1")
    on = get_oauth_config()
    assert on is not None and on.trust_proxy is True


def test_provider_stores_trust_proxy() -> None:
    p = SelfHostedOAuthProvider(password=_PW, base_url="https://h.example.com", trust_proxy=True)
    assert p._trust_proxy is True


def test_throttle_keys_on_peer_not_spoofed_cf_header() -> None:
    """End-to-end through /login: with the flag OFF, THROTTLE_MAX_FAILURES wrong POSTs — each
    with a FRESH sid and a DIFFERENT spoofed CF-Connecting-IP — still trip the throttle,
    proving the varying header is NOT the key (all failures bucket under the one peer).

    A fresh sid per POST is required: one sid burns after MAX_LOGIN_ATTEMPTS (< the throttle
    threshold) and a burned sid returns the expired form WITHOUT recording a failure."""
    assert MAX_LOGIN_ATTEMPTS < THROTTLE_MAX_FAILURES  # guards the fresh-sid necessity
    p = _provider()  # trust_proxy defaults False
    client = _client()
    asyncio.run(p.register_client(client))
    with TestClient(Starlette(routes=p.get_routes())) as c:
        for i in range(THROTTLE_MAX_FAILURES):
            sid = asyncio.run(p.authorize(client, _params())).split("sid=")[1]
            r = c.post(
                "/login",
                data={"sid": sid, "password": "nope"},
                headers={"cf-connecting-ip": f"203.0.113.{i}"},  # varies every request
                follow_redirects=False,
            )
            assert r.status_code == 401
        # next attempt (a fresh sid, another distinct spoofed IP) is throttled → the header
        # was never the key; the socket peer bucketed all failures together.
        sid = asyncio.run(p.authorize(client, _params())).split("sid=")[1]
        r = c.post(
            "/login",
            data={"sid": sid, "password": "nope"},
            headers={"cf-connecting-ip": "203.0.113.250"},
            follow_redirects=False,
        )
    assert r.status_code == 429


def test_throttle_separates_buckets_by_cf_header_when_trusted() -> None:
    """Inverse of the above: with trust_proxy=True, distinct CF-Connecting-IP values bucket
    SEPARATELY, so wrong POSTs from many spoofed IPs do NOT trip the throttle — proving the
    trusted path keys on the header. (Each distinct IP accrues a single failure < threshold.)"""
    p = SelfHostedOAuthProvider(password=_PW, base_url="https://host.example.com", trust_proxy=True)
    client = _client()
    asyncio.run(p.register_client(client))
    with TestClient(Starlette(routes=p.get_routes())) as c:
        for i in range(THROTTLE_MAX_FAILURES + 1):
            sid = asyncio.run(p.authorize(client, _params())).split("sid=")[1]
            r = c.post(
                "/login",
                data={"sid": sid, "password": "nope"},
                headers={"cf-connecting-ip": f"198.51.100.{i}"},  # a fresh IP each time
                follow_redirects=False,
            )
            assert r.status_code == 401  # never 429 — each IP has only one failure


# --------------------------------------------------------------------------- build_oauth_provider
def test_build_oauth_provider_wires_state_without_warning(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """state_path always resolves (get_oauth_config derives a base_url-keyed path), so
    build_oauth_provider wires it through — the old "state not persisted" startup warning
    was removed and must not fire. With no legacy_state_path set, migration is a no-op."""
    cfg = OAuthConfig(
        password=_PW, base_url="https://h.example.com", state_path=tmp_path / "oauth_state.json"
    )
    with caplog.at_level(logging.WARNING, logger="notebooklm.mcp._oauth"):
        provider = build_oauth_provider(cfg)
    assert isinstance(provider, SelfHostedOAuthProvider)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
