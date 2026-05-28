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
from ._middleware_chain_host import MiddlewareChainHost
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
    from ._middleware import Middleware
    from ._middleware_chain import MiddlewareChainBuilder
    from ._session_init import (
        SessionCollaborators,
        ValidatedSessionConfig,
        WiredMiddleware,
    )
    from ._session_lifecycle import ClientLifecycle
    from ._session_transport import SessionTransport
    from .types import ConnectionLimits

    # ADR-014 Rule 5 (Wave 4 of session-decoupling): the compile-time
    # ``Session: RpcOwner`` assertion was removed when the ``RpcOwner``
    # Protocol itself was deleted — ``RpcExecutor`` now takes its
    # collaborators directly via keyword arguments instead of reaching
    # them through a Session-shaped owner.


logger = logging.getLogger(__name__)

# Auth-snapshot canonical implementation lives on
# :class:`AuthRefreshCoordinator` (``_session_auth.py`` —
# ``AuthRefreshCoordinator.snapshot`` / ``.update_auth_tokens``). PR 8
# first collapsed the previously real-bodied ``Session._snapshot`` /
# ``Session.update_auth_tokens`` into thin delegates that forwarded
# through ``self._auth_coord``. PR #4b of the session-refactor arc
# then inlined ``Session._snapshot`` entirely — every site that needs
# an :class:`AuthSnapshot` now reads
# ``self._auth_coord.snapshot(auth=self.auth)`` directly. The
# coordinator method signatures take explicit ``auth`` / ``kernel``
# collaborators (the Session-shaped ``_AuthRefreshHost`` Protocol was
# deleted in favor of per-method explicit args).
# ``Session.update_auth_tokens`` is retained as a delegate by the
# retention contract pinned in ``docs/session-method-retention.md`` and
# ``tests/_lint/test_session_retention.py``. Wave 2 of plan
# ``host-protocol-removal`` lifted the only remaining production caller
# (``refresh_auth_session``) off this delegate by giving the auth-session
# refresh path explicit collaborator kwargs (it now invokes
# ``auth_coord.update_auth_tokens(auth=..., csrf=..., session_id=...)``
# directly); the Session delegate body itself is scheduled for deletion
# in Wave 3 along with ``Session.update_auth_headers`` and the
# retention-doc rows that pin them. The AST guards in
# ``tests/unit/test_concurrency_refresh_race.py``
# (``test_snapshot_acquires_auth_snapshot_lock`` /
# ``test_update_auth_tokens_has_no_await_inside_mutation_block``)
# inspect the coordinator's source via ``inspect.getsource(...)`` +
# AST parsing — changes to auth-snapshot invariants must be applied to
# the coordinator (not the surviving ``update_auth_tokens`` delegate).


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
# Stage B1 PR 2 — composition root (live)
# ----------------------------------------------------------------------
#
# These helpers (``resolve_seam_defaults`` / :func:`compose_session_internals`
# / :class:`ComposedSession`) and the ``Session._bind_*`` write-once
# setters were introduced in Stage B1 PR 1 and made LIVE in PR 2 of the
# post-refactoring plan (``docs/post-refactoring-plan-2026-05-27.md``).
#
# After PR 2, ``Session.__init__`` takes ``(*, collaborators, config,
# auth)`` and leaves the transport / chain / executor slots at ``None``.
# :func:`compose_session_internals` is the only path that produces a
# fully-bound :class:`Session` — it constructs the collaborators bundle,
# the transport, the wired middleware chain, and the :class:`RpcExecutor`,
# and drives the write-once binders on the Session. The fail-fast guards
# on :class:`Session` entry points (``_get_rpc_semaphore`` / ``open`` /
# ``close``) became load-bearing in PR 2 — they raise actionably if a caller
# exercises the Session before the composition root has bound the slots.
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
    APIs against. After Stage B1 PR 2, this is the canonical output of
    the composition root — :class:`NotebookLMClient` consumes it directly
    and feature adapters draw from ``composed.executor`` /
    ``composed.transport`` / ``composed.collaborators`` rather than
    reading back through Session accessors.
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
    :class:`Session.__init__` performed inline before Stage B1 PR 2.
    Resolution happens against the :mod:`notebooklm._session` module's
    bindings so the documented monkeypatch paths
    (``notebooklm._session.asyncio.sleep`` /
    ``notebooklm._session.httpx.AsyncClient`` and the lazy imports inside
    :func:`_default_decode_response` / :func:`_default_is_auth_error`)
    keep steering the seams at construction time.

    Called from :func:`compose_session_internals`. After PR 2 this is the
    single seam-resolution site; ``Session.__init__`` no longer touches
    the seam defaults.
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

    Stage B1 PR 2 made this helper LIVE — :class:`Session.__init__` no
    longer constructs the collaborator bundle / transport / chain
    inline; this helper does, and feeds them into a ``Session(*,
    collaborators=..., config=..., auth=...)`` constructor that just
    stores references and initialises the late-bound slots to ``None``
    before the write-once binders fire.

    The kwarg surface mirrors the historical :class:`Session.__init__`
    kwargs (production NotebookLMClient kwargs ∪ the four seam kwargs
    ``decode_response`` / ``sleep`` / ``is_auth_error`` /
    ``async_client_factory``). The seam kwargs are intentionally
    test-only — they are NOT exposed on ``NotebookLMClient.__init__``,
    which preserves the public surface. Tests construct Sessions via
    ``tests/_helpers/session_factory.build_session_for_tests`` (a thin
    forwarder that accepts the same kwargs and returns the
    :class:`Session` from a :class:`ComposedSession`).

    The first call inside the body MUST stay
    :func:`_refuse_synthetic_error_outside_test_context` — that
    preserves the existing earliest-opportunity refusal pinned by
    :mod:`tests.unit.concurrency.test_synthetic_error_transport_guard`.

    The lambda closures for the executor wiring
    (``decode_response`` / ``is_auth_error`` / ``sleep`` /
    ``timeout_provider`` / ``refresh_callback_enabled_provider`` /
    ``refresh_retry_delay_provider``) preserve the late-binding contract
    pinned by
    :func:`tests.unit.test_init_order.test_session_wires_seam_attributes_for_executor_and_chain`
    — post-construction ``session._decode_response = rebound`` (and the
    sibling seam reassignments) continue to take effect inside the live
    executor because the closures dereference ``session._<attr>`` on
    every call.
    """
    # MUST stay first — preserves the earliest-opportunity refusal that
    # ``test_synthetic_error_transport_guard`` pins.
    _refuse_synthetic_error_outside_test_context()
    resolved = resolve_seam_defaults(
        sleep=sleep,
        async_client_factory=async_client_factory,
        is_auth_error=is_auth_error,
        decode_response=decode_response,
    )
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
        decode_response=resolved["decode_response"],
        sleep=resolved["sleep"],
        is_auth_error=resolved["is_auth_error"],
        async_client_factory=resolved["async_client_factory"],
    )
    collaborators = build_collaborators(
        config,
        auth=auth,
        refresh_callback=refresh_callback,
        on_rpc_event=on_rpc_event,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
    )
    # The :class:`MiddlewareChainHost` owns the retry tunables, the
    # chain slot, and the chain leaf. It is constructed BEFORE
    # :class:`Session` because :func:`build_session_transport` and
    # :func:`wire_middleware_chain` both take it as a direct parameter.
    chain_host = MiddlewareChainHost(
        _auth_refresh=collaborators.auth_coord,
        _rate_limit_max_retries=config.rate_limit_max_retries,
        _server_error_max_retries=config.server_error_max_retries,
        _refresh_retry_delay=config.refresh_retry_delay,
    )
    session = Session(
        collaborators=collaborators,
        config=config,
        auth=auth,
        chain_host=chain_host,
    )
    transport = build_session_transport(
        collaborators,
        host=session,
        chain_host=chain_host,
        logger=logger,
    )
    session._bind_transport(transport)
    # Bind the transport on the host as well so the chain leaf
    # (:meth:`MiddlewareChainHost._authed_post_chain_terminal`) can
    # forward to it. Both sides are write-once and bound in this same
    # composition root, so the symmetric bind is safe.
    chain_host._bind_transport(transport)
    # The chain leaf wires through ``chain_host._authed_post_chain_terminal``
    # directly. Tests that need a fake terminal rebind on the host
    # (``core._chain_host._authed_post_chain_terminal = fake_terminal``);
    # the chain is rebuilt by the test around that new terminal. The
    # auth-snapshot lookup passes ``auth=auth`` (the live
    # :class:`AuthTokens`) directly — the coordinator method takes the
    # tokens explicitly instead of reaching through a Session-shaped
    # host (the ``_AuthRefreshHost`` Protocol that re-declared Session's
    # private slots was deleted).
    wired = wire_middleware_chain(
        config,
        collaborators,
        chain_host=chain_host,
        auth=auth,
        authed_post_chain_terminal=chain_host._authed_post_chain_terminal,
        rpc_semaphore_factory=session._get_rpc_semaphore,
    )
    # The chain slot lives on the host (``chain_host._authed_post_chain``)
    # and is installed exactly once here. The transport's ``chain_provider``
    # lambda reads ``chain_host._authed_post_chain`` directly on every
    # authed POST, so a test that swaps the chain via
    # ``core._chain_host._authed_post_chain = fake_chain`` continues to
    # steer the live chain.
    chain_host._authed_post_chain = wired.authed_post_chain
    # ``_bind_chain_metadata`` stores only the auxiliary chain artifacts
    # (``_chain_builder`` / ``_middlewares``) — the chain slot is owned
    # by the host and assigned above, so this binder has no role in the
    # canonical install site for ``_authed_post_chain``.
    session._bind_chain_metadata(wired)
    # Lambdas preserve the late-binding contract pinned by
    # ``tests/unit/test_init_order.py``:
    # post-construction ``session._decode_response = rebound`` /
    # ``_sleep = …`` / ``_is_auth_error = …`` reassignments continue
    # to take effect inside the executor because each closure
    # dereferences ``session._<attr>`` on every call.
    #
    # The ``*a, **kw`` forwarding form (instead of capturing the
    # callable by name) is intentional — it lets test doubles that
    # rebind ``session._is_auth_error`` / ``session._sleep`` to a
    # callable with a different signature (e.g. a ``Mock`` with
    # ``**kwargs``) keep working without the closure dropping
    # arguments. See gemini-code-assist PR #1086 review, finding 4.
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
        refresh_retry_delay_provider=lambda: chain_host._refresh_retry_delay,
    )
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
        *,
        collaborators: "SessionCollaborators",
        config: "ValidatedSessionConfig",
        auth: AuthTokens,
        chain_host: MiddlewareChainHost,
    ) -> None:
        """Initialise a Session from a pre-built collaborator bundle.

        :class:`Session` does not construct the bundle / transport /
        chain inline — :func:`compose_session_internals` builds all
        three, then calls this constructor with the validated config +
        the bundle + the auth tokens. The transport / chain / executor
        are written into the late-bound slots by the composition root
        via the :meth:`_bind_transport` / :meth:`_bind_chain_metadata`
        / :meth:`_bind_executor` write-once setters.

        ``chain_host`` is the :class:`MiddlewareChainHost` constructed
        by :func:`compose_session_internals` BEFORE this constructor.
        The host owns the retry tunables, the installed chain slot,
        and the chain leaf; :class:`Session` keeps a reference to it
        as ``self._chain_host`` so feature code and tests that need
        to rebind one of those slots can reach the host directly
        (``core._chain_host._rate_limit_max_retries = N``,
        ``core._chain_host._authed_post_chain = fake_chain``,
        ``core._chain_host._authed_post_chain_terminal = fake_terminal``).

        Production callers DO NOT instantiate :class:`Session` directly
        — :class:`NotebookLMClient` calls
        :func:`compose_session_internals` from its own ``__init__`` and
        feature adapters draw from the returned :class:`ComposedSession`.
        Tests use the canonical
        ``tests/_helpers/session_factory.build_session_for_tests``
        helper, which forwards through the same composition root.

        Args:
            collaborators: The :class:`SessionCollaborators` bundle
                constructed by :func:`build_collaborators` inside
                :func:`compose_session_internals`.
            config: The :class:`ValidatedSessionConfig` constructed by
                :func:`validate_constructor_args` inside
                :func:`compose_session_internals`.
            auth: Authentication tokens from browser login.
            chain_host: The :class:`MiddlewareChainHost` constructed by
                :func:`compose_session_internals` for this session. The
                host owns the chain leaf, the chain slot, and the three
                retry-budget tunables.
        """
        # ``_chain_host`` owns the retry tunables (``_rate_limit_max_retries``,
        # ``_server_error_max_retries``, ``_refresh_retry_delay``), the
        # chain slot (``_authed_post_chain``), and the chain leaf
        # (``_authed_post_chain_terminal``). :func:`compose_session_internals`
        # constructed the host with the live values BEFORE this Session
        # was instantiated, and it remains the canonical owner — there
        # are no Session-side aliases or descriptor forwards.
        self._chain_host = chain_host

        # The seam callables ``_decode_response`` / ``_sleep`` /
        # ``_is_auth_error`` — the executor closures dereference these
        # via ``session._<attr>`` on every call, so post-construction
        # reassignment continues to take effect.
        self.auth = auth
        self._decode_response: Callable[..., Any] = config.decode_response
        self._sleep: Callable[[float], Awaitable[Any]] = config.sleep
        self._is_auth_error: Callable[[Exception], bool] = config.is_auth_error
        self._max_concurrent_rpcs: int | None = config.max_concurrent_rpcs
        # Lazy-created per-instance — see :meth:`_get_rpc_semaphore`.
        self._rpc_semaphore: asyncio.Semaphore | None = None

        # The collaborator bundle is stored as a private attribute so
        # :class:`NotebookLMClient` can hoist the ``metrics``
        # collaborator off the same bundle the Session uses (e.g. for
        # ``NotebookLMClient.metrics_snapshot``). The Stage A
        # accessor properties (``Session.collaborators`` /
        # ``Session.session_transport`` / ``Session.rpc_executor``) that
        # previously exposed the bundle through the Session surface
        # were deleted in this PR — :class:`NotebookLMClient` reads
        # from the :class:`ComposedSession` it received instead.
        self._collaborators = collaborators
        self._metrics_obj = collaborators.metrics
        self._drain_tracker = collaborators.drain_tracker
        self._reqid = collaborators.reqid
        self._auth_coord = collaborators.auth_coord
        self._kernel = collaborators.kernel
        self._lifecycle = collaborators.lifecycle
        self.cookie_persistence = collaborators.cookie_persistence

        # Late-bound storage — these slots stay ``None`` until the
        # composition root in :func:`compose_session_internals` drives
        # the write-once binders. Entry points (``_get_rpc_semaphore`` /
        # ``open`` / ``close``) guard against
        # use-before-bind via :meth:`_require_constructed`. Types
        # mirror the corresponding :class:`WiredMiddleware` fields so
        # downstream readers see precise types rather than ``Any``
        # (claude[bot] review on PR #1089). The ``_authed_post_chain``
        # slot is owned by ``_chain_host``; it is not duplicated here.
        self._transport: SessionTransport | None = None
        self._chain_builder: MiddlewareChainBuilder | None = None
        self._middlewares: list[Middleware] | None = None
        self._rpc_executor: RpcExecutor | None = None

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
        # Stage B1 PR 2 fail-fast: this factory is captured by the
        # chain at construction time and invoked from middleware on
        # every rpc_call. A pre-composition call indicates the chain
        # is being exercised before the composition root drove
        # :meth:`_bind_transport`.
        self._require_constructed("_transport")
        if self._max_concurrent_rpcs is None:
            return nullcontext()
        if self._rpc_semaphore is None:
            self._rpc_semaphore = asyncio.Semaphore(self._max_concurrent_rpcs)
        return self._rpc_semaphore

    # ------------------------------------------------------------------
    # Write-once binders + fail-fast guards
    # ------------------------------------------------------------------
    #
    # The three ``_bind_*`` setters below accept exactly one bind per
    # attribute. They are reserved for :func:`compose_session_internals`
    # (the composition root) and are load-bearing — :meth:`Session.__init__`
    # leaves ``_transport`` / ``_chain_builder`` / ``_middlewares`` /
    # ``_rpc_executor`` at ``None``, so the composition root is the
    # single assignment site for each.
    #
    # ``_authed_post_chain`` is owned by :class:`MiddlewareChainHost`;
    # the composition root installs it via
    # ``chain_host._authed_post_chain = wired.authed_post_chain``. The
    # binder below stores only the auxiliary chain artifacts
    # (``_chain_builder`` / ``_middlewares``) so the chain slot has
    # exactly one assignment site.
    #
    # The executor is reachable directly via ``self._rpc_executor``
    # (and never re-nulled by ``close()`` — see
    # ``_session_lifecycle.py:close`` for the corresponding contract).

    def _bind_transport(self, transport: "SessionTransport") -> None:
        """Write-once setter for :attr:`_transport`.

        Raises ``RuntimeError`` on a second bind attempt.
        :func:`compose_session_internals` calls this after
        :func:`build_session_transport` returns; it is the single
        assignment site for :attr:`_transport` (Stage B1 PR 2 onwards).
        """
        if getattr(self, "_transport", None) is not None:
            raise RuntimeError("Session._transport already bound")
        self._transport = transport

    def _bind_chain_metadata(self, wired: "WiredMiddleware") -> None:
        """Write-once setter for the auxiliary chain-metadata artifacts.

        The canonical install site for ``_authed_post_chain`` is
        ``chain_host._authed_post_chain = wired.authed_post_chain`` in
        :func:`compose_session_internals`. This binder is left to store
        only the *auxiliary* artifacts —
        :class:`MiddlewareChainBuilder` (introspected by builder-level
        unit tests) and the ``middlewares`` list (introspected by
        ``test_chain_wiring.test_chain_seeded_with_final_adr_009_ordering``).
        Raises ``RuntimeError`` on a second bind attempt.

        Tests that need to swap the live chain after construction
        rebind ``core._chain_host._authed_post_chain = fake_chain`` so
        the transport's ``chain_provider`` lambda picks up the fake on
        the next authed POST; this binder does not participate in that
        post-construction rebind path.
        """
        if getattr(self, "_chain_builder", None) is not None:
            raise RuntimeError("Session._chain_metadata already bound")
        self._chain_builder = wired.chain_builder
        self._middlewares = wired.middlewares

    def _bind_executor(self, executor: RpcExecutor) -> None:
        """Write-once setter for :attr:`_rpc_executor`.

        Stage B1 PR 2 deleted the legacy lazy ``_get_rpc_executor``
        factory — :func:`compose_session_internals` is the only
        producer of an :class:`RpcExecutor`, and it drives this binder
        exactly once during composition. The slot is NOT re-nulled by
        :meth:`ClientLifecycle.close`; the executor persists across
        ``close()`` → ``open()`` cycles because the underlying
        transport collaborator (:class:`Kernel`) rebuilds its
        ``httpx.AsyncClient`` lazily on each ``open()``.
        """
        if getattr(self, "_rpc_executor", None) is not None:
            raise RuntimeError("Session._rpc_executor already bound")
        self._rpc_executor = executor

    def _require_constructed(self, attr_name: str) -> None:
        """Fail-fast guard for :class:`Session` entry points.

        Raises ``RuntimeError("Session not fully constructed: <attr> is
        None")`` when a required write-once binding is unset. Load-bearing
        after Stage B1 PR 2: :class:`Session.__init__` leaves the
        transport / chain / executor slots at ``None`` and only the
        composition root (:func:`compose_session_internals`) drives the
        binders, so this guard catches any path that exercises a
        :class:`Session` outside that root.

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

        Wave 2 of plan ``host-protocol-removal`` narrowed
        :meth:`ClientLifecycle.open` to take explicit collaborator
        kwargs; this forwarder unpacks its own collaborator aliases
        and passes them through so the lifecycle never reaches back
        through a Session-shaped host.
        """
        # Stage B1 PR 2 fail-fast: ensure full composition before
        # lifecycle work. The composition root
        # (:func:`compose_session_internals`) drives
        # :meth:`_bind_transport` before returning, so a ``None``
        # here means the Session was instantiated outside the
        # composition root and is unusable.
        self._require_constructed("_transport")
        await self._lifecycle.open(
            auth=self.auth,
            drain_tracker=self._drain_tracker,
            auth_coord=self._auth_coord,
            reqid=self._reqid,
            cookie_persistence=self.cookie_persistence,
        )

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
        5. Nulls out ``_kernel._http_client`` so a follow-up
           :meth:`open` rebuilds the live transport against a fresh
           ``httpx.AsyncClient``.

        Stage B1 PR 2 dropped the close-time ``_rpc_executor = None``
        step that previously lived in :meth:`ClientLifecycle.close` —
        the executor is composition-root-bound and persists across
        ``close()`` → ``open()`` cycles. See
        :mod:`tests.unit.test_lifecycle_executor_reuse` for the
        regression pin.

        Wave 2 of plan ``host-protocol-removal`` narrowed
        :meth:`ClientLifecycle.close` to take explicit collaborator
        kwargs; this forwarder unpacks its own collaborator aliases
        and passes them through.
        """
        # Stage B1 PR 2 fail-fast: same guard as :meth:`open`.
        self._require_constructed("_transport")
        await self._lifecycle.close(
            auth_coord=self._auth_coord,
            drain_tracker=self._drain_tracker,
            cookie_persistence=self.cookie_persistence,
        )

    async def _keepalive_loop(self, interval: float) -> None:
        """Background loop that periodically pokes the identity surface.

        Thin facade over :meth:`ClientLifecycle._keepalive_loop`. Retained
        as a ``Session`` method so ``test_client_keepalive`` and other
        tests that introspect ``core._keepalive_loop`` continue to resolve.

        Wave 2 of plan ``host-protocol-removal`` narrowed
        :meth:`ClientLifecycle._keepalive_loop` to take an explicit
        ``cookie_persistence`` kwarg; this forwarder supplies the
        Session's own collaborator alias.
        """
        await self._lifecycle._keepalive_loop(
            cookie_persistence=self.cookie_persistence,
            interval=interval,
        )

    @property
    def is_open(self) -> bool:
        """Check if the HTTP client is open."""
        return self._lifecycle.is_open()

    async def drain(self, timeout: float | None = None) -> None:
        """Stop accepting new operations and wait for in-flight ones to finish.

        Narrow forward to :meth:`TransportDrainTracker.drain` so the
        ``NotebookLMClient`` composition root no longer dereferences
        ``self._session._drain_tracker`` (a private collaborator slot)
        when implementing :meth:`NotebookLMClient.drain`. The method
        body intentionally stays a one-line delegation — Session does
        not add semantics here, it just exposes the drain capability
        with a name that does not depend on the underscore-prefixed
        storage slot.
        """
        await self._drain_tracker.drain(timeout=timeout)

    @property
    def lifecycle(self) -> "ClientLifecycle":
        """Return the :class:`ClientLifecycle` collaborator.

        Narrow read-only accessor introduced so the
        ``NotebookLMClient.refresh_auth`` composition root can pass the
        live :class:`ClientLifecycle` to
        :func:`refresh_auth_session` without dereferencing
        ``self._session._lifecycle`` (a private collaborator slot).
        The previous ``self._session._lifecycle`` reach-through was
        also load-bearing for
        ``tests/unit/test_concurrency_refresh_race.py``, which
        constructs a shell client via ``NotebookLMClient.__new__`` and
        sets only ``client._session`` — that test path resolves
        ``client._session.lifecycle`` to this property and continues to
        work unchanged.
        """
        return self._lifecycle

    def update_auth_headers(self) -> None:
        """Refresh auth metadata without resetting the live cookie jar.

        Call this after modifying auth tokens (e.g., after refresh_auth())
        to ensure the HTTP client uses the updated credentials. Delegates
        to :meth:`AuthRefreshCoordinator.update_auth_headers`; the cookie
        jar source is fetched via ``self._kernel.get_http_client()`` so the
        ``open()`` precondition (and its ``RuntimeError`` if not initialised)
        is enforced at one site. The coordinator no longer accepts a
        Session-shaped host — the two collaborators it reads (``auth`` /
        ``kernel``) are passed explicitly per call.

        Raises:
            RuntimeError: If client is not initialized.
        """
        self._auth_coord.update_auth_headers(auth=self.auth, kernel=self._kernel)

    async def update_auth_tokens(self, csrf: str, session_id: str) -> None:
        """Delegate to :meth:`AuthRefreshCoordinator.update_auth_tokens`.

        Retained on Session by the retention contract pinned in
        ``docs/session-method-retention.md`` and
        ``tests/_lint/test_session_retention.py``. Wave 2 of plan
        ``host-protocol-removal`` lifted the last production caller off
        this delegate: :func:`refresh_auth_session` now invokes
        ``auth_coord.update_auth_tokens(auth=..., csrf=...,
        session_id=...)`` directly through the explicit-collaborator
        kwargs introduced by that wave. The delegate body is scheduled
        for deletion in Wave 3 once the retention-doc rows for this
        method and :meth:`update_auth_headers` are also removed.
        PR 8 collapsed the previously real body into a delegate that
        forwards through ``self._auth_coord``; PR #4b of the
        session-refactor arc inlined sibling delegates but kept this
        one for the Protocol caller. The coordinator now takes ``auth``
        explicitly and routes the lock-wait metric through its own
        ``self._metrics`` (supplied at construction) instead of reaching
        through a Session-shaped host. The AST guard for the no-await
        mutation-block invariant lives on
        :meth:`AuthRefreshCoordinator.update_auth_tokens`
        (``test_concurrency_refresh_race.test_update_auth_tokens_has_no_await_inside_mutation_block``).
        """
        await self._auth_coord.update_auth_tokens(auth=self.auth, csrf=csrf, session_id=session_id)
