"""Core infrastructure for NotebookLM API client."""

import asyncio
import logging
import math
import random
import threading
import time
import warnings
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

import httpx

from ._callbacks import maybe_await_callback
from ._core_cache import (
    MAX_CONVERSATION_CACHE_SIZE as _DEFAULT_CONVERSATION_CACHE_SIZE,
)
from ._core_cache import (
    ConversationCache,
)
from ._core_cookie_persistence import CookiePersistence
from ._core_polling import PendingPolls, PollRegistry
from ._core_rpc import RpcExecutor
from ._core_transport import (
    MAX_RETRY_AFTER_SECONDS as MAX_RETRY_AFTER_SECONDS,
)
from ._core_transport import (
    AuthedTransport,
    _AuthSnapshot,
    _BuildRequest,
    _TransportAuthExpired,
    _TransportRateLimited,
    _TransportServerError,
)
from ._core_transport import (
    _parse_retry_after as _parse_retry_after,
)
from ._logging import get_request_id, reset_request_id, set_request_id
from .auth import (
    AuthTokens,
    CookieSnapshot,
    _rotate_cookies,
    build_cookie_jar,
    save_cookies_to_storage,
)
from .types import ClientMetricsSnapshot, RpcTelemetryEvent

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
    decode_response,
)

logger = logging.getLogger(__name__)
_OBSERVABILITY_INIT_LOCK = threading.Lock()


@dataclass(frozen=True)
class _TransportOperationToken:
    """Token for one accepted transport operation on a specific asyncio task."""

    task: asyncio.Task[Any] | None


MAX_CONVERSATION_CACHE_SIZE = _DEFAULT_CONVERSATION_CACHE_SIZE

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

# -----------------------------------------------------------------------------
# T8.E10 — Test-only synthetic-error transport (opt-in via env var)
# -----------------------------------------------------------------------------
#
# When ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set to one of ``429`` / ``5xx`` /
# ``expired_csrf``, the next outgoing batchexecute RPC gets a substituted
# synthetic response that the client maps onto its own exception domain. This
# plumbing exists so T8.E4 (and any future error-cassette PR) can record
# cassettes whose response shapes match what the client's exception mapping
# keys on — see ``tests/cassette_patterns.py:build_synthetic_error_response``.
#
# **Production behavior is unchanged when the env var is unset.** The transport
# wrapper is only constructed when the env var resolves to a valid mode; the
# default ``httpx.AsyncClient`` is built with no explicit transport otherwise.
#
# This is deliberately wired through the client's HTTP layer (not just the VCR
# config) so the substitution sits BELOW VCR — VCR records the synthetic
# response into the cassette as if it had come from the wire. Wiring it at the
# VCR-config layer only would mean the substitution never ran in record mode,
# leaving the plumbing inert (the Momus iter-1 rejection rationale).

ERROR_INJECT_ENV_VAR = "NOTEBOOKLM_VCR_RECORD_ERRORS"


def _get_error_injection_mode() -> str | None:
    """Return the synthetic-error mode from ``NOTEBOOKLM_VCR_RECORD_ERRORS``.

    Returns ``None`` when the env var is unset, empty, or carries an
    unrecognized value (we deliberately fail open rather than crash a
    cassette-recording run on a typo — the unit tests catch the typo path,
    and the VCR config validates the value separately).

    The valid-mode set is hardcoded here (rather than imported from
    ``tests.cassette_patterns``) so production import time never reaches into
    the test tree. The same set is mirrored in
    ``tests.cassette_patterns.VALID_ERROR_MODES`` and the
    ``synthetic_error`` marker validator in ``tests/conftest.py``; the
    duplication is intentional and bounded — adding a fourth mode requires
    updating all three sites, which the unit tests in ``tests/unit/
    test_vcr_config.py`` will surface immediately.
    """
    import os

    raw = os.environ.get(ERROR_INJECT_ENV_VAR, "").strip()
    if not raw:
        return None
    # Lowercase-normalize so callers can use ``"5XX"`` / ``"429"`` / etc.
    normalized = raw.lower()
    valid = {"429", "5xx", "expired_csrf"}
    if normalized not in valid:
        return None
    return normalized


