"""Core infrastructure for NotebookLM API client."""

import asyncio
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import httpx

from ._env import get_default_language
from ._logging import reset_request_id, set_request_id
from .auth import (
    AuthTokens,
    CookieSaveResult,
    CookieSnapshot,
    _rotate_cookies,
    advance_cookie_snapshot_after_save,
    build_cookie_jar,
    format_authuser_value,
    save_cookies_to_storage,
    snapshot_cookie_jar,
)
from .rpc import (
    AuthError,
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCMethod,
    RPCTimeoutError,
    ServerError,
    build_request_body,
    decode_response,
    encode_rpc_request,
    get_batchexecute_url,
)

logger = logging.getLogger(__name__)

# Maximum number of conversations to cache (FIFO eviction)
MAX_CONVERSATION_CACHE_SIZE = 100

# Default HTTP timeouts in seconds
DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0  # Connection establishment timeout

# Minimum keepalive interval to avoid accidentally rate-limiting accounts.google.com
DEFAULT_KEEPALIVE_MIN_INTERVAL = 60.0

# Auth error detection patterns (case-insensitive)
# Upper bound on Retry-After wait. Caps both integer-seconds and HTTP-date forms
# so a malicious or buggy server can't force a multi-hour pause.
MAX_RETRY_AFTER_SECONDS = 300


def _parse_retry_after(value: str | None) -> int | None:
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
    # HTTP-date form (RFC 7231 §7.1.1.1)
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


AUTH_ERROR_PATTERNS = (
    "authentication",
    "expired",
    "unauthorized",
    "login",
    "re-authenticate",
)


def _resolve_keepalive_interval(keepalive: float | None, min_interval: float) -> float | None:
    """Validate and clamp the keepalive interval.

    ``None`` disables the background task. Otherwise both values must be
    positive finite numbers; the effective interval is ``max(keepalive,
    min_interval)`` so callers can't accidentally lower the rate-limit floor.
    """
    if not (math.isfinite(min_interval) and min_interval > 0):
        raise ValueError(
            f"keepalive_min_interval must be a positive finite number, got {min_interval!r}"
        )
    if keepalive is None:
        return None
    if not (math.isfinite(keepalive) and keepalive > 0):
        raise ValueError(f"keepalive must be None or a positive finite number, got {keepalive!r}")
    return max(keepalive, min_interval)


def is_auth_error(error: Exception) -> bool:
    """Check if an exception indicates an authentication failure.

    Args:
        error: The exception to check.

    Returns:
        True if the error is likely due to authentication issues.
    """
    # AuthError is always an auth error
    if isinstance(error, AuthError):
        return True

    # Don't treat network/rate limit/server errors as auth errors
    # even if they're subclasses of RPCError
    if isinstance(
        error,
        NetworkError | RPCTimeoutError | RateLimitError | ServerError | ClientError,
    ):
        return False

    # HTTP 400/401/403 are auth errors.
    # Google returns 400 for expired CSRF tokens (not 401/403). Layer-1
    # recovery (refresh_auth) re-extracts SNlM0e from the NotebookLM
    # homepage and retries with a fresh token. The retry guard
    # (``_is_retry`` in ``rpc_call``) bounds wasted refreshes on legitimate
    # 400s (bad payload) to one extra GET per call.
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in (400, 401, 403)

    # RPCError with auth-related message
    if isinstance(error, RPCError):
        message = str(error).lower()
        return any(pattern in message for pattern in AUTH_ERROR_PATTERNS)

    return False


