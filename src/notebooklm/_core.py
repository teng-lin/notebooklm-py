"""Core infrastructure for NotebookLM API client."""

import asyncio
import logging
import math
import random
import threading
import time
import warnings
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast
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

if TYPE_CHECKING:
    from .types import ConnectionLimits

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
    resolve_rpc_id,
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


@dataclass(frozen=True)
class _AuthSnapshot:
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


class _TransportAuthExpired(Exception):
    """Raised by ``_perform_authed_post`` when the refresh callback itself
    failed during an auth recovery attempt.

    ``original`` is the transport-layer ``httpx.HTTPStatusError`` that
    triggered the refresh attempt — :func:`is_auth_error` only flags
    ``HTTPStatusError`` responses, so network-level ``RequestError``s never
    reach this path. The refresh callback's error is attached via
    ``__cause__``.

    Callers map this onto their own error domain:

    - ``rpc_call`` re-raises ``original`` so legacy callers that catch
      :class:`httpx.HTTPStatusError` keep working byte-for-byte.
    - ``query_post`` translates this into :class:`ChatError`.
    """

    def __init__(self, message: str, *, original: Exception):
        super().__init__(message)
        self.original = original


class _TransportRateLimited(Exception):
    """Raised by ``_perform_authed_post`` when the 429 retry budget is
    exhausted (or no retries are configured).

    ``retry_after`` carries the parsed (clamped) Retry-After value when the
    server provided one; ``response`` is the final httpx response so callers
    can read status / reason for their own error message.
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


class _TransportServerError(Exception):
    """Raised by ``_perform_authed_post`` when the server-error retry budget
    is exhausted.

    Covers two retryable failure modes that share an exponential-backoff
    schedule (synthesis C4 / T4):

    - ``isinstance(original, httpx.HTTPStatusError)`` with a 5xx status —
      the response is available via ``response`` / ``status_code``.
    - ``isinstance(original, httpx.RequestError)`` — a network-layer failure
      (timeout, connect error, ...). ``response`` and ``status_code`` are
      ``None`` in this case.

    Callers map this onto their own error domain:

    - ``rpc_call`` translates this back into the historical :class:`ServerError`
      / :class:`NetworkError` shapes the RPC API has always raised.
    - ``query_post`` translates this into :class:`ChatError` /
      :class:`NetworkError` to match the chat API contract.
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


