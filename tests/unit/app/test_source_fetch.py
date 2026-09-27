"""Outbound fetching pins validated addresses and bounds untrusted responses."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from notebooklm._app import source_fetch as fetch


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "https://user:secret@example.com/",
        "https://example.com\\@localhost/",
        "https://example.com/\npath",
        "https://[::1%25lo]/",
        "https:///missing",
        "https://example.com:99999/",
        "https://example.com:0/",
    ],
)
def test_reject_ambiguous_authorities_and_unsupported_schemes(url):
    with pytest.raises(ValueError):
        fetch.public_fetch_url(url)


def test_normalizes_hostname_and_strips_fragment():
    assert fetch.public_fetch_url("https://EXAMPLE.com./a?b=c#fragment") == (
        "https://example.com:443/a?b=c",
        "example.com",
        443,
    )


@pytest.mark.parametrize(
    ("url", "expected_host"),
    [
        ("https://faß.de/", "xn--fa-hia.de"),
        ("https://[2606:4700:4700::1111]/", "2606:4700:4700::1111"),
    ],
)
def test_hostname_normalization_preserves_modern_idna_and_ipv6(url, expected_host):
    normalized, host, port = fetch.public_fetch_url(url)
    assert host == expected_host
    assert port == 443
    authority = f"[{host}]" if ":" in host else host
    assert normalized == f"https://{authority}:443/"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "::ffff:127.0.0.1",
        "64:ff9b::127.0.0.1",
        "64:ff9b::169.254.169.254",
        "::127.0.0.1",
        "2002:0808:0808::1",
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",
        "64:ff9b:1::808:808",
        "fc00::1",
        "ff02::1",
    ],
)
async def test_rejects_nonpublic_addresses_even_in_mixed_dns_answers(monkeypatch, ip):
    resolver = AsyncMock(
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)),
        ]
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    with pytest.raises(ValueError, match="public unicast"):
        await fetch._public_addresses("example.com", 443)


@pytest.mark.asyncio
async def test_retains_all_public_addresses_without_duplicates(monkeypatch):
    resolver = AsyncMock(
        return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ]
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    assert await fetch._public_addresses("example.com", 443) == (
        "8.8.8.8",
        "2606:4700:4700::1111",
    )


@pytest.fixture
def sessions(monkeypatch):
    requests = pytest.importorskip("curl_cffi.requests")
    responses = []
    opened = []
    calls = []
    closed = []

    class FetchSession:
        def __init__(self, **kwargs):
            opened.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            status, headers, chunks = responses.pop(0)
            for chunk in chunks:
                if kwargs["content_callback"](chunk) != len(chunk):
                    raise ValueError("curl callback aborted")
            return SimpleNamespace(status_code=status, headers=headers)

    monkeypatch.setattr(requests, "AsyncSession", FetchSession)
    monkeypatch.setattr(fetch, "_public_addresses", AsyncMock(return_value=("8.8.8.8",)))
    return responses, opened, calls, closed


@pytest.mark.asyncio
async def test_pins_each_hop_without_credentials_or_environment_proxy(sessions):
    from curl_cffi import CurlOpt

    responses, opened, calls, closed = sessions
    responses.extend(
        [
            (302, {"location": "https://other.example/article"}, [b"redirect"]),
            (200, {"content-type": "text/plain"}, [b"A useful article. " * 20]),
        ]
    )
    result = await fetch.fetch_source("https://example.com")
    assert result.final_url == "https://other.example:443/article"
    assert len(opened) == len(closed) == 2
    assert opened[0]["curl_options"][CurlOpt.RESOLVE] == ["example.com:443:8.8.8.8"]
    assert opened[1]["curl_options"][CurlOpt.RESOLVE] == ["other.example:443:8.8.8.8"]
    for config in opened:
        assert config["trust_env"] is False
        assert config["curl_options"][CurlOpt.PROXY] == ""
        assert "cookies" not in config and "headers" not in config
    assert all(kwargs["allow_redirects"] is False for _, kwargs in calls)
    assert all(kwargs["verify"] is True for _, kwargs in calls)


@pytest.mark.asyncio
async def test_pins_all_validated_addresses_in_one_resolve_entry(sessions, monkeypatch):
    from curl_cffi import CurlOpt

    responses, opened, _, _ = sessions
    responses.append((200, {"content-type": "text/plain"}, [b"Useful article. " * 20]))
    monkeypatch.setattr(
        fetch,
        "_public_addresses",
        AsyncMock(return_value=("8.8.8.8", "2606:4700:4700::1111")),
    )
    await fetch.fetch_source("https://example.com/")
    assert opened[0]["curl_options"][CurlOpt.RESOLVE] == [
        "example.com:443:8.8.8.8,[2606:4700:4700::1111]"
    ]


@pytest.mark.asyncio
async def test_private_redirect_rejected_before_second_request(sessions, monkeypatch):
    responses, opened, _, _ = sessions
    responses.append((302, {"location": "https://internal.example/"}, []))
    monkeypatch.setattr(
        fetch,
        "_public_addresses",
        AsyncMock(side_effect=[("8.8.8.8",), ValueError("nonpublic destination")]),
    )
    with pytest.raises(ValueError, match="nonpublic"):
        await fetch.fetch_source("https://example.com/")
    assert len(opened) == 1


@pytest.mark.asyncio
async def test_https_redirect_cannot_downgrade_to_http(sessions):
    responses, opened, calls, _ = sessions
    responses.append((302, {"location": "http://example.com/article"}, []))
    with pytest.raises(ValueError, match="downgrade"):
        await fetch.fetch_source("https://example.com/")
    assert len(opened) == len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_scheme", ["http", "https"])
async def test_explicit_http_url_can_be_fetched_and_redirected(sessions, redirect_scheme):
    responses, _, calls, _ = sessions
    responses.extend(
        [
            (302, {"location": f"{redirect_scheme}://other.example/article"}, []),
            (200, {"content-type": "text/plain"}, [b"A useful article. " * 20]),
        ]
    )
    result = await fetch.fetch_source("http://example.com/")
    assert result.content.startswith("A useful article.")
    assert result.final_url.startswith(f"{redirect_scheme}://other.example:")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_byte_limit_aborts_callback_and_closes_session(sessions, monkeypatch):
    responses, _, _, closed = sessions
    monkeypatch.setattr(fetch, "MAX_FETCH_BYTES", 20)
    responses.append((200, {"content-type": "text/plain"}, [b"a" * 15, b"b" * 15]))
    with pytest.raises(ValueError, match="byte limit"):
        await fetch.fetch_source("https://example.com/")
    assert closed == [True]


@pytest.mark.asyncio
async def test_real_curl_byte_limit_stops_downloading_response(monkeypatch):
    pytest.importorskip("curl_cffi")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread

    finished = Event()
    sent = []
    chunk = b"a" * 65536
    response_bytes = 64 * 1024 * 1024

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(response_bytes))
            self.end_headers()
            try:
                for _ in range(response_bytes // len(chunk)):
                    self.wfile.write(chunk)
                    sent.append(len(chunk))
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                finished.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(fetch, "_public_addresses", AsyncMock(return_value=("127.0.0.1",)))
    monkeypatch.setattr(fetch, "MAX_FETCH_BYTES", 20000)
    try:
        with pytest.raises(ValueError, match="byte limit"):
            await fetch.fetch_source(f"http://limit-test.invalid:{server.server_port}/")
        assert await asyncio.to_thread(finished.wait, 5)
        assert sum(sent) < response_bytes
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.asyncio
async def test_redirect_limit(sessions):
    responses, opened, _, _ = sessions
    responses.extend([(302, {"location": "/loop"}, [])] * (fetch.MAX_REDIRECTS + 1))
    with pytest.raises(ValueError, match="redirect"):
        await fetch.fetch_source("https://example.com/")
    assert len(opened) == fetch.MAX_REDIRECTS + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 500])
async def test_http_error_is_not_imported(sessions, status):
    responses, _, _, _ = sessions
    responses.append((status, {"content-type": "text/plain"}, [b"error body " * 50]))
    with pytest.raises(ValueError, match="non-success"):
        await fetch.fetch_source("https://example.com/")


@pytest.mark.parametrize("mime", ["", "image/png", "application/pdf", "application/octet-stream"])
def test_binary_and_unknown_types_are_not_converted(mime):
    with pytest.raises(ValueError, match="only HTML"):
        fetch._decode_content(b"data " * 40, mime, "https://example.com/")


@pytest.mark.parametrize(
    "body",
    [
        "",
        "short",
        "Page not found. " * 30,
        "Just a moment. " * 30,
        "<script>" + "padding " * 1000 + "</script><p>Just a moment</p>",
    ],
)
def test_rejects_thin_error_and_challenge_content(body):
    pytest.importorskip("markdownify")
    with pytest.raises(ValueError, match="content rejected"):
        fetch._decode_content(body.encode(), "text/html", "https://example.com/")


def test_html_conversion_preserves_math_after_visible_text_gate():
    pytest.importorskip("markdownify")
    body = "<title>Article</title><body><p>" + "Useful text. " * 20 + "$x_1$</p></body>"
    result = fetch._decode_content(body.encode(), "text/html", "https://example.com/")
    assert result.title == "Article"
    assert "$x_1$" in result.content


@pytest.mark.asyncio
async def test_dns_resolution_is_inside_total_deadline(monkeypatch):
    pytest.importorskip("curl_cffi")

    async def resolve(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(fetch, "_public_addresses", resolve)
    monkeypatch.setattr(fetch, "FETCH_TIMEOUT", 0.01)
    with pytest.raises(asyncio.TimeoutError):
        await fetch.fetch_source("https://example.com/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "addresses", "hostname"),
    [
        (socket.AF_INET, ("127.0.0.1",), "pin-test.invalid"),
        (socket.AF_INET, ("127.0.0.2", "127.0.0.1"), "pin-test.invalid"),
        (socket.AF_INET6, ("::1",), "pin-test.invalid"),
        (socket.AF_INET6, ("::1",), "[::1]"),
    ],
    ids=["ipv4", "ipv4-failover", "ipv6-address", "ipv6-host"],
)
async def test_real_curl_uses_pinned_address_and_ignores_proxy(
    monkeypatch, family, addresses, hostname
):
    """Exercise actual libcurl options against a local fault-server, without DNS."""
    pytest.importorskip("curl_cffi")
    pytest.importorskip("markdownify")
    fetch.require_fetch_dependencies()
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"A locally served useful article. " * 20)

        def log_message(self, *args):
            pass

    class PinnedHTTPServer(ThreadingHTTPServer):
        address_family = family

    try:
        server = PinnedHTTPServer((addresses[-1], 0), Handler)
    except OSError:
        if family == socket.AF_INET6:
            pytest.skip("IPv6 loopback is unavailable")
        raise
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Only the policy seam is bypassed for this local transport check; the
    # nonpublic-address rejection is tested separately above.
    monkeypatch.setattr(fetch, "_public_addresses", AsyncMock(return_value=addresses))
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    try:
        result = await fetch.fetch_source(f"http://{hostname}:{server.server_port}/article")
        assert result.content.startswith("A locally served useful article.")
        assert seen[0]["Host"] == f"{hostname}:{server.server_port}"
        assert "Cookie" not in seen[0]
        assert "Authorization" not in seen[0]
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.asyncio
async def test_empty_dns_answer_does_not_make_request(monkeypatch):
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[]))
    with pytest.raises(ValueError, match="no addresses"):
        await fetch._public_addresses("missing.example", 443)


def test_binary_body_mislabeled_as_text_is_rejected():
    with pytest.raises(ValueError, match="binary"):
        fetch._decode_content(b"\x00" * 200, "text/plain", "https://example.com/")


@pytest.mark.asyncio
async def test_conversion_does_not_block_fetch_deadline(sessions, monkeypatch):
    from threading import Event

    responses, _, _, _ = sessions
    responses.append((200, {"content-type": "text/html"}, [b"<p>Article</p>"]))
    release = Event()

    def slow_decode(*args):
        release.wait(timeout=2)
        return fetch.FetchedSource("https://example.com/", "Article", "content")

    monkeypatch.setattr(fetch, "_decode_content", slow_decode)
    monkeypatch.setattr(fetch, "FETCH_TIMEOUT", 0.05)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await fetch.fetch_source("https://example.com/")
    finally:
        release.set()
