"""Core infrastructure for NotebookLM API client."""

import asyncio
import logging
import math
import random  # noqa: F401 - tests patch this for _backoff jitter
import threading
import time
import warnings
import weakref
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

import httpx

from ._core_auth import AuthRefreshCoordinator
from ._core_cookie_persistence import CookiePersistence
from ._core_drain import TransportDrainTracker

# Re-exported so the existing import path ``from notebooklm._core import
# _TransportOperationToken`` keeps working after the dataclass moved into
# ``_core_drain``. ``_core_drain`` is the source of truth for the token
# shape; the alias below is the backwards-compat anchor.
from ._core_drain import _TransportOperationToken as _TransportOperationToken
from ._core_lifecycle import ClientLifecycle
from ._core_metrics import ClientMetrics
from ._core_polling import PendingPolls, PollRegistry
from ._core_reqid import DEFAULT_STEP as _REQID_DEFAULT_STEP
from ._core_reqid import ReqidCounter
from ._core_rpc import RpcExecutor
from ._core_transport import (
    MAX_RETRY_AFTER_SECONDS as MAX_RETRY_AFTER_SECONDS,
)
from ._core_transport import (
    AuthedTransport,
    _AuthSnapshot,
    _BuildRequest,
)
from ._core_transport import (
    _parse_retry_after as _parse_retry_after,
)
from ._core_transport import (
    _TransportAuthExpired as _TransportAuthExpired,
)
from ._core_transport import (
    _TransportRateLimited as _TransportRateLimited,
)
from ._core_transport import (
    _TransportServerError as _TransportServerError,
)
from ._sources import fetch_source_ids