class ClientCore:
    """Core client infrastructure for HTTP and RPC operations.

    Handles:
    - HTTP client lifecycle (open/close)
    - RPC call encoding/decoding
    - Authentication headers
    - Conversation cache

    This class is used internally by the sub-client APIs (NotebooksAPI,
    ArtifactsAPI, etc.) and should not be used directly.
    """

    def __init__(
        self,
        auth: AuthTokens,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        refresh_callback: Callable[[], Awaitable[AuthTokens]] | None = None,
        refresh_retry_delay: float = 0.2,
        keepalive: float | None = None,
        keepalive_min_interval: float = DEFAULT_KEEPALIVE_MIN_INTERVAL,
        keepalive_storage_path: Path | None = None,
        rate_limit_max_retries: int = 0,
    ):
        """Initialize the core client.

        Args:
            auth: Authentication tokens from browser login.
            timeout: HTTP request timeout in seconds. Defaults to 30 seconds.
                This applies to read/write operations after connection is established.
            connect_timeout: Connection establishment timeout in seconds. Defaults to 10 seconds.
                A shorter connect timeout helps detect network issues faster.
            refresh_callback: Optional async callback to refresh auth tokens on failure.
                If provided, rpc_call will automatically retry once after refreshing.
            refresh_retry_delay: Delay in seconds before retrying after refresh.
            keepalive: Optional interval in seconds for a background task that pokes
                ``accounts.google.com/RotateCookies`` while the client is open. ``None``
                (default) disables the task. Must be ``None`` or a positive finite
                number; values below ``keepalive_min_interval`` are clamped up to
                that floor.
            keepalive_min_interval: Lower bound for ``keepalive`` (defaults to 60s)
                to avoid accidentally rate-limiting Google's identity surface.
                Must be a positive finite number.
            keepalive_storage_path: Optional storage path to persist rotated cookies
                to from the keepalive loop. Falls back to ``auth.storage_path``.
            rate_limit_max_retries: Max automatic retries when a 429 response carries
                a parseable ``Retry-After`` header. ``0`` (default) preserves the
                pre-Phase-3 contract of raising ``RateLimitError`` immediately —
                opt in to a positive value to enable bounded sleep-and-retry.
                Each retry sleeps for the (clamped) ``Retry-After`` value; that
                per-attempt value is capped at ``MAX_RETRY_AFTER_SECONDS``, but
                the cumulative sleep across N retries is ``N * cap``, so pick
                ``rate_limit_max_retries`` accordingly.

        Raises:
            ValueError: If ``keepalive`` or ``keepalive_min_interval`` is not a
                positive finite number.
        """
        self.auth = auth
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._refresh_callback = refresh_callback
        self._refresh_retry_delay = refresh_retry_delay
        if rate_limit_max_retries < 0:
            raise ValueError(f"rate_limit_max_retries must be >= 0, got {rate_limit_max_retries}")
        self._rate_limit_max_retries = rate_limit_max_retries
        self._refresh_lock: asyncio.Lock | None = asyncio.Lock() if refresh_callback else None
        self._refresh_task: asyncio.Task[AuthTokens] | None = None
        self._http_client: httpx.AsyncClient | None = None
        # Request ID counter for chat API (must be unique per request)
        self._reqid_counter: int = 100000
        # OrderedDict for FIFO eviction when cache exceeds MAX_CONVERSATION_CACHE_SIZE
        self._conversation_cache: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        # Keepalive background task configuration
        self._keepalive_interval: float | None = _resolve_keepalive_interval(
            keepalive, keepalive_min_interval
        )
        # Prefer the explicit storage_path if provided (e.g. NotebookLMClient(storage_path=...)
        # with a manually-built AuthTokens), otherwise fall back to auth.storage_path.
        self._keepalive_storage_path: Path | None = (
            keepalive_storage_path if keepalive_storage_path is not None else auth.storage_path
        )
        self._keepalive_task: asyncio.Task[None] | None = None
        # Serializes keepalive's worker-thread save with close()'s on-close save
        # so that newer state always wins. Without this, an in-flight keepalive
        # save kicked off before close() can finish *after* close()'s own save
        # and clobber it (an older snapshot overwriting the freshest state).
        self._save_lock = threading.Lock()
        # Open-time cookie snapshot — the input to the dirty-flag/delta merge
        # in save_cookies_to_storage. Captured in ``open()`` and forwarded
        # through every ``save_cookies`` call so a stale in-memory jar can't
        # clobber sibling-process writes (docs/auth-keepalive.md §3.4.1).
        # Per-instance, never module-global.
        self._loaded_cookie_snapshot: CookieSnapshot | None = None

    async def open(self) -> None:
        """Open the HTTP client connection.

        Called automatically by NotebookLMClient.__aenter__.
        Uses httpx.Cookies jar to properly handle cross-domain redirects
        (e.g., to accounts.google.com for auth token refresh).
        """
        if self._http_client is None:
            # Use granular timeouts: shorter connect timeout helps detect network issues
            # faster, while longer read/write timeouts accommodate slow responses
            timeout = httpx.Timeout(
                connect=self._connect_timeout,
                read=self._timeout,
                write=self._timeout,
                pool=self._timeout,
            )
            # Build cookies jar for cross-domain redirect support
            # Use pre-built jar if available, otherwise build one
            cookies = self.auth.cookie_jar or build_cookie_jar(
                cookies=self.auth.cookies,
                storage_path=self.auth.storage_path,
            )
            self._http_client = httpx.AsyncClient(
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                },
                cookies=cookies,
                timeout=timeout,
                follow_redirects=True,
            )

            # Capture the open-time snapshot AFTER the AsyncClient is built
            # (httpx normalizes domains on ingest) but BEFORE any rotation
            # could possibly fire. When AuthTokens carries a snapshot from a
            # failed pre-client save, keep it so the unpersisted delta can be
            # retried instead of treating the already-mutated jar as clean.
            self._loaded_cookie_snapshot = (
                dict(self.auth.cookie_snapshot)
                if self.auth.cookie_snapshot is not None
                else snapshot_cookie_jar(self._http_client.cookies)
            )
            self.auth.cookie_snapshot = self._loaded_cookie_snapshot

            # Spawn the keepalive task once the client is ready
            if self._keepalive_interval is not None:
                self._keepalive_task = asyncio.create_task(
                    self._keepalive_loop(self._keepalive_interval)
                )

    async def save_cookies(self, jar: httpx.Cookies, path: Path | None = None) -> None:
        """Persist a cookie jar through the shared save lock.

        Single chokepoint used by ``close()``, the keepalive loop, and
        ``NotebookLMClient.refresh_auth``. Routes every save through:

        1. **Snapshot the jar** on the event-loop thread so the worker isn't
           iterating a live ``AsyncClient.cookies`` that may be mutating
           (RPC redirects, the next poke iteration).
        2. **Hold ``self._save_lock``** (a ``threading.Lock``) for the duration
           of the off-loaded write. Multiple writers in the same process
           serialize through this lock so the newer caller always wins.
        3. **Off-load** the actual save to a worker thread via
           ``asyncio.to_thread`` so disk I/O never stalls the event loop.
        4. **Refresh the baseline snapshot** on success so that a subsequent
           save in this client computes deltas against what we just
           persisted — not against the open-time snapshot. Without this
           step the same delta would re-apply on every save, silently
           clobbering any sibling-process write that landed between two of
           our own saves (the keepalive + close common case).

        Cross-process serialization is handled at a different layer — the
        OS-level file lock inside :func:`save_cookies_to_storage` itself.

        Args:
            jar: The cookie jar to persist. A copy is taken on the loop thread
                before the worker reads it.
            path: Storage path. Falls back to ``self._keepalive_storage_path``,
                which itself falls back to ``self.auth.storage_path``. If both
                are ``None``, the call is a no-op.
        """
        effective_path = path if path is not None else self._keepalive_storage_path
        if effective_path is None:
            return
        save_path: Path = effective_path

        jar_copy = httpx.Cookies(jar)
        # Computed on the loop thread off ``jar_copy`` so the worker can refresh
        # the baseline without re-snapshotting a jar that may have mutated in
        # the meantime (next keepalive poke, in-flight RPC redirect).
        post_save_snapshot = snapshot_cookie_jar(jar_copy)

        def _save(
            s: httpx.Cookies = jar_copy,
            p: Path = save_path,
            lock: threading.Lock = self._save_lock,
            post: CookieSnapshot = post_save_snapshot,
            client: ClientCore = self,
        ) -> None:
            """Worker-thread save: hold the in-process lock around the disk write."""
            with lock:
                # Read the baseline INSIDE the lock so a prior save that
                # completed while we were queued advances ours too. Capturing
                # this on the loop thread would let a concurrent save observe
                # a stale baseline, compute deltas against pre-prior-save
                # state, hit CAS rejection on every key, and silently lose
                # the local rotation.
                snap = client._loaded_cookie_snapshot
                # Advance successful keys while preserving CAS-rejected ones.
                # A silent I/O error leaves the baseline untouched; an
                # exception does too. See class-level
                # ``_loaded_cookie_snapshot``.
                result = save_cookies_to_storage(
                    s,
                    p,
                    original_snapshot=snap,
                    return_result=True,
                )
                if isinstance(result, CookieSaveResult):
                    if result.ok:
                        client._loaded_cookie_snapshot = post
                    elif result.cas_rejected_keys:
                        client._loaded_cookie_snapshot = advance_cookie_snapshot_after_save(
                            snap, post, result.cas_rejected_keys
                        )
                    if client._loaded_cookie_snapshot is not None:
                        client.auth.cookie_snapshot = client._loaded_cookie_snapshot
                elif result:
                    client._loaded_cookie_snapshot = post
                    client.auth.cookie_snapshot = post

        await asyncio.to_thread(_save)

    async def close(self) -> None:
        """Close the HTTP client connection.

        Called automatically by NotebookLMClient.__aexit__.
        """
        # Stop the keepalive task before tearing down the HTTP client so the
        # loop can't issue a poke against an already-closed transport.
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
            self._keepalive_task = None

        if self._http_client:
            try:
                # Single source of truth for the on-close save: takes the
                # in-process lock, snapshots, off-loads. Serializes naturally
                # with any keepalive save still finishing in a worker thread
                # — close() owns the freshest jar and must win, not the older
                # snapshot.
                await self.save_cookies(self._http_client.cookies)
            except Exception as e:
                logger.warning("Failed to sync refreshed cookies during close: %s", e)
            finally:
                await self._http_client.aclose()
                self._http_client = None

    async def _keepalive_loop(self, interval: float) -> None:
        """Background loop that periodically pokes the identity surface.

        Sleeps ``interval`` seconds between iterations, then calls
        :func:`notebooklm.auth._rotate_cookies` to elicit ``__Secure-1PSIDTS``
        rotation. Any rotated cookies are persisted to ``storage_state.json``
        immediately (off-loop, via :func:`asyncio.to_thread`) so a long-lived
        client's freshness survives a crash.

        Error handling is split by failure mode:

        - Poke failures (network blips, ``accounts.google.com`` downtime) are
          opportunistic and logged at DEBUG. The next iteration retries.
        - Persistence failures hide the most important class of bug — a
          rotated cookie that exists in memory but not on disk — so they are
          logged at WARNING with the storage path.

        Both classes never propagate; the loop only exits via
        :class:`asyncio.CancelledError` from :meth:`close`.
        """
        logger.debug("Keepalive task started (interval=%.1fs)", interval)
        try:
            while True:
                await asyncio.sleep(interval)
                client = self._http_client
                if client is None:
                    # Client closed concurrently; exit gracefully.
                    return

                try:
                    # Bypass the layer-1 dedup guards: this loop is self-paced
                    # by ``keepalive_min_interval`` and never runs concurrently
                    # with itself. Pass the storage path so the bare call
                    # bumps the *per-profile* in-process timestamp, letting
                    # concurrent layer-1 callers (e.g. spawned ``fetch_tokens``
                    # tasks on the same profile) and other keepalive loops on
                    # the same profile see the fresh rotation and skip.
                    await _rotate_cookies(client, self._keepalive_storage_path)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - opportunistic best-effort
                    logger.debug("Keepalive poke failed (non-fatal): %s", exc)
                    continue

                if self._keepalive_storage_path is None:
                    continue

                try:
                    # save_cookies handles snapshot + lock + off-load.
                    await self.save_cookies(client.cookies)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Keepalive cookie persistence to %s failed: %s",
                        self._keepalive_storage_path,
                        exc,
                    )
        except asyncio.CancelledError:
            logger.debug("Keepalive task cancelled")
            raise

    @property
    def is_open(self) -> bool:
        """Check if the HTTP client is open."""
        return self._http_client is not None

    def update_auth_headers(self) -> None:
        """Refresh auth metadata without resetting the live cookie jar.

        Call this after modifying auth tokens (e.g., after refresh_auth())
        to ensure the HTTP client uses the updated credentials.

        The httpx client's cookie jar is authoritative once the session is
        open. Re-injecting startup cookies here can overwrite cookies refreshed
        during redirects to accounts.google.com.

        Raises:
            RuntimeError: If client is not initialized.
        """
        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")

        self.auth.cookie_jar = self._http_client.cookies

    def _build_url(self, rpc_method: RPCMethod, source_path: str = "/") -> str:
        """Build the batchexecute URL for an RPC call.

        Args:
            rpc_method: The RPC method to call.
            source_path: The source path parameter (usually notebook path).

        Returns:
            Full URL with query parameters.
        """
        params: dict[str, str] = {
            "rpcids": rpc_method.value,
            "source-path": source_path,
            "f.sid": self.auth.session_id,
            "hl": get_default_language(),
            "rt": "c",
        }
        # Multi-account: route batchexecute to the same Google account the
        # auth tokens were minted for. Email is preferred when known because
        # Google's integer account indices can change as browser accounts are
        # added or removed.
        if self.auth.account_email or self.auth.authuser:
            params["authuser"] = format_authuser_value(
                self.auth.authuser,
                self.auth.account_email,
            )
        return f"{get_batchexecute_url()}?{urlencode(params)}"

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        _rate_limit_retries: int = 0,
    ) -> Any:
        """Make an RPC call to the NotebookLM API.

        Automatically refreshes authentication tokens and retries once if an
        auth failure is detected and a refresh_callback was provided.

        Args:
            method: The RPC method to call.
            params: Parameters for the RPC call (nested list structure).
            source_path: The source path parameter (usually /notebook/{id}).
            allow_null: If True, don't raise error when response is null.
            _is_retry: Internal flag to prevent infinite retries.
            _rate_limit_retries: Internal counter for rate limit retries.

        Returns:
            Decoded response data.

        Raises:
            RuntimeError: If client is not initialized (not in context manager).
            httpx.HTTPStatusError: If HTTP request fails.
            RPCError: If RPC call fails or returns unexpected data.
        """
        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")

        # Only the outer rpc_call mints a request id; the recursive retry path
        # (_is_retry=True or rate limit retry) inherits the parent's id so a
        # single failure→refresh→retry sequence appears under one [req=<id>]
        # in the logs.
        if _is_retry or _rate_limit_retries > 0:
            return await self._rpc_call_impl(
                method, params, source_path, allow_null, _is_retry, _rate_limit_retries
            )

        _reqid_token = set_request_id()
        try:
            return await self._rpc_call_impl(
                method, params, source_path, allow_null, _is_retry, _rate_limit_retries
            )
        finally:
            reset_request_id(_reqid_token)

    async def _rpc_call_impl(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str,
        allow_null: bool,
        _is_retry: bool,
        _rate_limit_retries: int = 0,
    ) -> Any:
        # Caller (rpc_call) has already verified self._http_client is not None;
        # re-assert for mypy narrowing through this helper.
        assert self._http_client is not None
        start = time.perf_counter()
        logger.debug("RPC %s starting", method.name)

        url = self._build_url(method, source_path)
        rpc_request = encode_rpc_request(method, params)
        body = build_request_body(rpc_request, self.auth.csrf_token)

        try:
            response = await self._http_client.post(url, content=body)
            response.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            elapsed = time.perf_counter() - start

            # Check if this is an auth error and we can retry
            if not _is_retry and self._refresh_callback and is_auth_error(e):
                refreshed = await self._try_refresh_and_retry(
                    method, params, source_path, allow_null, e
                )
                if refreshed is not None:
                    return refreshed

            if isinstance(e, httpx.HTTPStatusError):
                status = e.response.status_code
                logger.error(
                    "RPC %s failed after %.3fs: HTTP %s",
                    method.name,
                    elapsed,
                    status,
                )

                # Map HTTP status codes to appropriate exception types
                if status == 429:
                    # Rate limiting - parse retry-after (integer or HTTP-date).
                    retry_after = _parse_retry_after(e.response.headers.get("retry-after"))
                    # Opt-in automatic retry: if the caller enabled the budget and
                    # the server gave us a parseable Retry-After, sleep and retry.
                    # _is_retry stays at its incoming value so the post-sleep call
                    # can still escalate via the auth-refresh path on 401/403.
                    if (
                        retry_after is not None
                        and _rate_limit_retries < self._rate_limit_max_retries
                    ):
                        logger.warning(
                            "RPC %s rate-limited (HTTP 429); sleeping %ds then retrying (%d/%d)",
                            method.name,
                            retry_after,
                            _rate_limit_retries + 1,
                            self._rate_limit_max_retries,
                        )
                        await asyncio.sleep(retry_after)
                        return await self.rpc_call(
                            method,
                            params,
                            source_path,
                            allow_null,
                            _is_retry=_is_retry,
                            _rate_limit_retries=_rate_limit_retries + 1,
                        )
                    msg = f"API rate limit exceeded calling {method.name}"
                    if retry_after:
                        msg += f". Retry after {retry_after} seconds"
                    raise RateLimitError(
                        msg, method_id=method.value, retry_after=retry_after
                    ) from e

                if 500 <= status < 600:
                    raise ServerError(
                        f"Server error {status} calling {method.name}: {e.response.reason_phrase}",
                        method_id=method.value,
                        status_code=status,
                    ) from e

                if 400 <= status < 500 and status not in (401, 403):
                    raise ClientError(
                        f"Client error {status} calling {method.name}: {e.response.reason_phrase}",
                        method_id=method.value,
                        status_code=status,
                    ) from e

                # 401/403 or other: Generic RPCError (handled by auth retry above)
                raise RPCError(
                    f"HTTP {status} calling {method.name}: {e.response.reason_phrase}",
                    method_id=method.value,
                ) from e

            # Network/connection errors
            else:
                logger.error("RPC %s failed after %.3fs: %s", method.name, elapsed, e)

                # Check ConnectTimeout first (more specific than general TimeoutException)
                if isinstance(e, httpx.ConnectTimeout):
                    raise NetworkError(
                        f"Connection timed out calling {method.name}: {e}",
                        method_id=method.value,
                        original_error=e,
                    ) from e

                # Timeout errors (general timeouts, not connection timeouts)
                if isinstance(e, httpx.TimeoutException):
                    raise RPCTimeoutError(
                        f"Request timed out calling {method.name}",
                        method_id=method.value,
                        timeout_seconds=self._timeout,
                        original_error=e,
                    ) from e

                # Connection errors (DNS, network unavailable, etc., excluding ConnectTimeout)
                if isinstance(e, httpx.ConnectError):
                    raise NetworkError(
                        f"Connection failed calling {method.name}: {e}",
                        method_id=method.value,
                        original_error=e,
                    ) from e

                # Other request errors
                raise NetworkError(
                    f"Request failed calling {method.name}: {e}",
                    method_id=method.value,
                    original_error=e,
                ) from e

        try:
            result = decode_response(response.text, method.value, allow_null=allow_null)
            elapsed = time.perf_counter() - start
            logger.debug("RPC %s completed in %.3fs", method.name, elapsed)
            return result
        except RPCError as e:
            elapsed = time.perf_counter() - start

            # Check if this is an auth error and we can retry
            if not _is_retry and self._refresh_callback and is_auth_error(e):
                refreshed = await self._try_refresh_and_retry(
                    method, params, source_path, allow_null, e
                )
                if refreshed is not None:
                    return refreshed

            logger.error("RPC %s failed after %.3fs", method.name, elapsed)
            raise
        except Exception as e:
            elapsed = time.perf_counter() - start
            logger.error("RPC %s failed after %.3fs: %s", method.name, elapsed, e)
            raise RPCError(
                f"Failed to decode response for {method.name}: {e}",
                method_id=method.value,
            ) from e

    async def _try_refresh_and_retry(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str,
        allow_null: bool,
        original_error: Exception,
    ) -> Any | None:
        """Attempt to refresh auth tokens and retry the RPC call.

        Uses a shared task pattern to ensure only one refresh operation runs
        at a time. Concurrent callers wait on the same task, preventing
        redundant refresh calls under high concurrency.

        Args:
            method: The RPC method to retry.
            params: Original parameters.
            source_path: Original source path.
            allow_null: Original allow_null setting.
            original_error: The auth error that triggered this retry.

        Returns:
            The RPC result if retry succeeds, None if refresh failed.

        Raises:
            The original error (with refresh error as cause) if refresh fails.
        """
        logger.info(
            "RPC %s auth error detected, attempting token refresh",
            method.name,
        )

        # This function is only called when _refresh_callback is set
        assert self._refresh_callback is not None

        # Use lock to coordinate refresh task creation
        # Note: refresh_callback is expected to update auth headers internally
        # Lock is always created when callback is set (see __init__)
        assert self._refresh_lock is not None

        # Determine which task to await (existing or new)
        async with self._refresh_lock:
            if self._refresh_task is not None and not self._refresh_task.done():
                # Another refresh is in progress, wait on it
                refresh_task = self._refresh_task
                logger.debug("Waiting on existing refresh task for RPC %s", method.name)
            else:
                # Start a new refresh task
                # Cast needed: Awaitable → Coroutine for create_task (async funcs return coroutines)
                coro = cast(Coroutine[Any, Any, AuthTokens], self._refresh_callback())
                self._refresh_task = asyncio.create_task(coro)
                refresh_task = self._refresh_task

        # Await refresh outside the lock so other callers can join
        try:
            await refresh_task
        except Exception as refresh_error:
            logger.warning("Token refresh failed: %s", refresh_error)
            raise original_error from refresh_error

        # Brief delay before retry to avoid hammering the API
        if self._refresh_retry_delay > 0:
            await asyncio.sleep(self._refresh_retry_delay)

        logger.info("Token refresh successful, retrying RPC %s", method.name)

        # Retry with refreshed tokens
        return await self.rpc_call(method, params, source_path, allow_null, _is_retry=True)

    def get_http_client(self) -> httpx.AsyncClient:
        """Get the underlying HTTP client for direct requests.

        Used by download operations that need direct HTTP access.

        Returns:
            The httpx.AsyncClient instance.

        Raises:
            RuntimeError: If client is not initialized.
        """
        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        return self._http_client

    def cache_conversation_turn(
        self, conversation_id: str, query: str, answer: str, turn_number: int
    ) -> None:
        """Cache a conversation turn locally.

        Uses FIFO eviction when cache exceeds MAX_CONVERSATION_CACHE_SIZE.

        Args:
            conversation_id: The conversation ID.
            query: The user's question.
            answer: The AI's response.
            turn_number: The turn number in the conversation.
        """
        is_new_conversation = conversation_id not in self._conversation_cache

        # Only evict when adding a NEW conversation at capacity
        if is_new_conversation:
            while len(self._conversation_cache) >= MAX_CONVERSATION_CACHE_SIZE:
                # popitem(last=False) removes oldest entry (FIFO)
                self._conversation_cache.popitem(last=False)
            self._conversation_cache[conversation_id] = []

        self._conversation_cache[conversation_id].append(
            {
                "query": query,
                "answer": answer,
                "turn_number": turn_number,
            }
        )

    def get_cached_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        """Get cached conversation turns.

        Args:
            conversation_id: The conversation ID.

        Returns:
            List of cached turns, or empty list if not found.
        """
        return self._conversation_cache.get(conversation_id, [])

    def clear_conversation_cache(self, conversation_id: str | None = None) -> bool:
        """Clear conversation cache.

        Args:
            conversation_id: Clear specific conversation, or all if None.

        Returns:
            True if cache was cleared.
        """
        if conversation_id:
            if conversation_id in self._conversation_cache:
                del self._conversation_cache[conversation_id]
                return True
            return False
        else:
            self._conversation_cache.clear()
            return True

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        """Extract all source IDs from a notebook.

        Fetches notebook data and extracts source IDs for use with
        chat and artifact generation when targeting specific sources.

        Args:
            notebook_id: The notebook ID.

        Returns:
            List of source IDs. Empty list if no sources or on error.

        Note:
            Source IDs are triple-nested in RPC: source[0][0] contains the ID.
        """
        params = [notebook_id, None, [2], None, 0]
        notebook_data = await self.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        source_ids: list[str] = []
        if not notebook_data or not isinstance(notebook_data, list):
            return source_ids

        # Schema-drift detection points: log WARNING at each isinstance/len
        # guard that fails on a non-empty response (real drift surfaces here,
        # not at the safety-net except below).
        try:
            if not isinstance(notebook_data[0], list):
                # notebook_data is already known to be a non-empty list here
                # (guarded by `if not notebook_data` above).
                logger.warning(
                    "get_source_ids: notebook_data[0] shape unexpected for %s "
                    "(schema drift?). top-type=%s",
                    notebook_id,
                    type(notebook_data[0]).__name__,
                )
                return source_ids

            notebook_info = notebook_data[0]
            if not (len(notebook_info) > 1 and isinstance(notebook_info[1], list)):
                logger.warning(
                    "get_source_ids: notebook_info[1] not list for %s (schema drift?). len=%d",
                    notebook_id,
                    len(notebook_info),
                )
                return source_ids

            sources = notebook_info[1]
            for source in sources:
                if not (isinstance(source, list) and source):
                    continue
                first = source[0]
                if not (isinstance(first, list) and first):
                    continue
                sid = first[0]
                if isinstance(sid, str):
                    source_ids.append(sid)
        except (IndexError, TypeError) as e:
            # Defense-in-depth: guards above should make this unreachable.
            logger.warning(
                "get_source_ids: unexpected exception despite guards for %s: %s",
                notebook_id,
                e,
                exc_info=True,
            )

        return source_ids
