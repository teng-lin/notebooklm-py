"""Client-owned composition holder state."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import TYPE_CHECKING, Any, TypeVar

from ._loop_affinity import assert_bound_loop
from ._loop_bound import LoopBoundPrimitive
from ._runtime.config import DEFAULT_MAX_CONCURRENT_RPCS

_T = TypeVar("_T")

if TYPE_CHECKING:
    from ._middleware.chain import MiddlewareChainBuilder
    from ._middleware.chain_host import MiddlewareChainHost
    from ._middleware.core import Middleware
    from ._rpc_executor import RpcExecutor
    from ._runtime.init import RuntimeCollaborators, WiredMiddleware
    from ._runtime.transport import RuntimeTransport


class ClientComposed(LoopBoundPrimitive):
    """Mutable holder for the client's composition state."""

    def __init__(
        self,
        *,
        max_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
    ) -> None:
        if max_concurrent_rpcs is not None and max_concurrent_rpcs < 1:
            raise ValueError(f"max_concurrent_rpcs must be >= 1, got {max_concurrent_rpcs!r}")
        self.max_concurrent_rpcs = max_concurrent_rpcs
        self._rpc_semaphore: asyncio.Semaphore | None = None
        # The loop-affinity guard for the lazy RPC semaphore (``_bound_loop``)
        # and the ``set_bound_loop`` template method come from the
        # :class:`~notebooklm._loop_bound.LoopBoundPrimitive` base. This holder
        # overrides :meth:`_on_loop_rebind` to discard the cached
        # ``asyncio.Semaphore`` on a loop change so a cross-loop reopen never
        # reuses a primitive bound to a dead loop. ``_bound_loop is None`` is a
        # silent no-op for standalone holders constructed without an ``open()``
        # (composition / unit fixtures).
        self._transport: RuntimeTransport | None = None
        self._executor: RpcExecutor | None = None
        self._chain_host: MiddlewareChainHost | None = None
        self._chain_builder: MiddlewareChainBuilder | None = None
        self._middlewares: list[Middleware] | None = None
        # Avoid a plain `.collaborators` attribute here: the ADR-0014 lint
        # reserves that bare name so feature APIs can't grab the whole
        # bundle.
        self._runtime_collaborators: RuntimeCollaborators | None = None

    @staticmethod
    def _require_bound(attr_name: str, value: _T | None) -> _T:
        if value is None:
            raise RuntimeError(f"ClientComposed not fully constructed: {attr_name} is None")
        return value

    @property
    def transport(self) -> RuntimeTransport:
        return self._require_bound("_transport", self._transport)

    @property
    def executor(self) -> RpcExecutor:
        return self._require_bound("_executor", self._executor)

    @property
    def chain_host(self) -> MiddlewareChainHost:
        return self._require_bound("_chain_host", self._chain_host)

    @property
    def chain_builder(self) -> MiddlewareChainBuilder:
        return self._require_bound("_chain_builder", self._chain_builder)

    @property
    def middlewares(self) -> list[Middleware]:
        return self._require_bound("_middlewares", self._middlewares)

    @property
    def runtime_collaborators(self) -> RuntimeCollaborators:
        return self._require_bound("_runtime_collaborators", self._runtime_collaborators)

    def bind_transport(self, transport: RuntimeTransport) -> None:
        if self._transport is not None:
            raise RuntimeError("ClientComposed._transport already bound")
        self._transport = transport

    def bind_executor(self, executor: RpcExecutor) -> None:
        if self._executor is not None:
            raise RuntimeError("ClientComposed._executor already bound")
        self._executor = executor

    def bind_chain_host(self, chain_host: MiddlewareChainHost) -> None:
        if self._chain_host is not None:
            raise RuntimeError("ClientComposed._chain_host already bound")
        self._chain_host = chain_host

    def bind_chain_metadata(self, wired: WiredMiddleware) -> None:
        if self._chain_builder is not None:
            raise RuntimeError("ClientComposed._chain_builder already bound")
        self._chain_builder = wired.chain_builder
        self._middlewares = wired.middlewares

    def bind_runtime_collaborators(self, collaborators: RuntimeCollaborators) -> None:
        if self._runtime_collaborators is not None:
            raise RuntimeError("ClientComposed._runtime_collaborators already bound")
        self._runtime_collaborators = collaborators

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Discard the cached RPC semaphore when the bound loop changes.

        Fires from :meth:`~notebooklm._loop_bound.LoopBoundPrimitive.set_bound_loop`
        only on a real loop change (and before ``_bound_loop`` is updated), so
        ``set_bound_loop`` is self-consistent even if called independently of
        :meth:`reset_after_open` (e.g. directly in a test or a future caller):
        a stale semaphore bound to the old loop must never be reused after a
        rebind. The production ``open()`` path also calls
        :meth:`reset_after_open` immediately after, so the discard is
        idempotent there.
        """
        self._rpc_semaphore = None

    def reset_after_open(self) -> None:
        """Discard the lazy RPC semaphore so a reopened client rebinds it.

        Called from :meth:`ClientLifecycle.open` (alongside the
        per-collaborator ``set_bound_loop`` propagation) so a client that was
        closed and reopened on a *different* event loop builds a fresh
        ``asyncio.Semaphore`` on the new loop instead of reusing the stale one
        bound to the old (now-dead) loop. On Python 3.10/3.11 reusing the
        stale semaphore can raise "bound to a different event loop" or mispark
        waiters; on 3.12+ the breakage is largely masked, but resetting keeps
        the behaviour consistent across versions.

        Mirrors :meth:`TransportDrainTracker.reset_after_open`. Deliberately
        narrow: dropping the reference is enough because the semaphore is
        reconstructed lazily on the next :meth:`get_rpc_semaphore` call from
        inside the new loop. ``max_concurrent_rpcs`` is left untouched.
        """
        self._rpc_semaphore = None

    def get_rpc_semaphore(self) -> AbstractAsyncContextManager[Any]:
        """Return the lazy per-client RPC semaphore, or a no-op context.

        The loop-affinity guard runs BEFORE the lazy ``asyncio.Semaphore``
        allocation so a cross-loop call (semaphore created under loop A,
        acquired from loop B) raises ``RuntimeError`` at the call site instead
        of reusing a primitive bound to the wrong loop. The check is a silent
        no-op when ``_bound_loop is None`` (standalone holders / unopened
        composition fixtures), matching the sibling primitives.
        """
        if self.max_concurrent_rpcs is None:
            return nullcontext()
        assert_bound_loop(self._bound_loop)
        if self._rpc_semaphore is None:
            self._rpc_semaphore = asyncio.Semaphore(self.max_concurrent_rpcs)
        return self._rpc_semaphore


__all__ = ["ClientComposed"]