class _SyntheticErrorTransport(httpx.AsyncBaseTransport):
    """Test-only httpx transport that substitutes synthetic error responses.

    Wraps an inner ``httpx.AsyncBaseTransport`` and substitutes a synthetic
    error response on outgoing batchexecute POSTs, built by
    ``tests.cassette_patterns.build_synthetic_error_response``. Non-batchexecute
    traffic (Scotty uploads, ``RotateCookies`` pokes, the homepage GET that
    extracts CSRF) passes through unchanged because none of those endpoints
    are in scope for error-shape cassettes.

    Substitution scope is controlled by ``always``:

    - ``always=True`` (the default for record-mode use): every batchexecute
      POST is substituted. This matters because the client's auth-refresh
      path re-issues the same RPC; we want the SAME error to fire on every
      retry inside the recording window so the cassette captures the full
      retry-and-fail sequence rather than substituting once and then letting
      a real response slip through on the retry.
    - ``always=False``: only the FIRST batchexecute POST is substituted; later
      POSTs fall through to the inner transport. Useful for tests that want
      to assert the client recovers after a single transient failure.

    This class is OPT-IN — ``ClientCore`` only wraps the transport when
    ``_get_error_injection_mode()`` returns a non-``None`` value, so removing
    the env var restores byte-for-byte production behavior.
    """

    def __init__(
        self,
        mode: str,
        inner: httpx.AsyncBaseTransport,
        *,
        always: bool = True,
    ):
        self._mode = mode
        self._inner = inner
        self._always = always
        self._fired = False
        # Resolved lazily on first use so this module doesn't import the test
        # tree at module load time.
        self._builder: Callable[[str], tuple[int, bytes, dict[str, str]]] | None = None

    def _is_batchexecute(self, request: httpx.Request) -> bool:
        # NotebookLM's batchexecute endpoint lives under
        # ``notebooklm.google.com/_/LabsTailwindUi/data/batchexecute``. We
        # match on the path suffix so any subdomain / region variant still
        # triggers substitution.
        return request.url.path.endswith("/batchexecute")

    def _load_builder(
        self,
    ) -> Callable[[str], tuple[int, bytes, dict[str, str]]]:
        if self._builder is not None:
            return self._builder
        # Import lazily and via importlib to avoid a hard dependency on the
        # tests tree from production code. The env var that gates this whole
        # path is itself test-only, so this import only ever runs in
        # recording / unit-test contexts.
        import importlib.util
        from pathlib import Path

        # Walk up from src/notebooklm/_core.py to the repo root, then dive
        # into tests/cassette_patterns.py. This keeps the lookup robust to
        # installed-package layouts (where ``tests/`` may not exist) — in
        # that case we raise a clear error rather than silently no-oping.
        repo_root = Path(__file__).resolve().parent.parent.parent
        target = repo_root / "tests" / "cassette_patterns.py"
        if not target.exists():
            raise RuntimeError(
                f"{ERROR_INJECT_ENV_VAR} is set but "
                f"tests/cassette_patterns.py is not available at {target}. "
                f"This plumbing is test-only — unset {ERROR_INJECT_ENV_VAR} "
                f"to restore normal behavior."
            )
        spec = importlib.util.spec_from_file_location("_notebooklm_cassette_patterns", target)
        # NOT ``assert`` — runtime invariant must survive ``python -O``. The
        # check is defensive (spec_from_file_location on an existing .py file
        # virtually always succeeds) but if it ever fails the user has clear
        # remediation via the env var.
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Failed to load module spec for {target}. "
                f"Unset {ERROR_INJECT_ENV_VAR} to restore normal behavior."
            )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._builder = cast(
            Callable[[str], tuple[int, bytes, dict[str, str]]],
            mod.build_synthetic_error_response,
        )
        return self._builder

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Substitute ONLY on POST batchexecute calls. Non-POST traffic on the
        # same path (a hypothetical GET batchexecute probe, OPTIONS preflight,
        # etc.) is out of scope for error-shape cassettes and must pass through
        # unchanged — see CodeRabbit feedback on PR #638.
        if (
            request.method.upper() == "POST"
            and self._is_batchexecute(request)
            and (self._always or not self._fired)
        ):
            self._fired = True
            status_code, body, headers = self._load_builder()(self._mode)
            response = httpx.Response(
                status_code=status_code,
                headers=headers,
                content=body,
                request=request,
            )
            # ``httpx.Response`` constructed this way is already "read" — VCR
            # can serialize it directly via its standard before_record hook.
            return response
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


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


