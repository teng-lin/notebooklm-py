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
from contextlib import AbstractAsyncContextManager, nullcontext
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

# Default ceiling on concurrent in-flight ``SourcesAPI.add_file`` uploads.
# Each in-flight upload holds one open file descriptor for the duration of
# the upload, so the cap is also an FD-exhaustion guard (see T7.D3 /
# audit §23). Sized for typical interactive workloads; tune higher for
# batch ingestion pipelines that ingest dozens of files in parallel and
# have headroom in the process FD limit (``ulimit -n``).
DEFAULT_MAX_CONCURRENT_UPLOADS = 4

# Default ceiling on simultaneous in-flight ``_perform_authed_post``
# RPC POSTs (T7.H1 / audit §8). Sits *below* the default httpx pool
# size (``ConnectionLimits.max_connections=100``) so short-lived helper
# requests outside the RPC path — refresh GETs, resumable-upload
# preflights — have pool headroom even when the RPC semaphore is
# saturated. The default is intentionally conservative because
# batchexecute itself rate-limits aggressive fan-out; callers with a
# higher account tier (or an external rate-limiter) can opt out via
# ``max_concurrent_rpcs=None``.
DEFAULT_MAX_CONCURRENT_RPCS = 16

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
        rate_limit_max_retries: int = 3,
        server_error_max_retries: int = 3,
        limits: "ConnectionLimits | None" = None,
        max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
        max_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
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
            rate_limit_max_retries: Max automatic retries on HTTP 429.
                Defaults to ``3`` (T7.H2 / audit §11) so programmatic users
                inherit "smart retry" behavior without having to opt in. Set
                to ``0`` to restore the pre-T7.H2 contract of raising
                ``RateLimitError`` immediately. Each retry sleeps for the
                ``Retry-After`` value when the server provides a parseable
                header (clamped at ``MAX_RETRY_AFTER_SECONDS``); when the
                header is absent or unparseable, the loop falls back to
                capped exponential backoff ``min(2 ** attempt, 30)`` seconds
                with ±20% jitter, matching the 5xx path so the positive
                default is still useful when Google omits the hint.
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
            max_concurrent_uploads: Ceiling on simultaneous in-flight
                ``SourcesAPI.add_file`` uploads. Defaults to
                ``DEFAULT_MAX_CONCURRENT_UPLOADS`` (4). ``None`` resolves to
                the default — unbounded uploads are intentionally rejected
                because each in-flight upload holds one open file
                descriptor for the duration of the upload, and an
                unbounded fan-out exhausts the per-process FD limit (audit
                §23 / T7.D3). Must be ``>= 1`` when supplied. Independent
                of the RPC connection pool because uploads use their own
                ``httpx.AsyncClient`` (Scotty endpoint) and don't share
                the RPC pool.
            max_concurrent_rpcs: Ceiling on simultaneous in-flight
                ``_perform_authed_post`` RPC POSTs. Defaults to
                ``DEFAULT_MAX_CONCURRENT_RPCS`` (16) — well below the
                default httpx pool size (``max_connections=100``) so
                short-lived helper requests (refresh GETs, upload
                preflights) outside this gate still have pool headroom.
                Pass ``None`` to disable the gate entirely (callers with
                an external rate-limiter or single-shot CLI work).
                Must be ``>= 1`` when supplied. Pre-T7.H1 the gate did
                not exist; heavy fan-out workloads tripped opaque
                ``httpx.PoolTimeout`` errors before the connection pool
                could surface clean back-pressure (audit §8). Cross-
                validation with ``limits.max_connections`` is enforced at
                the ``NotebookLMClient`` boundary (so the constraint
                applies whether ``limits`` is explicit or auto-defaulted
                inside ``ClientCore``).

        Raises:
            ValueError: If ``keepalive`` or ``keepalive_min_interval`` is not a
                positive finite number, or if ``max_concurrent_uploads`` /
                ``max_concurrent_rpcs`` is a non-positive integer.
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
        # ``None`` resolves to the default (``DEFAULT_MAX_CONCURRENT_UPLOADS``)
        # rather than meaning "unbounded" — the FD-exhaustion guard is the
        # whole point of the knob; an unbounded fan-out of ``add_file`` would
        # exhaust the per-process FD limit before the upload semaphore could
        # save us (audit §23 / T7.D3). Reject ``<= 0`` loudly at construction
        # rather than allowing a silently-misconfigured pipeline.
        if max_concurrent_uploads is None:
            self._max_concurrent_uploads = DEFAULT_MAX_CONCURRENT_UPLOADS
        else:
            if max_concurrent_uploads < 1:
                raise ValueError(
                    f"max_concurrent_uploads must be >= 1, got {max_concurrent_uploads!r}"
                )
            self._max_concurrent_uploads = max_concurrent_uploads
        # Lazily-created (``asyncio.Semaphore()`` needs a running loop in
        # some Python versions, and ``ClientCore`` can be constructed
        # outside one). Use ``get_upload_semaphore()`` to fetch the live
        # semaphore on demand. Per-instance — never module-global — so two
        # ``NotebookLMClient`` instances in the same process have
        # independent upload budgets.
        self._upload_semaphore: asyncio.Semaphore | None = None
        # RPC-fanout throttle (T7.H1 / audit §8). ``None`` means "no
        # gate" (caller has an external rate-limiter, or this is a
        # single-shot CLI invocation). Default ``DEFAULT_MAX_CONCURRENT_RPCS``
        # (16) sits well below the default ``ConnectionLimits.max_connections``
        # so helper GET/POSTs outside the RPC pipeline still have pool
        # headroom. Cross-validation with ``limits.max_connections`` is
        # enforced one layer up at ``NotebookLMClient.__init__`` because
        # ``ClientCore`` synthesizes its own ``ConnectionLimits()`` when
        # ``limits=None``, masking the relationship at this layer.
        if max_concurrent_rpcs is None:
            self._max_concurrent_rpcs: int | None = None
        else:
            if max_concurrent_rpcs < 1:
                raise ValueError(f"max_concurrent_rpcs must be >= 1, got {max_concurrent_rpcs!r}")
            self._max_concurrent_rpcs = max_concurrent_rpcs
        # Lazily-created for the same reason as ``_upload_semaphore``
        # (``asyncio.Semaphore()`` binds to the running loop in some
        # Python versions). Per-instance, never module-global. When
        # ``_max_concurrent_rpcs is None``, the accessor returns a
        # ``contextlib.nullcontext`` instead — see ``_get_rpc_semaphore``.
        self._rpc_semaphore: asyncio.Semaphore | None = None
        # Lazily-created — ``asyncio.Lock()`` needs a running loop in some
        # Python versions, and ``ClientCore`` can be constructed outside one
        # (e.g. a sync-mode ``NotebookLMClient(...)`` instantiation before the
        # caller's ``asyncio.run``). Use :meth:`_get_refresh_lock` to fetch
        # the live lock on demand. Mirrors the ``_reqid_lock`` /
        # ``_auth_snapshot_lock`` lazy-init pattern (audit §13 / T7.G1).
        # The lock gates single-flight refresh-task creation in
        # :meth:`_await_refresh` — the assert on ``_refresh_callback is not
        # None`` there is the real precondition; this lock is allocated on
        # first refresh attempt regardless of whether a callback was wired,
        # because asyncio is single-threaded and the check-then-assign in
        # ``_get_refresh_lock`` is race-free without an outer lock.
        self._refresh_lock: asyncio.Lock | None = None
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
        # Serializes ``_AuthSnapshot`` reads in :meth:`_snapshot` with the
        # refresh-side mutation block in :meth:`NotebookLMClient.refresh_auth`
        # (audit §12 / T7.F2). The lock holds only across the four
        # ``self.auth.*`` scalar reads / two scalar writes — never across
        # an ``await`` — so RPC throughput isn't serialized to refresh
        # latency. Lazy-init mirrors ``_reqid_lock`` because ``asyncio.Lock()``
        # needs a running loop in some Python versions. Distinct from
        # ``_refresh_lock`` (which is owned by refresh-task creation and
        # held across ``await self._refresh_callback()``): mixing the two
        # would re-introduce the reentrancy ambiguity T7.F2 set out to
        # avoid.
        self._auth_snapshot_lock: asyncio.Lock | None = None
        # Event-loop affinity guard (audit §14 / T7.G2). Captured in
        # :meth:`open` and checked in :meth:`_perform_authed_post`; a cheap
        # ``is`` comparison fails fast when a caller drives the same
        # ``ClientCore`` from a different loop (typical mistake: instantiating
        # under ``asyncio.run`` in one thread, then handing the client to
        # another thread's loop). Each client is per-loop — the asyncio
        # primitives we hold (``_reqid_lock``, ``_refresh_lock``,
        # ``_auth_snapshot_lock``, ``_upload_semaphore``, ``_rpc_semaphore``,
        # the ``httpx.AsyncClient`` pool, in-flight tasks like
        # ``_refresh_task``/``_keepalive_task``) are all bound to the loop
        # that ``open()`` ran on; reusing them under a different loop
        # produces hangs and ``RuntimeError`` deep in httpx instead of an
        # actionable message at the call site.
        self._bound_loop: asyncio.AbstractEventLoop | None = None
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
        #
        # Contract (audit §17 / T7.G5):
        # ``_save_lock`` is acquired ONLY inside ``_save()`` (run on a worker
        # thread via ``asyncio.to_thread``); never held by an async context to
        # prevent priority inversion against the event loop. A blocking
        # ``threading.Lock`` held on the loop thread would stall every other
        # coroutine — including the keepalive heartbeat and the cancellation
        # path — while a sibling worker thread does file I/O. Keep all
        # acquisitions inside the worker closure passed to ``asyncio.to_thread``.
        # See ``tests/unit/test_save_lock_contract.py`` for the regression guard.
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

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        """Return the per-instance upload semaphore, creating it on first use.

        The semaphore caps the number of in-flight ``SourcesAPI.add_file``
        uploads at ``max_concurrent_uploads`` (default
        ``DEFAULT_MAX_CONCURRENT_UPLOADS``). Each in-flight upload holds
        one open file descriptor for its duration, so the cap is also an
        FD-exhaustion guard (audit §23 / T7.D3).

        Scope of the cap:
          - The ``async with`` block in ``add_file`` covers FD-open,
            the two pre-upload RPCs (``_register_file_source`` and
            ``_start_resumable_upload``), and the streaming upload. The
            semaphore therefore also serializes those two RPCs — a side
            effect of the FD guard, not a separate quota.
          - The cap applies to the *blocking* ``add_file`` call. On
            post-finalize cancel (T7.C3), the shielded background
            ``finalize_task`` continues running with the FD still open
            after ``add_file``'s ``async with`` exits, so the
            instantaneous open-FD count can briefly exceed
            ``max_concurrent_uploads`` by the number of concurrently
            draining background tasks.

        Lazy construction is required because ``asyncio.Semaphore()`` in
        some Python versions binds to the running event loop at creation
        time, and ``ClientCore`` can be constructed outside any loop.
        Callers must invoke this from inside the loop where the upload
        will run — typically inside the ``async with`` block of
        ``add_file``.
        """
        if self._upload_semaphore is None:
            self._upload_semaphore = asyncio.Semaphore(self._max_concurrent_uploads)
        return self._upload_semaphore

    def _get_rpc_semaphore(self) -> AbstractAsyncContextManager[Any]:
        """Return the per-instance RPC semaphore (or a null-context).

        When ``max_concurrent_rpcs`` was set to ``None`` at construction
        time, this returns a :class:`contextlib.nullcontext` so the
        ``async with`` wrapper in :meth:`_perform_authed_post` collapses
        to a no-op (callers with their own external rate-limiter opted
        out of the gate). Otherwise it lazily constructs an
        ``asyncio.Semaphore`` bound to the running loop on first use,
        mirroring the lazy-init pattern of :attr:`_reqid_lock` /
        :attr:`_auth_snapshot_lock` / :meth:`get_upload_semaphore`.

        The check-then-assign is safe without an outer lock because
        asyncio is single-threaded: no other coroutine can execute
        between the ``is None`` check and the assignment unless we
        ``await`` (and we don't).
        """
        if self._max_concurrent_rpcs is None:
            return nullcontext()
        if self._rpc_semaphore is None:
            self._rpc_semaphore = asyncio.Semaphore(self._max_concurrent_rpcs)
        return self._rpc_semaphore

    async def open(self) -> None:
        """Open the HTTP client connection.

        Called automatically by NotebookLMClient.__aenter__.
        Uses httpx.Cookies jar to properly handle cross-domain redirects
        (e.g., to accounts.google.com for auth token refresh).

        Captures the running event loop in ``self._bound_loop`` so
        :meth:`_perform_authed_post` can fail fast if the same client is
        later driven from a different loop (audit §14 / T7.G2). Re-opening
        on a different loop intentionally replaces the binding — ``open()``
        is the only binding moment; ``close()`` does not unbind so an
        accidental cross-loop call after close still raises actionably.
        """
        if self._http_client is None:
            # Capture event-loop affinity before any awaitable resource is
            # built so the binding is consistent with the loop that owns
            # every primitive constructed below (T7.G2 / audit §14).
            self._bound_loop = asyncio.get_running_loop()
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

    def _get_auth_snapshot_lock(self) -> asyncio.Lock:
        """Return the lazily-initialised ``_auth_snapshot_lock``.

        ``asyncio.Lock()`` needs a running loop in some Python versions, so
        ``ClientCore.__init__`` leaves the field as ``None``. Callers must
        be inside an async context (which we are, since both
        :meth:`_snapshot` and :meth:`NotebookLMClient.refresh_auth` are
        coroutines). The check-then-assign is safe without an outer lock
        because asyncio is single-threaded — no other coroutine can
        execute between the ``is None`` check and the assignment unless
        we ``await``.
        """
        if self._auth_snapshot_lock is None:
            self._auth_snapshot_lock = asyncio.Lock()
        return self._auth_snapshot_lock

    def _get_refresh_lock(self) -> asyncio.Lock:
        """Return the lazily-initialised ``_refresh_lock``.

        ``asyncio.Lock()`` needs a running loop in some Python versions, so
        ``ClientCore.__init__`` leaves the field as ``None`` (audit §13 /
        T7.G1). Callers must be inside an async context — the only call site
        is :meth:`_await_refresh`, which is itself a coroutine. The
        check-then-assign is safe without an outer lock because asyncio is
        single-threaded: no other coroutine can execute between the
        ``is None`` check and the assignment unless we ``await``, so every
        concurrent caller resolves to the *same* lock instance and the
        single-flight refresh dedupe is preserved.
        """
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    async def _snapshot(self) -> _AuthSnapshot:
        """Capture the current auth headers as a frozen snapshot.

        Used by ``_perform_authed_post`` to make a single HTTP attempt's
        URL/body consistent (no mid-attempt mutation from refresh /
        keepalive). A fresh snapshot is taken on each retry.

        Acquires :attr:`_auth_snapshot_lock` for the four scalar reads so
        a concurrent ``refresh_auth`` can't interleave between
        ``csrf_token``/``session_id``/``authuser``/``account_email``
        reads. The critical section is purely synchronous attribute
        reads — no ``await``s — so the lock is uncontested in steady
        state and refresh's tiny write block can't block RPC throughput.

        The whole-request atomicity for ``(csrf, sid, cookies)`` on the
        wire still depends on the no-await invariant between this method
        returning and ``client.post(...)`` inside
        :meth:`_perform_authed_post` (see the AST guard in
        ``tests/unit/test_concurrency_refresh_race.py``). The lock
        guarantees the four scalars in the snapshot are coherent with
        each other; the no-await rule keeps the cookie axis aligned with
        them.
        """
        async with self._get_auth_snapshot_lock():
            return _AuthSnapshot(
                csrf_token=self.auth.csrf_token,
                session_id=self.auth.session_id,
                authuser=self.auth.authuser,
                account_email=self.auth.account_email,
            )

    def _build_url(
        self,
        rpc_method: RPCMethod,
        snapshot: _AuthSnapshot,
        source_path: str = "/",
        rpc_id_override: str | None = None,
    ) -> str:
        """Build the batchexecute URL for an RPC call.

        Args:
            rpc_method: The RPC method to call.
            snapshot: Frozen ``_AuthSnapshot`` captured by :meth:`_snapshot`
                under ``_auth_snapshot_lock``. The URL is built entirely
                from snapshot fields (``session_id``, ``authuser``,
                ``account_email``) so the URL and body for one attempt
                stay coherent across a concurrent refresh. Audit §12 /
                T7.F2: pre-fix this method read ``self.auth`` LIVE on
                each call, which let a refresh's write to
                ``self.auth.session_id`` slip between ``_snapshot()`` and
                ``_build_url()`` — producing a URL stamped with the new
                generation while the body still carried the old CSRF.
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
            "f.sid": snapshot.session_id,
            "hl": get_default_language(),
            "rt": "c",
        }
        # Multi-account: route batchexecute to the same Google account the
        # auth tokens were minted for. Email is preferred when known because
        # Google's integer account indices can change as browser accounts are
        # added or removed.
        if snapshot.account_email or snapshot.authuser:
            params["authuser"] = format_authuser_value(
                snapshot.authuser,
                snapshot.account_email,
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
        - **Rate-limit path** — on HTTP 429, sleeps and retries until
          ``rate_limit_max_retries`` is reached; after that, raises
          :class:`_TransportRateLimited` with the final response and
          parsed retry-after value. Sleep budget: ``Retry-After`` when
          parseable, otherwise ``min(2 ** attempt, 30)`` seconds with
          ±20% jitter (T7.H2 / audit §11). With
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

        # Event-loop affinity guard (audit §14 / T7.G2). Cheap ``is``
        # comparison — no per-call list traversal, no extra await. Fails
        # fast with an actionable message instead of a deep httpx /
        # asyncio.Lock error when the same client is reused across loops.
        # Placed BEFORE ``_get_rpc_semaphore()`` so a cross-loop misuse
        # never even reserves a semaphore slot.
        if self._bound_loop is not None and asyncio.get_running_loop() is not self._bound_loop:
            raise RuntimeError(
                "NotebookLMClient is bound to a different event loop. "
                "Each client is per-loop; create a new client in the target loop."
            )

        refreshed_this_call = False
        rate_limit_retries = 0
        server_error_retries = 0
        start = time.perf_counter()

        # ---------------------------------------------------------------
        # Semaphore placement contract (T7.H1 / audit §8) — DO NOT MOVE.
        #
        # The ``max_concurrent_rpcs`` semaphore is acquired HERE — at
        # ``_perform_authed_post``'s body — and ONLY here. This placement
        # is load-bearing for two independent reasons:
        #
        #   1. ``rpc_call`` (and its inner ``_rpc_call_impl``) implements
        #      a decode-time refresh-and-retry that *recursively* re-enters
        #      ``rpc_call(..., _is_retry=True)`` (see ``_core.py`` around
        #      the ``return await self.rpc_call(...)`` site at the tail of
        #      ``_handle_decode_auth_error``). If the semaphore were
        #      acquired at ``rpc_call`` instead, the outer call would hold
        #      one permit while awaiting the inner recursive call to
        #      release one — guaranteed deadlock at ``max_concurrent_rpcs=1``
        #      and permit-fragmentation risk at any cap.
        #
        #   2. ``NotebookLMClient.refresh_auth`` issues a *raw*
        #      ``http_client.get(homepage_url)`` — it doesn't go through
        #      ``_perform_authed_post`` at all. Wrapping refresh would
        #      double-gate the refresh-then-retry waterfall (refresh under
        #      one permit, post-refresh retry waiting for another) and
        #      let one slow refresh starve every in-flight RPC against
        #      the same client.
        #
        # The wrap is around the *entire* attempt loop (snapshot →
        # build → post → retry-backoff sleeps) deliberately: releasing
        # the permit during a 429/5xx backoff would let the next batch of
        # callers burst in just as the current cohort wakes up to retry,
        # undoing the smoothing the semaphore exists to provide.
        #
        # Future contributors: do NOT move this ``async with`` to wrap
        # ``rpc_call`` (deadlock) or ``refresh_auth`` (wrong protocol +
        # starvation). The auth-refresh path is reached via the inner
        # ``await self._await_refresh()`` below, which itself drops back
        # through this same critical section on its post-refresh retry
        # iteration — that's the contract callers depend on.
        # ---------------------------------------------------------------
        async with self._get_rpc_semaphore():
            while True:
                snapshot = await self._snapshot()
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
                            and rate_limit_retries < self._rate_limit_max_retries
                        ):
                            # Sleep budget: honor ``Retry-After`` when the server
                            # provides a parseable hint; otherwise fall back to
                            # capped exponential backoff with ±20% jitter (T7.H2
                            # / audit §11). The fallback mirrors the 5xx path so
                            # the positive ``rate_limit_max_retries`` default is
                            # still useful when Google omits the header.
                            if retry_after is not None:
                                sleep_seconds: float = retry_after
                                sleep_source = f"Retry-After={retry_after}s"
                            else:
                                backoff = min(2**rate_limit_retries, 30)
                                backoff += random.uniform(-0.2 * backoff, 0.2 * backoff)  # noqa: S311  # nosec B311 — jitter, not crypto
                                sleep_seconds = max(0.1, backoff)
                                sleep_source = f"exp-backoff={sleep_seconds:.1f}s"
                            logger.warning(
                                "%s rate-limited (HTTP 429); sleeping (%s) then retrying (%d/%d)",
                                log_label,
                                sleep_source,
                                rate_limit_retries + 1,
                                self._rate_limit_max_retries,
                            )
                            await asyncio.sleep(sleep_seconds)
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
                        isinstance(exc, httpx.HTTPStatusError)
                        and 500 <= exc.response.status_code < 600
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

        # Lazy-init the lock on first refresh attempt (audit §13 / T7.G1).
        # Every concurrent caller resolves to the same instance because
        # ``_get_refresh_lock`` runs synchronously in a single-threaded
        # asyncio loop, so single-flight task creation below is preserved.
        async with self._get_refresh_lock():
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
            # T7.F2: both the URL (via ``_build_url(snapshot, ...)``) and
            # the body (via ``snapshot.csrf_token``) now consume the
            # *same* frozen ``_AuthSnapshot`` captured under
            # ``_auth_snapshot_lock``. Pre-fix this factory passed only
            # ``snapshot.csrf_token`` to the body while ``_build_url``
            # re-read ``self.auth.session_id`` LIVE — a torn read that
            # let a concurrent refresh slip a new sid into the URL while
            # the body still carried the old csrf. Now URL + body are
            # generation-coherent for the lifetime of this attempt; cookie
            # coherence with the snapshot is still upheld by the no-await
            # invariant between ``_snapshot()`` returning and the
            # ``client.post(...)`` call inside ``_perform_authed_post``
            # (see the AST guards in
            # ``tests/unit/test_concurrency_refresh_race.py``).
            url = self._build_url(method, snapshot, source_path, rpc_id_override=resolved_id)
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
