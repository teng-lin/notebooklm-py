"""Test fixtures for the Tier-12 middleware chain.

These helpers let middleware tests build a chain with
``[middleware_under_test, ...]``, call it with a benign ``RpcRequest``, and
assert behavior without opening a real client/runtime HTTP stack.

Three helpers live here:

- :class:`FakeChainTerminal` — programmable terminal stub matching the
  ``NextCall`` shape: ``RpcRequest -> RpcResponse``.
- :func:`make_request` — factory for :class:`notebooklm._runtime.rpc_call.RpcRequest`
  instances with benign defaults. Tests override only the fields they care
  about via keyword arguments.
- :func:`chain_calls_through_to_terminal` — assertion helper that builds a
  chain over a :class:`FakeChainTerminal`, invokes it once, and returns
  whether the terminal was reached.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import httpx

from notebooklm._runtime.rpc_call import (
    NextCall,
    RpcRequest,
    RpcResponse,
)
from notebooklm._runtime.rpc_call_state import RpcCallState


class Behavior(Protocol):
    """Test-only callable shape for exercising one pipeline behavior."""

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse: ...


def build_chain(behaviors: Sequence[Behavior], terminal: NextCall) -> NextCall:
    """Test-only composer for focused behavior tests."""

    def wrap(behavior: Behavior, next_call: NextCall) -> NextCall:
        async def call(request: RpcRequest) -> RpcResponse:
            return await behavior(request, next_call)

        return call

    call = terminal
    for behavior in reversed(behaviors):
        call = wrap(behavior, call)
    return call


def make_call_state(**values: Any) -> RpcCallState:
    """Build typed call state; legacy context spellings ease test migration."""
    auth_snapshot = values.pop("auth_snapshot", None)
    auth_refreshed = bool(values.pop("auth_refreshed", False))
    queue_wait = values.pop("rpc_queue_wait_seconds", None)
    state = RpcCallState.create(auth_snapshot=auth_snapshot, **values)
    if auth_refreshed:
        state.mark_auth_refreshed()
    if queue_wait is not None:
        state.record_queue_wait(queue_wait)
    return state


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
        self.calls.append({"request": request, "state": request.state})

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

        return RpcResponse(response=response, state=request.state)


def make_request(
    *,
    base: RpcRequest | None = None,
    context_updates: dict[str, Any] | None = None,
    **overrides: Any,
) -> RpcRequest:
    """Build a fresh request, optionally copying and extending another request.

    Passing an unknown keyword raises ``TypeError`` early so test typos
    don't silently no-op.
    """
    defaults: dict[str, Any]
    if base is None:
        defaults = {
            "url": "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?authuser=0&_reqid=100000",
            "headers": {"X-Goog-AuthUser": "0"},
            "body": b"",
            "state": RpcCallState(),
        }
    else:
        defaults = {
            "url": base.url,
            "headers": dict(base.headers),
            "body": base.body,
            "state": base.state,
        }

    legacy_context = overrides.pop("context", None)
    if legacy_context is not None:
        overrides["state"] = make_call_state(**legacy_context)

    unknown = set(overrides) - set(defaults)
    if unknown:
        raise TypeError(
            "make_request() got unexpected keyword(s): "
            f"{sorted(unknown)!r}. Known fields: {sorted(defaults)!r}"
        )

    defaults.update(overrides)
    if context_updates:
        defaults["state"] = make_call_state(**context_updates)
    return RpcRequest(**defaults)


def chain_calls_through_to_terminal(
    terminal: FakeChainTerminal,
    middlewares: Sequence[Behavior],
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
    "Behavior",
    "FakeChainTerminal",
    "build_chain",
    "chain_calls_through_to_terminal",
    "make_call_state",
    "make_request",
]
