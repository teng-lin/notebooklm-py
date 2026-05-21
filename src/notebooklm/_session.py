"""Concrete session infrastructure for the NotebookLM API client."""

import asyncio
import logging
import random  # noqa: F401 - tests patch this for _backoff jitter
import threading
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import httpx

from ._authed_transport import (
    AuthedTransport,
    _AuthSnapshot,
    _BuildRequest,
)
from ._client_metrics import ClientMetrics
from ._cookie_persistence import CookiePersistence
from ._error_injection import _refuse_synthetic_error_outside_test_context
from ._kernel import Kernel
from ._loop_affinity import assert_bound_loop
from ._middleware import (
    Middleware,
    NextCall,
    RpcRequest,
    RpcResponse,
    build_chain,
)
from ._middleware_chain import MiddlewareChainBuilder
from ._middleware_semaphore import RPC_QUEUE_WAIT_CONTEXT_KEY
from ._polling_registry import PendingPolls, PollRegistry
from ._reqid_counter import DEFAULT_STEP as _REQID_DEFAULT_STEP
from ._reqid_counter import ReqidCounter
from ._rpc_executor import RpcExecutor
from ._session_auth import AuthRefreshCoordinator
from ._session_config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
    normalize_max_concurrent_uploads,
)
from ._session_helpers import _resolve_keepalive_interval
from ._session_lifecycle import ClientLifecycle, CookieRotator, CookieSaver
from ._transport_drain import TransportDrainTracker, _TransportOperationToken
from .auth import (
    AuthTokens,
    CookieSnapshot,
)
from .auth import (
    authuser_query as _authuser_query_value,
)
from .auth import (
    format_authuser_value as _format_authuser_header_value,
)
from .types import ClientMetricsSnapshot, RpcTelemetryEvent

if TYPE_CHECKING:
    from .types import ConnectionLimits

from .rpc import RPCMethod

logger = logging.getLogger(__name__)
_OBSERVABILITY_INIT_LOCK = threading.Lock()
# Guards ``_auth_coord`` backfill on ``__new__``-built fixtures. Mirrors the
# observability init lock so two threads can't both observe ``hasattr is False``
# and race to construct competing :class:`AuthRefreshCoordinator` instances.
#
# Auth-snapshot canonical implementation lives on
# :class:`AuthRefreshCoordinator` (``_session_auth.py`` —
# ``AuthRefreshCoordinator.snapshot`` / ``.update_auth_tokens``). The
# :class:`Session` methods of the same name (``Session._snapshot`` /
# ``Session.update_auth_tokens``) are thin delegates that forward through
# ``self._auth_coord``; PR 8 collapsed their pre-PR-8 real bodies. The AST
# guards in ``tests/unit/test_concurrency_refresh_race.py``
# (``test_snapshot_acquires_auth_snapshot_lock`` /
# ``test_update_auth_tokens_has_no_await_inside_mutation_block``) inspect
# the coordinator's source via ``inspect.getsource(...)`` + AST parsing —
# changes to auth-snapshot invariants must be applied to the coordinator
# (not the delegates here). Grep anchor for future maintainers:
# ``_AUTH_COORD_INIT_LOCK``.
_AUTH_COORD_INIT_LOCK = threading.Lock()


def _decode_response_late_bound(raw: str, rpc_id: str, *, allow_null: bool = False) -> Any:
    # Phase 2 PR 5 (``.sisyphus/plans/refactor-completion-plan.md``):
    # imports ``decode_response`` from the canonical :mod:`notebooklm.rpc`
    # surface rather than the legacy ``notebooklm._core`` compatibility
    # shim. Tests that patched ``notebooklm._core.decode_response`` are
    # re-targeted to ``notebooklm.rpc.decode_response`` in the same
    # commit so the live RPC decode path stays patchable end-to-end.
    from .rpc import decode_response

    return decode_response(raw, rpc_id, allow_null=allow_null)


def _sleep_late_bound(seconds: float) -> Awaitable[Any]:
    """Late-bound ``asyncio.sleep`` for tests that patch the module seam.

    Tests patch ``notebooklm._session.asyncio.sleep`` (this module is
    where the symbol is referenced) — e.g. ``test_authed_transport.py``
    and ``test_rpc_executor.py``. Patching the ``asyncio.sleep``
    attribute on the module singleton affects this function regardless
    of whether the ``import asyncio`` lives at module top or inside the
    body, because both forms resolve through the same ``asyncio`` module
    object; the function-body import is kept for symmetry with the
    other late-bound seams in this module.
    """
    import asyncio

    return asyncio.sleep(seconds)


def _live_is_auth_error(exc: Exception) -> bool:
    """Resolve ``is_auth_error`` against the canonical seam at call time.

    Python function-body name lookup hits the module ``__dict__`` on each
    call, so a ``monkeypatch.setattr("notebooklm._session_helpers.is_auth_error", ...)``
    swap is observed immediately. Used by every chain seed site that
    wires ``AuthRefreshMiddleware`` and by ``RpcExecutor`` so the
    project-wide test idiom of patching the symbol on the canonical
    module stays live without each seed site re-implementing the lambda.
    The historical ``notebooklm._core`` indirection was removed in
    v0.5.0 when the ``_core`` compatibility shim was deleted.
    """
    from ._session_helpers import is_auth_error

    return is_auth_error(exc)


