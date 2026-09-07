"""Bounded request framing for the test-only HTTP service."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

MAX_HEADERS = 64 * 1024
READ_TIMEOUT = 2.0


@dataclass(frozen=True)
class RequestHead:
    method: str
    target: str
    headers: dict[str, str]


async def read_head(reader: asyncio.StreamReader) -> RequestHead:
    raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
    if len(raw) > MAX_HEADERS:
        raise ValueError("request headers exceed limit")
    lines = raw[:-4].split(b"\r\n")
    method, target, version = lines[0].split(b" ", 2)
    if version != b"HTTP/1.1":
        raise ValueError("only HTTP/1.1 is supported")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator or not name or name.strip() != name:
            raise ValueError("malformed request header")
        key = name.decode("ascii").lower()
        if key in headers:
            raise ValueError("duplicate request header")
        headers[key] = value.decode("latin-1").strip()
    if not headers.get("host"):
        raise ValueError("missing request host")
    return RequestHead(method.decode("ascii"), target.decode("ascii"), headers)


async def iter_body(
    reader: asyncio.StreamReader, headers: dict[str, str], *, limit: int, block_size: int = 65536
) -> AsyncIterator[bytes]:
    """Yield bounded chunks without buffering a whole transfer or trusting framing."""
    encoding = headers.get("transfer-encoding")
    length = headers.get("content-length")
    if encoding is not None and length is not None:
        raise ValueError("ambiguous request framing")
    if encoding is not None and encoding.lower() != "chunked":
        raise ValueError("unsupported request transfer encoding")
    if encoding is None:
        if length is not None and (not length.isascii() or not length.isdecimal()):
            raise ValueError("invalid content length")
        remaining = int(length or "0")
        if remaining > limit:
            raise ValueError("request body exceeds limit")
        while remaining:
            data = await asyncio.wait_for(reader.read(min(block_size, remaining)), READ_TIMEOUT)
            if not data:
                raise asyncio.IncompleteReadError(b"", remaining)
            remaining -= len(data)
            yield data
        return
    total = 0
    while True:
        line = await asyncio.wait_for(reader.readuntil(b"\r\n"), READ_TIMEOUT)
        if len(line) > 1024:
            raise ValueError("chunk header exceeds limit")
        size_text = line[:-2].split(b";", 1)[0]
        if not size_text or any(c not in b"0123456789abcdefABCDEF" for c in size_text):
            raise ValueError("invalid chunk size")
        size = int(size_text, 16)
        if size + total > limit:
            raise ValueError("request body exceeds limit")
        if not size:
            # Consume bounded trailers, even when an upload does not send any.
            trailer_bytes = 0
            while True:
                trailer = await asyncio.wait_for(reader.readuntil(b"\r\n"), READ_TIMEOUT)
                trailer_bytes += len(trailer)
                if trailer_bytes > MAX_HEADERS:
                    raise ValueError("trailers exceed limit")
                if trailer == b"\r\n":
                    return
                if b":" not in trailer:
                    raise ValueError("malformed trailer")
        while size:
            data = await asyncio.wait_for(reader.read(min(block_size, size)), READ_TIMEOUT)
            if not data:
                raise asyncio.IncompleteReadError(b"", size)
            size -= len(data)
            total += len(data)
            yield data
        if await asyncio.wait_for(reader.readexactly(2), READ_TIMEOUT) != b"\r\n":
            raise ValueError("malformed chunk delimiter")


class BodyDigest:
    """Incremental evidence independent of response delivery and commit state."""

    def __init__(self) -> None:
        self.size = 0
        self._hash = hashlib.sha256()

    def update(self, data: bytes) -> None:
        self.size += len(data)
        self._hash.update(data)

    @property
    def hexdigest(self) -> str:
        return self._hash.hexdigest()