# ``save_cookies_to_storage`` is re-exported as ``notebooklm._core.save_cookies_to_storage``
# so existing ``monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", …)``
# sites in tests keep working (used in 8+ test files). The lifecycle helper
# (``_core_lifecycle.ClientLifecycle.save_cookies``) reads the attribute via
# ``from . import _core; _core.save_cookies_to_storage`` at call time so the
# monkeypatched value is what runs on the live save path.
#
# ``_rotate_cookies`` is re-exported on the same module-level attribute surface
# so ``tests/unit/concurrency/test_close_cancellation_leak.py:138``'s
# ``monkeypatch.setattr("notebooklm._core._rotate_cookies", …)`` keeps
# affecting the live keepalive loop (the lifecycle helper resolves it via
# ``from . import _core; _core._rotate_cookies`` at call time).
from .auth import (
    AuthTokens,
    CookieSnapshot,
)
from .auth import (
    _rotate_cookies as _rotate_cookies,
)
from .auth import (
    build_cookie_jar as build_cookie_jar,
)
from .auth import (
    save_cookies_to_storage as save_cookies_to_storage,
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
# Guards ``_auth_coord`` backfill on ``__new__``-built fixtures. Mirrors the
# observability init lock so two threads can't both observe ``hasattr is False``
# and race to construct competing :class:`AuthRefreshCoordinator` instances.
#
# Dual-implementation sites (kept identical for AST guards in
# ``tests/unit/test_concurrency_refresh_race.py`` that inspect
# ``inspect.getsource(ClientCore.update_auth_tokens)`` /
# ``ClientCore._snapshot``):
#   - ``ClientCore._snapshot`` (this file) ↔ ``AuthRefreshCoordinator.snapshot``
#   - ``ClientCore.update_auth_tokens`` (this file) ↔ ``AuthRefreshCoordinator.update_auth_tokens``
# Any change to auth-snapshot invariants must be applied to BOTH sites. Grep
# anchor for future maintainers: ``_AUTH_COORD_INIT_LOCK``.
_AUTH_COORD_INIT_LOCK = threading.Lock()


# Default HTTP timeouts in seconds
DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0  # Connection establishment timeout

# Minimum keepalive interval to avoid accidentally rate-limiting accounts.google.com
DEFAULT_KEEPALIVE_MIN_INTERVAL = 60.0

# Default ceiling on concurrent in-flight ``SourcesAPI.add_file`` uploads.
# Each in-flight upload holds one open file descriptor for the duration of
# the upload, so the cap is also an FD-exhaustion guard. Sized for typical
# interactive workloads; tune higher for batch ingestion pipelines that
# ingest dozens of files in parallel and have headroom in the process FD
# limit (``ulimit -n``).
DEFAULT_MAX_CONCURRENT_UPLOADS = 4

# Default ceiling on simultaneous in-flight ``_perform_authed_post``
# RPC POSTs. Sits *below* the default httpx pool
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
# Test-only synthetic-error transport (opt-in via env var)
# -----------------------------------------------------------------------------
#
# When ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set to one of ``429`` / ``5xx`` /
# ``expired_csrf``, the next outgoing batchexecute RPC gets a substituted
# synthetic response that the client maps onto its own exception domain. This
# plumbing exists so error-cassette recording can produce cassettes whose
# response shapes match what the client's exception mapping keys on — see
# ``tests/cassette_patterns.py:build_synthetic_error_response``.
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
                Defaults to ``3`` so programmatic users
                inherit "smart retry" behavior without having to opt in. Set
                to ``0`` to raise ``RateLimitError`` immediately. Each retry
                sleeps for the
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
                unbounded fan-out exhausts the per-process FD limit. Must
                be ``>= 1`` when supplied. Independent
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
                Must be ``>= 1`` when supplied. Before this gate was added,
                heavy fan-out workloads tripped opaque
                ``httpx.PoolTimeout`` errors before the connection pool
                could surface clean back-pressure. Cross-
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
        # HTTP timeouts, connection limits, keepalive interval / storage_path,
        # the live ``httpx.AsyncClient``, the captured ``_bound_loop``, and
        # the keepalive background task all live on ``self._lifecycle``
        # (constructed below alongside the other extracted helpers so the
        # inter-helper dependency order is obvious). Compat properties further
        # down preserve the legacy ``_timeout`` / ``_connect_timeout`` /
        # ``_limits`` / ``_http_client`` / ``_bound_loop`` /
        # ``_keepalive_task`` / ``_keepalive_interval`` /
        # ``_keepalive_storage_path`` ivar names for tests and first-party
        # callers that probe or assign them directly.
        _resolved_limits = limits if limits is not None else ConnectionLimits()
        # ``_refresh_retry_delay`` stays here directly — it is read on the
        # RPC retry path by ``RpcExecutor`` and ``AuthedTransport`` and SET
        # by integration tests against ``client._core``. The refresh
        # callback + the four refresh/auth-snapshot ivars (``_refresh_lock``,
        # ``_refresh_task``, ``_refresh_callback``, ``_auth_snapshot_lock``)
        # live on ``self._auth_coord``, constructed below alongside the other
        # extracted helpers so the inter-helper dependency order is obvious.
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
        # save us. Reject ``<= 0`` loudly at construction
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
        # RPC-fanout throttle. ``None`` means "no
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
        # Observability counters + telemetry callback. Compat properties
        # below (``_metrics_lock`` / ``_metrics`` / ``_on_rpc_event``) bridge
        # the legacy ivar names back into this helper.
        self._metrics_obj = ClientMetrics(on_rpc_event=on_rpc_event)
        # Transport drain bookkeeping (in-flight posts, drain condition,
        # per-task operation depth, draining flag). Compat properties below
        # (``_in_flight_posts`` / ``_drain_condition`` / ``_operation_depths``
        # / ``_draining``) bridge the legacy ivar names back into this
        # helper. The helper's ``__init__`` is event-loop-agnostic; the
        # ``asyncio.Condition`` is created lazily on first
        # ``get_drain_condition`` call.
        self._drain_tracker = TransportDrainTracker()
        # Request ID counter for chat API (must be unique per request).
        # The :class:`ReqidCounter` helper owns the monotonic ``_value`` and
        # the lazily-allocated ``asyncio.Lock`` that serialises mutation.
        # Compat properties below (``_reqid_counter`` /
        # ``_reqid_counter_value`` / ``_reqid_lock``) bridge the legacy ivar
        # names back into this helper. The ``on_lock_wait`` hook keeps the
        # cumulative ``lock_wait_seconds_*`` metrics ticking inside
        # ``self._metrics_obj`` even though the counter is now extracted.
        self._reqid = ReqidCounter(on_lock_wait=self._record_lock_wait)
        # Auth refresh coordination — single-flight refresh task, snapshot
        # serialization, and cookie-jar sync. The coordinator owns
        # ``_refresh_lock``, ``_refresh_task``, ``_refresh_callback``, and
        # ``_auth_snapshot_lock``; field names match the legacy
        # ``ClientCore`` ivars so the compat properties below delegate
        # cleanly. The ``_auth_snapshot_lock`` is intentionally distinct
        # from ``_refresh_lock`` — mixing them would re-introduce the
        # reentrancy ambiguity that snapshot-side serialization was added
        # to avoid. The attribute name ``_auth_coord`` is part of the
        # inter-helper contract for the upcoming B2/C1 extractions; do not
        # rename.
        self._auth_coord = AuthRefreshCoordinator(refresh_callback=refresh_callback)
        # HTTP-client lifecycle — owns ``_http_client``, ``_bound_loop``,
        # ``_keepalive_task``, ``_keepalive_interval``,
        # ``_keepalive_storage_path``, ``_timeout``, ``_connect_timeout``,
        # ``_limits``. Compat properties further down preserve the legacy
        # ivar names. The ``_resolve_keepalive_interval`` clamp stays in
        # this module's preamble (see top of file) because
        # ``tests/unit/test_vcr_config.py`` imports
        # ``_SyntheticErrorTransport`` / ``_get_error_injection_mode`` from
        # ``notebooklm._core`` by name, and the master plan pins the
        # preamble surface as the public-on-private contract.
        #
        # Event-loop affinity guard rationale: the lifecycle captures
        # ``asyncio.get_running_loop()`` in ``_bound_loop`` at ``open()`` time
        # and the cross-loop check in ``_perform_authed_post`` (via
        # :class:`AuthedTransport`) does a cheap ``is`` comparison against
        # it. Each client is per-loop — the asyncio primitives we hold
        # (``_reqid_lock``, ``_refresh_lock``, ``_auth_snapshot_lock``,
        # ``_upload_semaphore``, ``_rpc_semaphore``, the ``httpx.AsyncClient``
        # pool, in-flight tasks like ``_refresh_task`` / ``_keepalive_task``)
        # are all bound to the loop that ``open()`` ran on; reusing them
        # under a different loop produces hangs and ``RuntimeError`` deep
        # in httpx instead of an actionable message at the call site.
        #
        # Prefer the explicit storage_path if provided (e.g.
        # ``NotebookLMClient(storage_path=...)`` with a manually-built
        # ``AuthTokens``), otherwise fall back to ``auth.storage_path``.
        _resolved_storage_path: Path | None = (
            keepalive_storage_path if keepalive_storage_path is not None else auth.storage_path
        )
        self._lifecycle = ClientLifecycle(
            timeout=timeout,
            connect_timeout=connect_timeout,
            limits=_resolved_limits,
            keepalive_interval=_resolve_keepalive_interval(keepalive, keepalive_min_interval),
            keepalive_storage_path=_resolved_storage_path,
        )
        # Owns the in-process save lock and open-time cookie baseline while
        # compatibility properties below keep the legacy private attribute
        # names observable for current tests and first-party callers.
        self.cookie_persistence = CookiePersistence(self.auth, _resolved_storage_path)
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

    # ``ClientMetrics`` compat bridges. The three observability ivars now live
    # on ``self._metrics_obj``; each setter calls ``_ensure_observability_state``
    # first so a ``__new__``-built fixture (no ``__init__`` ran) can still
    # assign ``core._on_rpc_event = cb`` and have it write through.
    @property
    def _metrics_lock(self) -> threading.Lock:
        self._ensure_observability_state()
        return self._metrics_obj._metrics_lock

    @_metrics_lock.setter
    def _metrics_lock(self, value: threading.Lock) -> None:
        self._ensure_observability_state()
        self._metrics_obj._metrics_lock = value

    @property
    def _metrics(self) -> ClientMetricsSnapshot:
        self._ensure_observability_state()
        return self._metrics_obj._metrics

    @_metrics.setter
    def _metrics(self, value: ClientMetricsSnapshot) -> None:
        self._ensure_observability_state()
        self._metrics_obj._metrics = value

    @property
    def _on_rpc_event(self) -> Callable[[RpcTelemetryEvent], object] | None:
        self._ensure_observability_state()
        return self._metrics_obj._on_rpc_event

    @_on_rpc_event.setter
    def _on_rpc_event(self, value: Callable[[RpcTelemetryEvent], object] | None) -> None:
        self._ensure_observability_state()
        self._metrics_obj._on_rpc_event = value

    # ``TransportDrainTracker`` compat bridges. The four drain ivars now live
    # on ``self._drain_tracker``; each setter calls
    # ``_ensure_observability_state`` first so a ``__new__``-built fixture
    # (no ``__init__`` ran) can still assign (e.g.) ``core._draining = True``
    # or ``core._drain_condition = asyncio.Condition()`` and have it write
    # through to a real helper.
    @property
    def _in_flight_posts(self) -> int:
        self._ensure_observability_state()
        return self._drain_tracker._in_flight_posts

    @_in_flight_posts.setter
    def _in_flight_posts(self, value: int) -> None:
        self._ensure_observability_state()
        self._drain_tracker._in_flight_posts = value

    @property
    def _draining(self) -> bool:
        self._ensure_observability_state()
        return self._drain_tracker._draining

    @_draining.setter
    def _draining(self, value: bool) -> None:
        self._ensure_observability_state()
        self._drain_tracker._draining = value

    @property
    def _drain_condition(self) -> asyncio.Condition | None:
        self._ensure_observability_state()
        return self._drain_tracker._drain_condition

    @_drain_condition.setter
    def _drain_condition(self, value: asyncio.Condition | None) -> None:
        self._ensure_observability_state()
        self._drain_tracker._drain_condition = value

    @property
    def _operation_depths(self) -> weakref.WeakKeyDictionary[asyncio.Task[Any], int]:
        self._ensure_observability_state()
        return self._drain_tracker._operation_depths

    @_operation_depths.setter
    def _operation_depths(self, value: weakref.WeakKeyDictionary[asyncio.Task[Any], int]) -> None:
        self._ensure_observability_state()
        self._drain_tracker._operation_depths = value

    # ------------------------------------------------------------------
    # ``AuthRefreshCoordinator`` compat bridges. Refresh/auth-snapshot state
    # now lives on ``self._auth_coord``; the four legacy ivar names are
    # preserved as writeable properties so the dozens of test sites that
    # do ``core._refresh_callback = stub`` / ``core._refresh_lock = asyncio.Lock()``
    # keep working without modification. ``_ensure_auth_coord`` mirrors the
    # ``_ensure_observability_state`` backfill so ``__new__``-built fixtures
    # (no ``__init__`` ran) still resolve cleanly.
    # ------------------------------------------------------------------

    def _ensure_auth_coord(self) -> None:
        """Backfill ``_auth_coord`` for tests that construct via ``__new__``.

        Mirrors :meth:`_ensure_observability_state` — uses a module-level
        threading lock for double-checked locking so two threads racing
        through ``hasattr`` cannot both decide they need to construct a
        coordinator and silently discard each other's locks/refresh task.

        Also primes ``_metrics_obj`` because every coordinator method reaches
        into ``host._metrics_obj`` (e.g. ``record_lock_wait`` inside
        :meth:`AuthRefreshCoordinator.snapshot` /
        :meth:`AuthRefreshCoordinator.update_auth_tokens`). Without this,
        a ``__new__``-built fixture that calls ``_await_refresh`` /
        ``_snapshot`` / ``update_auth_tokens`` before any observability
        compat-bridge setter would surface as
        ``AttributeError: '_StubCore' has no attribute '_metrics_obj'``
        rather than backfilling gracefully.
        """
        if hasattr(self, "_auth_coord"):
            return
        self._ensure_observability_state()
        with _AUTH_COORD_INIT_LOCK:
            if not hasattr(self, "_auth_coord"):
                self._auth_coord = AuthRefreshCoordinator(refresh_callback=None)

    @property
    def _refresh_lock(self) -> asyncio.Lock | None:
        self._ensure_auth_coord()
        return self._auth_coord._refresh_lock

    @_refresh_lock.setter
    def _refresh_lock(self, value: asyncio.Lock | None) -> None:
        self._ensure_auth_coord()
        self._auth_coord._refresh_lock = value

    @property
    def _refresh_task(self) -> asyncio.Task[AuthTokens] | None:
        self._ensure_auth_coord()
        return self._auth_coord._refresh_task

    @_refresh_task.setter
    def _refresh_task(self, value: asyncio.Task[AuthTokens] | None) -> None:
        self._ensure_auth_coord()
        self._auth_coord._refresh_task = value

    @property
    def _refresh_callback(self) -> Callable[[], Awaitable[AuthTokens]] | None:
        self._ensure_auth_coord()
        return self._auth_coord._refresh_callback

    @_refresh_callback.setter
    def _refresh_callback(self, value: Callable[[], Awaitable[AuthTokens]] | None) -> None:
        self._ensure_auth_coord()
        self._auth_coord._refresh_callback = value

    @property
    def _auth_snapshot_lock(self) -> asyncio.Lock | None:
        self._ensure_auth_coord()
        return self._auth_coord._auth_snapshot_lock

    @_auth_snapshot_lock.setter
    def _auth_snapshot_lock(self, value: asyncio.Lock | None) -> None:
        self._ensure_auth_coord()
        self._auth_coord._auth_snapshot_lock = value

    # ------------------------------------------------------------------
    # ``ClientLifecycle`` compat bridges. HTTP-client lifecycle state now
    # lives on ``self._lifecycle``; the eight legacy ivar names (
    # ``_http_client``, ``_bound_loop``, ``_keepalive_task``,
    # ``_keepalive_interval``, ``_keepalive_storage_path``, ``_timeout``,
    # ``_connect_timeout``, ``_limits``) are preserved here as
    # ``@property`` bridges. ``_http_client`` and ``_bound_loop`` carry
    # writeable setters because tests SET them directly (15+ sites for
    # ``_http_client``; ``_bound_loop`` setter is required by the master
    # plan for future C1 access patterns). The other six are getter-only
    # because no SET sites were found in the test surface.
    # ``_ensure_lifecycle`` mirrors the ``_ensure_observability_state`` /
    # ``_ensure_auth_coord`` backfill so ``__new__``-built fixtures (no
    # ``__init__`` ran) still resolve cleanly.
    # ------------------------------------------------------------------

    def _ensure_lifecycle(self) -> None:
        """Backfill ``_lifecycle`` for tests that construct via ``__new__``.

        Uses a module-level threading lock (the existing
        ``_OBSERVABILITY_INIT_LOCK``) for double-checked locking so two
        threads racing through ``hasattr`` cannot both decide they need to
        construct a lifecycle and silently discard each other's
        ``_http_client`` references.

        ``__new__``-built fixtures may not have the underlying timeout /
        limits attributes; we synthesise a minimally-configured lifecycle
        in that case (the same shape ``ClientCore.__init__`` would produce
        for default args).
        """
        if hasattr(self, "_lifecycle"):
            return
        with _OBSERVABILITY_INIT_LOCK:
            if not hasattr(self, "_lifecycle"):
                # Lazy import to break the types.py -> _core.py cycle.
                from .types import ConnectionLimits

                self._lifecycle = ClientLifecycle(
                    timeout=DEFAULT_TIMEOUT,
                    connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                    limits=ConnectionLimits(),
                    keepalive_interval=None,
                    keepalive_storage_path=None,
                )

    @property
    def _http_client(self) -> httpx.AsyncClient | None:
        self._ensure_lifecycle()
        return self._lifecycle._http_client

    @_http_client.setter
    def _http_client(self, value: httpx.AsyncClient | None) -> None:
        self._ensure_lifecycle()
        self._lifecycle._http_client = value

    @property
    def _bound_loop(self) -> asyncio.AbstractEventLoop | None:
        self._ensure_lifecycle()
        return self._lifecycle._bound_loop

    @_bound_loop.setter
    def _bound_loop(self, value: asyncio.AbstractEventLoop | None) -> None:
        self._ensure_lifecycle()
        self._lifecycle._bound_loop = value

    @property
    def _keepalive_task(self) -> asyncio.Task[None] | None:
        self._ensure_lifecycle()
        return self._lifecycle._keepalive_task

    @_keepalive_task.setter
    def _keepalive_task(self, value: asyncio.Task[None] | None) -> None:
        # Setter retained because these were plain ivars pre-extraction
        # (and Python attributes are writeable by default). No SET sites in
        # the current test surface, but mirroring the pre-extraction
        # contract avoids future friction.
        self._ensure_lifecycle()
        self._lifecycle._keepalive_task = value

    @property
    def _keepalive_interval(self) -> float | None:
        self._ensure_lifecycle()
        return self._lifecycle._keepalive_interval

    @_keepalive_interval.setter
    def _keepalive_interval(self, value: float | None) -> None:
        self._ensure_lifecycle()
        self._lifecycle._keepalive_interval = value

    @property
    def _keepalive_storage_path(self) -> Path | None:
        self._ensure_lifecycle()
        return self._lifecycle._keepalive_storage_path

    @_keepalive_storage_path.setter
    def _keepalive_storage_path(self, value: Path | None) -> None:
        self._ensure_lifecycle()
        self._lifecycle._keepalive_storage_path = value

    @property
    def _timeout(self) -> float:
        self._ensure_lifecycle()
        return self._lifecycle._timeout

    @_timeout.setter
    def _timeout(self, value: float) -> None:
        # Required by ``RpcOwner`` Protocol (``_core_rpc.py``) which
        # declares ``_timeout: float`` as a settable variable. Pre-extraction
        # ``_timeout`` was a plain ivar so attribute assignment worked
        # implicitly; the property bridge needs an explicit setter to
        # preserve that contract.
        self._ensure_lifecycle()
        self._lifecycle._timeout = value

    @property
    def _connect_timeout(self) -> float:
        self._ensure_lifecycle()
        return self._lifecycle._connect_timeout

    @_connect_timeout.setter
    def _connect_timeout(self, value: float) -> None:
        self._ensure_lifecycle()
        self._lifecycle._connect_timeout = value

    @property
    def _limits(self) -> "ConnectionLimits":
        self._ensure_lifecycle()
        return self._lifecycle._limits

    @_limits.setter
    def _limits(self, value: "ConnectionLimits") -> None:
        self._ensure_lifecycle()
        self._lifecycle._limits = value

    # ------------------------------------------------------------------
    # Request-id counter (chat API requires a monotonic ``_reqid`` URL param).
    #
    # Historical contract: callers did ``self._core._reqid_counter += 100000``
    # then read the new value. Two concurrent ``ChatAPI.ask`` calls on the same
    # core would race on the read-modify-write, producing duplicate ``_reqid``
    # values that Google rejects.
    #
    # New contract: ``await core.next_reqid()`` performs the increment under
    # ``_reqid_lock`` and returns the post-increment value. The state lives in
    # :class:`notebooklm._core_reqid.ReqidCounter` (``self._reqid``); the
    # properties below are read/write bridges that keep the legacy
    # ``_reqid_counter`` / ``_reqid_counter_value`` / ``_reqid_lock`` ivar
    # names observable for existing tests and first-party callers. Direct
    # mutation of ``_reqid_counter`` still works for backwards compatibility
    # but emits ``DeprecationWarning`` — bypass the warning for legitimate
    # test setup by writing to ``_reqid_counter_value`` instead.
    # ------------------------------------------------------------------

    @property
    def _reqid_counter(self) -> int:
        """Current request-id counter value. Read access is safe; write access
        via the property setter emits ``DeprecationWarning``.
        """
        return self._reqid.value

    @_reqid_counter.setter
    def _reqid_counter(self, value: int) -> None:
        warnings.warn(
            "Direct mutation of ClientCore._reqid_counter is deprecated; "
            "use `await core.next_reqid()` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._reqid.set_value(value)

    @property
    def _reqid_counter_value(self) -> int:
        """Bypass-warning bridge for tests that seed the counter directly.

        Writing through ``_reqid_counter`` emits a ``DeprecationWarning``;
        test fixtures that legitimately need to set the counter without
        paying that tax write to ``_reqid_counter_value`` instead.
        """
        return self._reqid.value

    @_reqid_counter_value.setter
    def _reqid_counter_value(self, value: int) -> None:
        self._reqid.set_value(value)

    @property
    def _reqid_lock(self) -> asyncio.Lock | None:
        """Bridge to the lazily-allocated reqid lock.

        Historically a plain ivar; preserved as a property with a setter so
        test fixtures that inject their own ``asyncio.Lock`` (or reset it to
        ``None`` to force re-allocation) continue to work.
        """
        return self._reqid._lock

    @_reqid_lock.setter
    def _reqid_lock(self, value: asyncio.Lock | None) -> None:
        self._reqid._lock = value

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

    async def next_reqid(self, step: int = _REQID_DEFAULT_STEP) -> int:
        """Atomically increment the request-id counter and return the new value.

        Thin facade over :meth:`ReqidCounter.next_reqid`. The default ``step``
        is sourced from :data:`notebooklm._core_reqid.DEFAULT_STEP` so the
        facade and the underlying helper cannot silently drift apart; see
        :class:`notebooklm._core_reqid.ReqidCounter` for the full contract,
        validation rules, and lazy-lock semantics.
        """
        return await self._reqid.next_reqid(step)

    def metrics_snapshot(self) -> ClientMetricsSnapshot:
        """Return cumulative observability counters for this client instance."""
        self._ensure_observability_state()
        return self._metrics_obj.snapshot()

    def _ensure_observability_state(self) -> None:
        """Backfill observability fields for tests that construct via ``__new__``.

        Gates on ``_metrics_obj`` AND ``_drain_tracker`` (both real instance
        attributes) — the property-bridged ivars' ``hasattr`` probes are
        always True because the descriptors live on the class.
        """
        if hasattr(self, "_metrics_obj") and hasattr(self, "_drain_tracker"):
            return
        with _OBSERVABILITY_INIT_LOCK:
            if not hasattr(self, "_metrics_obj"):
                self._metrics_obj = ClientMetrics(on_rpc_event=None)
            if not hasattr(self, "_drain_tracker"):
                self._drain_tracker = TransportDrainTracker()

    def _increment_metrics(self, **increments: int | float) -> None:
        self._ensure_observability_state()
        self._metrics_obj.increment(**increments)

    def _record_rpc_queue_wait(self, wait_seconds: float) -> None:
        self._ensure_observability_state()
        self._metrics_obj.record_rpc_queue_wait(wait_seconds)

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        """Record time spent waiting for the upload semaphore."""
        self._ensure_observability_state()
        self._metrics_obj.record_upload_queue_wait(wait_seconds)

    def _record_lock_wait(self, wait_seconds: float) -> None:
        self._ensure_observability_state()
        self._metrics_obj.record_lock_wait(wait_seconds)

    async def _emit_rpc_event(self, event: RpcTelemetryEvent) -> None:
        """Invoke the optional telemetry callback without affecting RPC behavior."""
        self._ensure_observability_state()
        await self._metrics_obj.emit_rpc_event(event)

    def _get_drain_condition(self) -> asyncio.Condition:
        self._ensure_observability_state()
        return self._drain_tracker.get_drain_condition()

    def _current_operation_depth(self, task: asyncio.Task[Any] | None) -> int:
        self._ensure_observability_state()
        return self._drain_tracker.current_operation_depth(task)

    async def _begin_transport_post(self, log_label: str) -> _TransportOperationToken:
        """Reject new top-level transport work once graceful drain has started."""
        self._ensure_observability_state()
        return await self._drain_tracker.begin_transport_post(log_label)

    async def _begin_transport_task(
        self,
        task: asyncio.Task[Any],
        log_label: str,
    ) -> _TransportOperationToken:
        """Admit an internally-spawned task as part of the current operation."""
        self._ensure_observability_state()
        return await self._drain_tracker.begin_transport_task(task, log_label)

    async def _finish_transport_post(self, token: _TransportOperationToken) -> None:
        self._ensure_observability_state()
        await self._drain_tracker.finish_transport_post(token)

    async def drain(self, timeout: float | None = None) -> None:
        """Stop accepting new client operations and wait for in-flight ones to finish.

        If ``timeout`` expires, ``TimeoutError`` is raised and the client
        remains in draining mode so shutdown callers do not accidentally admit
        new work after a missed deadline.
        """
        self._ensure_observability_state()
        await self._drain_tracker.drain(timeout)

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        """Return the per-instance upload semaphore, creating it on first use.

        The semaphore caps the number of in-flight ``SourcesAPI.add_file``
        uploads at ``max_concurrent_uploads`` (default
        ``DEFAULT_MAX_CONCURRENT_UPLOADS``). Each in-flight upload holds
        one open file descriptor for its duration, so the cap is also an
        FD-exhaustion guard.

        Scope of the cap:
          - The ``async with`` block in ``add_file`` covers FD-open,
            the two pre-upload RPCs (``_register_file_source`` and
            ``_start_resumable_upload``), and the streaming upload. The
            semaphore therefore also serializes those two RPCs — a side
            effect of the FD guard, not a separate quota.
          - The cap applies to the *blocking* ``add_file`` call. On
            post-finalize cancel, the shielded background
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
        ``notebooklm._core.is_auth_error`` or ``notebooklm._core.asyncio.sleep``
        still affect live transport behavior after the collaborator has been
        constructed. Backoff jitter routes through ``notebooklm._backoff``,
        which in turn calls ``random.uniform`` on the shared module.
        ``tests/unit/test_core_transport.py`` relies on monkeypatching
        ``notebooklm._core.random.uniform`` to reach that jitter path; keep the
        otherwise-unused module import so the path stays available. Attribute
        patches on the singleton ``random`` module are visible to all importers.
        """
        transport = getattr(self, "_authed_transport", None)
        if transport is None:
            transport = AuthedTransport(
                self,
                is_auth_error=lambda exc: is_auth_error(exc),
                sleep=lambda seconds: asyncio.sleep(seconds),
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

        Called automatically by NotebookLMClient.__aenter__. Delegates to
        :meth:`ClientLifecycle.open` — that helper builds the
        ``httpx.AsyncClient`` (with the opt-in
        :class:`_SyntheticErrorTransport` wrap when
        ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set), captures the running
        event loop into ``self._bound_loop``, and spawns the keepalive
        task. Idempotent — calling ``open()`` while already open is a
        no-op. Re-opening after a prior :meth:`close` intentionally
        replaces the loop binding; :meth:`close` does not unbind so an
        accidental cross-loop call after close still raises actionably.
        """
        self._ensure_lifecycle()
        await self._lifecycle.open(self)

    async def save_cookies(self, jar: httpx.Cookies, path: Path | None = None) -> None:
        """Persist a cookie jar through the shared cookie-persistence collaborator.

        Thin facade over :meth:`ClientLifecycle.save_cookies`. The storage
        writer ``save_cookies_to_storage`` is resolved from this module at
        call time inside the lifecycle helper so existing
        ``monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", …)``
        sites continue to affect the live save path.
        """
        self._ensure_lifecycle()
        await self._lifecycle.save_cookies(self, jar, path)

    async def close(self) -> None:
        """Close the HTTP client connection.

        Called automatically by NotebookLMClient.__aexit__. Delegates to
        :meth:`ClientLifecycle.close`, which:

        1. Cancels and joins the keepalive task (so the loop can't issue a
           poke against an already-closed transport).
        2. Drains in-flight artifact poll tasks held by ``self.poll_registry``.
        3. Saves cookies one last time through ``save_cookies``.
        4. Calls ``aclose()`` under :func:`asyncio.shield` so cancellation
           arriving mid-close cannot leak the underlying httpx transport.
        5. Nulls out ``_http_client``, ``_authed_transport`` and
           ``_rpc_executor`` so a follow-up :meth:`open` rebuilds the
           transport collaborators against the new ``httpx.AsyncClient``.
        """
        self._ensure_lifecycle()
        await self._lifecycle.close(self)

    async def _keepalive_loop(self, interval: float) -> None:
        """Background loop that periodically pokes the identity surface.

        Thin facade over :meth:`ClientLifecycle._keepalive_loop`. Retained
        as a ``ClientCore`` method so ``test_client_keepalive`` and other
        tests that introspect ``core._keepalive_loop`` continue to resolve.
        """
        self._ensure_lifecycle()
        await self._lifecycle._keepalive_loop(self, interval)

    @property
    def is_open(self) -> bool:
        """Check if the HTTP client is open."""
        self._ensure_lifecycle()
        return self._lifecycle.is_open()

    def update_auth_headers(self) -> None:
        """Refresh auth metadata without resetting the live cookie jar.

        Call this after modifying auth tokens (e.g., after refresh_auth())
        to ensure the HTTP client uses the updated credentials. Delegates
        to :meth:`AuthRefreshCoordinator.update_auth_headers`; the cookie
        jar source is fetched via ``self.get_http_client()`` so the open()
        precondition (and its ``RuntimeError`` if not initialised) is
        enforced at one site.

        Raises:
            RuntimeError: If client is not initialized.
        """
        self._ensure_auth_coord()
        self._auth_coord.update_auth_headers(self)

    def _get_auth_snapshot_lock(self) -> asyncio.Lock:
        """Return the lazily-initialised auth-snapshot lock.

        Delegates to :meth:`AuthRefreshCoordinator.get_auth_snapshot_lock`.
        The check-then-assign there is safe without an outer lock because
        asyncio is single-threaded — no other coroutine can execute between
        the ``is None`` check and the assignment unless we ``await`` (and
        the accessor does not).
        """
        self._ensure_auth_coord()
        return self._auth_coord.get_auth_snapshot_lock()

    def _get_refresh_lock(self) -> asyncio.Lock:
        """Return the lazily-initialised refresh lock.

        Delegates to :meth:`AuthRefreshCoordinator.get_refresh_lock`. Every
        concurrent caller resolves to the *same* lock instance because the
        check-then-assign is race-free in a single-threaded asyncio loop,
        so the single-flight refresh dedupe in :meth:`_await_refresh` is
        preserved.
        """
        self._ensure_auth_coord()
        return self._auth_coord.get_refresh_lock()

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

        Body is kept here as real code (rather than delegating to
        :meth:`AuthRefreshCoordinator.snapshot`) so the AST guard at
        ``tests/unit/test_concurrency_refresh_race.py::test_snapshot_acquires_auth_snapshot_lock``
        — which inspects this method's source and asserts it contains an
        ``async with`` over ``_auth_snapshot_lock`` — keeps operating on
        the real implementation. The coordinator method has the same
        semantic shape (lock acquire → scalar reads → return) but routes
        the lock-wait metric through the host's ``_metrics_obj`` directly
        rather than via the ``_record_lock_wait`` facade.

        Whole-request atomicity for ``(csrf, sid, cookies)`` on the wire
        still depends on the no-await invariant between this method
        returning and ``client.post(...)`` inside
        :meth:`_perform_authed_post` (see the related AST guard in
        ``tests/unit/test_concurrency_refresh_race.py``).
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
        """Atomically update auth token scalars under the snapshot lock.

        The body is kept here as real code (rather than a delegate to
        :meth:`AuthRefreshCoordinator.update_auth_tokens`) so the AST
        guard at ``tests/unit/test_concurrency_refresh_race.py:304-334``
        — which inspects the source of this method and asserts there is
        no ``await`` inside the csrf/session_id mutation block — keeps
        operating on the real implementation. The coordinator method
        has the same semantic shape (lock acquire → two scalar writes)
        but routes the lock-wait metric through the host's
        ``_metrics_obj`` directly rather than via ``_record_lock_wait``.
        """
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

        Delegates to :meth:`AuthRefreshCoordinator.await_refresh`. The
        coordinator preserves the single-flight semantics — concurrent
        callers share one refresh task so a thundering herd of 401s on the
        same client triggers exactly one token refresh. The lock protects
        task-creation only; the await on the task itself happens outside
        the lock so other callers can join, and the join is wrapped in
        :func:`asyncio.shield` so a cancelled waiter unwinds locally
        without propagating ``CancelledError`` into the shared task. The
        ``_refresh_task`` slot is left intact across cancellation and is
        replaced only on the next refresh wave once the current task
        transitions to ``done()``.
        """
        self._ensure_auth_coord()
        await self._auth_coord.await_refresh(self)

    async def query_post(
        self,
        *,
        build_request: _BuildRequest,
        parse_label: str,
    ) -> httpx.Response:
        """Compatibility wrapper around :meth:`RpcExecutor.query_post`.

        The chat-flavored exception mapping (``ChatError`` / ``NetworkError``
        translation, drain-aware operation-token bookkeeping) lives on the
        executor; this facade preserves the method shape so callers and tests
        can mock ``core.query_post = AsyncMock(...)`` by attribute.
        """
        return await self._get_rpc_executor().query_post(
            build_request=build_request,
            parse_label=parse_label,
        )

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
        """Compatibility wrapper around :meth:`RpcExecutor.execute_with_telemetry`.

        The executor owns the telemetry, reqid, drain, and decode-time
        refresh-and-retry plumbing; this facade preserves the method shape so
        the 30+ tests that mock ``core.rpc_call = AsyncMock(...)`` by
        attribute keep working. See
        :meth:`notebooklm._core_rpc.RpcExecutor.execute_with_telemetry` for
        the full contract (kwargs ``_is_retry`` / ``disable_internal_retries``
        flow through unchanged; ``RuntimeError`` is raised if the client is
        not initialized).
        """
        return await self._get_rpc_executor().execute_with_telemetry(
            method,
            params,
            source_path,
            allow_null,
            _is_retry,
            disable_internal_retries=disable_internal_retries,
        )

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

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        """Extract all source IDs from a notebook.

        Thin facade over :func:`notebooklm._sources.fetch_source_ids` —
        retained on :class:`ClientCore` because first-party callers and the
        test suite continue to invoke ``core.get_source_ids(...)``.

        Args:
            notebook_id: The notebook ID.

        Returns:
            List of source IDs. Empty list if no sources or on error.
        """
        return await fetch_source_ids(self, notebook_id)