class Session:
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
        cookie_saver: CookieSaver | None = None,
        cookie_rotator: CookieRotator | None = None,
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
                inside ``Session``).
            on_rpc_event: Optional callback invoked after each logical
                ``rpc_call`` succeeds or fails. The callback receives a
                backend-agnostic :class:`RpcTelemetryEvent`; exceptions raised
                by the callback are logged and never mask the RPC result.
            cookie_saver: Optional injectable seam (Phase 2 PR 3) overriding
                the on-disk cookie writer used by
                :meth:`ClientLifecycle.save_cookies`. ``None`` (default)
                resolves to :func:`_default_cookie_saver`, which late-binds
                to ``notebooklm._auth.storage.save_cookies_to_storage`` so
                the canonical-seam monkeypatch surface keeps affecting the
                live path. Must be sync (``def``, not ``async def``) — it
                runs inside ``asyncio.to_thread``. Custom callables bypass
                the late-bind hop entirely.
            cookie_rotator: Optional injectable seam (Phase 2 PR 3)
                overriding the keepalive-loop rotator. ``None`` (default)
                resolves to :func:`_default_cookie_rotator`, which late-binds
                to ``notebooklm._auth.keepalive._rotate_cookies``. Must be
                async — it is awaited from :meth:`ClientLifecycle._keepalive_loop`.

        Raises:
            ValueError: If ``keepalive`` or ``keepalive_min_interval`` is not a
                positive finite number, or if ``max_concurrent_uploads`` /
                ``max_concurrent_rpcs`` is a non-positive integer.
            RuntimeError: If ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set to a
                recognised mode without a ``PYTEST_CURRENT_TEST`` environment
                marker. The env var is test-only — see
                :func:`_refuse_synthetic_error_outside_test_context`.
        """
        # P1-12: refuse instantiation if the test-only synthetic-error env var
        # is set without pytest context. Catches leaked deploy envs at the
        # earliest opportunity, before any HTTP client is constructed. The
        # guard is a no-op for the normal production path (env var unset)
        # and for legitimate pytest contexts (PYTEST_CURRENT_TEST set).
        _refuse_synthetic_error_outside_test_context()
        # Lazy import to break the types.py -> _core.py cycle.
        from .types import ConnectionLimits

        self.auth = auth
        # HTTP timeouts, connection limits, keepalive interval / storage_path,
        # the live ``httpx.AsyncClient``, the captured ``_bound_loop``, and
        # the keepalive background task all live on ``self._lifecycle``
        # (constructed below alongside the other extracted helpers so the
        # inter-helper dependency order is obvious). Compat properties further
        # down preserve the legacy ``_timeout`` / ``_http_client`` /
        # ``_bound_loop`` / ``_keepalive_task`` / ``_keepalive_interval`` /
        # ``_keepalive_storage_path`` ivar names for tests and first-party
        # callers that probe or assign them directly. The
        # ``_connect_timeout`` / ``_limits`` bridges were dropped in
        # D1-audit-full; access them via ``self._lifecycle`` if needed.
        _resolved_limits = limits if limits is not None else ConnectionLimits()
        # ``_refresh_retry_delay`` stays here directly — it is read on the
        # RPC retry path by ``RpcExecutor`` and ``AuthedTransport`` and SET
        # by integration tests against ``client._session``. The refresh
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
        # Keep fail-fast validation for private Session callers, but the
        # actual upload semaphore state is owned by ``SourceUploadPipeline``.
        normalize_max_concurrent_uploads(max_concurrent_uploads)
        # RPC-fanout throttle. ``None`` means "no
        # gate" (caller has an external rate-limiter, or this is a
        # single-shot CLI invocation). Default ``DEFAULT_MAX_CONCURRENT_RPCS``
        # (16) sits well below the default ``ConnectionLimits.max_connections``
        # so helper GET/POSTs outside the RPC pipeline still have pool
        # headroom. Cross-validation with ``limits.max_connections`` is
        # enforced one layer up at ``NotebookLMClient.__init__`` because
        # ``Session`` synthesizes its own ``ConnectionLimits()`` when
        # ``limits=None``, masking the relationship at this layer.
        if max_concurrent_rpcs is None:
            self._max_concurrent_rpcs: int | None = None
        else:
            if max_concurrent_rpcs < 1:
                raise ValueError(f"max_concurrent_rpcs must be >= 1, got {max_concurrent_rpcs!r}")
            self._max_concurrent_rpcs = max_concurrent_rpcs
        # Lazily-created because ``asyncio.Semaphore()`` binds to the
        # running loop in some Python versions. Per-instance, never
        # module-global. When
        # ``_max_concurrent_rpcs is None``, the accessor returns a
        # ``contextlib.nullcontext`` instead — see ``_get_rpc_semaphore``.
        self._rpc_semaphore: asyncio.Semaphore | None = None
        # Observability counters + telemetry callback. Compat properties
        # below (``_metrics_lock`` / ``_metrics`` / ``_on_rpc_event``) bridge
        # the legacy ivar names back into this helper.
        self._metrics_obj = ClientMetrics(on_rpc_event=on_rpc_event)
        # Transport drain bookkeeping (in-flight posts, drain condition,
        # per-task operation depth, draining flag). Compat properties below
        # (``_in_flight_posts`` / ``_drain_condition`` / ``_draining``)
        # bridge the legacy ivar names back into this helper. The
        # ``_operation_depths`` bridge was dropped in D1-audit-full; access
        # the WeakKeyDictionary on ``self._drain_tracker`` directly. The
        # helper's ``__init__`` is event-loop-agnostic; the
        # ``asyncio.Condition`` is created lazily on first
        # ``get_drain_condition`` call.
        self._drain_tracker = TransportDrainTracker()
        # Request ID counter for chat API (must be unique per request).
        # The :class:`ReqidCounter` helper owns the monotonic ``_value`` and
        # the lazily-allocated ``asyncio.Lock`` that serialises mutation.
        # The ``_reqid_counter`` compat property below bridges the legacy
        # ivar name back into this helper; ``_reqid_counter_value`` and
        # ``_reqid_lock`` bridges were dropped in D1-audit-full (access
        # ``self._reqid.value`` / ``self._reqid._lock`` directly if needed).
        # The ``on_lock_wait`` hook keeps the
        # cumulative ``lock_wait_seconds_*`` metrics ticking inside
        # ``self._metrics_obj`` even though the counter is now extracted.
        self._reqid = ReqidCounter(on_lock_wait=self._record_lock_wait)
        # Auth refresh coordination — single-flight refresh task, snapshot
        # serialization, and cookie-jar sync. The coordinator owns
        # ``_refresh_lock``, ``_refresh_task``, ``_refresh_callback``, and
        # ``_auth_snapshot_lock``; field names match the legacy
        # ``Session`` ivars so the surviving compat properties
        # (``_refresh_lock``, ``_refresh_task``, ``_refresh_callback``)
        # delegate cleanly. The ``_auth_snapshot_lock`` bridge was dropped
        # in D1-audit-full; the live lock is reachable via
        # :meth:`_get_auth_snapshot_lock`.
        # The auth snapshot lock is intentionally distinct from
        # ``_refresh_lock`` — mixing them would re-introduce the
        # reentrancy ambiguity that snapshot-side serialization was added
        # to avoid. The attribute name ``_auth_coord`` is part of the
        # inter-helper contract for the upcoming B2/C1 extractions; do not
        # rename.
        self._auth_coord = AuthRefreshCoordinator(refresh_callback=refresh_callback)
        # HTTP-client lifecycle — owns loop binding, keepalive, and close
        # ordering while delegating the live ``httpx.AsyncClient`` to
        # ``self._kernel``. Compat properties further down preserve the legacy
        # ivar names. The ``_resolve_keepalive_interval`` clamp lives in
        # :mod:`notebooklm._session_helpers` and is imported above; we call
        # it directly here. (The historical ``notebooklm._core`` re-export
        # was removed in v0.5.0.)
        #
        # Event-loop affinity guard rationale: the lifecycle captures
        # ``asyncio.get_running_loop()`` in ``_bound_loop`` at ``open()`` time
        # and the cross-loop check in ``_perform_authed_post`` (via
        # :class:`AuthedTransport`) does a cheap ``is`` comparison against
        # it. Each client is per-loop — the asyncio primitives we hold
        # (``_reqid_lock``, ``_refresh_lock``, ``_auth_snapshot_lock``,
        # ``_rpc_semaphore``, the ``httpx.AsyncClient``
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
        self._kernel = Kernel(async_client_factory=httpx.AsyncClient)
        self._lifecycle = ClientLifecycle(
            timeout=timeout,
            connect_timeout=connect_timeout,
            limits=_resolved_limits,
            keepalive_interval=_resolve_keepalive_interval(keepalive, keepalive_min_interval),
            keepalive_storage_path=_resolved_storage_path,
            kernel=self._kernel,
            # Phase 2 PR 3 injectable seams. ``None`` is forwarded so the
            # lifecycle's ``or _default_*`` resolves to the late-binding
            # wrapper — preserving the existing ``_core`` monkeypatch
            # surface for unchanged callers.
            cookie_saver=cookie_saver,
            cookie_rotator=cookie_rotator,
        )
        # Owns the in-process save lock and open-time cookie baseline while
        # compatibility properties below keep the legacy private attribute
        # names observable for current tests and first-party callers.
        self.cookie_persistence = CookiePersistence(self.auth, _resolved_storage_path)
        self._drain_hooks: dict[str, Callable[[], Awaitable[None]]] = {}
        # Compatibility-only: active artifact polling state is owned by ArtifactsAPI.
        self.poll_registry: PollRegistry = PollRegistry()
        self._authed_transport: AuthedTransport | None = None
        self._rpc_executor: RpcExecutor | None = None
        # ADR-009 chain construction. PR history, leaf exception shape,
        # and ``RpcRequest.context`` contract live in
        # ``_middleware_chain.py`` module docstring.
        self._chain_builder = MiddlewareChainBuilder(
            drain_tracker=self._drain_tracker,
            metrics=self._metrics_obj,
            rpc_semaphore_factory=self._get_rpc_semaphore,
            rate_limit_max_retries_provider=lambda: self._rate_limit_max_retries,
            server_error_max_retries_provider=lambda: self._server_error_max_retries,
            refresh_retry_delay_provider=lambda: self._refresh_retry_delay,
            refresh_callable=self._await_refresh,
            is_auth_error=_live_is_auth_error,
            refresh_callback_enabled_provider=lambda: self._auth_coord.has_refresh_callback,
        )
        self._middlewares: list[Middleware] = self._chain_builder.build()
        self._authed_post_chain: NextCall = build_chain(
            self._middlewares,
            self._authed_post_chain_terminal,
        )

    @property
    def _save_lock(self) -> threading.Lock:
        """Compatibility bridge to ``CookiePersistence``'s in-process save lock."""
        return self.cookie_persistence.save_lock

    # ``_save_lock`` setter dropped in arch-d2-cutover: zero external callers.

    @property
    def _loaded_cookie_snapshot(self) -> CookieSnapshot | None:
        """Compatibility bridge to the cookie save baseline.

        Phase 4 deleted the matching ``.setter``; write on
        ``self.cookie_persistence.loaded_cookie_snapshot`` directly.
        """
        return self.cookie_persistence.loaded_cookie_snapshot

    # ``ClientMetrics`` compat bridges. The three observability ivars now live
    # on ``self._metrics_obj``; the read-side bridges call
    # ``_ensure_observability_state`` first so a ``__new__``-built fixture
    # (no ``__init__`` ran) can still read through. Phase 4 deleted the
    # matching ``.setter`` halves — write on ``self._metrics_obj.X``
    # directly (after calling ``_ensure_observability_state()`` if you
    # constructed via ``Session.__new__``).
    @property
    def _metrics_lock(self) -> threading.Lock:
        self._ensure_observability_state()
        return self._metrics_obj._metrics_lock

    @property
    def _metrics(self) -> ClientMetricsSnapshot:
        self._ensure_observability_state()
        return self._metrics_obj._metrics

    @property
    def _on_rpc_event(self) -> Callable[[RpcTelemetryEvent], object] | None:
        self._ensure_observability_state()
        return self._metrics_obj._on_rpc_event

    # ``TransportDrainTracker`` compat bridges. The four drain ivars now live
    # on ``self._drain_tracker``; the read-side bridges call
    # ``_ensure_observability_state`` first so a ``__new__``-built fixture
    # (no ``__init__`` ran) can still read through. Phase 4 deleted the
    # matching ``.setter`` halves — write on ``self._drain_tracker.X``
    # directly.
    @property
    def _in_flight_posts(self) -> int:
        self._ensure_observability_state()
        return self._drain_tracker._in_flight_posts

    @property
    def _draining(self) -> bool:
        self._ensure_observability_state()
        return self._drain_tracker._draining

    @property
    def _drain_condition(self) -> asyncio.Condition | None:
        self._ensure_observability_state()
        return self._drain_tracker._drain_condition

    # ``_operation_depths`` compat bridge dropped (D1-audit-full): zero
    # external callers; direct ivar lives on ``self._drain_tracker``.

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
        """Phase 4 deleted the matching ``.setter``; write on
        ``self._auth_coord._refresh_lock`` directly.
        """
        self._ensure_auth_coord()
        return self._auth_coord._refresh_lock

    @property
    def _refresh_task(self) -> asyncio.Task[AuthTokens] | None:
        self._ensure_auth_coord()
        return self._auth_coord._refresh_task

    # ``_refresh_task`` setter dropped in arch-d2-cutover: zero external callers.

    @property
    def _refresh_callback(self) -> Callable[[], Awaitable[AuthTokens]] | None:
        self._ensure_auth_coord()
        return self._auth_coord._refresh_callback

    @_refresh_callback.setter
    def _refresh_callback(self, value: Callable[[], Awaitable[AuthTokens]] | None) -> None:
        self._ensure_auth_coord()
        self._auth_coord._refresh_callback = value

    # ``_auth_snapshot_lock`` compat bridge dropped (D1-audit-full): zero
    # external callers. Live accessor remains ``_get_auth_snapshot_lock()`` /
    # ``AuthRefreshCoordinator.get_auth_snapshot_lock()``.

    # ------------------------------------------------------------------
    # ``ClientLifecycle`` compat bridges. HTTP-client lifecycle state now
    # lives on ``self._lifecycle``; the six surviving legacy ivar names
    # (``_http_client``, ``_bound_loop``, ``_keepalive_task``,
    # ``_keepalive_interval``, ``_keepalive_storage_path``, ``_timeout``)
    # are preserved here as ``@property`` bridges. The
    # ``_connect_timeout`` / ``_limits`` bridges were dropped in
    # D1-audit-full (zero external callers). The ``_timeout`` bridge is
    # retained because ``RpcExecutor`` (``_rpc_executor.py``) reads
    # ``self._owner._timeout`` via the :class:`RpcOwner` Protocol; removing
    # it would surface as ``AttributeError`` on every RPC call.
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
        in that case (the same shape ``Session.__init__`` would produce
        for default args).
        """
        if hasattr(self, "_lifecycle"):
            return
        with _OBSERVABILITY_INIT_LOCK:
            if not hasattr(self, "_lifecycle"):
                # Lazy import to break the types.py -> _core.py cycle.
                from .types import ConnectionLimits

                self._kernel = Kernel(async_client_factory=httpx.AsyncClient)
                self._lifecycle = ClientLifecycle(
                    timeout=DEFAULT_TIMEOUT,
                    connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                    limits=ConnectionLimits(),
                    keepalive_interval=None,
                    keepalive_storage_path=None,
                    kernel=self._kernel,
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
        # Required by the ``_AuthedTransportHost`` Protocol (declares
        # ``_bound_loop`` as a settable variable). No external SET sites,
        # but the Protocol contract demands a settable property.
        self._ensure_lifecycle()
        self._lifecycle._bound_loop = value

    @property
    def _keepalive_task(self) -> asyncio.Task[None] | None:
        self._ensure_lifecycle()
        return self._lifecycle._keepalive_task

    # ``_keepalive_task`` setter dropped in arch-d2-cutover: zero external callers.

    @property
    def _keepalive_interval(self) -> float | None:
        """Phase 4 deleted the matching ``.setter`` (zero external write
        sites); write on ``self._lifecycle._keepalive_interval`` directly
        if a test needs to override it.
        """
        self._ensure_lifecycle()
        return self._lifecycle._keepalive_interval

    @property
    def _keepalive_storage_path(self) -> Path | None:
        self._ensure_lifecycle()
        return self._lifecycle._keepalive_storage_path

    # ``_keepalive_storage_path`` setter dropped in arch-d2-cutover: zero
    # external callers.

    @property
    def _timeout(self) -> float:
        self._ensure_lifecycle()
        return self._lifecycle._timeout

    @_timeout.setter
    def _timeout(self, value: float) -> None:
        # Required by ``RpcOwner`` Protocol (``_rpc_executor.py``) which
        # declares ``_timeout: float`` as a settable variable. Pre-extraction
        # ``_timeout`` was a plain ivar so attribute assignment worked
        # implicitly; the property bridge needs an explicit setter to
        # preserve that contract.
        self._ensure_lifecycle()
        self._lifecycle._timeout = value

    # ``_connect_timeout`` and ``_limits`` compat bridges dropped
    # (D1-audit-full): zero external callers; live values remain on
    # ``self._lifecycle`` (and the lifecycle helper reads them as plain
    # ivars when it builds the ``httpx.AsyncClient``).

    # ------------------------------------------------------------------
    # Request-id counter (chat API requires a monotonic ``_reqid`` URL param).
    #
    # Historical contract: callers did ``self._session._reqid_counter += 100000``
    # then read the new value. Two concurrent ``ChatAPI.ask`` calls on the same
    # core would race on the read-modify-write, producing duplicate ``_reqid``
    # values that Google rejects.
    #
    # New contract: ``await core.next_reqid()`` performs the increment under
    # ``ReqidCounter._lock`` and returns the post-increment value. The state
    # lives in :class:`notebooklm._reqid_counter.ReqidCounter` (``self._reqid``);
    # the ``_reqid_counter`` property below is the last surviving read/write
    # bridge — direct mutation of ``_reqid_counter`` still works for
    # backwards compatibility but emits ``DeprecationWarning``. The
    # ``_reqid_counter_value`` / ``_reqid_lock`` compat bridges were dropped
    # (D1-audit-full): zero external callers; tests that need to seed the
    # counter or substitute the lock should reach through ``self._reqid``
    # directly.
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
            "Direct mutation of Session._reqid_counter is deprecated; "
            "use `await core.next_reqid()` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._reqid.set_value(value)

    @property
    def _pending_polls(self) -> PendingPolls:
        """Deprecated compatibility view of ``poll_registry.pending``.

        The active artifact polling registry is feature-owned. Phase 4
        deleted the matching ``.setter``; write on
        ``self.poll_registry.pending`` directly. The read-side bridge
        remains for external callers that still query
        ``Session._pending_polls``.
        """
        return self.poll_registry.pending

    def register_drain_hook(self, name: str, hook: Callable[[], Awaitable[None]]) -> None:
        """Register or replace a feature-owned close-time drain hook."""
        self._drain_hooks[name] = hook

    async def next_reqid(self, step: int = _REQID_DEFAULT_STEP) -> int:
        """Atomically increment the request-id counter and return the new value.

        Thin facade over :meth:`ReqidCounter.next_reqid`. The default ``step``
        is sourced from :data:`notebooklm._reqid_counter.DEFAULT_STEP` so the
        facade and the underlying helper cannot silently drift apart; see
        :class:`notebooklm._reqid_counter.ReqidCounter` for the full contract,
        validation rules, and lazy-lock semantics.
        """
        return await self._reqid.next_reqid(step)

    def metrics_snapshot(self) -> ClientMetricsSnapshot:
        """Return cumulative observability counters for this client instance."""
        self._ensure_observability_state()
        return self._metrics_obj.snapshot()

    def _ensure_observability_state(self) -> None:
        """Backfill observability fields for tests that construct via ``__new__``.

        Gates on ``_metrics_obj`` AND ``_drain_tracker`` AND
        ``_max_concurrent_rpcs`` (all three are real instance attributes,
        not property-bridged ivars whose ``hasattr`` probes would always
        be True because the descriptors live on the class). The
        ``_max_concurrent_rpcs`` slot was added by PR 12.9 audit-find #1
        so a ``__new__``-built fixture can call ``_perform_authed_post``
        without ``AttributeError`` when ``SemaphoreMiddleware`` reads the
        accessor.
        """
        if (
            hasattr(self, "_metrics_obj")
            and hasattr(self, "_drain_tracker")
            and hasattr(self, "_max_concurrent_rpcs")
        ):
            return
        with _OBSERVABILITY_INIT_LOCK:
            if not hasattr(self, "_metrics_obj"):
                self._metrics_obj = ClientMetrics(on_rpc_event=None)
            if not hasattr(self, "_drain_tracker"):
                self._drain_tracker = TransportDrainTracker()
            if not hasattr(self, "_max_concurrent_rpcs"):
                # Audit-find #1 (PR 12.9): the RPC semaphore wraps the
                # chain dispatch in ``_perform_authed_post`` and reads
                # this attr to decide whether to gate at all. A
                # ``__new__``-built fixture must see ``None`` (no gate)
                # so the first authed-POST call doesn't ``AttributeError``
                # — the live ``Session.__init__`` path sets it
                # explicitly to either ``DEFAULT_MAX_CONCURRENT_RPCS`` or
                # the operator's override.
                self._max_concurrent_rpcs = None

    def _ensure_authed_post_chain(self) -> None:
        """Backfill the middleware chain for tests that construct via ``__new__``.

        Mirrors :meth:`_ensure_observability_state` — a ``__new__``-built
        fixture skips ``__init__`` and so misses ``_chain_builder``,
        ``_middlewares``, and ``_authed_post_chain``. The first call to
        :meth:`_perform_authed_post` on such a fixture would raise
        ``AttributeError``; this helper backfills all three slots via
        :class:`MiddlewareChainBuilder` with the same chain shape that
        ``__init__`` would have produced (ADR-009 ordering pinned by
        ``tests/unit/test_chain_wiring.py:235`` and at builder level by
        ``tests/unit/test_middleware_chain_builder.py``).

        Guarded by :data:`_OBSERVABILITY_INIT_LOCK` for the same reason
        :meth:`_ensure_observability_state` is — two threads observing
        ``hasattr is False`` simultaneously must not both construct a
        chain (one would clobber the other and break the
        ``self._middlewares`` ↔ ``self._authed_post_chain`` linkage).
        """
        if hasattr(self, "_authed_post_chain"):
            return
        # ``MetricsMiddleware`` needs ``self._metrics_obj``, which a
        # ``__new__``-built fixture hasn't constructed yet. Run the
        # observability backfill BEFORE acquiring
        # ``_OBSERVABILITY_INIT_LOCK`` (its own contract is "no-op when
        # already initialized" and it takes the same lock internally —
        # acquiring twice on this thread would deadlock since the lock
        # is a plain :class:`threading.Lock`, not a reentrant lock).
        self._ensure_observability_state()
        with _OBSERVABILITY_INIT_LOCK:
            if hasattr(self, "_authed_post_chain"):
                return
            self._ensure_auth_coord()
            if not hasattr(self, "_middlewares"):
                if not hasattr(self, "_chain_builder"):
                    # __new__-fixture path: __init__ was bypassed. Build
                    # the builder with safe getattr-defaults mirroring
                    # __init__'s argument defaults so a fixture that
                    # never set the attrs still gets sane middleware
                    # instances. ``_drain_tracker`` and ``_metrics_obj``
                    # are guaranteed populated by the
                    # ``_ensure_observability_state()`` call above the
                    # lock.
                    self._chain_builder = MiddlewareChainBuilder(
                        drain_tracker=self._drain_tracker,
                        metrics=self._metrics_obj,
                        rpc_semaphore_factory=self._get_rpc_semaphore,
                        rate_limit_max_retries_provider=lambda: getattr(
                            self, "_rate_limit_max_retries", 3
                        ),
                        server_error_max_retries_provider=lambda: getattr(
                            self, "_server_error_max_retries", 3
                        ),
                        # ``getattr`` default matches ``__init__``'s argument
                        # default (``refresh_retry_delay: float = 0.2``) so a
                        # ``__new__``-built fixture that never set this attr
                        # sees the same post-refresh sleep as the normal
                        # path. (Restores the alignment first landed in PR
                        # #850 / commit 73cf680 — inadvertently reverted to
                        # 0.0 during the PR 12.9 cleanup refactor; CodeRabbit
                        # caught the regression on PR #883.)
                        refresh_retry_delay_provider=lambda: getattr(
                            self, "_refresh_retry_delay", 0.2
                        ),
                        refresh_callable=self._await_refresh,
                        is_auth_error=_live_is_auth_error,
                        refresh_callback_enabled_provider=(
                            lambda: self._auth_coord.has_refresh_callback
                        ),
                    )
                self._middlewares = self._chain_builder.build()
            self._authed_post_chain = build_chain(
                self._middlewares,
                self._authed_post_chain_terminal,
            )

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

    # Session/support surface consumed by feature APIs and private helpers.
    @property
    def kernel(self) -> Kernel:
        self._ensure_lifecycle()
        return self._kernel

    @property
    def authuser(self) -> int:
        return self.auth.authuser

    @property
    def account_email(self) -> str | None:
        return self.auth.account_email

    def authuser_query(self) -> str:
        return _authuser_query_value(self.authuser, self.account_email)

    def authuser_header(self) -> str:
        return _format_authuser_header_value(self.authuser, self.account_email)

    def live_cookies(self) -> httpx.Cookies:
        return self.get_http_client().cookies

    @property
    def bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the open-time captured event loop for affinity checks.

        Defensive ``isinstance`` so a ``MagicMock``-shaped fixture whose
        ``_lifecycle`` auto-vivifies into a mock doesn't synthesize a fake
        loop object that the affinity helper would otherwise treat as a
        real (mismatched) loop. Returns ``None`` when the underlying core
        has no lifecycle or has not been opened; the affinity helper
        treats ``None`` as a silent no-op.
        """
        lifecycle = getattr(self, "_lifecycle", None)
        if lifecycle is None:
            return None
        loop = lifecycle.get_bound_loop()
        return loop if isinstance(loop, asyncio.AbstractEventLoop) else None

    def assert_bound_loop(self) -> None:
        """Raise if this core is used from a loop other than its open-time loop."""
        assert_bound_loop(self.bound_loop)

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

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]:
        """Return a drain-tracked operation scope for feature-owned work."""

        @asynccontextmanager
        async def scope() -> AsyncIterator[None]:
            token = await self._begin_transport_post(label)
            try:
                yield None
            finally:
                await self._finish_transport_post(token)

        return scope()

    async def drain(self, timeout: float | None = None) -> None:
        """Stop accepting new client operations and wait for in-flight ones to finish.

        If ``timeout`` expires, ``TimeoutError`` is raised and the client
        remains in draining mode so shutdown callers do not accidentally admit
        new work after a missed deadline.
        """
        self._ensure_observability_state()
        await self._drain_tracker.drain(timeout)

    def _get_rpc_semaphore(self) -> AbstractAsyncContextManager[Any]:
        """Return the per-instance RPC semaphore (or a null-context).

        When ``max_concurrent_rpcs`` was set to ``None`` at construction
        time, this returns a :class:`contextlib.nullcontext` so the
        ``async with`` wrapper in :meth:`_perform_authed_post` collapses
        to a no-op (callers with their own external rate-limiter opted
        out of the gate). Otherwise it lazily constructs an
        ``asyncio.Semaphore`` bound to the running loop on first use,
        mirroring the lazy-init pattern of :attr:`_reqid_lock` /
        :attr:`_auth_snapshot_lock`.

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
        ``notebooklm._session_helpers.is_auth_error`` or
        ``notebooklm._session.asyncio.sleep`` still affect live transport
        behavior after the collaborator has been constructed. Backoff
        jitter routes through ``notebooklm._backoff``, which in turn calls
        ``random.uniform`` on the shared module.
        ``tests/unit/test_authed_transport.py`` relies on monkeypatching
        ``notebooklm._session.random.uniform`` to reach that jitter path;
        keep the otherwise-unused module import so the path stays
        available. Attribute patches on the singleton ``random`` module
        are visible to all importers.
        """
        transport = getattr(self, "_authed_transport", None)
        if transport is None:
            transport = AuthedTransport(self, logger=logger)
            self._authed_transport = transport
        return transport

    def _get_rpc_executor(self) -> RpcExecutor:
        """Return the RPC execution collaborator, lazily initialized.

        The adapters resolve through this module at call time so existing
        monkeypatches of ``notebooklm.rpc.decode_response``,
        ``notebooklm._session_helpers.is_auth_error``, and
        ``notebooklm._session.asyncio.sleep`` keep affecting live RPC
        behavior after the collaborator has been constructed.
        """
        executor = getattr(self, "_rpc_executor", None)
        if executor is None:
            executor = RpcExecutor(
                self,
                decode_response_late_bound=_decode_response_late_bound,
                is_auth_error=_live_is_auth_error,
                sleep=_sleep_late_bound,
            )
            self._rpc_executor = executor
        return executor

    async def open(self) -> None:
        """Open the HTTP client connection.

        Called automatically by NotebookLMClient.__aenter__. Delegates to
        :meth:`ClientLifecycle.open` — that helper builds the
        ``httpx.AsyncClient`` (always the default transport; the
        ``NOTEBOOKLM_VCR_RECORD_ERRORS`` opt-in is enforced by
        :class:`ErrorInjectionMiddleware` at chain layer, not by wrapping
        the transport — see ADR-009 close-out notes), captures the
        running event loop into ``self._bound_loop``, and spawns the
        keepalive task. Idempotent — calling ``open()`` while already
        open is a no-op. Re-opening after a prior :meth:`close`
        intentionally replaces the loop binding; :meth:`close` does not
        unbind so an
        accidental cross-loop call after close still raises actionably.
        """
        self._ensure_lifecycle()
        await self._lifecycle.open(self)

    async def save_cookies(self, jar: httpx.Cookies, path: Path | None = None) -> None:
        """Persist a cookie jar through the shared cookie-persistence collaborator.

        Thin facade over :meth:`ClientLifecycle.save_cookies`. The storage
        writer resolves through ``self._lifecycle._cookie_saver`` — by
        default the ``_default_cookie_saver`` wrapper that late-binds to
        ``notebooklm._auth.storage.save_cookies_to_storage`` so a
        ``monkeypatch.setattr("notebooklm._auth.storage.save_cookies_to_storage", …)``
        on the canonical seam keeps affecting the live save path. Phase 2
        PR 4 added the ``cookie_saver=`` constructor kwarg as the
        preferred test-side seam; passing a custom callable there bypasses
        the late-bind hop entirely.
        """
        self._ensure_lifecycle()
        await self._lifecycle.save_cookies(self, jar, path)

    async def close(self) -> None:
        """Close the HTTP client connection.

        Called automatically by NotebookLMClient.__aexit__. Delegates to
        :meth:`ClientLifecycle.close`, which:

        1. Cancels and joins the keepalive task (so the loop can't issue a
           poke against an already-closed transport).
        2. Runs registered feature drain hooks.
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
        as a ``Session`` method so ``test_client_keepalive`` and other
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
        """Delegate to :meth:`AuthRefreshCoordinator.snapshot`.

        Body lived here pre-PR-8 so the AST guard at
        ``tests/unit/test_concurrency_refresh_race.py::test_snapshot_acquires_auth_snapshot_lock``
        could inspect ``Session._snapshot`` via ``inspect.getsource(...)``
        + ``ast.parse(...)`` for the lock acquire. PR 8 moved the guard
        to inspect :meth:`AuthRefreshCoordinator.snapshot` (the canonical
        implementation), so the body collapses to a delegate here.

        The coordinator's body has the same semantic shape (lock acquire
        → four scalar reads → return) but routes the lock-wait metric
        through ``host._metrics_obj`` directly rather than via the
        ``_record_lock_wait`` facade. Whole-request atomicity for
        ``(csrf, sid, cookies)`` on the wire still depends on the
        no-await invariant between this method returning and
        ``client.post(...)`` inside :meth:`_perform_authed_post` (see
        the related AST guard in
        ``tests/unit/test_concurrency_refresh_race.py``).
        """
        self._ensure_auth_coord()
        return await self._auth_coord.snapshot(self)

    async def update_auth_tokens(self, csrf: str, session_id: str) -> None:
        """Delegate to :meth:`AuthRefreshCoordinator.update_auth_tokens`.

        Body lived here pre-PR-8 so the AST guard at
        ``tests/unit/test_concurrency_refresh_race.py::test_update_auth_tokens_has_no_await_inside_mutation_block``
        could inspect ``Session.update_auth_tokens`` via
        ``inspect.getsource(...)`` + ``ast.parse(...)`` for the no-await
        invariant inside the csrf/session_id mutation block. PR 8 moved
        the guard to inspect
        :meth:`AuthRefreshCoordinator.update_auth_tokens` (the canonical
        implementation), so the body collapses to a delegate here. The
        coordinator's body has the same semantic shape (lock acquire →
        two scalar writes inside ``try``/``finally``) but routes the
        lock-wait metric through ``host._metrics_obj`` directly rather
        than via the ``_record_lock_wait`` facade.
        """
        self._ensure_auth_coord()
        await self._auth_coord.update_auth_tokens(self, csrf, session_id)

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

    async def _authed_post_chain_terminal(self, request: RpcRequest) -> RpcResponse:
        """Chain leaf — adapts ``RpcRequest`` into ``AuthedTransport`` call shape.

        Reads ``build_request`` / ``log_label`` / ``disable_internal_retries``
        from ``request.context`` and delegates to
        :meth:`AuthedTransport.perform_authed_post` — the shared seam that
        covers both :meth:`Session._perform_authed_post` and
        ``RpcExecutor.execute`` (which calls ``_perform_authed_post`` at
        ``_rpc_executor.py:275``). Wraps the returned :class:`httpx.Response` in
        an :class:`RpcResponse` so middlewares above the leaf see the chain
        contract from ``_middleware.py``.

        ``self._get_authed_transport()`` is resolved on every invocation so
        late-bound monkeypatches of ``_get_authed_transport`` (e.g. fixtures
        that swap the transport mid-test) still affect live behavior. The
        ``RpcRequest.url`` / ``RpcRequest.headers`` / ``RpcRequest.body``
        dataclass fields stay unpopulated through Tier-12 — middlewares
        in PRs 12.3–12.9 carried behavior out of :class:`AuthedTransport`
        but the leaf still rebuilds the request from ``build_request`` +
        ``AuthSnapshot`` rather than reading the dataclass fields.
        Population is deferred to Tier-13 row 13.2 (``Kernel.post``
        rewrite) — see ADR-009 close-out notes §"AuthRefreshMiddleware
        shipped without rebuild closures". See ADR-009
        §"RpcRequest.context keys" for the metadata vocabulary.
        """
        context = request.context
        build_request = context["build_request"]
        log_label = context["log_label"]
        disable_internal_retries = context.get("disable_internal_retries", False)
        response = await self._get_authed_transport().perform_authed_post(
            build_request=build_request,
            log_label=log_label,
            disable_internal_retries=disable_internal_retries,
        )
        return RpcResponse(response=response, context=context)

    async def _perform_authed_post(
        self,
        *,
        build_request: _BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
        rpc_method: str | None = None,
    ) -> httpx.Response:
        """Authed POST entry point — routes through the middleware chain.

        Compatibility surface preserved so ``RpcExecutor.execute``
        (``_rpc_executor.py:275``), ``_chat_transport`` (``_chat_transport.py:64``),
        and direct callers (``client._session._perform_authed_post(...)``) keep
        the same keyword-only signature. The body now builds an
        :class:`RpcRequest` with the three keyword-only args stashed into
        ``context`` and dispatches into :attr:`_authed_post_chain`.
        Middlewares land one per PR in 12.3–12.8; the wiring shape stays
        unchanged.

        ``rpc_method`` (new in PR 12.4) is the resolved method name string
        (``RPCMethod.name``) for RPC callers and ``None`` for the chat
        streaming path. ``MetricsMiddleware`` reads it from
        ``request.context["rpc_method"]`` to populate
        :attr:`RpcTelemetryEvent.method` and to decide whether to fire the
        emission at all — chat-side callers that pass ``None`` skip emission,
        matching the pre-chain behavior (where ``_chat_transport`` never
        called ``_emit_rpc_event``).

        ``RpcRequest.url`` / ``RpcRequest.headers`` / ``RpcRequest.body``
        intentionally stay empty until PRs 12.5/12.7/12.8 begin populating
        them as middlewares strip behavior out of :class:`AuthedTransport`.
        """
        self._ensure_authed_post_chain()
        request = RpcRequest(
            url="",
            headers={},
            body=b"",
            context={
                "build_request": build_request,
                "log_label": log_label,
                "disable_internal_retries": disable_internal_retries,
                "rpc_method": rpc_method,
            },
        )

        # The ``max_concurrent_rpcs`` slot is acquired by
        # :class:`SemaphoreMiddleware` (chain position 2, between Metrics
        # and Retry) — that placement keeps Drain admitting queued tasks
        # AND keeps Metrics timing the queue wait, while still bounding
        # the retry-and-refresh cohort to one slot per logical RPC.
        # The middleware writes the queue-wait duration to
        # ``request.context[RPC_QUEUE_WAIT_CONTEXT_KEY]`` so the recorder
        # below can forward it to ``ClientMetrics`` without giving the
        # middleware an opinionated ``ClientMetrics`` dependency.
        try:
            result = await self._authed_post_chain(request)
            return result.response
        finally:
            # Record queue wait even if the chain raised — pre-Tier-12
            # ``AuthedTransport.perform_authed_post`` recorded the wait
            # immediately after semaphore acquisition, so a failed chain
            # (RetryMiddleware budget exhaustion, AuthRefreshMiddleware
            # refresh failure, etc.) MUST still surface the queue-wait
            # latency. ``SemaphoreMiddleware`` writes the duration to
            # ``request.context[RPC_QUEUE_WAIT_CONTEXT_KEY]`` after the
            # semaphore is acquired; absence of the key means the slot
            # was never acquired and there's nothing to record (gemini
            # PR 12.9 finding).
            queue_wait = request.context.get(RPC_QUEUE_WAIT_CONTEXT_KEY)
            if queue_wait is not None:
                self._record_rpc_queue_wait(queue_wait)

    async def transport_post(
        self,
        build_request: _BuildRequest,
        parse_label: str,
        *,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        """Session transport facade required by the Tier-13 contract."""
        # ``Session`` exposes ``parse_label`` for the later feature retype.
        # The existing transport leaf still calls the same value ``log_label``.
        return await self._perform_authed_post(
            build_request=build_request,
            log_label=parse_label,
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

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        """Compatibility wrapper around :meth:`RpcExecutor.execute_with_telemetry`.

        The executor owns the telemetry, reqid, drain, and decode-time
        refresh-and-retry plumbing; this facade preserves the method shape so
        the 30+ tests that mock ``core.rpc_call = AsyncMock(...)`` by
        attribute keep working. See
        :meth:`notebooklm._rpc_executor.RpcExecutor.execute_with_telemetry` for
        the full contract (kwargs ``_is_retry`` / ``disable_internal_retries``
        / ``operation_variant`` flow through unchanged; ``RuntimeError`` is
        raised if the client is not initialized).
        """
        return await self._get_rpc_executor().execute_with_telemetry(
            method,
            params,
            source_path,
            allow_null,
            _is_retry,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
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
        operation_variant: str | None = None,
    ) -> Any:
        """Compatibility wrapper around :class:`RpcExecutor`."""
        return await self._get_rpc_executor().execute(
            method,
            params,
            source_path,
            allow_null,
            _is_retry,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
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
        operation_variant: str | None = None,
    ) -> Any | None:
        """Compatibility wrapper around :class:`RpcExecutor`."""
        return await self._get_rpc_executor().try_refresh_and_retry(
            method,
            params,
            source_path,
            allow_null,
            original_error,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
        )

    def get_http_client(self) -> httpx.AsyncClient:
        """Get the underlying HTTP client for direct requests.

        Used by download operations that need direct HTTP access.

        Returns:
            The httpx.AsyncClient instance.

        Raises:
            RuntimeError: If client is not initialized.
        """
        self._ensure_lifecycle()
        return self._lifecycle.get_http_client()
