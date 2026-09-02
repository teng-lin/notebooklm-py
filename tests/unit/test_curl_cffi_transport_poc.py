"""PoC proof for the curl_cffi httpx-compat adapter.

Drives ``CurlCffiAsyncClient`` against a local stdlib HTTP server to prove the
contract the transport kernel relies on, end-to-end, without Google auth:

* ``.get()`` returns a real ``httpx.Response`` (``.text``/``.url``/``.raise_for_status``);
* server ``Set-Cookie`` round-trips back into the authoritative ``httpx.Cookies`` jar
  AND is re-sent on the next request (the PSIDTS-rotation-critical path);
* ``stream_post_with_size_cap`` works verbatim over the adapter's ``.stream()``;
* a 5xx maps through ``raise_mapped_post_error`` to ``TransportServerError``;
* the ``NOTEBOOKLM_TRANSPORT=curl_cffi`` env seam selects the adapter.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

pytest.importorskip("curl_cffi", reason="requires the optional [impersonate] extra")

from notebooklm._curl_cffi_transport import CurlCffiAsyncClient  # noqa: E402
from notebooklm._web.transport.errors import (  # noqa: E402
    TransportServerError,
    raise_mapped_post_error,
)
from notebooklm._web.transport.streaming_post import stream_post_with_size_cap  # noqa: E402

# No module-level asyncio mark: the project runs ``asyncio_mode = "auto"`` so async
# tests are collected automatically, and a blanket mark would wrongly tag the sync
# pure-logic tests below.


class _Handler(BaseHTTPRequestHandler):
    stall_started = threading.Event()
    stall_release = threading.Event()

    def log_message(self, *_a):  # silence test server
        pass

    def _seen_cookie(self) -> str:
        return self.headers.get("Cookie", "")

    def do_GET(self):  # noqa: N802
        if self.path == "/stall":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"a")
            self.wfile.flush()
            self.stall_started.set()
            self.stall_release.wait(timeout=5)
            try:
                self.wfile.write(b"b")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path == "/boom":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"kaboom")
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        body = f"token=ABC123 cookie_seen={self._seen_cookie()}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", "ROTATED=newval; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _echo_safe(value: str) -> str:
        """Strip CR/LF before reflecting a request value into a response.

        Echoing a client-controlled value into a response header is HTTP
        response splitting (flagged by CodeQL) even in a test server. The
        assertions only need the value's content, so drop the control
        characters and bound the length.
        """
        return "".join(ch for ch in value if ch not in "\r\n")[:200]

    def do_DELETE(self):  # noqa: N802
        if self.path == "/gone":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/deleted")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        auth = self._echo_safe(self.headers.get("Authorization", ""))
        body = f"deleted {self.path} auth={auth}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Set-Cookie", "DELCOOKIE=set; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        if self.path == "/boom":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"put:" + data[:8]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("X-Echo-Len", str(len(data)))
        # Echo what the client actually sent, so a test can prove the header
        # and cookie plumbing is real rather than merely non-fatal.
        self.send_header(
            "X-Echo-Content-Type", self._echo_safe(self.headers.get("Content-Type", ""))
        )
        self.send_header("X-Echo-Cookie", self._echo_safe(self.headers.get("Cookie", "")))
        self.send_header(
            "X-Echo-Upload-Command",
            self._echo_safe(self.headers.get("X-Goog-Upload-Command", "")),
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        if self.path == "/slow":
            import time

            time.sleep(0.4)  # let the client cancel mid-flight
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/boom":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"unavailable")
            return
        body = b"echo:" + data
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def refused_url():
    """A loopback address guaranteed to refuse connections.

    Port 1 is only *usually* free; a local service can bind it and turn a
    transport-failure test into a false pass. A socket that is bound but never
    listening refuses deterministically, and holding it open for the test keeps
    the port reserved.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    try:
        yield f"127.0.0.1:{sock.getsockname()[1]}"
    finally:
        sock.close()


@pytest.fixture
def server():
    _Handler.stall_started.clear()
    _Handler.stall_release.clear()
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        _Handler.stall_release.set()
        # Fully tear down: stop the loop, join the thread, close the socket —
        # otherwise handles/threads leak across the per-test servers (flaky on Windows).
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


