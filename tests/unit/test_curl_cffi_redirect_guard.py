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

    def credential_for(url: str) -> httpx.Cookies:
        policy_calls.append(url)
        return jar

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