def _decode_response_late_bound(raw: str, rpc_id: str, *, allow_null: bool = False) -> Any:
    return decode_response(raw, rpc_id, allow_null=allow_null)


def _sleep_late_bound(seconds: float) -> Awaitable[Any]:
    return asyncio.sleep(seconds)


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
        on_rpc_event: Callable[[RpcTelemetryEvent], object] | None = None,
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
            on_rpc_event: Optional callback invoked after each logical
                ``rpc_call`` succeeds or fails. The callback receives a
                backend-agnostic :class:`RpcTelemetryEvent`; exceptions raised
                by the callback are logged and never mask the RPC result.

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
        self._on_rpc_event = on_rpc_event
        self._metrics_lock = threading.Lock()
        self._metrics = ClientMetricsSnapshot()
        self._draining = False
        self._in_flight_posts = 0
        self._drain_condition: asyncio.Condition | None = None
        self._operation_depths: weakref.WeakKeyDictionary[asyncio.Task[Any], int] = (
            weakref.WeakKeyDictionary()
        )
        # Request ID counter for chat API (must be unique per request).
        # Access via the ``next_reqid()`` async method, which guards mutation
        # under ``_reqid_lock``. Direct mutation through the ``_reqid_counter``
        # property setter emits a ``DeprecationWarning``; bypass the warning
        # for legitimate test setup by writing to ``_reqid_counter_value``.
        self._reqid_counter_value: int = 100000
        # Lazily-created — ``asyncio.Lock()`` needs a running loop in some
        # Python versions, and this object can be constructed outside one.
        self._reqid_lock: asyncio.Lock | None = None
        # Serializes ``_AuthSnapshot`` reads in :meth:`_snapshot` with
        # :meth:`ClientCore.update_auth_tokens` during auth refresh
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
        self._conversation_cache_manager = ConversationCache()
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
        # Owns the in-process save lock and open-time cookie baseline while
        # compatibility properties below keep the legacy private attribute
        # names observable for current tests and first-party callers.
        self.cookie_persistence = CookiePersistence(self.auth, self._keepalive_storage_path)
        self.poll_registry: PollRegistry = PollRegistry()
        self._authed_transport: AuthedTransport | None = None
        self._rpc_executor: RpcExecutor | None = None

    @property
    def _save_lock(self) -> threading.Lock:
        """Compatibility bridge to ``CookiePersistence``'s in-process save lock."""
        return self.cookie_persistence.save_lock

    @_save_lock.setter
    def _save_lock(self, value: threading.Lock) -> None:
        self.cookie_persistence.save_lock = value

    @property
    def _loaded_cookie_snapshot(self) -> CookieSnapshot | None:
        """Compatibility bridge to the cookie save baseline."""
        return self.cookie_persistence.loaded_cookie_snapshot

    @_loaded_cookie_snapshot.setter
    def _loaded_cookie_snapshot(self, value: CookieSnapshot | None) -> None:
        self.cookie_persistence.loaded_cookie_snapshot = value

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

    @property
    def _pending_polls(self) -> PendingPolls:
        """Deprecated compatibility view of ``poll_registry.pending``.

        Feature APIs now access polling state through ``poll_registry`` or a
        narrow capability adapter. This bridge remains for external callers and
        tests that still read or assign ``ClientCore._pending_polls`` directly.
        """
        return self.poll_registry.pending

    @_pending_polls.setter
    def _pending_polls(self, value: PendingPolls) -> None:
        self.poll_registry.pending = value

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
        wait_start = time.perf_counter()
        await self._reqid_lock.acquire()
        self._record_lock_wait(time.perf_counter() - wait_start)
        try:
            self._reqid_counter_value += step
            return self._reqid_counter_value
        finally:
            self._reqid_lock.release()

    def metrics_snapshot(self) -> ClientMetricsSnapshot:
        """Return cumulative observability counters for this client instance."""
        self._ensure_observability_state()
        with self._metrics_lock:
            return replace(self._metrics)

    def _ensure_observability_state(self) -> None:
        """Backfill observability fields for tests that construct via ``__new__``."""
        if (
            hasattr(self, "_on_rpc_event")
            and hasattr(self, "_metrics_lock")
            and hasattr(self, "_metrics")
            and hasattr(self, "_draining")
            and hasattr(self, "_in_flight_posts")
            and hasattr(self, "_drain_condition")
            and hasattr(self, "_operation_depths")
        ):
            return
        with _OBSERVABILITY_INIT_LOCK:
            if not hasattr(self, "_on_rpc_event"):
                self._on_rpc_event = None
            if not hasattr(self, "_metrics_lock"):
                self._metrics_lock = threading.Lock()
            if not hasattr(self, "_metrics"):
                self._metrics = ClientMetricsSnapshot()
            if not hasattr(self, "_draining"):
                self._draining = False
            if not hasattr(self, "_in_flight_posts"):
                self._in_flight_posts = 0
            if not hasattr(self, "_drain_condition"):
                self._drain_condition = None
            if not hasattr(self, "_operation_depths"):
                self._operation_depths = weakref.WeakKeyDictionary()

    def _increment_metrics(self, **increments: int | float) -> None:
        """Increment numeric metrics fields under the metrics lock."""
        self._ensure_observability_state()
        with self._metrics_lock:
            values = {
                field_name: getattr(self._metrics, field_name) + increment
                for field_name, increment in increments.items()
            }
            self._metrics = replace(self._metrics, **values)

    def _record_rpc_queue_wait(self, wait_seconds: float) -> None:
        self._ensure_observability_state()
        with self._metrics_lock:
            self._metrics = replace(
                self._metrics,
                rpc_queue_wait_seconds_total=(
                    self._metrics.rpc_queue_wait_seconds_total + wait_seconds
                ),
                rpc_queue_wait_seconds_max=max(
                    self._metrics.rpc_queue_wait_seconds_max,
                    wait_seconds,
                ),
            )

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        """Record time spent waiting for the upload semaphore."""
        self._ensure_observability_state()
        with self._metrics_lock:
            self._metrics = replace(
                self._metrics,
                upload_queue_wait_seconds_total=(
                    self._metrics.upload_queue_wait_seconds_total + wait_seconds
                ),
                upload_queue_wait_seconds_max=max(
                    self._metrics.upload_queue_wait_seconds_max,
                    wait_seconds,
                ),
            )

    def _record_lock_wait(self, wait_seconds: float) -> None:
        self._ensure_observability_state()
        with self._metrics_lock:
            self._metrics = replace(
                self._metrics,
                lock_wait_seconds_total=self._metrics.lock_wait_seconds_total + wait_seconds,
                lock_wait_seconds_max=max(self._metrics.lock_wait_seconds_max, wait_seconds),
            )

    async def _emit_rpc_event(self, event: RpcTelemetryEvent) -> None:
        """Invoke the optional telemetry callback without affecting RPC behavior."""
        self._ensure_observability_state()
        if self._on_rpc_event is None:
            return
        try:
            await maybe_await_callback(self._on_rpc_event, event)
        except Exception as exc:  # noqa: BLE001 - observability must not alter behavior
            logger.warning("RPC telemetry callback failed: %s", exc)

    def _get_drain_condition(self) -> asyncio.Condition:
        self._ensure_observability_state()
        if self._drain_condition is None:
            self._drain_condition = asyncio.Condition()
        return self._drain_condition

    def _current_operation_depth(self, task: asyncio.Task[Any] | None) -> int:
        if task is None:
            return 0
        return self._operation_depths.get(task, 0)

    async def _begin_transport_post(self, log_label: str) -> _TransportOperationToken:
        """Reject new top-level transport work once graceful drain has started."""
        condition = self._get_drain_condition()
        task = asyncio.current_task()
        depth = self._current_operation_depth(task)
        async with condition:
            if self._draining and depth == 0:
                raise RuntimeError(
                    "NotebookLMClient is draining; new client operations are not accepted "
                    f"({log_label})."
                )
            if task is not None:
                self._operation_depths[task] = depth + 1
            self._in_flight_posts += 1
        return _TransportOperationToken(task=task)

    async def _begin_transport_task(
        self,
        task: asyncio.Task[Any],
        log_label: str,
    ) -> _TransportOperationToken:
        """Admit an internally-spawned task as part of the current operation."""
        condition = self._get_drain_condition()
        current_depth = self._current_operation_depth(asyncio.current_task())
        async with condition:
            if self._draining and current_depth == 0:
                raise RuntimeError(
                    "NotebookLMClient is draining; new client operations are not accepted "
                    f"({log_label})."
                )
            self._operation_depths[task] = self._operation_depths.get(task, 0) + 1
            self._in_flight_posts += 1
        return _TransportOperationToken(task=task)

    async def _finish_transport_post(self, token: _TransportOperationToken) -> None:
        condition = self._get_drain_condition()
        async with condition:
            if token.task is not None:
                depth = self._operation_depths.get(token.task, 0)
                if depth <= 1:
                    self._operation_depths.pop(token.task, None)
                else:
                    self._operation_depths[token.task] = depth - 1
            self._in_flight_posts -= 1
            if self._in_flight_posts == 0:
                condition.notify_all()

    async def drain(self, timeout: float | None = None) -> None:
        """Stop accepting new client operations and wait for in-flight ones to finish.

        If ``timeout`` expires, ``TimeoutError`` is raised and the client
        remains in draining mode so shutdown callers do not accidentally admit
        new work after a missed deadline.
        """
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {timeout!r}")
        condition = self._get_drain_condition()
        async with condition:
            self._draining = True
            if self._in_flight_posts == 0:
                return
            await asyncio.wait_for(
                condition.wait_for(lambda: self._in_flight_posts == 0),
                timeout=timeout,
            )

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

    def _get_authed_transport(self) -> AuthedTransport:
        """Return the authenticated transport collaborator, lazily initialized.

        The adapters intentionally resolve through this module at call time so
        existing tests and private callers that monkeypatch
        ``notebooklm._core.is_auth_error``, ``notebooklm._core.asyncio.sleep``,
        or ``notebooklm._core.random.uniform`` still affect live transport
        behavior after the collaborator has been constructed.
        """
        transport = getattr(self, "_authed_transport", None)
        if transport is None:
            transport = AuthedTransport(
                self,
                is_auth_error=lambda exc: is_auth_error(exc),
                sleep=lambda seconds: asyncio.sleep(seconds),
                uniform=lambda low, high: random.uniform(low, high),  # noqa: S311 # nosec B311
                logger=logger,
            )
            self._authed_transport = transport
        return transport

    def _get_rpc_executor(self) -> RpcExecutor:
        """Return the RPC execution collaborator, lazily initialized.

        The adapters resolve through this module at call time so existing
        monkeypatches of ``notebooklm._core.decode_response``,
        ``notebooklm._core.is_auth_error``, and
        ``notebooklm._core.asyncio.sleep`` keep affecting live RPC behavior
        after the collaborator has been constructed.
        """
        executor = getattr(self, "_rpc_executor", None)
        if executor is None:
            executor = RpcExecutor(
                self,
                decode_response_late_bound=_decode_response_late_bound,
                is_auth_error=lambda exc: is_auth_error(exc),
                sleep=_sleep_late_bound,
            )
            self._rpc_executor = executor
        return executor

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
            self._draining = False
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
            # T8.E10 — opt-in synthetic-error transport wrapper. When the env
            # var is unset (the default) this is a no-op and the AsyncClient
            # is constructed exactly as before. See ``_SyntheticErrorTransport``
            # docstring at module top.
            error_mode = _get_error_injection_mode()
            synthetic_transport: httpx.AsyncBaseTransport | None = None
            if error_mode is not None:
                # When we supply a custom ``transport=`` to ``AsyncClient``,
                # httpx no longer constructs its own internal transport from
                # the ``limits=`` kwarg below — those limits are consumed by
                # the inner transport here instead, so connection-pool sizing
                # remains identical to the no-injection path.
                inner_transport = httpx.AsyncHTTPTransport(
                    limits=self._limits.to_httpx_limits(),
                )
                synthetic_transport = _SyntheticErrorTransport(error_mode, inner_transport)
                logger.info(
                    "T8.E10 synthetic-error injection enabled (mode=%s) — "
                    "production paths will see substituted responses",
                    error_mode,
                )
            self._http_client = httpx.AsyncClient(
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                },
                cookies=cookies,
                timeout=timeout,
                follow_redirects=True,
                # ``limits=`` is honored when ``transport=None`` (default) —
                # httpx builds its own default transport with these limits.
                # When ``transport=synthetic_transport`` (T8.E10 record mode)
                # this kwarg is ignored by httpx and the inner_transport above
                # carries the limits instead. The redundant pass is harmless
                # and avoids a branch on the AsyncClient construction site.
                limits=self._limits.to_httpx_limits(),
                transport=synthetic_transport,
            )

            # Capture the open-time snapshot AFTER the AsyncClient is built
            # (httpx normalizes domains on ingest) but BEFORE any rotation
            # could possibly fire. When AuthTokens carries a snapshot from a
            # failed pre-client save, keep it so the unpersisted delta can be
            # retried instead of treating the already-mutated jar as clean.
            self.cookie_persistence.capture_open_snapshot(self._http_client.cookies)

            # Spawn the keepalive task once the client is ready
            if self._keepalive_interval is not None:
                self._keepalive_task = asyncio.create_task(
                    self._keepalive_loop(self._keepalive_interval)
                )

    async def save_cookies(self, jar: httpx.Cookies, path: Path | None = None) -> None:
        """Persist a cookie jar through the shared cookie-persistence collaborator.

        This remains the single chokepoint used by ``close()``, the keepalive
        loop, and ``NotebookLMClient.refresh_auth``. The storage writer and
        thread offload callable are resolved from this module at call time so
        existing ``notebooklm._core`` monkeypatch paths continue to affect the
        live save path.
        """
        await self.cookie_persistence.save(
            jar,
            path,
            save_cookies_to_storage=save_cookies_to_storage,
            to_thread=asyncio.to_thread,
        )

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
        :meth:`_snapshot` and :meth:`update_auth_tokens` are coroutines).
        The check-then-assign is safe without an outer lock
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
        wait_start = time.perf_counter()
        async with self._get_auth_snapshot_lock():
            self._record_lock_wait(time.perf_counter() - wait_start)
            return _AuthSnapshot(
                csrf_token=self.auth.csrf_token,
                session_id=self.auth.session_id,
                authuser=self.auth.authuser,
                account_email=self.auth.account_email,
            )

    async def update_auth_tokens(self, csrf: str, session_id: str) -> None:
        """Atomically update auth token scalars under the snapshot lock."""
        lock = self._get_auth_snapshot_lock()
        wait_start = time.perf_counter()
        await lock.acquire()
        self._record_lock_wait(time.perf_counter() - wait_start)
        try:
            self.auth.csrf_token = csrf
            self.auth.session_id = session_id
        finally:
            lock.release()

    def _build_url(
        self,
        rpc_method: RPCMethod,
        snapshot: _AuthSnapshot,
        source_path: str = "/",
        rpc_id_override: str | None = None,
    ) -> str:
        """Compatibility wrapper around :class:`RpcExecutor` URL building."""
        return self._get_rpc_executor().build_url(
            rpc_method,
            snapshot,
            source_path,
            rpc_id_override=rpc_id_override,
        )

    async def _perform_authed_post(
        self,
        *,
        build_request: _BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        """Compatibility wrapper around :class:`AuthedTransport`."""
        return await self._get_authed_transport().perform_authed_post(
            build_request=build_request,
            log_label=log_label,
            disable_internal_retries=disable_internal_retries,
        )

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
        lock = self._get_refresh_lock()
        wait_start = time.perf_counter()
        await lock.acquire()
        self._record_lock_wait(time.perf_counter() - wait_start)
        try:
            if self._refresh_task is not None and not self._refresh_task.done():
                refresh_task = self._refresh_task
                logger.debug("Joining existing refresh task")
            else:
                coro = cast(Coroutine[Any, Any, AuthTokens], self._refresh_callback())
                self._refresh_task = asyncio.create_task(coro)
                refresh_task = self._refresh_task
        finally:
            lock.release()

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

        operation_token = await self._begin_transport_post(parse_label)
        try:
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
        finally:
            await self._finish_transport_post(operation_token)
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

        method_name = getattr(method, "name", str(method))
        operation_token = await self._begin_transport_post(f"RPC {method_name}")
        self._increment_metrics(rpc_calls_started=1)
        start = time.perf_counter()
        _reqid_token = None if get_request_id() is not None else set_request_id()
        try:
            result = await self._rpc_call_impl(
                method,
                params,
                source_path,
                allow_null,
                _is_retry,
                disable_internal_retries=disable_internal_retries,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - start
            self._increment_metrics(rpc_calls_failed=1, rpc_latency_seconds_total=elapsed)
            await self._emit_rpc_event(
                RpcTelemetryEvent(
                    method=method_name,
                    status="error",
                    elapsed_seconds=elapsed,
                    request_id=get_request_id(),
                    error_type=type(exc).__name__,
                )
            )
            raise
        else:
            elapsed = time.perf_counter() - start
            self._increment_metrics(rpc_calls_succeeded=1, rpc_latency_seconds_total=elapsed)
            await self._emit_rpc_event(
                RpcTelemetryEvent(
                    method=method_name,
                    status="success",
                    elapsed_seconds=elapsed,
                    request_id=get_request_id(),
                )
            )
            return result
        finally:
            if _reqid_token is not None:
                reset_request_id(_reqid_token)
            await self._finish_transport_post(operation_token)

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
        """Compatibility wrapper around :class:`RpcExecutor`."""
        return await self._get_rpc_executor().execute(
            method,
            params,
            source_path,
            allow_null,
            _is_retry,
            disable_internal_retries=disable_internal_retries,
        )

    def _raise_rpc_error_from_http_status(
        self,
        exc: httpx.HTTPStatusError,
        method: RPCMethod,
    ) -> NoReturn:
        """Compatibility wrapper around :class:`RpcExecutor`."""
        self._get_rpc_executor().raise_rpc_error_from_http_status(exc, method)

    def _raise_rpc_error_from_request_error(
        self,
        exc: httpx.RequestError,
        method: RPCMethod,
    ) -> NoReturn:
        """Compatibility wrapper around :class:`RpcExecutor`."""
        self._get_rpc_executor().raise_rpc_error_from_request_error(exc, method)

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
        """Compatibility wrapper around :class:`RpcExecutor`."""
        return await self._get_rpc_executor().try_refresh_and_retry(
            method,
            params,
            source_path,
            allow_null,
            original_error,
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
        self._conversation_cache_manager.cache_conversation_turn(
            conversation_id,
            query,
            answer,
            turn_number,
            max_size=MAX_CONVERSATION_CACHE_SIZE,
        )

    def get_cached_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        """Get cached conversation turns.

        Args:
            conversation_id: The conversation ID.

        Returns:
            List of cached turns, or empty list if not found.
        """
        return self._conversation_cache_manager.get_cached_conversation(conversation_id)

    def clear_conversation_cache(self, conversation_id: str | None = None) -> bool:
        """Clear conversation cache.

        Args:
            conversation_id: Clear specific conversation, or all if None.

        Returns:
            True if cache was cleared.
        """
        return self._conversation_cache_manager.clear(conversation_id)

    @property
    def _conversation_cache(self) -> OrderedDict[str, list[dict[str, Any]]]:
        """Compatibility view of the conversation cache backing mapping."""
        return self._conversation_cache_manager.conversations

    @_conversation_cache.setter
    def _conversation_cache(self, value: OrderedDict[str, list[dict[str, Any]]]) -> None:
        self._conversation_cache_manager = ConversationCache(value)

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
