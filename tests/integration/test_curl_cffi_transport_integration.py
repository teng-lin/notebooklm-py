"""Integration tests for the opt-in curl_cffi transport.

Unlike the unit suite (``tests/unit/test_curl_cffi_transport_poc.py``), which
constructs ``CurlCffiAsyncClient`` directly, these drive the transport the way
production does: set ``NOTEBOOKLM_TRANSPORT=curl_cffi`` and go through the runtime
factory resolvers (``_resolve_async_client_factory`` / ``resolve_transport_factory``)
so the env-gated *selection* + the streaming helpers + the adapter are exercised
together against a real (local) HTTP server. curl_cffi bypasses VCR (it isn't
httpx), so this tier is local-server-backed, not cassette-backed — hence
``allow_no_vcr``.
"""

from __future__ import annotations

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from notebooklm._curl_cffi_transport import CurlCffiAsyncClient
from notebooklm._web.transport.init import _resolve_async_client_factory
from notebooklm._web.transport.streaming_post import stream_post_with_size_cap

# Keep this module COLLECTED even when the optional extra is absent (the imports
# above don't need curl_cffi at import time) so its ``allow_no_vcr`` marker stays
# visible to the test-taxonomy inventory. A module-level ``importorskip`` would
# drop the file from collection, making its allowlist entry look stale. The
# ``skipif`` instead skips at runtime when curl_cffi isn't installed.
pytestmark = [
    pytest.mark.allow_no_vcr,
    pytest.mark.skipif(
        importlib.util.find_spec("curl_cffi") is None,
        reason="requires the optional [impersonate] extra",
    ),
]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # silence
        pass

    def do_GET(self):  # noqa: N802
        body = f"token=OK cookie={self.headers.get('Cookie', '')}".encode()
        self.send_response(200)
        self.send_header("Set-Cookie", "ROTATED=v; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = b"echo:" + data
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


async def test_env_var_selects_curl_cffi_and_round_trips(server, monkeypatch):
    """NOTEBOOKLM_TRANSPORT=curl_cffi -> the runtime resolver builds a curl client
    that round-trips a real GET/POST and rotates cookies back into the jar."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    factory = _resolve_async_client_factory(None)  # reads the ambient env var
    client = factory(cookies=httpx.Cookies())
    assert isinstance(client, CurlCffiAsyncClient)
    try:
        get_resp = await client.get(f"{server}/")
        assert isinstance(get_resp, httpx.Response)
        assert "token=OK" in get_resp.text
        assert client.cookies.get("ROTATED") == "v"

        post_resp = await client.post(f"{server}/rpc", headers={}, content=b"payload")
        assert post_resp.content == b"echo:payload"
    finally:
        await client.aclose()


async def test_streaming_post_over_resolved_curl_transport(server, monkeypatch):
    """The size-capped streaming POST helper works over the env-selected curl client."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    client = _resolve_async_client_factory(None)(cookies=httpx.Cookies())
    try:
        resp = await stream_post_with_size_cap(
            client, f"{server}/rpc", body=b"rpc-body", headers={"Content-Type": "text/plain"}
        )
        assert isinstance(resp, httpx.Response)
        assert resp.content == b"echo:rpc-body"
    finally:
        await client.aclose()


async def test_stream_upload_over_resolved_curl_transport(server, monkeypatch, tmp_path):
    """A file streams through the env-selected curl client's low-level upload path."""
    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    payload = b"integration-upload-" + b"y" * 4000
    p = tmp_path / "src.bin"
    p.write_bytes(payload)
    client = _resolve_async_client_factory(None)(cookies=httpx.Cookies())
    try:
        resp = await client.stream_upload(
            f"{server}/upload", p, total_bytes=len(payload), headers={"X-Up": "1"}
        )
        assert resp.status_code == 200
        assert resp.content == b"echo:" + payload
    finally:
        await client.aclose()


async def test_env_var_unset_resolves_to_httpx(monkeypatch):
    """Without the opt-in, the resolver returns plain httpx.AsyncClient (default path)."""
    monkeypatch.delenv("NOTEBOOKLM_TRANSPORT", raising=False)
    assert _resolve_async_client_factory(None) is httpx.AsyncClient