# Build-request factory: receives a fresh ``_AuthSnapshot`` and returns the
# triple (url, body, extra_headers) for one HTTP attempt. ``_perform_authed_post``
# invokes this once per attempt so refreshed snapshots are picked up on retry.
_BuildRequest = Callable[[_AuthSnapshot], tuple[str, str, dict[str, str]]]


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
        server_error_max_retries: int = 3,
        limits: "ConnectionLimits | None" = None,
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
            server_error_max_retries: Max automatic retries for retryable transient
                transport failures: HTTP 5xx responses and network-layer
                ``httpx.RequestError`` (timeouts, connect errors). Defaults to
                ``3``. Uses exponential backoff ``min(2 ** attempt, 30)``
                seconds — 5xx responses rarely carry ``Retry-After``, so the
                429 model doesn't apply. Set to ``0`` to disable. Refresh-path
                errors (400/401/403) are NOT covered here; those follow the
                existing auth-refresh-and-retry flow.
            limits: HTTP connection-pool tuning (``ConnectionLimits``). ``None``
                (default) constructs a ``ConnectionLimits()`` with defaults
                sized for typical batchexecute fan-out (max_connections=100,
                max_keepalive_connections=50, keepalive_expiry=30.0). Pass an
                explicit ``ConnectionLimits(...)`` to widen the pool for
                heavy batch workloads (e.g. FastAPI/Django services that
                share one client across many concurrent requests).

        Raises:
            ValueError: If ``keepalive`` or ``keepalive_min_interval`` is not a
                positive finite number.
        """
        # Lazy import to break the types.py -> _core.py cycle.
        from .types import ConnectionLimits

        self.auth = auth
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._limits = limits if limits is not None else ConnectionLimits()
        self._refresh_callback = refresh_callback
        self._refresh_retry_delay = refresh_retry_delay
        if rate_limit_max_retries < 0:
            raise ValueError(f"rate_limit_max_retries must be >= 0, got {rate_limit_max_retries}")
        self._rate_limit_max_retries = rate_limit_max_retries
        if server_error_max_retries < 0:
            raise ValueError(
                f"server_error_max_retries must be >= 0, got {server_error_max_retries}"
            )
        self._server_error_max_retries = server_error_max_retries
        self._refresh_lock: asyncio.Lock | None = asyncio.Lock() if refresh_callback else None
        self._refresh_task: asyncio.Task[AuthTokens] | None = None
        self._http_client: httpx.AsyncClient | None = None
        # Request ID counter for chat API (must be unique per request).
        # Access via the ``next_reqid()`` async method, which guards mutation
        # under ``_reqid_lock``. Direct mutation through the ``_reqid_counter``
        # property setter emits a ``DeprecationWarning``; bypass the warning
        # for legitimate test setup by writing to ``_reqid_counter_value``.
        self._reqid_counter_value: int = 100000
        # Lazily-created — ``asyncio.Lock()`` needs a running loop in some
        # Python versions, and this object can be constructed outside one.
        self._reqid_lock: asyncio.Lock | None = None
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
        # Leader/follower polling-dedupe registry for
        # ``ArtifactsAPI.wait_for_completion`` (audit §21 / T7.E2).
        # Keyed by ``(notebook_id, task_id)``. Each entry is a
        # ``(future, task)`` pair: the first caller for a key is the
        # *leader* and spawns the ``task`` (a shielded ``_poll_loop`` task);
        # subsequent callers (*followers*) attach to ``future`` via
        # ``asyncio.shield(future)`` so their per-caller cancellations
        # don't propagate to the underlying poll. The task reference is
        # kept alongside the future so the running poll task can't be
        # GC'd if the leader is cancelled with no followers attached
        # (Python's task-GC contract is permissive). Per-instance —
        # never module-global, so a fresh ``ClientCore`` cannot inherit
        # a dangling entry from a prior instance.
        self._pending_polls: dict[
            tuple[str, str], tuple[asyncio.Future[Any], asyncio.Task[Any]]
        ] = {}

    # ------------------------------------------------------------------
    # Request-id counter (chat API requires a monotonic ``_reqid`` URL param).
    #
    # Historical contract: callers did ``self._core._reqid_counter += 100000``
    # then read the new value. Two concurrent ``ChatAPI.ask`` calls on the same
    # core would race on the read-modify-write, producing duplicate ``_reqid``
    # values that Google rejects (audit C3 / synthesis §6 Tier-2 item 2).
    #
    # New contract: ``await core.next_reqid()`` performs the increment under
    # ``_reqid_lock`` and returns the post-increment value. The lock is
    # created lazily so a ``ClientCore`` can be constructed outside a running
    # event loop. Direct mutation of ``_reqid_counter`` still works for
    # backwards compatibility but emits ``DeprecationWarning``.
    # ------------------------------------------------------------------

    @property
    def _reqid_counter(self) -> int:
        """Current request-id counter value. Read access is safe; write access
        via the property setter emits ``DeprecationWarning``.
        """
        return self._reqid_counter_value

    @_reqid_counter.setter
    def _reqid_counter(self, value: int) -> None:
        warnings.warn(
            "Direct mutation of ClientCore._reqid_counter is deprecated; "
            "use `await core.next_reqid()` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._reqid_counter_value = value

    async def next_reqid(self, step: int = 100000) -> int:
        """Atomically increment the request-id counter and return the new value.

        Args:
            step: Increment applied to the counter. Defaults to ``100000`` to
                match the historical bump used by ``ChatAPI.ask``. Must be a
                positive ``int`` (not ``bool``); ``step <= 0`` would break
                monotonicity / uniqueness guarantees that Google's chat
                backend relies on.

        Returns:
            The post-increment counter value. Successive calls return strictly
            monotonic, distinct values even under ``asyncio.gather``.

        Raises:
            TypeError: If ``step`` is not an ``int`` (bool is rejected even
                though it is a subclass of ``int``).
            ValueError: If ``step`` is not positive.
        """
        # ``bool`` is a subclass of ``int`` in Python — reject it explicitly so
        # ``next_reqid(step=True)`` doesn't silently degrade to ``step=1``.
        if not isinstance(step, int) or isinstance(step, bool):
            raise TypeError(f"step must be int, got {type(step).__name__}")
        if step <= 0:
            raise ValueError(f"step must be positive, got {step!r}")
        # Safe: no await between check and assign, so no other coroutine can race us here.
        if self._reqid_lock is None:
            # Lazy init — safe to construct here because we're already in an
            # async context (caller is awaiting us).
            self._reqid_lock = asyncio.Lock()
        async with self._reqid_lock:
            self._reqid_counter_value += step
            return self._reqid_counter_value

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
                limits=self._limits.to_httpx_limits(),
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

        Cancellation safety (T7.B4 / audit §7):
        the entire close sequence is wrapped in ``try/finally`` and the
        final ``self._http_client.aclose()`` is wrapped in
        ``asyncio.shield`` — without the shield, a ``CancelledError``
        arriving during keepalive teardown or the cookie save would
        skip ``aclose()`` and leak the underlying httpx transport.
        ``self._http_client = None`` runs in an inner ``finally`` so
        the instance is consistently marked closed even if the
        shielded ``aclose`` itself raises.
        """
        try:
            # Stop the keepalive task before tearing down the HTTP client so
            # the loop can't issue a poke against an already-closed transport.
            if self._keepalive_task is not None:
                self._keepalive_task.cancel()
                await asyncio.gather(self._keepalive_task, return_exceptions=True)
                self._keepalive_task = None

            if self._http_client:
                try:
                    # Single source of truth for the on-close save: takes the
                    # in-process lock, snapshots, off-loads. Serializes
                    # naturally with any keepalive save still finishing in a
                    # worker thread — close() owns the freshest jar and must
                    # win, not the older snapshot.
                    await self.save_cookies(self._http_client.cookies)
                except Exception as e:
                    logger.warning("Failed to sync refreshed cookies during close: %s", e)
        finally:
            if self._http_client:
                try:
                    # Shield: cancellation arriving mid-aclose must not leak
                    # the transport. The shielded aclose runs to completion;
                    # ``self._http_client = None`` then makes ``is_open``
                    # return False correctly.
                    await asyncio.shield(self._http_client.aclose())
                finally:
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

    def _snapshot(self) -> _AuthSnapshot:
        """Capture the current auth headers as a frozen snapshot.

        Used by ``_perform_authed_post`` to make a single HTTP attempt's
        URL/body consistent (no mid-attempt mutation from refresh /
        keepalive). A fresh snapshot is taken on each retry.
        """
        return _AuthSnapshot(
            csrf_token=self.auth.csrf_token,
            session_id=self.auth.session_id,
            authuser=self.auth.authuser,
            account_email=self.auth.account_email,
        )

    def _build_url(
        self,
        rpc_method: RPCMethod,
        source_path: str = "/",
        rpc_id_override: str | None = None,
    ) -> str:
        """Build the batchexecute URL for an RPC call.

        Args:
            rpc_method: The RPC method to call.
            source_path: The source path parameter (usually notebook path).
            rpc_id_override: Optional resolved RPC id string used in the
                ``rpcids=`` query param. When provided, the SAME string must
                also be passed to :func:`encode_rpc_request` so the URL and
                body stay in sync. See ``resolve_rpc_id`` for the
                ``NOTEBOOKLM_RPC_OVERRIDES`` plumbing.

        Returns:
            Full URL with query parameters.
        """
        rpc_id = rpc_id_override if rpc_id_override is not None else rpc_method.value
        params: dict[str, str] = {
            "rpcids": rpc_id,
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

    async def _perform_authed_post(
        self,
        *,
        build_request: _BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        """Run an authed POST through the shared retry/refresh pipeline.

        The pipeline is the transport-level core that both ``rpc_call`` and
        ``query_post`` share. Per-attempt behavior:

        1. Take a fresh ``_AuthSnapshot`` via :meth:`_snapshot`.
        2. Invoke ``build_request(snapshot)`` to assemble ``(url, body,
           extra_headers)``. The factory is called *once per attempt* so that
           retries pick up refreshed credentials instead of replaying a stale
           pre-refresh URL/body — see synthesis §6 Tier-2 item 4.
        3. POST via the underlying ``httpx.AsyncClient`` and call
           ``raise_for_status()``.

        Error-boundary contract (callers must wrap into their own typed
        exceptions):

        - **Auth refresh path** — when a refresh callback is configured and
          the failure looks like an auth error (HTTP 400/401/403, see
          :func:`is_auth_error`), the helper awaits a shared refresh task and
          retries once with a fresh snapshot. If the refresh callback itself
          raises, the original transport exception is wrapped in
          :class:`_TransportAuthExpired` (refresh error chained via
          ``__cause__``) so callers can re-raise the original unchanged
          (``rpc_call``) or translate to their own typed error
          (``query_post``). If the post-refresh retry's POST fails for a
          non-auth reason, that exception propagates as-is.
        - **Rate-limit path** — on HTTP 429 with a parseable Retry-After,
          sleeps and retries until ``rate_limit_max_retries`` is reached;
          after that, raises :class:`_TransportRateLimited` with the final
          response and parsed retry-after value. With no parseable header or
          ``rate_limit_max_retries == 0``, raises immediately.
        - **Server-error path** — on HTTP 5xx, or any ``httpx.RequestError``
          (network-layer failures: timeouts, connect errors), sleeps with
          exponential backoff ``min(2 ** attempt, 30)`` seconds and retries
          until ``server_error_max_retries`` is reached; after that, raises
          :class:`_TransportServerError`. ``server_error_max_retries == 0``
          short-circuits to an immediate raise. This path does NOT honor
          ``Retry-After`` because 5xx rarely carries it.
        - All other errors propagate as :class:`httpx.HTTPStatusError` /
          :class:`httpx.RequestError` unchanged.

        Caller responsibilities:

        - Manage the ``set_request_id`` context (so retries within a single
          logical call share one ``[req=<id>]`` tag).
        - Decode the response (this helper does no parsing).
        - Wrap transport exceptions into the caller's error domain:
          ``rpc_call`` maps into :class:`RPCError`-family exceptions;
          ``query_post`` maps into :class:`ChatError` / :class:`NetworkError`.

        Args:
            build_request: Factory invoked once per attempt with a fresh
                ``_AuthSnapshot``. Must return ``(url, body, extra_headers)``.
                ``extra_headers`` is merged onto the httpx client's defaults
                for this attempt only.
            log_label: Caller-friendly label embedded in log lines (e.g. an
                RPC method name or ``"chat.ask"``).

        Returns:
            The raw ``httpx.Response`` from the successful attempt. The
            caller owns decoding.
        """
        assert self._http_client is not None
        client = self._http_client

        refreshed_this_call = False
        rate_limit_retries = 0
        server_error_retries = 0
        start = time.perf_counter()

        while True:
            snapshot = self._snapshot()
            url, body, headers = build_request(snapshot)

            try:
                if headers:
                    response = await client.post(url, content=body, headers=headers)
                else:
                    response = await client.post(url, content=body)
                response.raise_for_status()
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                # --- Auth refresh path ---------------------------------
                if (
                    not refreshed_this_call
                    and self._refresh_callback is not None
                    and is_auth_error(exc)
                ):
                    logger.info(
                        "%s auth error detected, attempting token refresh",
                        log_label,
                    )
                    try:
                        await self._await_refresh()
                    except Exception as refresh_error:
                        logger.warning("Token refresh failed: %s", refresh_error)
                        # Signal "refresh failed" to the caller via a typed
                        # transport exception so the RPC mapper can re-raise
                        # the *original* HTTPStatusError unchanged (matches
                        # the historical ``_try_refresh_and_retry`` contract
                        # — see ``test_no_retry_on_cookie_expiration``).
                        raise _TransportAuthExpired(
                            f"auth refresh failed for {log_label}",
                            original=exc,
                        ) from refresh_error
                    if self._refresh_retry_delay > 0:
                        await asyncio.sleep(self._refresh_retry_delay)
                    logger.info("Token refresh successful, retrying %s", log_label)
                    refreshed_this_call = True
                    # Loop around: next iteration takes a FRESH snapshot,
                    # rebuilds the request body with the new csrf/sid, and
                    # re-POSTs.
                    continue

                # --- 429 rate-limit path --------------------------------
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                    retry_after = _parse_retry_after(exc.response.headers.get("retry-after"))
                    # ``disable_internal_retries`` (T7.B2) suppresses the 429
                    # retry loop for declared mutating create RPCs whose retries
                    # would risk duplicate-resource creation. The API-layer
                    # ``_idempotency.idempotent_create`` wrapper owns the
                    # probe-then-retry loop instead.
                    if (
                        not disable_internal_retries
                        and retry_after is not None
                        and rate_limit_retries < self._rate_limit_max_retries
                    ):
                        logger.warning(
                            "%s rate-limited (HTTP 429); sleeping %ds then retrying (%d/%d)",
                            log_label,
                            retry_after,
                            rate_limit_retries + 1,
                            self._rate_limit_max_retries,
                        )
                        await asyncio.sleep(retry_after)
                        rate_limit_retries += 1
                        continue
                    raise _TransportRateLimited(
                        f"{log_label} rate-limited (HTTP 429)",
                        retry_after=retry_after,
                        response=exc.response,
                        original=exc,
                    ) from exc

                # --- 5xx / network retry path -------------------------------
                # Exponential backoff: 5xx responses rarely carry Retry-After
                # so we don't use the 429 model. ``httpx.RequestError`` covers
                # transient network-layer failures (timeouts, connect errors,
                # remote-protocol blips) that are reasonable to retry.
                is_server_error = (
                    isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600
                )
                is_network_error = isinstance(exc, httpx.RequestError)
                if is_server_error or is_network_error:
                    # ``disable_internal_retries`` (T7.B2) short-circuits the
                    # 5xx / network retry loop for declared mutating create
                    # RPCs (e.g. CREATE_NOTEBOOK, ADD_SOURCE) where a naive
                    # re-POST after a server commit would duplicate the
                    # resource. The API-layer ``idempotent_create`` wrapper
                    # owns the probe-then-retry loop instead.
                    if (
                        not disable_internal_retries
                        and server_error_retries < self._server_error_max_retries
                    ):
                        # Exponential backoff capped at 30s. The cap blunts
                        # thundering-herd well past the first few retries
                        # (every retry beyond ~5 attempts waits exactly 30s),
                        # but the early retries (1s, 2s, 4s, ...) can still
                        # synchronize across clients that all failed on the
                        # same transient backend blip. Add a small ±20% jitter
                        # so concurrent retries are spread out.
                        backoff = min(2**server_error_retries, 30)
                        backoff += random.uniform(-0.2 * backoff, 0.2 * backoff)  # noqa: S311  # nosec B311 — jitter, not crypto
                        backoff = max(0.1, backoff)
                        status_label = (
                            f"HTTP {exc.response.status_code}"  # type: ignore[union-attr]
                            if is_server_error
                            else type(exc).__name__
                        )
                        logger.warning(
                            "%s server/network error (%s); backing off %.1fs then retrying (%d/%d)",
                            log_label,
                            status_label,
                            backoff,
                            server_error_retries + 1,
                            self._server_error_max_retries,
                        )
                        await asyncio.sleep(backoff)
                        server_error_retries += 1
                        continue
                    if is_server_error:
                        status_error = cast(httpx.HTTPStatusError, exc)
                        raise _TransportServerError(
                            f"{log_label} server error "
                            f"(HTTP {status_error.response.status_code}) after "
                            f"{server_error_retries} retries",
                            original=status_error,
                            response=status_error.response,
                            status_code=status_error.response.status_code,
                        ) from exc
                    raise _TransportServerError(
                        f"{log_label} network error after {server_error_retries} retries: {exc}",
                        original=exc,
                    ) from exc

                # --- Anything else: propagate the raw transport error ----
                elapsed = time.perf_counter() - start
                logger.debug(
                    "%s transport error after %.3fs: %s",
                    log_label,
                    elapsed,
                    exc,
                )
                raise

            # Success
            return response

    async def _await_refresh(self) -> None:
        """Run / join the shared refresh task.

        Concurrent callers share one refresh task so a thundering herd of
        401s on the same client triggers exactly one token refresh. The lock
        protects task-creation only; the await on the task itself happens
        outside the lock so other callers can join.

        The join is wrapped in :func:`asyncio.shield` (T7.C1, audit §4) so
        that a caller cancelled while waiting — e.g. via
        ``asyncio.wait_for(..., timeout=...)`` — unwinds locally without
        propagating the ``CancelledError`` into the *shared* refresh task.
        Without the shield, one cancelled waiter would cancel the
        underlying task, taking down every sibling joined to the same
        single-flight refresh. The slot at ``self._refresh_task`` is left
        intact across the cancellation and is replaced only on the next
        refresh wave once the current task transitions to ``done()``.
        """
        assert self._refresh_callback is not None
        assert self._refresh_lock is not None

        async with self._refresh_lock:
            if self._refresh_task is not None and not self._refresh_task.done():
                refresh_task = self._refresh_task
                logger.debug("Joining existing refresh task")
            else:
                coro = cast(Coroutine[Any, Any, AuthTokens], self._refresh_callback())
                self._refresh_task = asyncio.create_task(coro)
                refresh_task = self._refresh_task

        await asyncio.shield(refresh_task)

    async def query_post(
        self,
        *,
        build_request: _BuildRequest,
        parse_label: str,
    ) -> httpx.Response:
        """Chat-side semantic owner around :meth:`_perform_authed_post`.

        Wraps the shared transport pipeline with chat-flavored exception
        mapping: transport-layer auth failures become
        :class:`~notebooklm.exceptions.ChatError`, and transport-layer
        network/rate-limit failures become
        :class:`~notebooklm.exceptions.NetworkError` /
        :class:`~notebooklm.exceptions.ChatError` respectively. This keeps
        ChatAPI free of HTTP-status branching and matches the historical
        contract of ``ChatAPI.ask`` (T2.D will migrate that caller).

        Args:
            build_request: See :meth:`_perform_authed_post`.
            parse_label: Caller-friendly label used in log lines and error
                messages (e.g. ``"chat.ask"``).
        """
        # Import here to avoid a circular import: exceptions imports from
        # this module's siblings.
        from .exceptions import ChatError, NetworkError

        try:
            return await self._perform_authed_post(
                build_request=build_request,
                log_label=parse_label,
            )
        except _TransportAuthExpired as exc:
            raise ChatError(
                f"{parse_label} failed: authentication expired and refresh did not recover"
            ) from exc
        except _TransportRateLimited as exc:
            raise ChatError(
                f"{parse_label} rate-limited (HTTP 429)."
                + (
                    f" Retry after {exc.retry_after} seconds."
                    if exc.retry_after is not None
                    else ""
                )
            ) from exc
        except _TransportServerError as exc:
            if isinstance(exc.original, httpx.HTTPStatusError):
                raise ChatError(
                    f"{parse_label} failed with HTTP {exc.original.response.status_code} "
                    f"after retries: {exc.original}"
                ) from exc
            # Network-layer failure (RequestError / Timeout).
            # ``_perform_authed_post`` only wraps ``httpx.RequestError`` into
            # ``_TransportServerError`` on the network path; this guard keeps
            # the contract enforced under ``python -O`` (where ``assert``
            # would be stripped) and gives a clear diagnostic if the
            # invariant ever drifts.
            if not isinstance(exc.original, httpx.RequestError):
                raise TypeError(
                    f"Unexpected _TransportServerError.original type: {type(exc.original)}"
                ) from exc
            # Preserve the timeout-specific message: TimeoutException is a
            # subclass of RequestError, so without this branch read/connect
            # timeouts would surface as a generic "network error after
            # retries" line and lose the "timed out" signal callers rely on.
            if isinstance(exc.original, httpx.TimeoutException):
                raise NetworkError(
                    f"{parse_label} timed out after retries: {exc.original}",
                    original_error=exc.original,
                ) from exc
            raise NetworkError(
                f"{parse_label} network error after retries: {exc.original}",
                original_error=exc.original,
            ) from exc
        except httpx.HTTPStatusError as exc:
            # Non-5xx / non-401 / non-429 status errors fall through
            # ``_perform_authed_post``'s "Anything else" branch (e.g. a 404
            # or unhandled 4xx).
            raise ChatError(
                f"{parse_label} failed with HTTP {exc.response.status_code}: {exc}"
            ) from exc
        # NOTE: bare ``httpx.TimeoutException`` / ``httpx.RequestError``
        # handlers were removed here because ``_perform_authed_post`` always
        # either retries those errors or wraps them in
        # ``_TransportServerError`` (handled above), so they cannot reach
        # this scope.

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any:
        """Make an RPC call to the NotebookLM API.

        Automatically refreshes authentication tokens and retries once if an
        auth failure is detected and a refresh_callback was provided.

        Args:
            method: The RPC method to call.
            params: Parameters for the RPC call (nested list structure).
            source_path: The source path parameter (usually /notebook/{id}).
            allow_null: If True, don't raise error when response is null.
            _is_retry: Internal flag to prevent infinite decode-time retries.
            disable_internal_retries: When True, suppresses the inner 5xx /
                429 / network retry loop in ``_perform_authed_post`` so that
                the first transport-level failure surfaces immediately. Used
                by declared mutating create RPCs (T7.B2): a naive re-POST
                after a server-side commit would duplicate the resource, so
                the API-layer ``_idempotency.idempotent_create`` wrapper
                owns the probe-then-retry loop instead. The auth-refresh
                path is unaffected (a 401 → refresh → retry is still legal
                because the request was rejected, not accepted).

        Returns:
            Decoded response data.

        Raises:
            RuntimeError: If client is not initialized (not in context manager).
            httpx.HTTPStatusError: If HTTP request fails.
            RPCError: If RPC call fails or returns unexpected data.
        """
        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")

        # Only the outer rpc_call mints a request id; the decode-time retry
        # path (``_is_retry=True``) inherits the parent's id so a single
        # decode-error → refresh → retry sequence appears under one
        # ``[req=<id>]`` in the logs. HTTP-status retries (auth + 429) happen
        # inside ``_perform_authed_post`` without recursion, so they don't
        # need this guard.
        if _is_retry:
            return await self._rpc_call_impl(
                method,
                params,
                source_path,
                allow_null,
                _is_retry,
                disable_internal_retries=disable_internal_retries,
            )

        _reqid_token = set_request_id()
        try:
            return await self._rpc_call_impl(
                method,
                params,
                source_path,
                allow_null,
                _is_retry,
                disable_internal_retries=disable_internal_retries,
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
        *,
        disable_internal_retries: bool = False,
    ) -> Any:
        # Caller (rpc_call) has already verified self._http_client is not None;
        # re-assert for mypy narrowing through this helper.
        assert self._http_client is not None
        start = time.perf_counter()
        logger.debug("RPC %s starting", method.name)

        # Resolve the RPC id ONCE per logical call. ``NOTEBOOKLM_RPC_OVERRIDES``
        # lets users self-patch when Google rotates an obfuscated method id;
        # the resolved value MUST flow into both the URL's ``rpcids=`` query
        # param and the request body's ``f.req`` payload (the wire format
        # treats a mismatch as malformed). Resolving once also means decode
        # below uses the same id we asked the server for.
        resolved_id = resolve_rpc_id(method.name, method.value)

        # ``_perform_authed_post`` calls this factory once per HTTP attempt;
        # on retry it passes a fresh snapshot so the body is rebuilt with the
        # refreshed CSRF and the URL with the refreshed session id /
        # authuser. Capturing ``self.auth.csrf_token`` here directly would
        # snapshot at outer-call time and replay a stale token on retry.
        rpc_request = encode_rpc_request(method, params, rpc_id_override=resolved_id)

        def _build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            # Deliberate divergence: the body uses the snapshot's csrf_token
            # (a frozen point-in-time copy) while ``_build_url`` reads
            # ``self.auth.session_id`` / ``self.auth.authuser`` /
            # ``self.auth.account_email`` LIVE off ``self.auth``. The two
            # stay consistent only because the no-await invariant between
            # ``_snapshot()`` and the ``client.post(...)`` call inside
            # ``_perform_authed_post`` guarantees no coroutine can mutate
            # ``self.auth`` between snapshot capture and request build (see
            # the AST guard in ``tests/unit/test_concurrency_refresh_race.py``
            # — adding an ``await`` anywhere in this factory or in
            # ``_build_url`` would silently desync URL from body across a
            # refresh). The snapshot's session_id/authuser/account_email
            # fields are carried for symmetry / future-proofing but are not
            # the source of truth on this code path.
            url = self._build_url(method, source_path, rpc_id_override=resolved_id)
            body = build_request_body(rpc_request, snapshot.csrf_token)
            return url, body, {}

        try:
            response = await self._perform_authed_post(
                build_request=_build,
                log_label=f"RPC {method.name}",
                disable_internal_retries=disable_internal_retries,
            )
        except _TransportAuthExpired as exc:
            # Refresh callback raised. Historical contract:
            # the *original* transport exception escapes with the refresh
            # error attached via ``__cause__`` (already chained inside
            # ``_perform_authed_post``). No status-code mapping happens for
            # this path — callers that catch :class:`httpx.HTTPStatusError`
            # see exactly what they used to see pre-extraction.
            raise exc.original from exc.__cause__
        except _TransportRateLimited as exc:
            elapsed = time.perf_counter() - start
            logger.error(
                "RPC %s failed after %.3fs: HTTP 429",
                method.name,
                elapsed,
            )
            msg = f"API rate limit exceeded calling {method.name}"
            if exc.retry_after:
                msg += f". Retry after {exc.retry_after} seconds"
            raise RateLimitError(
                msg,
                method_id=method.value,
                retry_after=exc.retry_after,
            ) from exc.original
        except _TransportServerError as exc:
            elapsed = time.perf_counter() - start
            # Translate the budget-exhaustion signal back into the historical
            # RPC error shape: 5xx → ServerError; network → NetworkError /
            # RPCTimeoutError. ``_raise_rpc_error_from_*`` already does the
            # right mapping for the underlying ``original`` exception.
            if isinstance(exc.original, httpx.HTTPStatusError):
                logger.error(
                    "RPC %s failed after %.3fs: HTTP %s (server-error retries exhausted)",
                    method.name,
                    elapsed,
                    exc.original.response.status_code,
                )
                self._raise_rpc_error_from_http_status(exc.original, method)
            else:
                # ``_perform_authed_post`` only wraps ``httpx.RequestError``
                # into ``_TransportServerError`` on the network path; this
                # guard keeps the contract enforced under ``python -O``
                # (where ``assert`` would be stripped).
                if not isinstance(exc.original, httpx.RequestError):
                    raise TypeError(
                        f"Unexpected _TransportServerError.original type: {type(exc.original)}"
                    ) from exc
                logger.error(
                    "RPC %s failed after %.3fs: %s (server-error retries exhausted)",
                    method.name,
                    elapsed,
                    exc.original,
                )
                self._raise_rpc_error_from_request_error(exc.original, method)
        except httpx.HTTPStatusError as exc:
            elapsed = time.perf_counter() - start
            logger.error(
                "RPC %s failed after %.3fs: HTTP %s",
                method.name,
                elapsed,
                exc.response.status_code,
            )
            self._raise_rpc_error_from_http_status(exc, method)
        # NOTE: bare ``httpx.RequestError`` handler was removed here because
        # ``_perform_authed_post`` always either retries network-layer
        # errors or wraps them in ``_TransportServerError`` (handled above),
        # so they cannot reach this scope.

        # ---------- Decode -------------------------------------------------
        # Decode-time auth retry stays RPC-specific: Google sometimes
        # returns a 200 with an auth-shaped RPCError payload (the body says
        # "authentication expired" instead of a 401 status code). The chat
        # streaming format doesn't have this pattern, so the retry lives
        # here, not in ``_perform_authed_post``.
        try:
            # The server echoes back whatever RPC id we sent on the wire, so
            # decode against the resolved id (override-aware) rather than the
            # canonical ``method.value`` — otherwise an override would parse
            # as "RPC id not found in response".
            result = decode_response(response.text, resolved_id, allow_null=allow_null)
            elapsed = time.perf_counter() - start
            logger.debug("RPC %s completed in %.3fs", method.name, elapsed)
            return result
        except RPCError as e:
            elapsed = time.perf_counter() - start

            # Check if this is an auth error and we can retry
            if not _is_retry and self._refresh_callback and is_auth_error(e):
                refreshed = await self._try_refresh_and_retry(
                    method,
                    params,
                    source_path,
                    allow_null,
                    e,
                    disable_internal_retries=disable_internal_retries,
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

    def _raise_rpc_error_from_http_status(
        self,
        exc: httpx.HTTPStatusError,
        method: RPCMethod,
    ) -> NoReturn:
        """Map an HTTP-status failure onto the RPC error hierarchy.

        Centralizes the status-to-exception mapping that historically lived
        inline in ``_rpc_call_impl``. Always raises — typed ``NoReturn`` so
        mypy sees the caller's control flow terminates here.
        """
        status = exc.response.status_code

        if status == 429:
            # _perform_authed_post normally raises ``_TransportRateLimited``
            # before reaching here. This branch covers callers that bypass
            # the helper or a 429 surfacing after an auth retry.
            retry_after = _parse_retry_after(exc.response.headers.get("retry-after"))
            msg = f"API rate limit exceeded calling {method.name}"
            if retry_after:
                msg += f". Retry after {retry_after} seconds"
            raise RateLimitError(msg, method_id=method.value, retry_after=retry_after) from exc

        if 500 <= status < 600:
            raise ServerError(
                f"Server error {status} calling {method.name}: {exc.response.reason_phrase}",
                method_id=method.value,
                status_code=status,
            ) from exc

        if 400 <= status < 500 and status not in (401, 403):
            raise ClientError(
                f"Client error {status} calling {method.name}: {exc.response.reason_phrase}",
                method_id=method.value,
                status_code=status,
            ) from exc

        # 401/403 or other: Generic RPCError (auth retry already attempted by
        # _perform_authed_post when a refresh callback was configured).
        raise RPCError(
            f"HTTP {status} calling {method.name}: {exc.response.reason_phrase}",
            method_id=method.value,
        ) from exc

    def _raise_rpc_error_from_request_error(
        self,
        exc: httpx.RequestError,
        method: RPCMethod,
    ) -> NoReturn:
        """Map a non-HTTPStatus transport failure onto NetworkError/RPCTimeoutError.

        Always raises — typed ``NoReturn`` so mypy treats the caller's
        control flow as terminating here, preventing an unbound-``response``
        warning in the decode block of ``_rpc_call_impl``.
        """
        # Check ConnectTimeout first (more specific than general TimeoutException)
        if isinstance(exc, httpx.ConnectTimeout):
            raise NetworkError(
                f"Connection timed out calling {method.name}: {exc}",
                method_id=method.value,
                original_error=exc,
            ) from exc

        if isinstance(exc, httpx.TimeoutException):
            raise RPCTimeoutError(
                f"Request timed out calling {method.name}",
                method_id=method.value,
                timeout_seconds=self._timeout,
                original_error=exc,
            ) from exc

        if isinstance(exc, httpx.ConnectError):
            raise NetworkError(
                f"Connection failed calling {method.name}: {exc}",
                method_id=method.value,
                original_error=exc,
            ) from exc

        raise NetworkError(
            f"Request failed calling {method.name}: {exc}",
            method_id=method.value,
            original_error=exc,
        ) from exc

    async def _try_refresh_and_retry(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str,
        allow_null: bool,
        original_error: Exception,
        *,
        disable_internal_retries: bool = False,
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
            disable_internal_retries: When True, suppress the inner 5xx /
                429 / network retry loop on the post-refresh ``rpc_call``,
                so transport failures are surfaced immediately for the
                caller's idempotency wrapper to handle.

        Returns:
            The RPC result if retry succeeds, None if refresh failed.

        Raises:
            The original error (with refresh error as cause) if refresh fails.
        """
        logger.info(
            "RPC %s auth error detected, attempting token refresh",
            method.name,
        )

        # Delegate the shared-task + lock dance to ``_await_refresh`` so this
        # decode-time retry path stays in lockstep with the transport-time
        # path inside ``_perform_authed_post``. On refresh failure, surface
        # the *original* RPC decode error (not the refresh error) so callers
        # see the symptom they originally hit — refresh failure is attached
        # as ``__cause__``.
        try:
            await self._await_refresh()
        except Exception as refresh_error:
            logger.warning("Token refresh failed: %s", refresh_error)
            raise original_error from refresh_error

        # Brief delay before retry to avoid hammering the API
        if self._refresh_retry_delay > 0:
            await asyncio.sleep(self._refresh_retry_delay)

        logger.info("Token refresh successful, retrying RPC %s", method.name)

        # Retry with refreshed tokens
        return await self.rpc_call(
            method,
            params,
            source_path,
            allow_null,
            _is_retry=True,
            disable_internal_retries=disable_internal_retries,
        )

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
