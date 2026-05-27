"""Concrete session infrastructure for the NotebookLM API client."""

import asyncio
import logging
import random  # noqa: F401 - tests patch this for _backoff jitter
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ._error_injection import _refuse_synthetic_error_outside_test_context
from ._middleware import (
    RpcRequest,
    RpcResponse,
)
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
from .types import RpcTelemetryEvent

if TYPE_CHECKING:
    from ._session_init import SessionCollaborators, WiredMiddleware
    from ._session_transport import SessionTransport
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


# ----------------------------------------------------------------------
# Stage B1 PR 1 — composition primitives (no behavior change)
# ----------------------------------------------------------------------
#
# These helpers (``resolve_seam_defaults`` / :func:`compose_session_internals`
# / :class:`ComposedSession`) and the ``Session._bind_*`` write-once
# setters were introduced in Stage B1 PR 1 of the post-refactoring plan
# (``docs/post-refactoring-plan-2026-05-27.md``) as a no-behavior-change
# additive step toward moving ``build_collaborators`` ownership out of
# ``Session.__init__`` into ``NotebookLMClient``.
#
# **In PR 1 these primitives are DORMANT** — ``Session.__init__`` still
# does the legacy inline construction sequence. PR 2 of Stage B1 rewrites
# ``Session.__init__`` to take ``(*, collaborators, config, auth)``,
# moves the composition root into ``NotebookLMClient.__init__`` via
# :func:`compose_session_internals`, and starts exercising the
# ``_bind_*`` setters at composition time. The fail-fast guards on
# ``Session`` entry points are inert in PR 1 (because inline
# construction always sets the required attributes) and become
# load-bearing in PR 2.
#
# The helper lives in :mod:`notebooklm._session` (not
# :mod:`notebooklm._session_init`) so seam-default resolution happens
# against this module's bindings, preserving the documented monkeypatch
# contract at :mod:`_session_init` lines 19-25.


@dataclass(frozen=True)
class ComposedSession:
    """Result of :func:`compose_session_internals`.

    Bundles the fully-constructed :class:`Session` with the collaborators
    and late-bound dependencies that ``NotebookLMClient`` wires feature
    APIs against. PR 2 of Stage B1 starts consuming this; in PR 1 it is
    a returnable artifact of the dormant helper.
    """

    session: "Session"
    transport: SessionTransport
    executor: RpcExecutor
    collaborators: "SessionCollaborators"


def resolve_seam_defaults(
    *,
    sleep: Callable[[float], Awaitable[Any]] | None,
    async_client_factory: Callable[..., httpx.AsyncClient] | None,
    is_auth_error: Callable[[Exception], bool] | None,
    decode_response: Callable[..., Any] | None,
) -> dict[str, Callable[..., Any]]:
    """Resolve ``None``-default seam callables against this module's bindings.

    Centralizes the ``X if X is not None else <module-attr>`` dance that
    :class:`Session.__init__` currently performs inline. Resolution happens
    against the :mod:`notebooklm._session` module's bindings so the
    documented monkeypatch paths
    (``notebooklm._session.asyncio.sleep`` /
    ``notebooklm._session.httpx.AsyncClient`` and the lazy imports inside
    :func:`_default_decode_response` / :func:`_default_is_auth_error`)
    keep steering the seams at construction time.

    PR 1: not invoked from production code. Reserved for
    :func:`compose_session_internals` (also dormant in PR 1).
    """
    return {
        "sleep": asyncio.sleep if sleep is None else sleep,
        "async_client_factory": (
            httpx.AsyncClient if async_client_factory is None else async_client_factory
        ),
        "is_auth_error": (_default_is_auth_error() if is_auth_error is None else is_auth_error),
        "decode_response": (
            _default_decode_response() if decode_response is None else decode_response
        ),
    }


