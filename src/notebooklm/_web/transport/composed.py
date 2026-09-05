"""Client-owned composition holder state."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

_T = TypeVar("_T")

if TYPE_CHECKING:
    from .executor import RpcExecutor
    from .init import WiredMiddleware
    from .middleware.chain import MiddlewareChainBuilder
    from .middleware.chain_host import MiddlewareChainHost
    from .middleware.core import Middleware
    from .runtime import RuntimeTransport


class ClientComposed:
    """Mutable holder for the client's composition state."""

    def __init__(self) -> None:
        self._transport: RuntimeTransport | None = None
        self._executor: RpcExecutor | None = None
        self._chain_host: MiddlewareChainHost | None = None
        self._chain_builder: MiddlewareChainBuilder | None = None
        self._middlewares: list[Middleware] | None = None

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


__all__ = ["ClientComposed"]
