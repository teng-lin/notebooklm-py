"""SSRF / #1521 coverage for ``CurlCffiAsyncClient.get_guarded`` (hermetic).

``get_guarded`` replicates the httpx ``redirect_guard`` event-hook behavior for
the opt-in curl_cffi transport: it follows redirects manually
(``allow_redirects=False``) and re-validates every hop's scheme + host against
the injected predicate BEFORE connecting.

Two layers are pinned here:

* **Pre-request host validation** — bad initial/redirect hosts are rejected
  before any curl call (so these need no network). The key vector is the one
  Codex flagged: curl_cffi's ``requote_uri`` un-escapes ``%2e``→``.``, so the
  guard must validate the RAW host (which still contains ``%``) and never the
  decoded form.
* **Redirect-loop mechanics** — the underlying ``self._curl.get`` is stubbed so
  we can drive 302 chains deterministically AND assert the SSRF-critical request
  flags (``allow_redirects=False`` + ``quote=False``) on every hop.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

if importlib.util.find_spec("curl_cffi") is None:
    if os.environ.get("CI"):
        pytest.fail("CI must install the impersonate extra for curl credential-policy coverage")
    pytest.skip("requires the optional [impersonate] extra", allow_module_level=True)

from notebooklm._artifact.downloads import (  # noqa: E402
    _is_trusted_download_host,
    _make_download_client,
)
from notebooklm._curl_cffi_transport import CurlCffiAsyncClient  # noqa: E402
from notebooklm._hop_credentials import HopCredentials  # noqa: E402


def _trust_local(host: str | None) -> bool:
    """Trust the real Google allowlist plus a stand-in 'trusted' first hop."""
    return host == "storage.googleapis.com" or _is_trusted_download_host(host)


class _FakeResp:
    """Minimal stand-in for a curl_cffi Response in the redirect loop."""

    def __init__(
        self, status: int, *, location: str | None = None, content: bytes = b"", url: str = ""
    ):
        self.status_code = status
        self.headers = httpx.Headers({"location": location} if location else {})
        self.content = content
        self.url = url or "https://storage.googleapis.com/x"


def _stub_curl_get(client: CurlCffiAsyncClient, responses, calls):
    it = iter(responses)

    async def _fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return next(it)

    client._curl.get = _fake_get  # object-attr stub (not a notebooklm._ string target)


# --- Pre-request host validation: rejected before any curl call ---


@pytest.mark.parametrize(
    "url",
    [
        "https://evil%2egoogleapis.com/x",  # %2e -> '.' if decoded => trusted-looking (#1521)
        "https://evil%2Egoogleapis.com/x",  # uppercase hex variant
        "https://storage.googleapis.com%2eevil.example/x",  # suffix-smuggling
        "https://evil.example/x",  # plainly untrusted
        "http://storage.googleapis.com/x",  # non-HTTPS trusted host
        "https://storage.googleapis.com@evil.com/x",  # userinfo — real host is evil.com
    ],
)
async def test_get_guarded_rejects_bad_initial_host(url):
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    calls: list = []
    _stub_curl_get(client, [], calls)  # would StopIteration if a request slipped through
    try:
        with pytest.raises(httpx.RequestError):
            await client.get_guarded(url, is_trusted_host=_is_trusted_download_host)
        assert calls == []  # rejected BEFORE any network request
    finally:
        await client.aclose()


# --- Redirect-loop mechanics (stubbed transport) ---


async def test_get_guarded_follows_trusted_redirect_with_safe_flags():
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    calls: list = []
    _stub_curl_get(
        client,
        [
            _FakeResp(302, location="https://storage.googleapis.com/final"),
            _FakeResp(200, content=b"FINAL-BYTES", url="https://storage.googleapis.com/final"),
        ],
        calls,
    )
    try:
        resp = await client.get_guarded(
            "https://storage.googleapis.com/start", is_trusted_host=_trust_local
        )
        assert resp.status_code == 200
        assert resp.content == b"FINAL-BYTES"
        # Followed the relative-resolved Location to the final hop.
        assert [u for u, _ in calls] == [
            "https://storage.googleapis.com/start",
            "https://storage.googleapis.com/final",
        ]
        # SSRF-critical: every hop disabled auto-follow AND skipped requoting.
        assert all(
            kw.get("allow_redirects") is False and kw.get("quote") is False for _, kw in calls
        )
    finally:
        await client.aclose()


async def test_get_guarded_applies_cookie_policy_on_every_redirect_hop():
    """curl_cffi attaches the policy-selected jar per hop, never at session scope."""
    client = CurlCffiAsyncClient(cookies=None)
    calls: list = []
    _stub_curl_get(
        client,
        [
            _FakeResp(302, location="https://storage.googleapis.com/final"),
            _FakeResp(200, content=b"ok", url="https://storage.googleapis.com/final"),
        ],
        calls,
    )
    jar = httpx.Cookies()
    jar.set("SID", "secret", domain=".googleapis.com")
    policy_calls: list[str] = []

    async def credential_for(url: str) -> HopCredentials:
        policy_calls.append(url)
        return HopCredentials(cookies=jar)

    try:
        await client.get_guarded(
            "https://storage.googleapis.com/start",
            is_trusted_host=_trust_local,
            credential_for=credential_for,
        )
    finally:
        await client.aclose()

    expected = [
        "https://storage.googleapis.com/start",
        "https://storage.googleapis.com/final",
    ]
    assert policy_calls == expected
    assert [url for url, _kwargs in calls] == expected
    assert all(kwargs["cookies"] is jar.jar for _url, kwargs in calls)
    assert all(kwargs["discard_cookies"] is True for _url, kwargs in calls)


async def test_get_guarded_policy_replaces_constructor_credentials_then_drops_them():
    """The selected jar/header wins on hop 1 and None is credential-free on hop 2."""
    constructor_jar = httpx.Cookies()
    constructor_jar.set("SID", "constructor", domain=".googleapis.com")
    selected_jar = httpx.Cookies()
    selected_jar.set("SID", "selected", domain=".googleapis.com")
    client = CurlCffiAsyncClient(
        cookies=constructor_jar,
        headers={"Authorization": "Bearer constructor"},
    )
    calls: list = []
    _stub_curl_get(
        client,
        [
            _FakeResp(302, location="https://storage.googleapis.com/final"),
            _FakeResp(200, content=b"ok", url="https://storage.googleapis.com/final"),
        ],
        calls,
    )

    async def credential_for(url: str) -> HopCredentials | None:
        if url.endswith("/start"):
            return HopCredentials(
                cookies=selected_jar,
                headers={"Authorization": "Bearer selected"},
            )
        return None

    try:
        await client.get_guarded(
            "https://storage.googleapis.com/start",
            is_trusted_host=_trust_local,
            credential_for=credential_for,
        )
    finally:
        await client.aclose()

    first_kwargs = calls[0][1]
    second_kwargs = calls[1][1]
    assert first_kwargs["cookies"] is selected_jar.jar
    assert first_kwargs["headers"]["authorization"] == "Bearer selected"
    assert first_kwargs["discard_cookies"] is True
    assert second_kwargs["cookies"] == {}
    assert "authorization" not in second_kwargs.get("headers", {})
    assert second_kwargs["discard_cookies"] is True


async def test_curl_discard_cookies_prevents_per_request_cookie_session_promotion():
    """Exercise real curl_cffi parsing: hop credentials never enter session state."""
    seen_cookies: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            seen_cookies.append(self.headers.get("Cookie"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = CurlCffiAsyncClient(cookies=None)
    jar = httpx.Cookies()
    jar.set("SID", "selected", domain="127.0.0.1")
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        await client._curl.get(url, cookies=jar.jar, discard_cookies=True)
        await client._curl.get(url, discard_cookies=True)
    finally:
        await client.aclose()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert seen_cookies == ["SID=selected", None]


async def test_get_guarded_selected_jar_receives_redirect_set_cookie():
    """curl preserves response rotation in the selected jar without session promotion."""
    client = CurlCffiAsyncClient(cookies=None)
    selected_jar = httpx.Cookies()
    selected_jar.set("SID", "selected", domain=".googleapis.com")
    responses = iter(
        [
            _FakeResp(302, location="https://storage.googleapis.com/final"),
            _FakeResp(200, content=b"ok", url="https://storage.googleapis.com/final"),
        ]
    )
    seen_cookies: list[str | None] = []

    async def fake_get(url: str, **kwargs):
        request = httpx.Request("GET", url)
        httpx.Cookies(kwargs["cookies"]).set_cookie_header(request)
        seen_cookies.append(request.headers.get("cookie"))
        response = next(responses)
        if url.endswith("/start"):
            response.headers = httpx.Headers(
                [
                    ("location", "https://storage.googleapis.com/final"),
                    ("set-cookie", "ROTATED=next; Path=/; Secure"),
                    ("set-cookie", "SECOND=two; Path=/; Secure"),
                ]
            )
        return response

    client._curl.get = fake_get

    async def credential_for(_url: str) -> HopCredentials:
        return HopCredentials(cookies=selected_jar)

    try:
        await client.get_guarded(
            "https://storage.googleapis.com/start",
            is_trusted_host=_trust_local,
            credential_for=credential_for,
        )
    finally:
        await client.aclose()

    assert seen_cookies[0] == "SID=selected"
    assert set(seen_cookies[1].split("; ")) == {
        "SID=selected",
        "ROTATED=next",
        "SECOND=two",
    }


async def test_get_guarded_structured_jar_domain_matches_each_redirect_host():
    """The curl request spy observes only the jar cookie matching each hop."""
    client = CurlCffiAsyncClient(cookies=None)
    selected_jar = httpx.Cookies()
    selected_jar.set("API", "api-cookie", domain=".googleapis.com")
    selected_jar.set("MEDIA", "media-cookie", domain=".googleusercontent.com")
    responses = iter(
        [
            _FakeResp(302, location="https://lh3.googleusercontent.com/final"),
            _FakeResp(200, content=b"ok", url="https://lh3.googleusercontent.com/final"),
        ]
    )
    seen: list[tuple[str, str | None]] = []

    async def fake_get(url: str, **kwargs):
        # Production hands curl_cffi the structured CookieJar on every call;
        # materialize its standard domain/path decision in the request spy.
        request = httpx.Request("GET", url)
        httpx.Cookies(kwargs["cookies"]).set_cookie_header(request)
        seen.append((request.url.host, request.headers.get("cookie")))
        return next(responses)

    client._curl.get = fake_get

    async def credential_for(_url: str) -> HopCredentials:
        return HopCredentials(cookies=selected_jar)

    try:
        await client.get_guarded(
            "https://storage.googleapis.com/start",
            is_trusted_host=_trust_local,
            credential_for=credential_for,
        )
    finally:
        await client.aclose()

    assert seen == [
        ("storage.googleapis.com", "API=api-cookie"),
        ("lh3.googleusercontent.com", "MEDIA=media-cookie"),
    ]


async def test_get_guarded_blocks_untrusted_redirect_target():
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    calls: list = []
    _stub_curl_get(client, [_FakeResp(302, location="https://evil.example/x")], calls)
    try:
        with pytest.raises(httpx.RequestError):
            await client.get_guarded(
                "https://storage.googleapis.com/start", is_trusted_host=_trust_local
            )
        assert len(calls) == 1  # first hop fetched, redirect target rejected pre-fetch
    finally:
        await client.aclose()


async def test_get_guarded_blocks_percent_encoded_redirect_target():
    """The #1521 trap on a redirect hop: %2e must NOT decode into a trusted host."""
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    calls: list = []
    _stub_curl_get(client, [_FakeResp(302, location="https://evil%2egoogleapis.com/x")], calls)
    try:
        with pytest.raises(httpx.RequestError):
            await client.get_guarded(
                "https://storage.googleapis.com/start", is_trusted_host=_trust_local
            )
    finally:
        await client.aclose()