async def test_get_returns_httpx_response_and_round_trips_cookies(server):
    client = CurlCffiAsyncClient(headers={"X-Test": "1"}, cookies=httpx.Cookies())
    try:
        r1 = await client.get(f"{server}/")
        assert isinstance(r1, httpx.Response)
        assert r1.status_code == 200
        assert "token=ABC123" in r1.text
        assert str(r1.url).endswith("/")
        # Server's Set-Cookie landed in the authoritative httpx jar.
        assert client.cookies.get("ROTATED") == "newval"
        # ...and is re-sent on the next request (PSIDTS-rotation path).
        r2 = await client.get(f"{server}/")
        assert "ROTATED=newval" in r2.text
    finally:
        await client.aclose()


async def test_stream_post_with_size_cap_works_over_adapter(server):
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    try:
        resp = await stream_post_with_size_cap(
            client, f"{server}/rpc", body=b"payload", headers={"Content-Type": "text/plain"}
        )
        assert isinstance(resp, httpx.Response)
        assert resp.status_code == 200
        assert resp.content == b"echo:payload"
    finally:
        await client.aclose()


async def test_stream_abort_detaches_stalled_curl_body_before_context_exit(server):
    """A peer that stops after headers cannot strand curl_cffi's ``__aexit__``."""

    client = CurlCffiAsyncClient(cookies=httpx.Cookies(), timeout=httpx.Timeout(None))
    response_cm = client.stream("GET", f"{server}/stall", follow_redirects=False)
    entered = False
    try:
        response = await asyncio.wait_for(response_cm.__aenter__(), timeout=1.0)
        entered = True
        iterator = response.aiter_bytes()
        assert await asyncio.wait_for(anext(iterator), timeout=1.0) == b"a"
        assert await asyncio.to_thread(_Handler.stall_started.wait, 1.0)

        blocked_body = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)
        assert not blocked_body.done()

        timeout = TimeoutError("aggregate deadline")
        await asyncio.wait_for(response.aclose(), timeout=1.0)
        await asyncio.wait_for(
            response_cm.__aexit__(type(timeout), timeout, timeout.__traceback__),
            timeout=1.0,
        )
        entered = False
        outcome = (await asyncio.gather(blocked_body, return_exceptions=True))[0]
        assert isinstance(outcome, (asyncio.CancelledError, StopAsyncIteration))
        assert response._r.quit_now.is_set()
        assert response._r.astream_task.done()
    finally:
        if entered:
            response.abort()
            await response_cm.__aexit__(None, None, None)
        await client.aclose()


async def test_server_error_maps_to_transport_server_error(server):
    import logging

    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    try:
        with pytest.raises(TransportServerError):
            try:
                await stream_post_with_size_cap(client, f"{server}/boom", body=b"x", headers=None)
            except httpx.HTTPStatusError as exc:
                raise_mapped_post_error(
                    log_label="poc", exc=exc, start=0.0, logger=logging.getLogger("poc")
                )
    finally:
        await client.aclose()


def test_to_curl_timeout_preserves_connect_and_read():
    """httpx.Timeout's connect+read map to curl_cffi's (connect, read) tuple."""
    from notebooklm._curl_cffi_transport import _to_curl_timeout

    assert _to_curl_timeout(None) is None
    assert _to_curl_timeout(30.0) == 30.0
    assert _to_curl_timeout(httpx.Timeout(connect=10.0, read=60.0, write=5.0, pool=5.0)) == (
        10.0,
        60.0,
    )
    # read-only Timeout collapses to the single read float.
    assert _to_curl_timeout(httpx.Timeout(None, read=45.0)) == 45.0


