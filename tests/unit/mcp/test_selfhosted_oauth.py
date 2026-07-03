"""Tests for the self-hosted OAuth authorization server (``notebooklm.mcp._oauth``).

Covers config resolution (off / partial / weak / non-https / ok), the password gate
(`/login` GET form, wrong→401+retry, right→302+code, throttle, pending bounds), the DCR
cap, `build_auth` composition, persistence round-trip, and an offline end-to-end
register→authorize→login→token→verify flow.
"""

from __future__ import annotations

import asyncio
import logging

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
    THROTTLE_MAX_FAILURES,
    TRUST_PROXY_ENV,
    OAuthConfig,
    SelfHostedOAuthProvider,
    _client_ip,
    build_oauth_provider,
    get_oauth_config,
)

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


def test_config_ok_with_state_path(_clear_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OAUTH_PASSWORD_ENV, _PW)
    monkeypatch.setenv(OAUTH_BASE_URL_ENV, "https://host.example.com")
    monkeypatch.setenv("NOTEBOOKLM_HOME", "/data")
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "server")
    cfg = get_oauth_config()
    assert cfg is not None and cfg.state_path is not None
    # Path.parts is OS-agnostic (Windows uses backslash separators, so a string suffix
    # check on forward slashes would spuriously fail on the Windows CI matrix).
    assert cfg.state_path.parts[-3:] == ("profiles", "server", "oauth_state.json")


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


# --------------------------------------------------------------------------- #1761 startup warning
def test_build_oauth_provider_warns_when_state_not_persisted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OAuth on but state_path None → one startup WARNING naming the persistence env vars."""
    cfg = OAuthConfig(password=_PW, base_url="https://h.example.com", state_path=None)
    with caplog.at_level(logging.WARNING, logger="notebooklm.mcp._oauth"):
        provider = build_oauth_provider(cfg)
    assert isinstance(provider, SelfHostedOAuthProvider)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "NOTEBOOKLM_HOME" in warnings[0].message and "NOTEBOOKLM_PROFILE" in warnings[0].message


def test_build_oauth_provider_no_warn_when_state_persisted(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """With a state_path set, the non-persistence warning must NOT fire."""
    cfg = OAuthConfig(
        password=_PW, base_url="https://h.example.com", state_path=tmp_path / "oauth_state.json"
    )
    with caplog.at_level(logging.WARNING, logger="notebooklm.mcp._oauth"):
        provider = build_oauth_provider(cfg)
    assert isinstance(provider, SelfHostedOAuthProvider)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
