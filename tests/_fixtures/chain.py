"""Test fixtures for the Tier-12 middleware chain.

These helpers let middleware tests build a chain with
``[middleware_under_test, ...]``, call it with a benign ``RpcRequest``, and
assert behavior without opening a real client/runtime HTTP stack.

Three helpers live here:

- :class:`FakeChainTerminal` — programmable terminal stub matching the
  ``NextCall`` shape: ``RpcRequest -> RpcResponse``.
- :func:`make_request` — factory for :class:`notebooklm._middleware.core.RpcRequest`
  instances with benign defaults. Tests override only the fields they care
  about via keyword arguments.
- :func:`chain_calls_through_to_terminal` — assertion helper that builds a
  chain over a :class:`FakeChainTerminal`, invokes it once, and returns
  whether the terminal was reached.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from notebooklm._middleware.core import (
    Middleware,
    RpcRequest,
    RpcResponse,
    build_chain,
)


class FakeChainTerminal:
    """Programmable stub for the middleware chain terminal."""

    def __init__(
        self,
        *,
        response: httpx.Response | None = None,
        response_factory: Callable[[], httpx.Response] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.response: httpx.Response | None = response
        self.response_factory: Callable[[], httpx.Response] | None = response_factory
        self.raises: BaseException | None = raises
        self.calls: list[dict[str, Any]] = []

    @property
    def was_called(self) -> bool:
        """``True`` if the terminal was called at least once."""
        return bool(self.calls)

    @property
    def call_count(self) -> int:
        """Number of times the terminal was called."""
        return len(self.calls)

    async def __call__(self, request: RpcRequest) -> RpcResponse:
        """Record the request and return the configured response envelope."""
        self.calls.append({"request": request, "context": request.context})

        # Resolution priority: raises → response_factory → response →
        # built-in 200/empty default. The call is recorded before any
        # configured exception so tests can still assert call_count.
        if self.raises is not None:
            raise self.raises

        if self.response_factory is not None:
            response = self.response_factory()
        elif self.response is not None:
            response = self.response
        else:
            response = httpx.Response(status_code=200, content=b"")

        return RpcResponse(response=response, context=request.context)


def make_request(**overrides: Any) -> RpcRequest:
    """Build an :class:`RpcRequest` with benign defaults plus overrides.

    Passing an unknown keyword raises ``TypeError`` early so test typos
    don't silently no-op.
    """
    defaults: dict[str, Any] = {
        "url": "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?authuser=0&_reqid=100000",
        "headers": {"X-Goog-AuthUser": "0"},
        "body": b"",
        "context": {},
    }

    unknown = set(overrides) - set(defaults)
    if unknown:
        raise TypeError(
            "make_request() got unexpected keyword(s): "
            f"{sorted(unknown)!r}. Known fields: {sorted(defaults)!r}"
        )

    defaults.update(overrides)
    return RpcRequest(**defaults)


def chain_calls_through_to_terminal(
    terminal: FakeChainTerminal,
    middlewares: Sequence[Middleware],
) -> bool:
    """Return ``True`` iff invoking the chain reaches the terminal."""
    chain = build_chain(middlewares, terminal)

    async def driver() -> RpcResponse:
        return await chain(make_request())

    # ``asyncio.run`` raises if there's already a running loop. The fixture
    # is meant to be called from synchronous test bodies; tests that need
    # to invoke the chain from an async context should compose
    # ``build_chain`` + ``make_request`` directly.
    asyncio.run(driver())
    return terminal.was_called


__all__ = [
    "FakeChainTerminal",
    "chain_calls_through_to_terminal",
    "make_request",
]
