"""Cassette-conformance: the curl_cffi transport must handle REAL recorded server
responses the same way httpx does — without a live API.

Motivation: the bugs we found running e2e under ``NOTEBOOKLM_TRANSPORT=curl_cffi``
(e.g. gzip'd RPC bodies decoding) slipped past the toy-echo hermetic tests. This
tier closes that gap offline by replaying REAL recorded responses (from
``tests/cassettes/``) through both transports and asserting equivalence:

* **Decode equivalence (class: response/gzip/shape).** vcrpy stores *decoded*
  bodies, so we re-gzip a real recorded body on the way out (with
  ``Content-Encoding: gzip``) — exercising curl_cffi's libcurl auto-decompress on
  real RPC-shaped data — and assert curl_cffi's decoded ``httpx.Response`` is
  byte-identical to httpx's.
* **Request-carry equivalence (class: request divergence).** A request with
  cookies + headers + body goes through both transports to a recording server;
  the auth-relevant parts received must match (ignoring the intentionally
  different browser-impersonation headers).

curl_cffi bypasses VCR (libcurl, not httpx) so this is local-server-backed
(``allow_no_vcr``) rather than vcrpy-intercepted.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from notebooklm._curl_cffi_transport import CurlCffiAsyncClient
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

_CASSETTE_DIR = Path(__file__).resolve().parent.parent / "cassettes" / "web"


def _real_recorded_bodies(limit: int = 4) -> list[tuple[str, bytes]]:
    """Real recorded server-response bodies sourced from ``tests/cassettes/``.

    Sourced dynamically (not pinned to one cassette) so the test stays valid as
    cassettes are re-recorded. Picks substantial bodies to exercise the size path.
    """
    out: list[tuple[str, bytes]] = []
    for path in sorted(_CASSETTE_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for inter in (data or {}).get("interactions", []):
            body = inter.get("response", {}).get("body", {})
            raw = body.get("string") if isinstance(body, dict) else None
            if isinstance(raw, str) and len(raw) > 200:
                out.append((path.name, raw.encode("utf-8", "surrogatepass")))
                break
        if len(out) >= limit:
            break
    return out


_BODIES = _real_recorded_bodies()


def test_conformance_guard_is_not_inert():
    """Fail LOUD (not silent-skip) if no cassette bodies were sourced.

    The gzip tests below ``skipif(not _BODIES)`` so they don't error in a
    cassette-less checkout — but this file's whole point is regression-guarding
    the gzip-decode bug, so an empty ``_BODIES`` (cassettes moved/renamed) must
    surface as a failure here, not a green skip.
    """
    assert _BODIES, "no bodies sourced from tests/cassettes/ — gzip conformance guard is inert"


class _GzipHandler(BaseHTTPRequestHandler):
    """Serves a fixed body gzip-compressed (mirrors how Google sends RPC bodies)."""

    payload: bytes = b""

    def log_message(self, *_a):
        pass

    def _serve_gzip(self):
        gz = gzip.compress(type(self).payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(gz)))
        self.end_headers()
        self.wfile.write(gz)

    def do_GET(self):  # noqa: N802
        self._serve_gzip()

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._serve_gzip()


class _EchoRequestHandler(BaseHTTPRequestHandler):
    """Echoes back the auth-relevant parts of the request it received."""

    def log_message(self, *_a):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        out = json.dumps(
            {
                "cookie": self.headers.get("Cookie", ""),
                "content_type": self.headers.get("Content-Type", ""),
                "x_goog": self.headers.get("x-goog-upload-command", ""),
                "body": body.decode("utf-8", "replace"),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@contextmanager
def _local_server(handler):
    """Run ``handler`` on a background HTTP server, yield its base URL, then tear down.

    Full teardown (stop loop, join thread, close socket) avoids handle/thread leaks
    across the per-test servers (flaky on Windows otherwise).
    """
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


@pytest.mark.skipif(not _BODIES, reason="no cassettes available to source bodies from")
@pytest.mark.parametrize("name,body", _BODIES, ids=[n for n, _ in _BODIES])
async def test_gzip_recorded_body_decodes_identically(name, body):
    """A real recorded body, served gzip'd, decodes identically via curl_cffi and httpx."""
    _GzipHandler.payload = body
    with _local_server(_GzipHandler) as url:
        curl = CurlCffiAsyncClient(cookies=httpx.Cookies())
        httpx_client = httpx.AsyncClient()
        try:
            curl_resp = await stream_post_with_size_cap(curl, f"{url}/rpc", body=b"x", headers=None)
            httpx_resp = await stream_post_with_size_cap(
                httpx_client, f"{url}/rpc", body=b"x", headers=None
            )
            # Both must auto-decompress to the ORIGINAL body, and agree with each other.
            assert curl_resp.content == body, f"curl_cffi mis-decoded {name}"
            assert curl_resp.content == httpx_resp.content, f"curl_cffi != httpx for {name}"
            assert curl_resp.status_code == httpx_resp.status_code == 200
        finally:
            await curl.aclose()
            await httpx_client.aclose()


@pytest.mark.skipif(not _BODIES, reason="no cassettes available to source bodies from")
@pytest.mark.parametrize("name,body", _BODIES[:1], ids=[n for n, _ in _BODIES[:1]])
async def test_get_download_decodes_identically(name, body):
    """Download (GET) path: a real gzip'd body fetched via curl_cffi.get() decodes
    identically to httpx. (Artifact downloads route through the active transport
    via get_guarded — see test_curl_cffi_redirect_guard.py for the #1521 SSRF
    coverage; this covers the plain GET decode primitive.)"""
    _GzipHandler.payload = body
    with _local_server(_GzipHandler) as url:
        curl = CurlCffiAsyncClient(cookies=httpx.Cookies())
        httpx_client = httpx.AsyncClient()
        try:
            curl_resp = await curl.get(f"{url}/download")
            httpx_resp = await httpx_client.get(f"{url}/download")
            assert curl_resp.content == body, f"curl_cffi GET mis-decoded {name}"
            assert curl_resp.content == httpx_resp.content
        finally:
            await curl.aclose()
            await httpx_client.aclose()


async def test_request_carry_matches_httpx():
    """curl_cffi forwards the same auth-relevant request (cookies/content-type/body) as httpx."""
    jar = httpx.Cookies()
    jar.set("SID", "secret", domain="127.0.0.1")  # match the local server host so it's sent
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "x-goog-upload-command": "upload",
    }
    with _local_server(_EchoRequestHandler) as url:
        curl = CurlCffiAsyncClient(cookies=httpx.Cookies(jar))
        httpx_client = httpx.AsyncClient(cookies=httpx.Cookies(jar))
        try:
            curl_seen = json.loads(
                (await curl.post(f"{url}/up", headers=headers, content=b"f.req=1")).text
            )
            httpx_seen = json.loads(
                (await httpx_client.post(f"{url}/up", headers=headers, content=b"f.req=1")).text
            )
            # The transport must carry the body + auth-relevant headers identically.
            assert curl_seen["body"] == httpx_seen["body"] == "f.req=1"
            assert curl_seen["content_type"] == httpx_seen["content_type"]
            assert curl_seen["x_goog"] == httpx_seen["x_goog"] == "upload"
            # Cookie may be ordered/formatted differently; assert the value is present in both.
            assert "SID=secret" in curl_seen["cookie"]
            assert "SID=secret" in httpx_seen["cookie"]
        finally:
            await curl.aclose()
            await httpx_client.aclose()
