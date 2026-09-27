"""Bounded, credential-free fetching for opt-in URL recovery.

Every hop uses a fresh session pinned to an already validated public address.
This is deliberately separate from the authenticated NotebookLM transport.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..exceptions import ValidationError
from ..utils import html_to_markdown
from .content_sanity import text_content_warning

MAX_FETCH_BYTES = 2_000_000
FETCH_TIMEOUT = 30.0
MAX_REDIRECTS = 5
_TEXT_TYPES = {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}


@dataclass(frozen=True)
class FetchedSource:
    final_url: str
    title: str
    content: str


def require_fetch_dependencies() -> None:
    """Fail before the first source mutation if an optional dependency is absent."""
    try:
        import curl_cffi.requests  # noqa: F401
        import markdownify  # noqa: F401
    except ImportError as exc:
        raise ValidationError(
            "URL fallback requires: pip install 'notebooklm-py[impersonate,markdown]'"
        ) from exc


def public_fetch_url(url: str) -> tuple[str, str, int]:
    """Normalize one HTTP(S) target without libcurl/urllib authority disagreements."""
    if len(url) > 8192 or "\\" in url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("Invalid fallback URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Fallback requires an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Fallback URLs cannot contain credentials")
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii")
    if not host or "%" in host:
        raise ValueError("Invalid fallback hostname")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if port < 1 or port > 65535:
        raise ValueError("Invalid fallback port")
    authority = f"[{host}]" if ":" in host else host
    normalized = urlunsplit(
        (parsed.scheme, f"{authority}:{port}", parsed.path or "/", parsed.query, "")
    )
    return normalized, host, port


async def _public_address(host: str, port: int) -> str:
    addresses = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("Fallback hostname has no addresses")
    approved: list[str] = []
    for _family, _type, _proto, _canon, address in addresses:
        ip = ipaddress.ip_address(address[0])
        checked = getattr(ip, "ipv4_mapped", None) or ip
        if not checked.is_global or checked.is_multicast:
            raise ValueError("Fallback requires public unicast addresses on every hop")
        approved.append(str(ip))
    return approved[0]


def _decode_content(body: bytes, content_type: str, final_url: str) -> FetchedSource:
    header = Message()
    header["content-type"] = content_type
    mime = header.get_content_type()
    if not content_type or mime not in _TEXT_TYPES:
        raise ValueError("Fallback supports only HTML and plain/Markdown text")
    text = body.decode(header.get_content_charset() or "utf-8")
    if "\x00" in text:
        raise ValueError("Fallback response contains binary content")
    title = ""
    visible = text
    if mime in {"text/html", "application/xhtml+xml"}:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        for element in soup(["script", "style", "noscript", "template"]):
            element.decompose()
        visible = (soup.body or soup).get_text(" ", strip=True)
        text = html_to_markdown(str(soup.body or soup))
    warning = text_content_warning(visible)
    if warning is not None:
        raise ValueError("Fallback content rejected: " + warning)
    return FetchedSource(final_url=final_url, title=title[:300], content=text.strip())


async def _fetch(url: str) -> FetchedSource:
    from curl_cffi import CurlOpt
    from curl_cffi.requests import AsyncSession

    for hop in range(MAX_REDIRECTS + 1):
        url, host, port = public_fetch_url(url)
        address = await _public_address(host, port)
        pinned = f"[{address}]" if ":" in address else address
        resolve_host = f"[{host}]" if ":" in host else host
        body = bytearray()
        oversized = False

        def receive(chunk: bytes, buffer: bytearray = body) -> int:
            nonlocal oversized
            if len(buffer) + len(chunk) > MAX_FETCH_BYTES:
                oversized = True
                return 0  # Abort libcurl before retaining an oversized/decompressed body.
            buffer.extend(chunk)
            return len(chunk)

        # New cookie jar, no auth headers, no environment proxy, no connection
        # reuse across origins. RESOLVE preserves Host and TLS SNI/verification.
        async with AsyncSession(
            impersonate="chrome",
            trust_env=False,
            curl_options={
                CurlOpt.RESOLVE: [f"{resolve_host}:{port}:{pinned}"],
                CurlOpt.PROXY: "",
                CurlOpt.NOPROXY: "*",
            },
        ) as session:
            try:
                response = await session.get(
                    url,
                    allow_redirects=False,
                    timeout=FETCH_TIMEOUT,
                    content_callback=receive,
                    verify=True,
                )
            except Exception:
                if oversized:
                    raise ValueError("Fallback response exceeds the byte limit") from None
                raise
        if oversized:
            raise ValueError("Fallback response exceeds the byte limit")
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            if not location or hop == MAX_REDIRECTS:
                raise ValueError("Invalid or excessive fallback redirects")
            url = urljoin(url, location)
            continue
        if not 200 <= response.status_code < 300:
            raise ValueError("Fallback server returned a non-success status")
        return _decode_content(bytes(body), response.headers.get("content-type", ""), url)
    raise AssertionError("unreachable")


async def fetch_source(url: str) -> FetchedSource:
    """Fetch public textual content within one DNS/redirect/download time budget."""
    return await asyncio.wait_for(_fetch(url), timeout=FETCH_TIMEOUT)
