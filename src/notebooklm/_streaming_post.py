"""Low-level streaming POST helper for the concrete transport kernel."""

from __future__ import annotations

__all__ = [
    "MAX_RPC_RESPONSE_BYTES",
    "stream_post_with_size_cap",
]

from typing import Any

import httpx

from ._request_types import PostBody
from .exceptions import RPCResponseTooLargeError

# Upper bound on a single RPC response body. The streaming POST path enforces
# this with a running size guard so a runaway or hostile server can't exhaust
# process memory by emitting a huge body. 50 MiB is far above any legitimate
# batchexecute response we've observed and well below the OOM threshold on a
# typical workstation. Kept in this module (not ``_runtime/config.py``) so the
# streaming read loop can read it without creating an import cycle through the
# session layer.
MAX_RPC_RESPONSE_BYTES = 50 * 1024 * 1024

# Headers that must NOT survive onto a Response rebuilt from already-decoded
# body bytes. ``content-encoding`` would make ``httpx.Response.__init__``
# re-run the gzip/brotli/zstd decoder on bytes that ``aiter_bytes()`` already
# decoded once, raising ``DecodingError: Error -3 ... incorrect header check``.
# ``content-length`` advertises the compressed size from the wire and no
# longer matches the decoded buffer we hand to the rebuilt Response. Compared
# against ``key.lower()`` so case variants from the wire all match.
_STRIP_HEADERS_ON_REBUFFER = frozenset({"content-encoding", "content-length"})


async def stream_post_with_size_cap(
    client: httpx.AsyncClient,
    url: str,
    *,
    body: PostBody,
    headers: dict[str, str] | None,
    timeout: httpx.Timeout | float | None = None,
    max_bytes: int = MAX_RPC_RESPONSE_BYTES,
) -> httpx.Response:
    """Issue a streaming POST and buffer the body with a running size guard.

    Uses :meth:`httpx.AsyncClient.stream` so the body is read chunk-by-chunk and
    aborted as soon as the running total exceeds ``max_bytes``. The buffered
    bytes are then attached to a fresh :class:`httpx.Response` with the same
    status code, headers, and request, so downstream callers can keep using
    ``response.text`` / ``response.content`` exactly as they did when this was a
    plain ``client.post`` call.

    Error semantics are preserved verbatim: ``response.raise_for_status()`` is
    invoked while still inside the streaming context so chain middlewares and
    the terminal error mapper see the same :class:`httpx.HTTPStatusError`, with
    ``exc.response.headers`` intact (the response headers arrive before any body
    chunk, so reading them does not require consuming the stream).
    """
    stream_kwargs: dict[str, Any] = {"content": body}
    if headers:
        stream_kwargs["headers"] = headers
    if timeout is not None:
        stream_kwargs["timeout"] = timeout
    async with client.stream("POST", url, **stream_kwargs) as response:
        response.raise_for_status()
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise RPCResponseTooLargeError(
                    f"RPC response exceeded {max_bytes} bytes "
                    f"(read {len(buffer)} bytes before aborting)",
                    limit_bytes=max_bytes,
                    bytes_read=len(buffer),
                )
        # Reconstruct a fully-buffered Response so downstream consumers
        # (``_rpc_executor.py`` decode path) can use ``.text`` / ``.content``
        # without dealing with stream state. The request handle is carried
        # over so log/repr surfaces still point at the originating request.
        #
        # ``response.aiter_bytes()`` above yields already-decoded body chunks,
        # so the buffered payload is plain bytes. Filter out
        # ``content-encoding`` (and the now-mismatched ``content-length``) via
        # a dict comprehension — ``httpx.Headers`` inherits from
        # :class:`collections.abc.Mapping`, NOT ``MutableMapping``, so we
        # avoid relying on ``.pop()`` (which is not part of the documented
        # contract and could change across the ``>=0.27,<0.29`` httpx pin).
        # ``httpx.Response(headers=...)`` accepts a plain ``dict`` of
        # ``str -> str`` so this is the documented input shape.
        rebuilt_headers = {
            k: v for k, v in response.headers.items() if k.lower() not in _STRIP_HEADERS_ON_REBUFFER
        }
        return httpx.Response(
            status_code=response.status_code,
            headers=rebuilt_headers,
            content=bytes(buffer),
            request=response.request,
        )
