"""Authenticated transport pipeline for NotebookLM core operations."""

from __future__ import annotations

__all__ = [
    "MAX_RETRY_AFTER_SECONDS",
    "MAX_RPC_RESPONSE_BYTES",
    "AuthedTransport",
    "_AuthedTransportHost",
    "AuthSnapshot",
    "BuildRequest",
    "PostBody",
    "TransportAuthExpired",
    "TransportRateLimited",
    "TransportServerError",
    "parse_retry_after",
    "stream_post_with_size_cap",
]

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from .exceptions import RPCResponseTooLargeError

if TYPE_CHECKING:
    from ._kernel import Kernel

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
    invoked while still inside the streaming context so the existing
    auth-refresh / 429 / 5xx branches in :meth:`AuthedTransport.perform_authed_post`
    see the same :class:`httpx.HTTPStatusError` they always did, with
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


class _AuthedTransportHost(Protocol):
    """Minimal host surface the post-Tier-12 leaf actually reads.

    Pre-Tier-12 this Protocol declared retry budgets, refresh hooks,
    metrics increments, the RPC semaphore, queue-wait recording — every
    cross-cutting concern. Tier-12 PRs 12.4 / 12.5 / 12.7 / 12.8 lifted
    those into chain middlewares, and PR 12.9 lifted the RPC
    semaphore + queue-wait recording up to
    :meth:`Session._perform_authed_post` (so the slot wraps the
    whole chain invocation, not just one HTTP attempt).

    Session-shrink PR 3 narrowed the Protocol further: the
    ``_http_client`` pre-open guard moved to a
    :meth:`Kernel.get_http_client` call (which raises the historical
    ``RuntimeError`` when the client is unopened), and the bound-loop
    affinity check moved UP to :meth:`Session._perform_authed_post` so
    it fires once per chain invocation (not once per leaf attempt). The
    Protocol now declares only the members the leaf still touches:

    - ``_kernel`` (concrete-class reference; streaming-POST transport
      + pre-open guard via ``get_http_client()``)
    - ``_snapshot`` (fresh ``AuthSnapshot`` per attempt)
    """

    _kernel: Kernel

    async def _snapshot(self) -> AuthSnapshot: ...


class AuthedTransport:
    """Single-attempt authenticated POST.

    Post-Tier-12 the leaf is a pure POST — every retry decision (429 / 5xx
    via :class:`RetryMiddleware`, 401/403/400-CSRF via
    :class:`AuthRefreshMiddleware`) lives in the middleware chain. The
    constructor's pre-Tier-12 ``is_auth_error`` and ``sleep`` callbacks
    are gone: the chain owns both.
    """

    def __init__(
        self,
        host: _AuthedTransportHost,
        *,
        logger: logging.Logger,
    ):
        self._host = host
        self._logger = logger

    async def perform_authed_post(
        self,
        *,
        build_request: BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        """Single authed POST attempt.

        Pre-Tier-12 this method drove the entire retry-and-refresh
        pipeline. After PRs 12.5 (Drain) / 12.7 (Retry) / 12.8
        (AuthRefresh) the leaf is a pure POST — every retry decision
        happens in the chain.

        ``disable_internal_retries`` is accepted for signature stability
        but no longer read here — the middleware reads the flag from
        ``request.context`` directly. PR 12.9 keeps the parameter so the
        legacy ``_chat_transport`` call site still type-checks; a
        follow-up post-13.x cleanup can drop it.

        Semaphore wrapping moved UP to ``Session._perform_authed_post``
        in PR 12.9: the slot is held for the WHOLE chain invocation
        (initial attempt + all chain-level retries by RetryMiddleware /
        AuthRefreshMiddleware), restoring the pre-Tier-12 "one slot per
        logical RPC" backpressure contract. Acquiring it here too would
        deadlock — ``asyncio.Semaphore`` is not reentrant.
        """
        host = self._host
        # Pre-open guard: ``Kernel.get_http_client()`` raises the historical
        # ``RuntimeError("Client not initialized. Use 'async with' context.")``
        # when the HTTP client hasn't been opened yet. Session-shrink PR 3
        # routed this through the kernel accessor so the Protocol can drop
        # ``_http_client`` without losing the early-fail surface.
        host._kernel.get_http_client()

        # Event-loop affinity guard moved UP to
        # :meth:`Session._perform_authed_post` (session-shrink PR 3) so the
        # check fires once per chain invocation rather than once per leaf
        # attempt. The leaf no longer reads ``host._bound_loop``.

        start = time.perf_counter()

        # The leaf is a pure POST: 429 and 5xx/network failures raise
        # ``TransportRateLimited`` / ``TransportServerError`` so
        # :class:`RetryMiddleware` (outside this leaf) decides whether to
        # retry; raw ``httpx.HTTPStatusError`` (e.g. 400 / 401 / 403)
        # propagates so :class:`AuthRefreshMiddleware` (also outside this
        # leaf) can catch it via ``is_auth_error`` and drive
        # refresh-then-retry. ``raise from exc`` preserves the chained
        # transport exception for diagnostic display.
        snapshot = await host._snapshot()
        url, body, headers = build_request(snapshot)

        try:
            # Streaming POST via :class:`Kernel` (PR #850). The size guard
            # lives inside ``Kernel.post``'s stream-read loop;
            # ``raise_for_status()`` is invoked before any body chunk is
            # read so the chain middlewares see the same
            # :class:`httpx.HTTPStatusError` they did when this used
            # ``client.post``.
            response = await host._kernel.post(
                url,
                headers=headers,
                body=body,
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            # --- 429: raise for ``RetryMiddleware`` to catch ----------
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
                raise TransportRateLimited(
                    f"{log_label} rate-limited (HTTP 429)",
                    retry_after=retry_after,
                    response=exc.response,
                    original=exc,
                ) from exc

            # --- 5xx / network: raise for ``RetryMiddleware`` ---------
            if isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600:
                raise TransportServerError(
                    f"{log_label} server error (HTTP {exc.response.status_code})",
                    original=exc,
                    response=exc.response,
                    status_code=exc.response.status_code,
                ) from exc
            if isinstance(exc, httpx.RequestError):
                raise TransportServerError(
                    f"{log_label} network error: {exc}",
                    original=exc,
                ) from exc

            # --- 4xx auth shapes (400/401/403) and everything else:
            # propagate the raw ``httpx.HTTPStatusError`` so
            # ``AuthRefreshMiddleware`` outside this leaf decides whether
            # to refresh-and-retry via ``is_auth_error``.
            elapsed = time.perf_counter() - start
            self._logger.debug(
                "%s transport error after %.3fs: %s",
                log_label,
                elapsed,
                exc,
            )
            raise

        # Success
        return response