# --- Factory-selection wiring: downloads actually route through get_guarded ---


async def test_make_download_client_routes_through_get_guarded_under_curl_env(monkeypatch):
    """Under NOTEBOOKLM_TRANSPORT=curl_cffi the download client is curl_cffi AND its
    getter drives get_guarded with the real #1521 trusted-host predicate."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    client, do_get = _make_download_client(httpx.Cookies(), timeout=30.0)
    assert isinstance(client, CurlCffiAsyncClient)
    captured: dict = {}

    async def fake_guarded(url, *, is_trusted_host, **kwargs):
        captured["url"] = url
        captured["pred"] = is_trusted_host
        return httpx.Response(200, content=b"ok", request=httpx.Request("GET", url))

    client.get_guarded = fake_guarded
    try:
        resp = await do_get("https://storage.googleapis.com/x")
    finally:
        await client.aclose()
    assert resp.content == b"ok"
    assert captured["url"] == "https://storage.googleapis.com/x"
    # The SSRF allowlist predicate must be the one actually wired in.
    assert captured["pred"] is _is_trusted_download_host


async def test_make_download_client_uses_httpx_by_default(monkeypatch):
    monkeypatch.delenv("NOTEBOOKLM_TRANSPORT", raising=False)
    client, _ = _make_download_client(httpx.Cookies(), timeout=30.0)
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()


async def test_get_guarded_fails_closed_on_redirect_without_location():
    """A 3xx with no Location must error (fail closed), not return the 3xx body."""
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    calls: list = []
    _stub_curl_get(client, [_FakeResp(302)], calls)  # 302, no location header
    try:
        with pytest.raises(httpx.RequestError, match="Location"):
            await client.get_guarded(
                "https://storage.googleapis.com/start", is_trusted_host=_trust_local
            )
    finally:
        await client.aclose()


async def test_get_guarded_caps_redirects():
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    calls: list = []
    # Endless self-redirect; cap must trip.
    _stub_curl_get(
        client,
        [_FakeResp(302, location="https://storage.googleapis.com/loop") for _ in range(20)],
        calls,
    )
    try:
        with pytest.raises(httpx.RequestError, match="redirect"):
            await client.get_guarded(
                "https://storage.googleapis.com/loop",
                is_trusted_host=_trust_local,
                max_redirects=3,
            )
    finally:
        await client.aclose()