async def test_get_follows_real_redirect_with_per_request_kwargs_and_raw_jar(server):
    """Secondary auth clients pass a raw CookieJar + per-request follow_redirects/timeout.

    Hits a real 302 so a broken ``_redirects()`` translation (httpx
    ``follow_redirects`` -> curl ``allow_redirects``) actually fails the test.
    """
    from http.cookiejar import CookieJar

    client = CurlCffiAsyncClient(cookies=CookieJar())  # raw jar, not httpx.Cookies
    try:
        r = await client.get(
            f"{server}/redirect", follow_redirects=True, timeout=httpx.Timeout(5.0, read=10.0)
        )
        assert r.status_code == 200  # followed 302 -> / (200), not the raw redirect
        assert "token=ABC123" in r.text  # body of the final page
        assert str(r.url).endswith("/")  # final URL after the hop
        assert isinstance(client.cookies, httpx.Cookies)
        assert client.cookies.get("ROTATED") == "newval"
    finally:
        await client.aclose()


async def test_post_returns_httpx_response_and_echoes_body(server):
    """`.post()` buffers the body, returns an httpx.Response, preserves headers."""
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    try:
        r = await client.post(f"{server}/rpc", headers={"X-T": "1"}, content=b"hello")
        assert isinstance(r, httpx.Response)
        assert r.status_code == 200
        assert r.content == b"echo:hello"
    finally:
        await client.aclose()


async def test_transport_error_maps_to_httpx_request_error():
    """A connection failure surfaces as httpx.RequestError (what the mapper expects)."""
    import socket

    # Reserve an ephemeral port then release it, so it's reliably closed (port 1 is
    # only usually-closed and would make this flaky across the OS matrix).
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{s.getsockname()[1]}"
    s.close()
    client = CurlCffiAsyncClient(cookies=httpx.Cookies(), timeout=2.0)
    try:
        with pytest.raises(httpx.RequestError):
            await client.get(f"{base}/")
        with pytest.raises(httpx.RequestError):
            await client.post(f"{base}/", content=b"x")
    finally:
        await client.aclose()


async def test_materialize_body_types():
    """_materialize handles bytes/str/None/async-iter/sync-iter/BytesIO and rejects the rest."""
    import io

    from notebooklm._curl_cffi_transport import _materialize

    async def agen():
        yield b"ab"
        yield b"cd"

    assert await _materialize(b"x") == b"x"
    assert await _materialize(None) is None
    assert await _materialize("hi") == b"hi"
    assert await _materialize(agen()) == b"abcd"
    assert await _materialize([b"a", b"b"]) == b"ab"
    assert await _materialize(io.BytesIO(b"zz")) == b"zz"
    with pytest.raises(TypeError):
        await _materialize(12345)


async def test_resolve_transport_factory_curl_and_unknown(monkeypatch):
    """resolve_transport_factory: curl_cffi when opted in, httpx default, raise on typo."""
    from notebooklm._curl_cffi_transport import resolve_transport_factory

    monkeypatch.delenv("NOTEBOOKLM_TRANSPORT", raising=False)
    assert resolve_transport_factory() is httpx.AsyncClient

    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    factory = resolve_transport_factory()
    inst = factory(cookies=httpx.Cookies())
    try:
        assert isinstance(inst, CurlCffiAsyncClient)
    finally:
        await inst.aclose()

    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curlcffi")  # typo
    with pytest.raises(ValueError, match="Unknown NOTEBOOKLM_TRANSPORT"):
        resolve_transport_factory()


async def test_timeout_for_honors_explicit_falsy_and_defaults_when_absent():
    """An explicit per-request timeout=0/None is preserved; only an absent one defaults."""
    client = CurlCffiAsyncClient(timeout=30.0)
    try:
        assert client._timeout_for({}) == 30.0  # absent -> session default
        assert client._timeout_for({"timeout": 0}) == 0  # explicit immediate, not default
        assert client._timeout_for({"timeout": None}) is None  # explicit no-timeout
    finally:
        await client.aclose()


async def test_caller_cookies_jar_is_not_mutated():
    """Adapter copies cookies (like httpx.AsyncClient) so the caller's jar is untouched."""
    caller = httpx.Cookies()
    caller.set("SID", "x", domain="example.com")
    client = CurlCffiAsyncClient(cookies=caller)
    try:
        assert client.cookies.jar is not caller.jar  # copied, not aliased
        assert client.cookies.get("SID") == "x"  # contents preserved
    finally:
        await client.aclose()


