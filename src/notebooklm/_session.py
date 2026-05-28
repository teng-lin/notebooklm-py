"""Concrete session infrastructure for the NotebookLM API client."""

from __future__ import annotations

import asyncio  # noqa: F401 - compatibility patch surface for default sleep
import logging
import random  # noqa: F401 - tests patch this for _backoff jitter
from typing import TYPE_CHECKING

import httpx  # noqa: F401 - compatibility patch surface for AsyncClient defaults

from ._rpc_executor import RpcExecutor
from ._session_transport import SessionTransport
from .auth import (
    AuthTokens,
)

if TYPE_CHECKING:
    from ._client_seams import ClientSeams
    from ._middleware import Middleware
    from ._middleware_chain import MiddlewareChainBuilder
    from ._middleware_chain_host import MiddlewareChainHost
    from ._session_init import (
        SessionCollaborators,
        ValidatedSessionConfig,
        WiredMiddleware,
    )

    # ADR-014 Rule 5 (Wave 4 of session-decoupling): the compile-time
    # ``Session: RpcOwner`` assertion was removed when the ``RpcOwner``
    # Protocol itself was deleted — ``RpcExecutor`` now takes its
    # collaborators directly via keyword arguments instead of reaching
    # them through a Session-shaped owner.


logger = logging.getLogger(__name__)

# Auth-snapshot canonical implementation lives on
# :class:`AuthRefreshCoordinator` (``_session_auth.py`` —
# ``AuthRefreshCoordinator.snapshot`` / ``.update_auth_tokens`` /
# ``.update_auth_headers``). PR 8 first collapsed the previously
# real-bodied ``Session._snapshot`` / ``Session.update_auth_tokens``
# into thin delegates that forwarded through ``self._auth_coord``.
# PR #4b of the session-refactor arc then inlined
# ``Session._snapshot`` entirely — every site that needs an
# :class:`AuthSnapshot` now reads
# ``self._auth_coord.snapshot(auth=self.auth)`` directly. The
# coordinator method signatures take explicit ``auth`` / ``kernel``
# collaborators (the Session-shaped ``_AuthRefreshHost`` Protocol was
# deleted in favor of per-method explicit args). Wave 3 of plan
# ``host-protocol-removal`` deleted the remaining Session-level
# ``update_auth_tokens`` / ``update_auth_headers`` delegates and the
# ``lifecycle`` property; production callers
# (:func:`refresh_auth_session`, the integration tests that previously
# poked the headers via ``core.update_auth_headers()``) now invoke
# the coordinator methods directly with explicit kwargs.
# The AST guards in ``tests/unit/test_concurrency_refresh_race.py``
# (``test_snapshot_acquires_auth_snapshot_lock`` /
# ``test_update_auth_tokens_has_no_await_inside_mutation_block``)
# inspect the coordinator's source via ``inspect.getsource(...)`` +
# AST parsing — changes to auth-snapshot invariants must be applied to
# :meth:`AuthRefreshCoordinator.update_auth_tokens` directly.


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

    _seams: ClientSeams

    def __init__(
        self,
        *,
        collaborators: SessionCollaborators,
        config: ValidatedSessionConfig,
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

        self.auth = auth

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
        # the write-once binders. Entry points (``open`` / ``close``)
        # guard against use-before-bind via :meth:`_require_constructed`.
        # Types mirror the corresponding :class:`WiredMiddleware` fields so
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

    def _bind_transport(self, transport: SessionTransport) -> None:
        """Write-once setter for :attr:`_transport`.

        Raises ``RuntimeError`` on a second bind attempt.
        :func:`compose_session_internals` calls this after
        :func:`build_session_transport` returns; it is the single
        assignment site for :attr:`_transport` (Stage B1 PR 2 onwards).
        """
        if getattr(self, "_transport", None) is not None:
            raise RuntimeError("Session._transport already bound")
        self._transport = transport

    def _bind_chain_metadata(self, wired: WiredMiddleware) -> None:
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

    # ``lifecycle`` (@property), ``update_auth_headers``, and
    # ``update_auth_tokens`` were deleted in Wave 3 of plan
    # ``host-protocol-removal``. Callers now invoke the canonical
    # collaborator methods directly with explicit kwargs
    # (``auth_coord.update_auth_tokens(auth=..., csrf=..., session_id=...)``
    # / ``auth_coord.update_auth_headers(auth=..., kernel=...)`` /
    # ``self._collaborators.lifecycle`` for the refresh path). See
    # ``docs/session-method-retention.md`` **Deleted** section.
