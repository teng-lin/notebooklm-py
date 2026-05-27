"""Concrete session infrastructure for the NotebookLM API client."""

import asyncio
import logging
import random  # noqa: F401 - tests patch this for _backoff jitter
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ._error_injection import _refuse_synthetic_error_outside_test_context
from ._kernel import Kernel
from ._middleware import (
    RpcRequest,
    RpcResponse,
)
from ._reqid_counter import DEFAULT_STEP as _REQID_DEFAULT_STEP
from ._request_types import BuildRequest
from ._rpc_executor import RpcExecutor
from ._session_config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from ._session_init import (
    build_collaborators,
    build_session_transport,
    validate_constructor_args,
    wire_middleware_chain,
)
from ._session_lifecycle import CookieRotator, CookieSaver
from ._session_transport import SessionTransport
from .auth import (
    AuthTokens,
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

    # ADR-014 Rule 5 (Wave 4 of session-decoupling): the compile-time
    # ``Session: RpcOwner`` assertion was removed when the ``RpcOwner``
    # Protocol itself was deleted — ``RpcExecutor`` now takes its
    # collaborators directly via keyword arguments instead of reaching
    # them through a Session-shaped owner.


from .rpc import RPCMethod

logger = logging.getLogger(__name__)

# Auth-snapshot canonical implementation lives on
# :class:`AuthRefreshCoordinator` (``_session_auth.py`` —
# ``AuthRefreshCoordinator.snapshot`` / ``.update_auth_tokens``). PR 8
# first collapsed the previously real-bodied ``Session._snapshot`` /
# ``Session.update_auth_tokens`` into thin delegates that forwarded
# through ``self._auth_coord``. PR #4b of the session-refactor arc
# then inlined ``Session._snapshot`` entirely — every site that needs
# an :class:`AuthSnapshot` now reads ``self._auth_coord.snapshot(self)``
# directly. ``Session.update_auth_tokens`` is retained as a delegate
# because :class:`RefreshAuthCore` in ``_auth/session.py`` is the
# structural Protocol used by ``refresh_auth_session`` and still
# requires that method on the core. The AST guards in
# ``tests/unit/test_concurrency_refresh_race.py``
# (``test_snapshot_acquires_auth_snapshot_lock`` /
# ``test_update_auth_tokens_has_no_await_inside_mutation_block``)
# inspect the coordinator's source via ``inspect.getsource(...)`` +
# AST parsing — changes to auth-snapshot invariants must be applied to
# the coordinator (not the surviving ``update_auth_tokens`` delegate).


# Three previously module-level test seams (one each for RPC response
# decoding, the awaitable used by retry/backoff loops, and the
# authentication-error classifier) were retired in favour of
# constructor-injected callables on :class:`Session`. Tests that need to
# substitute behaviour pass ``decode_response=…``, ``sleep=…``, or
# ``is_auth_error=…`` keyword arguments to :class:`Session` directly
# instead of monkeypatching module attributes. See ``docs/improvement.md``
# §4.1 for the rationale.


def _default_decode_response() -> Callable[..., Any]:
    """Resolve the canonical RPC response decoder used when
    :class:`Session` is constructed without an explicit
    ``decode_response=`` kwarg.

    The function is invoked **eagerly** (once per ``Session()`` call)
    and captures its result immediately. The ``import`` inside the body
    is deferred so the attribute lookup goes through
    ``notebooklm.rpc.decode_response`` at construction time — the
    canonical monkeypatch surface documented in ADR-007. This is NOT
    a late-binding wrapper — see ``docs/improvement.md`` §4.1 for the
    contrast with the retired ``_decode_response_late_bound``.
    """
    from .rpc import decode_response

    return decode_response


def _default_is_auth_error() -> Callable[[Exception], bool]:
    """Resolve the canonical auth-error classifier used when
    :class:`Session` is constructed without an explicit
    ``is_auth_error=`` kwarg.

    The function is invoked **eagerly** (once per ``Session()`` call)
    and captures its result immediately. The ``import`` inside the body
    is deferred so the attribute lookup goes through
    ``notebooklm._session_helpers.is_auth_error`` at construction
    time — the canonical monkeypatch surface documented in ADR-007.
    This is NOT a late-binding wrapper — see ``docs/improvement.md``
    §4.1 for the contrast with the retired ``_live_is_auth_error``.
    """
    from ._session_helpers import is_auth_error

    return is_auth_error


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
        *,
        decode_response: Callable[..., Any] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        is_auth_error: Callable[[Exception], bool] | None = None,
        async_client_factory: Callable[..., httpx.AsyncClient] | None = None,
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
            decode_response: Override for the canonical RPC response
                decoder. ``None`` (default) resolves to
                :func:`notebooklm.rpc.decode_response` via the
                module-level imported binding at construction time —
                tests that ``monkeypatch.setattr("notebooklm.rpc.decode_response",
                fake)`` BEFORE constructing :class:`Session` still steer
                the captured callable. Replaces the retired module-level
                decode wrapper, which performed the lookup on every call;
                tests that need to swap the decoder AFTER construction
                should pass an explicit callable here or assign
                ``session._decode_response = fake`` before the first RPC.
                See ``docs/improvement.md`` §4.1.
            sleep: Override for the awaitable used by retry/backoff loops.
                ``None`` (default) resolves to :func:`asyncio.sleep` via
                the module-level binding at construction time. Replaces
                the retired module-level sleep wrapper — tests can pass
                ``sleep=fake_sleep`` directly or
                ``monkeypatch.setattr("notebooklm._session.asyncio.sleep",
                fake_sleep)`` BEFORE constructing :class:`Session`.
            is_auth_error: Override for the authentication-error classifier
                used by the chain's ``AuthRefreshMiddleware`` and by
                :class:`RpcExecutor`'s decode-time refresh path. ``None``
                (default) resolves to
                :func:`notebooklm._session_helpers.is_auth_error` via the
                module-level imported binding at construction time.
                Replaces the retired module-level classifier wrapper.
            async_client_factory: Override for the live ``httpx.AsyncClient``
                factory used by :meth:`Kernel.open` to build the live
                transport. ``None`` (default) resolves to
                :class:`httpx.AsyncClient` via a module-level name lookup
                at call time, so tests that
                ``monkeypatch.setattr("notebooklm._session.httpx.AsyncClient",
                fake)`` before constructing the client still steer the
                live transport build. Pass an explicit callable to install
                a mock transport (e.g. via ``httpx.MockTransport``) without
                going through the late-bind hop. The retired
                ``Kernel.http_client`` setter previously absorbed that
                post-construction mutation. See ``docs/improvement.md``
                §4.2.

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
        config = validate_constructor_args(
            timeout=timeout,
            connect_timeout=connect_timeout,
            refresh_retry_delay=refresh_retry_delay,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            keepalive=keepalive,
            keepalive_min_interval=keepalive_min_interval,
            keepalive_storage_path=keepalive_storage_path,
            auth_storage_path=auth.storage_path,
            limits=limits,
            max_concurrent_uploads=max_concurrent_uploads,
            max_concurrent_rpcs=max_concurrent_rpcs,
            # Seam defaults resolve against THIS module's ``asyncio`` /
            # ``httpx`` bindings (see ``_session_init.py`` module
            # docstring for the seam-resolution boundary).
            decode_response=_default_decode_response()
            if decode_response is None
            else decode_response,
            sleep=asyncio.sleep if sleep is None else sleep,
            is_auth_error=_default_is_auth_error() if is_auth_error is None else is_auth_error,
            async_client_factory=httpx.AsyncClient
            if async_client_factory is None
            else async_client_factory,
        )

        # Plain-attribute assignments precede ``build_collaborators``
        # because the chain provider lambdas in
        # :func:`wire_middleware_chain` read ``_rate_limit_max_retries``
        # / ``_server_error_max_retries`` / ``_refresh_retry_delay``
        # from ``self`` live (integration tests SET them
        # post-construction).
        self.auth = auth
        self._decode_response: Callable[..., Any] = config.decode_response
        self._sleep: Callable[[float], Awaitable[Any]] = config.sleep
        self._is_auth_error: Callable[[Exception], bool] = config.is_auth_error
        self._refresh_retry_delay = config.refresh_retry_delay
        self._rate_limit_max_retries = config.rate_limit_max_retries
        self._server_error_max_retries = config.server_error_max_retries
        self._max_concurrent_rpcs: int | None = config.max_concurrent_rpcs
        # Lazy-created per-instance — see :meth:`_get_rpc_semaphore`.
        self._rpc_semaphore: asyncio.Semaphore | None = None

        collaborators = build_collaborators(
            config,
            auth=auth,
            refresh_callback=refresh_callback,
            on_rpc_event=on_rpc_event,
            cookie_saver=cookie_saver,
            cookie_rotator=cookie_rotator,
        )
        self._metrics_obj = collaborators.metrics
        self._drain_tracker = collaborators.drain_tracker
        self._reqid = collaborators.reqid
        self._auth_coord = collaborators.auth_coord
        self._kernel = collaborators.kernel
        self._lifecycle = collaborators.lifecycle
        self.cookie_persistence = collaborators.cookie_persistence
        self.poll_registry = collaborators.poll_registry
        # ``_drain_hooks`` storage moved to ``TransportDrainTracker`` in
        # Wave 2 of the session-decoupling plan (ADR-014 Rule 1).
        # ``Session.register_drain_hook`` is now a one-line forward.
        self._rpc_executor: RpcExecutor | None = None

        # The authed POST hot path (chain terminal, freshness rebuild,
        # and ``_perform_authed_post`` entry) lives on
        # :class:`SessionTransport` (move #4c — ``docs/improvement.md``
        # §3.1). Build the transport BEFORE :func:`wire_middleware_chain`
        # so the chain leaf can route through :class:`Session`; the
        # transport reaches the chain itself through a live-binding
        # ``chain_provider`` closure that reads
        # ``self._authed_post_chain`` (set just below), which preserves
        # the long-standing test pattern of reassigning
        # ``core._authed_post_chain = fake_chain`` post-construction.
        # The transport receives :data:`logger` (this module's logger,
        # ``notebooklm._session``) so transport-error log lines stay in
        # the historical namespace rather than acquiring a new
        # ``notebooklm._session_transport`` namespace.
        self._transport: SessionTransport = build_session_transport(
            collaborators, host=self, logger=logger
        )

        # The chain leaf wires to the :class:`Session`-side forward
        # (:meth:`_authed_post_chain_terminal`), not directly to
        # :meth:`SessionTransport.terminal`. The forward is the canonical
        # seam: a subclass override or fixture-time class-level
        # monkeypatch of ``Session._authed_post_chain_terminal`` keeps
        # steering the live chain leaf, matching pre-extraction
        # behavior. The forward adds one bound-method dispatch hop per
        # chain leaf invocation — negligible overhead.
        wired = wire_middleware_chain(
            config,
            collaborators,
            host=self,
            authed_post_chain_terminal=self._authed_post_chain_terminal,
            rpc_semaphore_factory=self._get_rpc_semaphore,
        )
        self._chain_builder = wired.chain_builder
        self._middlewares = wired.middlewares
        self._authed_post_chain = wired.authed_post_chain

    def register_drain_hook(self, name: str, hook: Callable[[], Awaitable[None]]) -> None:
        """Register or replace a feature-owned close-time drain hook.

        Forward to :meth:`TransportDrainTracker.register_drain_hook` per
        ADR-014 Rule 1; the storage and firing live on the tracker since
        Wave 2 of the session-decoupling plan.
        """
        self._drain_tracker.register_drain_hook(name, hook)

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
        return self._metrics_obj.snapshot()

    def _increment_metrics(self, **increments: int | float) -> None:
        self._metrics_obj.increment(**increments)

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        """Record time spent waiting for the upload semaphore."""
        self._metrics_obj.record_upload_queue_wait(wait_seconds)

    # Session/support surface consumed by feature APIs and private helpers.
    @property
    def kernel(self) -> Kernel:
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
        """Raise if this core is used from a loop other than its open-time loop.

        Forward to :meth:`ClientLifecycle.assert_bound_loop` per ADR-014
        Rule 1; ``ClientLifecycle`` satisfies the ``LoopGuard`` capability
        Protocol directly since Wave 2 of the session-decoupling plan.
        """
        self._lifecycle.assert_bound_loop()

    async def _emit_rpc_event(self, event: RpcTelemetryEvent) -> None:
        """Invoke the optional telemetry callback without affecting RPC behavior."""
        await self._metrics_obj.emit_rpc_event(event)

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]:
        """Return a drain-tracked operation scope for feature-owned work.

        Forward to :meth:`TransportDrainTracker.operation_scope` per
        ADR-014 Rule 1; ``TransportDrainTracker`` satisfies the
        ``OperationScopeProvider`` capability Protocol directly since
        Wave 2 of the session-decoupling plan. Plain sync function (no
        ``@asynccontextmanager``) so the inner context manager flows
        through unchanged; callers still write
        ``async with session.operation_scope(label):``.
        """
        return self._drain_tracker.operation_scope(label)

    async def drain(self, timeout: float | None = None) -> None:
        """Stop accepting new client operations and wait for in-flight ones to finish.

        If ``timeout`` expires, ``TimeoutError`` is raised and the client
        remains in draining mode so shutdown callers do not accidentally admit
        new work after a missed deadline.
        """
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

    def _get_rpc_executor(self) -> RpcExecutor:
        """Return the RPC execution collaborator, lazily initialized.

        The decode/sleep/is-auth-error callables are the constructor-injected
        seams (``Session(..., decode_response=…, sleep=…, is_auth_error=…)``).
        Tests substitute behaviour at construction time rather than via
        ``monkeypatch.setattr`` on module attributes; see
        ``docs/improvement.md`` §4.1 for the migration rationale.
        """
        executor = getattr(self, "_rpc_executor", None)
        if executor is None:
            # ADR-014 Rule 5 (Wave 4 of session-decoupling): RpcExecutor
            # takes its collaborators directly via keyword-only arguments
            # instead of reaching them through a Session-shaped owner.
            executor = RpcExecutor(
                kernel=self._kernel,
                transport=self._transport,
                auth_refresh=self._auth_coord,
                metrics=self._metrics_obj,
                decode_response=self._decode_response,
                is_auth_error=self._is_auth_error,
                sleep=self._sleep,
                timeout_provider=lambda: self._lifecycle._timeout,
                refresh_callback_enabled_provider=lambda: self._auth_coord.has_refresh_callback,
                refresh_retry_delay_provider=lambda: self._refresh_retry_delay,
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
        5. Nulls out ``_kernel._http_client`` and ``_rpc_executor`` so a
           follow-up :meth:`open` rebuilds transport collaborators against
           the new ``httpx.AsyncClient``.
        """
        await self._lifecycle.close(self)

    async def _keepalive_loop(self, interval: float) -> None:
        """Background loop that periodically pokes the identity surface.

        Thin facade over :meth:`ClientLifecycle._keepalive_loop`. Retained
        as a ``Session`` method so ``test_client_keepalive`` and other
        tests that introspect ``core._keepalive_loop`` continue to resolve.
        """
        await self._lifecycle._keepalive_loop(self, interval)

    @property
    def is_open(self) -> bool:
        """Check if the HTTP client is open."""
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
        self._auth_coord.update_auth_headers(self)

    async def update_auth_tokens(self, csrf: str, session_id: str) -> None:
        """Delegate to :meth:`AuthRefreshCoordinator.update_auth_tokens`.

        Retained on Session because the :class:`RefreshAuthCore`
        Protocol in ``_auth/session.py`` (consumed by
        :func:`refresh_auth_session`) structurally requires this method
        on the core. PR 8 collapsed the previously real body into a
        delegate that forwards through ``self._auth_coord``; PR #4b of
        the session-refactor arc inlined sibling delegates but kept
        this one for the Protocol caller. The coordinator routes the
        lock-wait metric through ``host._metrics_obj`` directly. The
        AST guard for the no-await mutation-block invariant now lives
        on :meth:`AuthRefreshCoordinator.update_auth_tokens`
        (``test_concurrency_refresh_race.test_update_auth_tokens_has_no_await_inside_mutation_block``).
        """
        await self._auth_coord.update_auth_tokens(self, csrf, session_id)

    async def _refresh_request_for_current_auth(self, request: RpcRequest) -> RpcRequest:
        """Forward to :meth:`SessionTransport.refresh_request_for_current_auth`
        (body moved in move #4c — ``docs/improvement.md`` §3.1; AST guard
        now inspects the collaborator method directly).
        """
        return await self._transport.refresh_request_for_current_auth(request)

    async def _authed_post_chain_terminal(self, request: RpcRequest) -> RpcResponse:
        """Middleware chain leaf — forwards to :meth:`SessionTransport.terminal`.

        The body moved to the collaborator in move #4c
        (``docs/improvement.md`` §3.1). :meth:`Session.__init__` wires
        this method as the chain leaf (``wire_middleware_chain`` receives
        ``self._authed_post_chain_terminal``), so this forward IS the
        live chain leaf — not a test-only entry point. Routing through
        the Session forward (rather than directly to
        :meth:`SessionTransport.terminal`) preserves the canonical seam:
        a subclass override or fixture-time class-level monkeypatch of
        this method keeps steering the live chain leaf. AST guard
        (:func:`tests.unit.test_concurrency_refresh_race.test_kernel_post_terminal_has_no_await_before_post_per_attempt`)
        inspects :meth:`SessionTransport.terminal` directly because the
        forward carries no try/await structure.
        """
        return await self._transport.terminal(request)

    async def _perform_authed_post(
        self,
        *,
        build_request: BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
        rpc_method: str | None = None,
    ) -> httpx.Response:
        """Forward to :meth:`SessionTransport.perform_authed_post` (body
        moved in move #4c — ``docs/improvement.md`` §3.1). Retained on
        :class:`Session` per ADR-014 Rule 4 as a load-bearing middleware-chain
        seam: ``_chat_transport`` and ``client._session._perform_authed_post(...)``
        direct callers still reach it here. ``RpcExecutor`` no longer calls
        this method (Wave 4 of session-decoupling: the executor takes
        :class:`SessionTransport` directly and calls
        ``self._transport.perform_authed_post`` without going through Session).
        ``_chat_transport`` and ``client._session._perform_authed_post(...)``
        direct callers keep the same keyword-only signature.
        """
        return await self._transport.perform_authed_post(
            build_request=build_request,
            log_label=log_label,
            disable_internal_retries=disable_internal_retries,
            rpc_method=rpc_method,
        )

    async def transport_post(
        self,
        build_request: BuildRequest,
        parse_label: str,
        *,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        """Session transport facade required by the Tier-13 contract."""
        # ``Session`` exposes ``parse_label`` for the later feature retype; the
        # chain context still names that value ``log_label``.
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
        # Wave 3b of session-decoupling (Task 1.0): ``await_refresh`` no
        # longer takes a host parameter — its only host attribute reach
        # (``_metrics_obj``) is now supplied via the coordinator's
        # constructor.
        await self._auth_coord.await_refresh()

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
        """Compatibility wrapper around :meth:`RpcExecutor.rpc_call`.

        The executor owns the telemetry, reqid, and decode-time
        refresh-and-retry plumbing; this facade preserves the method shape so
        the 30+ tests that mock ``core.rpc_call = AsyncMock(...)`` by
        attribute keep working. See
        :meth:`notebooklm._rpc_executor.RpcExecutor.rpc_call` for
        the full contract (kwargs ``_is_retry`` / ``disable_internal_retries``
        / ``operation_variant`` flow through unchanged; ``RuntimeError`` is
        raised if the client is not initialized).
        """
        return await self._get_rpc_executor().rpc_call(
            method,
            params,
            source_path,
            allow_null,
            _is_retry,
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
        return self._kernel.get_http_client()