async def test_stream_upload_streams_from_disk(server, tmp_path):
    """stream_upload() streams a file body via low-level libcurl (Path + open-file)."""
    payload = b"streamed-body-" + b"x" * 5000
    p = tmp_path / "blob.bin"
    p.write_bytes(payload)

    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    try:
        # Path source — opened/closed internally.
        r1 = await client.stream_upload(
            f"{server}/rpc", p, total_bytes=len(payload), headers={"X-Up": "1"}
        )
        assert isinstance(r1, httpx.Response)
        assert r1.status_code == 200
        assert r1.content == b"echo:" + payload

        # Open binary file source — read, not closed by stream_upload.
        with p.open("rb") as fh:
            r2 = await client.stream_upload(
                f"{server}/rpc", fh, total_bytes=len(payload), headers={"X-Up": "1"}
            )
            assert fh.closed is False  # caller owns it
        assert r2.content == b"echo:" + payload
    finally:
        await client.aclose()


async def test_stream_upload_error_status_returns_raisable_response(server, tmp_path):
    """A 5xx from the upload endpoint comes back as a Response the caller can raise on."""
    p = tmp_path / "b.bin"
    p.write_bytes(b"data")
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    try:
        r = await client.stream_upload(f"{server}/boom", p, total_bytes=4, headers={})
        assert r.status_code == 503
        with pytest.raises(httpx.HTTPStatusError):
            r.raise_for_status()
    finally:
        await client.aclose()


async def test_stream_upload_connection_error_maps_to_request_error(tmp_path):
    """A connection failure in the low-level path maps to httpx.RequestError (not CurlError)."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{s.getsockname()[1]}"
    s.close()
    p = tmp_path / "b.bin"
    p.write_bytes(b"data")
    client = CurlCffiAsyncClient(cookies=httpx.Cookies(), timeout=2.0)
    try:
        with pytest.raises(httpx.RequestError):
            await client.stream_upload(f"{base}/up", p, total_bytes=4, headers={})
    finally:
        await client.aclose()


async def test_connect_and_stall_timeouts_never_zero():
    """The stall guard is never disabled — a 0/None/sub-second timeout floors to defaults."""
    cases = [
        (0, (30, 300)),
        (None, (30, 300)),
        (5, (5, 5)),  # scalar applies to both connect + read (httpx semantics)
        (httpx.Timeout(0, read=0), (30, 300)),
        (httpx.Timeout(5.0, read=120.0), (5, 120)),
    ]
    for to, expected in cases:
        client = CurlCffiAsyncClient(cookies=httpx.Cookies(), timeout=to)
        try:
            assert client._connect_and_stall_timeouts() == expected
        finally:
            await client.aclose()


async def test_stream_upload_drains_worker_on_cancel(server, tmp_path):
    """Cancelling stream_upload propagates CancelledError but drains the worker (no orphan)."""
    import asyncio

    p = tmp_path / "b.bin"
    p.write_bytes(b"x" * 100)
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    try:
        task = asyncio.ensure_future(
            client.stream_upload(f"{server}/slow", p, total_bytes=100, headers={})
        )
        await asyncio.sleep(0.1)  # let it reach perform()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task  # propagates only after the worker drains (~0.4s server sleep)
    finally:
        await client.aclose()  # would hang/error if the worker were orphaned


async def test_cookie_header_for_filters_by_domain():
    """_cookie_header_for sends only cookies matching the upload host."""
    cookies = httpx.Cookies()
    cookies.set("SID", "g", domain=".google.com")
    cookies.set("OTHER", "x", domain=".example.com")
    client = CurlCffiAsyncClient(cookies=cookies)
    try:
        hdr = client._cookie_header_for("https://notebooklm.google.com/upload/_/")
        assert "SID=g" in hdr
        assert "OTHER" not in hdr  # different domain not sent
    finally:
        await client.aclose()


async def test_env_seam_selects_curl_cffi_factory(monkeypatch):
    from notebooklm._runtime.init import _resolve_async_client_factory

    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    factory = _resolve_async_client_factory(None)
    inst = factory(
        headers={}, cookies=httpx.Cookies(), timeout=None, follow_redirects=True, limits=None
    )
    try:
        assert isinstance(inst, CurlCffiAsyncClient)
    finally:
        await inst.aclose()


# ---------------------------------------------------------------------------
# DELETE — the Drive staging cleanup path
# ---------------------------------------------------------------------------


async def test_delete_returns_an_httpx_response_and_syncs_cookies(server):
    """Without this method the Drive cleanup in ``_android.drive_staging`` raises
    ``AttributeError`` under ``NOTEBOOKLM_TRANSPORT=curl_cffi`` and, because the
    cleanup is best-effort, silently leaves the staged file in the user's Drive."""
    client = CurlCffiAsyncClient(cookies=httpx.Cookies())
    try:
        response = await client.delete(f"{server}/staged-file")

        assert isinstance(response, httpx.Response)
        assert response.status_code == 200
        assert response.text.startswith("deleted /staged-file")
        # The response jar is synced back like every other verb.
        assert client.cookies.get("DELCOOKIE") == "set"
    finally:
        await client.aclose()