def compose_session_internals(
    *,
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
    decode_response: Callable[..., Any] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    is_auth_error: Callable[[Exception], bool] | None = None,
    async_client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> ComposedSession:
    """Single entry point that owns the full Session composition sequence.

    PR 1 (this PR): DORMANT. ``Session.__init__`` still performs the
    inline construction below; this helper exists only as the future
    home of that sequence. PR 2 of Stage B1 rewrites
    ``Session.__init__`` to ``(*, collaborators, config, auth)`` and
    moves the composition root into ``NotebookLMClient.__init__`` via
    this helper.

    The kwarg surface mirrors the current :class:`Session.__init__`
    kwargs (production NotebookLMClient kwargs ∪ the four seam kwargs
    ``decode_response`` / ``sleep`` / ``is_auth_error`` /
    ``async_client_factory``). The seam kwargs are intentionally
    test-only — they are NOT exposed on ``NotebookLMClient.__init__``,
    which preserves the public surface.

    The first call inside the body MUST stay
    :func:`_refuse_synthetic_error_outside_test_context` — that
    preserves the existing :class:`Session.__init__` side-effect pinned
    by :mod:`tests.unit.concurrency.test_synthetic_error_transport_guard`.

    The lambda closures for the executor wiring
    (``decode_response`` / ``is_auth_error`` / ``sleep`` /
    ``timeout_provider`` / ``refresh_callback_enabled_provider`` /
    ``refresh_retry_delay_provider``) preserve the late-binding contract
    pinned by :mod:`tests.unit.test_init_order` lines 622-672 —
    post-construction ``session._decode_response = rebound`` (and the
    sibling seam reassignments) continue to take effect inside the live
    executor because the closures dereference ``session._<attr>`` on
    every call.
    """
    # MUST stay first — preserves the existing Session.__init__ side
    # effect that ``test_synthetic_error_transport_guard`` pins. The
    # legacy ``Session.__init__`` invocation below would also fire this
    # guard, but the contract documented in the post-refactoring plan
    # requires it to be the FIRST call inside ``compose_session_internals``
    # so PR 2's helper (where ``Session.__init__`` no longer runs the
    # guard inline) preserves the same earliest-opportunity refusal.
    _refuse_synthetic_error_outside_test_context()
    # ``resolve_seam_defaults`` is invoked unconditionally so the helper
    # exercises the seam-resolution boundary documented in
    # ``_session_init.py`` (lines 19-25). In PR 1 the resolved callables
    # are discarded — legacy ``Session.__init__`` re-resolves seams
    # inline against the same module bindings; both paths produce
    # identical seam values. PR 2 of Stage B1 threads the resolved
    # callables straight into ``validate_constructor_args`` and the
    # legacy inline resolution disappears, so the call site here
    # becomes load-bearing without a code shape change.
    resolve_seam_defaults(
        sleep=sleep,
        async_client_factory=async_client_factory,
        is_auth_error=is_auth_error,
        decode_response=decode_response,
    )
    # PR 1: hand the legacy ``Session.__init__`` the same kwargs the
    # caller passed in (it re-resolves seams + validates the config +
    # builds collaborators inline). The helper then reads back
    # ``session._collaborators`` / ``session._transport`` for the
    # ``ComposedSession`` bundle so ``composed.collaborators`` /
    # ``composed.transport`` reference the SAME objects the session is
    # actually using (a fresh ``build_collaborators`` call here would
    # produce a duplicate bundle that diverged from the live session
    # state). PR 2 inverts this — the helper constructs collaborators
    # / transport / chain itself and feeds them into a ``Session(*,
    # collaborators=..., config=..., auth=...)`` constructor that no
    # longer does inline construction.
    session = Session(
        auth,
        timeout=timeout,
        connect_timeout=connect_timeout,
        refresh_callback=refresh_callback,
        refresh_retry_delay=refresh_retry_delay,
        keepalive=keepalive,
        keepalive_min_interval=keepalive_min_interval,
        keepalive_storage_path=keepalive_storage_path,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        limits=limits,
        max_concurrent_uploads=max_concurrent_uploads,
        max_concurrent_rpcs=max_concurrent_rpcs,
        on_rpc_event=on_rpc_event,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
        decode_response=decode_response,
        sleep=sleep,
        is_auth_error=is_auth_error,
        async_client_factory=async_client_factory,
    )
    collaborators = session._collaborators
    transport = session._transport
    if transport is None:  # pragma: no cover - defensive; legacy __init__ sets this
        raise RuntimeError(
            "compose_session_internals: Session.__init__ did not produce a transport"
        )
    # Lambdas preserve the late-binding contract pinned by
    # ``tests/unit/test_init_order.py:622-672`` — post-construction
    # ``session._decode_response = rebound`` / ``_sleep = …`` /
    # ``_is_auth_error = …`` reassignments continue to take effect
    # inside the executor.
    # The `*a, **kw` forwarding form matches the lazy ``_get_rpc_executor``
    # factory (see lines 743-746) so future signature changes or custom
    # test-double overrides on ``session._is_auth_error`` / ``session._sleep``
    # propagate identically through both construction paths
    # (gemini-code-assist PR #1086 review, finding 4).
    executor = RpcExecutor(
        kernel=collaborators.kernel,
        transport=transport,
        auth_refresh=collaborators.auth_coord,
        metrics=collaborators.metrics,
        decode_response=lambda *a, **kw: session._decode_response(*a, **kw),
        is_auth_error=lambda *a, **kw: session._is_auth_error(*a, **kw),
        sleep=lambda *a, **kw: session._sleep(*a, **kw),
        timeout_provider=lambda: collaborators.lifecycle._timeout,
        refresh_callback_enabled_provider=lambda: collaborators.auth_coord.has_refresh_callback,
        refresh_retry_delay_provider=lambda: session._refresh_retry_delay,
    )
    # ``Session.__init__`` (PR 1) does not pre-bind the executor — it
    # initialises the slot to ``None`` and lazily fills it via
    # :meth:`Session._get_rpc_executor`. The write-once binder accepts
    # the first bind, so calling it here from the dormant helper does
    # not conflict with the legacy lazy-init path (lazy init only
    # triggers when ``_rpc_executor`` is ``None``).
    session._bind_executor(executor)
    return ComposedSession(
        session=session,
        transport=transport,
        executor=executor,
        collaborators=collaborators,
    )


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
                ``SessionTransport.perform_authed_post`` RPC POSTs. Defaults to
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
        # ADR-014 Rule 3 Stage A (Wave 6 of session-decoupling): store the
        # bundle so the ``Session.collaborators`` accessor below can expose
        # it as a single typed attribute for ``NotebookLMClient.__init__``
        # feature wiring. Stage B (Wave 7 follow-up) moves
        # ``build_collaborators`` ownership to NotebookLMClient and deletes
        # this storage along with the accessor.
        self._collaborators = collaborators
        self._metrics_obj = collaborators.metrics
        self._drain_tracker = collaborators.drain_tracker
        self._reqid = collaborators.reqid
        self._auth_coord = collaborators.auth_coord
        self._kernel = collaborators.kernel
        self._lifecycle = collaborators.lifecycle
        self.cookie_persistence = collaborators.cookie_persistence
        self.poll_registry = collaborators.poll_registry
        # ``_drain_hooks`` storage moved to ``TransportDrainTracker`` in
        # Wave 2 of the session-decoupling plan (ADR-014 Rule 1);
        # ``Session.register_drain_hook`` was deleted in Wave 11a.
        self._rpc_executor: RpcExecutor | None = None

        # The authed POST hot path (chain terminal, freshness rebuild,
        # and ``perform_authed_post`` entry) lives on
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

    # ------------------------------------------------------------------
    # ADR-014 Rule 3 Stage A accessors (Wave 6 of session-decoupling)
    # ------------------------------------------------------------------
    #
    # Three typed accessors that let ``NotebookLMClient.__init__`` wire
    # feature APIs with the collaborators they actually depend on, instead
    # of passing the whole ``Session`` and having features reach for
    # underscore-prefixed attributes. The base bundle (``collaborators``)
    # exposes the eight fields ``SessionCollaborators`` carries today; two
    # narrow accessors expose the late-bound collaborators that are NOT on
    # the dataclass (``session_transport`` is constructed after the bundle
    # via ``build_session_transport``; ``rpc_executor`` is lazy via
    # ``_get_rpc_executor``).
    #
    # Per Stage B (Wave 7 follow-up): when ``build_collaborators`` ownership
    # moves to ``NotebookLMClient``, all three accessors are deleted along
    # with the ``self._collaborators`` storage above.
    #
    # The Wave 6 lint guard (``tests/_lint/test_client_composition.py``)
    # restricts reads of these accessors to ``client.py`` + ``_session.py``
    # + ``tests/`` to keep them from becoming a discoverability hub.

    @property
    def collaborators(self) -> "SessionCollaborators":
        """Typed access to the constructed collaborator bundle (ADR-014 Rule 3 Stage A)."""
        return self._collaborators

    @property
    def session_transport(self) -> "SessionTransport":
        """Late-bound collaborator not present on :class:`SessionCollaborators`
        today (constructed via :func:`build_session_transport` after the bundle).
        Deleted in Wave 7 follow-up along with the other Stage-A accessors.
        """
        return self._transport

    @property
    def rpc_executor(self) -> RpcExecutor:
        """Lazily-constructed collaborator not present on :class:`SessionCollaborators`
        today. Deleted in Wave 7 follow-up along with the other Stage-A accessors.
        """
        return self._get_rpc_executor()

    def assert_bound_loop(self) -> None:
        """Raise if this core is used from a loop other than its open-time loop.

        Forward to :meth:`ClientLifecycle.assert_bound_loop` per ADR-014
        Rule 1; ``ClientLifecycle`` satisfies the ``LoopGuard`` capability
        Protocol directly since Wave 2 of the session-decoupling plan.
        """
        self._lifecycle.assert_bound_loop()

    def _get_rpc_semaphore(self) -> AbstractAsyncContextManager[Any]:
        """Return the per-instance RPC semaphore (or a null-context).

        When ``max_concurrent_rpcs`` was set to ``None`` at construction
        time, this returns a :class:`contextlib.nullcontext` so the
        ``async with`` wrapper inside the chain's ``SemaphoreMiddleware``
        collapses to a no-op (callers with their own external rate-limiter
        opted out of the gate). Otherwise it lazily constructs an
        ``asyncio.Semaphore`` bound to the running loop on first use,
        mirroring the lazy-init pattern of :attr:`_reqid_lock` /
        :attr:`_auth_snapshot_lock`.

        The check-then-assign is safe without an outer lock because
        asyncio is single-threaded: no other coroutine can execute
        between the ``is None`` check and the assignment unless we
        ``await`` (and we don't).
        """
        # Stage B1 PR 1 fail-fast: this factory is captured by the chain
        # at construction time and invoked from middleware on every
        # rpc_call. A pre-composition call indicates the chain is being
        # exercised before the full composition completed. Inert under
        # inline construction.
        self._require_constructed("_transport")
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
                # Late-bind so tests that patch ``session._decode_response``
                # after construction (legacy pattern in
                # ``tests/integration/test_auto_refresh.py`` etc.) still take
                # effect under the Wave 7 wiring where ``_get_rpc_executor``
                # is invoked eagerly from ``NotebookLMClient.__init__``.
                decode_response=lambda *a, **kw: self._decode_response(*a, **kw),
                is_auth_error=lambda *a, **kw: self._is_auth_error(*a, **kw),
                sleep=lambda *a, **kw: self._sleep(*a, **kw),
                timeout_provider=lambda: self._lifecycle._timeout,
                refresh_callback_enabled_provider=lambda: self._auth_coord.has_refresh_callback,
                refresh_retry_delay_provider=lambda: self._refresh_retry_delay,
            )
            self._rpc_executor = executor
        return executor

    # ------------------------------------------------------------------
    # Stage B1 PR 1 — write-once binders + fail-fast guards
    # ------------------------------------------------------------------
    #
    # The three ``_bind_*`` setters below accept exactly one bind per
    # attribute. They are reserved for :func:`compose_session_internals`
    # (the Stage B1 PR 2 composition root) and are DORMANT in PR 1
    # because ``Session.__init__`` still inline-constructs the
    # transport / chain / executor.
    #
    # Subtlety: because PR 1's :class:`Session.__init__` already sets
    # ``self._transport`` / ``self._chain_builder`` /
    # ``self._middlewares`` / ``self._authed_post_chain`` to non-None
    # values inline, calling :meth:`_bind_transport` or
    # :meth:`_bind_chain` after the legacy ``__init__`` will raise
    # ``RuntimeError`` ("already bound"). That's intentional — the
    # write-once contract guards against accidental double-construction.
    # PR 2 inverts ``Session.__init__`` to leave these slots at ``None``
    # so the binders become the single assignment site. ``_rpc_executor``
    # is the one slot that ``__init__`` leaves at ``None`` (lazy via
    # :meth:`_get_rpc_executor`), so :meth:`_bind_executor` is the only
    # binder that fires in PR 1 if a caller exercises
    # :func:`compose_session_internals`.

    def _bind_transport(self, transport: "SessionTransport") -> None:
        """Write-once setter for :attr:`_transport`.

        Raises ``RuntimeError`` on a second bind attempt. PR 2 of Stage
        B1 calls this from :func:`compose_session_internals` after
        :func:`build_session_transport` returns; PR 1 leaves the binder
        dormant because :class:`Session.__init__` still wires the
        transport inline.
        """
        if getattr(self, "_transport", None) is not None:
            raise RuntimeError("Session._transport already bound")
        self._transport = transport

    def _bind_chain(self, wired: "WiredMiddleware") -> None:
        """Write-once setter for the wired middleware chain.

        Stores the three chain artifacts (``_chain_builder``,
        ``_middlewares``, ``_authed_post_chain``) in one call so the
        composition root cannot leave the chain partially wired. Raises
        ``RuntimeError`` on a second bind attempt.

        The ``_authed_post_chain`` attribute itself remains a mutable
        seam — the long-standing test pattern of reassigning
        ``core._authed_post_chain = fake_chain`` post-construction is
        unaffected because that path mutates the attribute directly,
        not via this binder. Only repeated calls to :meth:`_bind_chain`
        itself raise.
        """
        if getattr(self, "_chain_builder", None) is not None:
            raise RuntimeError("Session._chain already bound")
        self._chain_builder = wired.chain_builder
        self._middlewares = wired.middlewares
        self._authed_post_chain = wired.authed_post_chain

    def _bind_executor(self, executor: RpcExecutor) -> None:
        """Write-once setter for :attr:`_rpc_executor`.

        Distinct from the lazy :meth:`_get_rpc_executor` factory in two
        ways: (a) :meth:`_get_rpc_executor` is idempotent and lazily
        builds the executor only if ``_rpc_executor is None``, whereas
        :meth:`_bind_executor` is an explicit composition step that
        raises on a second bind; (b) the lazy factory exists for the
        ``close()`` → ``open()`` re-binding cycle (close nulls the
        slot, the next ``rpc_call`` lazily refills it) which Stage B1
        PR 2 removes. Until then, both coexist: PR 1's
        :func:`compose_session_internals` exercises the binder once,
        and the lazy factory takes over after ``close()`` for clients
        that re-open.
        """
        if getattr(self, "_rpc_executor", None) is not None:
            raise RuntimeError("Session._rpc_executor already bound")
        self._rpc_executor = executor

    def _require_constructed(self, attr_name: str) -> None:
        """Fail-fast guard for :class:`Session` entry points.

        Raises ``RuntimeError("Session not fully constructed: <attr> is
        None")`` when a required write-once binding is unset. Inert in
        PR 1 of Stage B1 because ``Session.__init__`` still inline-sets
        the slots; becomes load-bearing in PR 2 when the inline
        construction moves to :func:`compose_session_internals` and the
        guards catch any pre-binder call.

        The lookup uses :func:`getattr` with a ``None`` default so the
        check works during ``__init__`` itself (before the attribute
        has been assigned for the first time) — that path raises the
        same actionable message instead of an obscure ``AttributeError``.
        """
        if getattr(self, attr_name, None) is None:
            raise RuntimeError(f"Session not fully constructed: {attr_name} is None")

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
        # Stage B1 PR 1 fail-fast: ensure full composition before lifecycle
        # work. Inert under inline construction (legacy ``__init__`` sets
        # ``_transport`` before returning).
        self._require_constructed("_transport")
        await self._lifecycle.open(self)

    async def close(self) -> None:
        """Close the HTTP client connection.

        Called automatically by NotebookLMClient.__aexit__. Delegates to
        :meth:`ClientLifecycle.close`, which:

        1. Cancels and joins the keepalive task (so the loop can't issue a
           poke against an already-closed transport).
        2. Runs registered feature drain hooks.
        3. Saves cookies one last time through ``ClientLifecycle.save_cookies``.
        4. Calls ``aclose()`` under :func:`asyncio.shield` so cancellation
           arriving mid-close cannot leak the underlying httpx transport.
        5. Nulls out ``_kernel._http_client`` and ``_rpc_executor`` so a
           follow-up :meth:`open` rebuilds transport collaborators against
           the new ``httpx.AsyncClient``.
        """
        # Stage B1 PR 1 fail-fast: same guard as :meth:`open` — close()
        # tears down lifecycle state that depends on the composition
        # bundle. Inert under inline construction.
        self._require_constructed("_transport")
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
        jar source is fetched via ``self._kernel.get_http_client()`` so the
        ``open()`` precondition (and its ``RuntimeError`` if not initialised)
        is enforced at one site.

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
        # Stage B1 PR 1 fail-fast: ``_transport`` is the proxy for "full
        # composition completed" because :class:`Session.__init__` sets
        # it inline and it is never nulled by close(). The lazy executor
        # slot (``_rpc_executor``) is intentionally NOT checked here —
        # it is None between ``Session.__init__`` (which leaves it lazy)
        # and the first ``_get_rpc_executor()`` call, and is re-nulled
        # by ``ClientLifecycle.close``. Inert under inline construction.
        self._require_constructed("_transport")
        return await self._get_rpc_executor().rpc_call(
            method,
            params,
            source_path,
            allow_null,
            _is_retry,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
        )
