"""Middleware-chain host (Stage B2 PR 1 of the post-refactoring plan).

The :class:`MiddlewareChainHost` owns the four pieces of state that the
wired middleware chain reads on every authed POST plus the chain leaf
itself:

* the three retry-budget tunables (``_rate_limit_max_retries`` /
  ``_server_error_max_retries`` / ``_refresh_retry_delay``) that the
  chain's provider lambdas dereference live (``getattr(host, …)``);
* the installed chain reference (``_authed_post_chain``) that the
  transport's ``chain_provider`` closure dereferences on every authed
  POST so post-construction reassignment continues to steer the live
  chain;
* the chain leaf coroutine (``_authed_post_chain_terminal``) that
  forwards to :meth:`SessionTransport.terminal`;
* the dynamic refresh delegate (``await_refresh``) that callers reach
  through :meth:`Session._await_refresh`.

This module is intentionally narrow:

* It does NOT know about metrics, the kernel, the http client, the
  RPC semaphore, or the auth snapshot. Those live on :class:`Session`
  (the auth-snapshot host) and the collaborator bundle.
* The host has no back-reference to :class:`Session` — it is reachable
  from :class:`Session` (via ``self._chain_host``) but not the other
  way around. This breaks the historical Session ↔ transport cycle the
  way ADR-014 Rule 4 (post-refactoring plan, Stage B2) anticipated.

Stage B2 PR 1 wires the host into :class:`Session.__init__` and routes
the five mutable test seams (``_authed_post_chain_terminal``,
``_authed_post_chain``, ``_rate_limit_max_retries``,
``_server_error_max_retries``, ``_refresh_retry_delay``) through
writable ``@property`` descriptors on :class:`Session` that write
through to the host. Transport / wire signatures are still ``host=
session`` in PR 1 — providers continue to read ``session._<attr>``
which now writes through to the host. Stage B2 PR 2 will split
``build_session_transport`` and ``wire_middleware_chain`` so the chain
reads from the host directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._middleware import NextCall, RpcRequest, RpcResponse
    from ._session_auth import AuthRefreshCoordinator
    from ._session_transport import SessionTransport


@dataclass
class MiddlewareChainHost:
    """Owner of the middleware-chain mutable state and chain leaf.

    Constructed by :func:`compose_session_internals` BEFORE
    :class:`Session`. The transport is bound write-once via
    :meth:`_bind_transport` after :func:`build_session_transport`
    returns — this resolves the host ↔ transport construction cycle
    without giving either side a permanent back-reference to the other.

    Attributes:
        _auth_refresh: The :class:`AuthRefreshCoordinator` collaborator.
            :meth:`await_refresh` looks up the coordinator dynamically
            on every call so a fixture-time rebind of
            ``host._auth_refresh.await_refresh = fake`` keeps steering
            the live refresh-and-retry path.
        _rate_limit_max_retries: Budget consumed by the retry middleware
            on 429 responses. Stored on the host (the chain's provider
            lambda reads ``host._rate_limit_max_retries`` live, so
            mid-flight rebinding takes effect on the next attempt).
        _server_error_max_retries: Budget consumed by the retry
            middleware on 5xx responses. Same live-read contract.
        _refresh_retry_delay: Backoff between refresh-retry attempts
            in the auth-refresh middleware. Same live-read contract.
        _authed_post_chain: The wired middleware chain. ``None`` until
            :func:`compose_session_internals` assigns it through the
            :class:`Session` descriptor forward (which writes through
            to this slot). The transport's ``chain_provider`` lambda
            reads this attribute every authed POST.
        _transport: The :class:`SessionTransport` collaborator. ``None``
            until :meth:`_bind_transport` fires; after that bind the
            chain leaf (:meth:`_authed_post_chain_terminal`) can forward
            to ``transport.terminal``.
    """

    _auth_refresh: AuthRefreshCoordinator
    _rate_limit_max_retries: int
    _server_error_max_retries: int
    _refresh_retry_delay: float
    _authed_post_chain: NextCall | None = None
    _transport: SessionTransport | None = None

    def _bind_transport(self, transport: SessionTransport) -> None:
        """Write-once setter for :attr:`_transport`.

        Raises :class:`RuntimeError` on a second bind attempt — the
        composition root (:func:`compose_session_internals`) is the
        single legitimate caller, and it fires this once after
        :func:`build_session_transport` returns. The same write-once
        shape on :class:`Session` (:meth:`Session._bind_transport`)
        guarantees both sides of the host ↔ transport relationship are
        bound exactly once at composition time.
        """
        if self._transport is not None:
            raise RuntimeError("MiddlewareChainHost._transport already bound")
        self._transport = transport

    async def _authed_post_chain_terminal(self, request: RpcRequest) -> RpcResponse:
        """Middleware-chain leaf — forwards to :meth:`SessionTransport.terminal`.

        Reachable through the :class:`Session` writable descriptor
        forward (``session._authed_post_chain_terminal`` resolves to
        this bound method until a test installs a fake terminal via
        ``session._authed_post_chain_terminal = fake_terminal``; the
        descriptor's setter writes the fake through to this host so
        subsequent reads pick it up).

        Raises :class:`RuntimeError` if the transport is not yet bound.
        This can only happen if a caller exercised the chain before
        the composition root finished — the fail-fast guard mirrors
        the corresponding :meth:`Session._require_constructed` guard
        on the Session entry points (introduced in Stage B1 PR 2).
        """
        transport = self._transport
        if transport is None:
            raise RuntimeError("MiddlewareChainHost not fully constructed: _transport is None")
        return await transport.terminal(request)

    async def await_refresh(self) -> None:
        """Run / join the shared refresh task on the coordinator.

        Dynamic delegation — looks up ``self._auth_refresh.await_refresh``
        on every call so a fixture-time rebind of the coordinator's
        method (or of ``host._auth_refresh`` itself) keeps steering the
        live refresh path. The single-flight semantics, lock contract,
        and ``asyncio.shield`` cancellation handling all live inside
        :meth:`AuthRefreshCoordinator.await_refresh` — this method is a
        thin forward whose only job is to provide the chain a stable
        ``refresh_callable`` reference at construction time while still
        allowing the underlying implementation to be rebound for tests.
        """
        await self._auth_refresh.await_refresh()


__all__ = ["MiddlewareChainHost"]