async def test_delete_forwards_request_headers(server):
    """Dropping ``headers`` would make the authenticated Drive cleanup 401 and
    silently leave the staged file behind, so the server echoes what it saw."""
    client = CurlCffiAsyncClient()
    try:
        response = await client.delete(
            f"{server}/staged-file", headers={"Authorization": "Bearer token"}
        )

        assert response.status_code == 200
        assert "auth=Bearer token" in response.text
    finally:
        await client.aclose()


async def test_delete_surfaces_a_not_found_without_raising(server):
    """Cleanup is best-effort; a 404 is a normal response, not a transport error."""
    client = CurlCffiAsyncClient()
    try:
        response = await client.delete(f"{server}/gone")

        assert response.status_code == 404
    finally:
        await client.aclose()


async def test_delete_maps_a_transport_failure_to_httpx_request_error(refused_url):
    client = CurlCffiAsyncClient()
    try:
        with pytest.raises(httpx.RequestError):
            await client.delete(f"http://{refused_url}/staged-file")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# stream_upload — the resumable upload leg
# ---------------------------------------------------------------------------


async def test_stream_upload_sends_a_file_from_disk_without_buffering(server, tmp_path):
    payload = b"x" * (512 * 1024)
    source = tmp_path / "upload.bin"
    source.write_bytes(payload)
    client = CurlCffiAsyncClient()
    try:
        response = await client.stream_upload(
            f"{server}/target",
            source,
            total_bytes=len(payload),
            headers={
                "Content-Type": "application/octet-stream",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            method="PUT",
        )

        assert response.status_code == 200
        assert response.headers["X-Echo-Len"] == str(len(payload))
        # The server echoes the body's leading bytes, so a corrupted or
        # truncated read function is caught, not just a wrong length.
        assert response.text == "put:" + payload[:8].decode()
        # Real resumable-upload callers depend on these reaching the server.
        assert response.headers["X-Echo-Content-Type"] == "application/octet-stream"
        assert response.headers["X-Echo-Upload-Command"] == "upload, finalize"
    finally:
        await client.aclose()


async def test_stream_upload_accepts_an_already_open_handle_and_leaves_it_open(server, tmp_path):
    """``source`` as a file object is caller-owned — the adapter must not close it."""
    payload = b"y" * 512
    source = tmp_path / "upload.bin"
    source.write_bytes(payload)
    client = CurlCffiAsyncClient()
    handle = source.open("rb")
    try:
        response = await client.stream_upload(
            f"{server}/target",
            handle,
            total_bytes=len(payload),
            headers={},
            method="PUT",
        )

        assert response.status_code == 200
        assert handle.closed is False
    finally:
        handle.close()
        await client.aclose()


async def test_stream_upload_reports_progress_per_chunk(server, tmp_path):
    """Several positive callbacks, not one after buffering the whole file."""
    payload = b"z" * (512 * 1024)
    source = tmp_path / "upload.bin"
    source.write_bytes(payload)
    seen: list[int] = []

    async def on_chunk(count: int) -> None:
        seen.append(count)

    client = CurlCffiAsyncClient()
    try:
        response = await client.stream_upload(
            f"{server}/target",
            source,
            total_bytes=len(payload),
            headers={},
            method="PUT",
            on_chunk=on_chunk,
        )

        assert response.status_code == 200
        assert sum(seen) == len(payload)
        assert all(count > 0 for count in seen)
        assert len(seen) > 1, f"body was delivered in one read: {seen}"
    finally:
        await client.aclose()


async def test_stream_upload_surfaces_a_server_error_as_a_response(server, tmp_path):
    source = tmp_path / "upload.bin"
    source.write_bytes(b"data")
    client = CurlCffiAsyncClient()
    try:
        response = await client.stream_upload(
            f"{server}/boom",
            source,
            total_bytes=4,
            headers={},
            method="PUT",
        )

        assert response.status_code == 500
    finally:
        await client.aclose()


async def test_stream_upload_maps_a_transport_failure_to_httpx_request_error(tmp_path, refused_url):
    source = tmp_path / "upload.bin"
    source.write_bytes(b"data")
    client = CurlCffiAsyncClient()
    try:
        with pytest.raises(httpx.RequestError):
            await client.stream_upload(
                f"http://{refused_url}/target",
                source,
                total_bytes=4,
                headers={},
                method="PUT",
            )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Client and stream lifecycle
# ---------------------------------------------------------------------------


async def test_the_client_works_as_an_async_context_manager(server):
    async with CurlCffiAsyncClient() as client:
        response = await client.get(f"{server}/")

    assert response.status_code == 200


async def test_get_guarded_maps_a_transport_failure_to_httpx_request_error(refused_url):
    client = CurlCffiAsyncClient()
    try:
        with pytest.raises(httpx.RequestError):
            # HTTPS: the scheme guard runs before the connection, and rejecting
            # ``http://`` here would never reach the transport-failure branch.
            await client.get_guarded(
                f"https://{refused_url}/asset", is_trusted_host=lambda _host: True
            )
    finally:
        await client.aclose()


async def test_opening_a_stream_against_a_dead_peer_raises_and_closes_the_handle(
    monkeypatch, refused_url
):
    """``__aexit__`` is not auto-called when ``__aenter__`` raises.

    The curl stream context is instrumented so this proves the explicit
    failure-path cleanup ran, rather than relying on the later
    ``client.aclose()`` to tidy the whole session.
    """
    client = CurlCffiAsyncClient()
    exits: list[object] = []
    real_stream = client._curl.stream

    def _instrumented_stream(method, url, **kwargs):
        cm = real_stream(method, url, **kwargs)
        real_aexit = cm.__aexit__

        async def _tracking_aexit(*exc):
            exits.append(exc[0])
            return await real_aexit(*exc)

        cm.__aexit__ = _tracking_aexit
        return cm

    monkeypatch.setattr(client._curl, "stream", _instrumented_stream)
    try:
        with pytest.raises(httpx.RequestError):
            async with client.stream("GET", f"http://{refused_url}/asset"):
                pass  # pragma: no cover - the enter must raise

        assert exits, "the curl stream handle was never closed on the failure path"
    finally:
        await client.aclose()


async def test_aborting_a_streamed_response_twice_is_idempotent(server):
    client = CurlCffiAsyncClient()
    try:
        async with client.stream("GET", f"{server}/") as response:
            response.abort()
            response.abort()
    finally:
        await client.aclose()


async def test_closing_a_streamed_response_after_abort_settles_cleanly(server):
    client = CurlCffiAsyncClient()
    try:
        async with client.stream("GET", f"{server}/") as response:
            response.abort()
            await response.aclose()
    finally:
        await client.aclose()


async def test_stream_upload_sends_the_session_cookie_jar_for_that_url(server, tmp_path):
    source = tmp_path / "upload.bin"
    source.write_bytes(b"payload")
    cookies = httpx.Cookies()
    cookies.set("SESSION", "value", domain="127.0.0.1")
    client = CurlCffiAsyncClient(cookies=cookies)
    try:
        response = await client.stream_upload(
            f"{server}/target",
            source,
            total_bytes=7,
            headers={},
            method="PUT",
            overall_timeout=30.0,
        )

        assert response.status_code == 200
        # Without the CURLOPT_COOKIE setup this upload leg would be anonymous.
        assert "SESSION=value" in response.headers["X-Echo-Cookie"]
    finally:
        await client.aclose()
