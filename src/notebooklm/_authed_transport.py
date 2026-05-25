"""Transport request types and low-level streaming POST helpers."""

from __future__ import annotations

__all__ = [
    "MAX_RETRY_AFTER_SECONDS",
    "MAX_RPC_RESPONSE_BYTES",
    "AuthSnapshot",
    "BuildRequest",
    "PostBody",
    "TransportAuthExpired",
    "TransportRateLimited",
    "TransportServerError",
    "parse_retry_after",
    "stream_post_with_size_cap",
]

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .exceptions import RPCResponseTooLargeError

# Upper bound on Retry-After wait. Caps both integer-seconds and HTTP-date forms
# so a malicious or buggy server can't force a multi-hour pause.
MAX_RETRY_AFTER_SECONDS = 300

# Upper bound on a single RPC response body. The streaming POST path enforces
# this with a running size guard so a runaway or hostile server can't exhaust
# process memory by emitting a huge body. 50 MiB is far above any legitimate
# batchexecute response we've observed and well below the OOM threshold on a
# typical workstation. Kept in this module (not ``_core.py``) so the streaming
# read loop can read it without creating an import cycle through ``_core``.
MAX_RPC_RESPONSE_BYTES = 50 * 1024 * 1024

# Headers that must NOT survive onto a Response rebuilt from already-decoded
# body bytes. ``content-encoding`` would make ``httpx.Response.__init__``
# re-run the gzip/brotli/zstd decoder on bytes that ``aiter_bytes()`` already
# decoded once, raising ``DecodingError: Error -3 ... incorrect header check``.
# ``content-length`` advertises the compressed size from the wire and no
# longer matches the decoded buffer we hand to the rebuilt Response. Compared
# against ``key.lower()`` so case variants from the wire all match.
_STRIP_HEADERS_ON_REBUFFER = frozenset({"content-encoding", "content-length"})


def parse_retry_after(value: str | None) -> int | None:
    """Parse RFC 7231 Retry-After: integer-seconds OR HTTP-date.

    Returns seconds-until-retry as a non-negative int, clamped to
    ``MAX_RETRY_AFTER_SECONDS``. Returns ``None`` for empty or unparseable input.
    """
    if not value:
        return None
    value = value.strip()
    # Integer-seconds form (most common)
    try:
        return min(MAX_RETRY_AFTER_SECONDS, max(0, int(value)))
    except ValueError:
        pass
    # HTTP-date form (RFC 7231 section 7.1.1.1)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return min(MAX_RETRY_AFTER_SECONDS, max(0, int(delta)))


@dataclass(frozen=True)
class AuthSnapshot:
    """Point-in-time view of auth headers used to build a single request.

    Captured once per HTTP attempt by ``_perform_authed_post`` and passed
    into the caller-supplied ``build_request`` factory so the URL/body are
    consistent for that attempt. On retry, a *new* snapshot is taken so
    refreshed credentials are picked up before the rebuild.
    """

    csrf_token: str
    session_id: str
    authuser: int
    account_email: str | None


class TransportAuthExpired(Exception):
    """Raised by ``AuthRefreshMiddleware`` when the refresh callback itself
    failed during an auth recovery attempt.

    Pre-Tier-12 this was raised by the leaf's auth-refresh-once branch.
    PR 12.8 lifted that branch into
    :class:`notebooklm._middleware_auth_refresh.AuthRefreshMiddleware`;
    the class definition stays here so the existing import path
    (``from notebooklm._authed_transport import TransportAuthExpired``)
    keeps working for ``_chat_transport.chat_aware_authed_post`` and its
    tests.

    ``original`` is the transport-layer ``httpx.HTTPStatusError`` that
    triggered the refresh attempt. The refresh callback's error is attached via
    ``__cause__``.
    """

    def __init__(self, message: str, *, original: Exception):
        super().__init__(message)
        self.original = original


class TransportRateLimited(Exception):
    """Raised by ``_perform_authed_post`` when the 429 retry budget is
    exhausted (or no retries are configured).
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: int | None,
        response: httpx.Response,
        original: httpx.HTTPStatusError,
    ):
        super().__init__(message)
        self.retry_after = retry_after
        self.response = response
        self.original = original


class TransportServerError(Exception):
    """Raised by ``_perform_authed_post`` when the server-error retry budget
    is exhausted.
    """

    def __init__(
        self,
        message: str,
        *,
        original: Exception,
        response: httpx.Response | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.original = original
        self.response = response
        self.status_code = status_code


# Build-request factory: receives a fresh ``AuthSnapshot`` and returns the
# triple (url, body, extra_headers) for one HTTP attempt. The transport invokes
# this once per attempt so refreshed snapshots are picked up on retry.
PostBody = str | bytes
BuildRequest = Callable[[AuthSnapshot], tuple[str, PostBody, dict[str, str] | None]]


async def stream_post_with_size_cap(
    client: httpx.AsyncClient,
    url: str,
    *,
    body: PostBody,
    headers: dict[str, str] | None,
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
