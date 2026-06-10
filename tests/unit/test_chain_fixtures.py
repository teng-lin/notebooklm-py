"""Unit tests for the ``tests/_fixtures/chain.py`` test substrate."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from notebooklm._middleware.core import (
    NextCall,
    RpcRequest,
    RpcResponse,
    build_chain,
)

# Match the import idiom documented in ``tests/_fixtures/__init__.py``: the
# ``tests/`` package chain is complete, so ``tests._fixtures.chain`` is the
# fully-qualified import path for these helpers.
from tests._fixtures.chain import (
    FakeChainTerminal,
    chain_calls_through_to_terminal,
    make_request,
)

# ---------------------------------------------------------------------------
# make_request
# ---------------------------------------------------------------------------


def test_make_request_defaults_are_benign() -> None:
    req = make_request()
    assert req.url.startswith("https://notebooklm.google.com/")
    assert "X-Goog-AuthUser" in req.headers
    assert req.body == b""
    assert req.context == {}


def test_make_request_overrides_replace_defaults() -> None:
    req = make_request(url="https://x", body=b"payload", context={"rpc_method": "ListNotebooks"})
    assert req.url == "https://x"
    assert req.body == b"payload"
    assert req.context == {"rpc_method": "ListNotebooks"}


def test_make_request_unknown_kwarg_raises_type_error() -> None:
    """Typo guard — unknown overrides raise eagerly rather than silently no-op."""
    with pytest.raises(TypeError, match="unexpected keyword"):
        make_request(rpc_method="ListNotebooks")  # should be in context


def test_make_request_context_is_independent_per_call() -> None:
    """Each call returns a fresh ``context`` dict — no shared mutable state."""
    a = make_request()
    b = make_request()
    a.context["leak"] = "value"
    assert "leak" not in b.context


# ---------------------------------------------------------------------------
# FakeChainTerminal
# ---------------------------------------------------------------------------


def test_fake_chain_terminal_records_calls() -> None:
    terminal = FakeChainTerminal()

    request = make_request(context={"log_label": "my-label"})

    async def driver() -> RpcResponse:
        return await terminal(request)

    resp = asyncio.run(driver())
    assert resp.response.status_code == 200
    assert terminal.was_called is True
    assert terminal.call_count == 1
    assert terminal.calls[0]["request"] is request
    assert terminal.calls[0]["context"] is request.context


def test_fake_chain_terminal_default_response_is_fresh_each_call() -> None:
    """Default 200/empty response is constructed per call (not shared)."""
    terminal = FakeChainTerminal()

    async def driver() -> tuple[httpx.Response, httpx.Response]:
        a = await terminal(make_request())
        b = await terminal(make_request())
        return a.response, b.response

    a, b = asyncio.run(driver())
    assert a is not b  # fresh instances per call
    assert a.status_code == 200
    assert b.status_code == 200


def test_fake_chain_terminal_explicit_response_is_returned() -> None:
    canned = httpx.Response(status_code=204, content=b"")
    terminal = FakeChainTerminal(response=canned)

    async def driver() -> httpx.Response:
        result = await terminal(make_request())
        return result.response

    result = asyncio.run(driver())
    assert result is canned


def test_fake_chain_terminal_response_factory_produces_per_call_responses() -> None:
    counter = {"n": 0}

    def factory() -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(status_code=200 + counter["n"], content=b"")

    terminal = FakeChainTerminal(response_factory=factory)

    async def driver() -> list[int]:
        results: list[int] = []
        for _ in range(3):
            resp = await terminal(make_request())
            results.append(resp.response.status_code)
        return results

    statuses = asyncio.run(driver())
    assert statuses == [201, 202, 203]


def test_fake_chain_terminal_raises_when_configured() -> None:
    terminal = FakeChainTerminal(raises=httpx.RequestError("boom"))

    async def driver() -> None:
        await terminal(make_request())

    with pytest.raises(httpx.RequestError, match="boom"):
        asyncio.run(driver())
    # The call is still recorded — the fake records first, then raises.
    assert terminal.call_count == 1


# ---------------------------------------------------------------------------
# chain_calls_through_to_terminal
# ---------------------------------------------------------------------------


def test_chain_calls_through_with_no_middlewares() -> None:
    """Empty chain still reaches the terminal."""
    terminal = FakeChainTerminal()
    assert chain_calls_through_to_terminal(terminal, []) is True
    assert terminal.call_count == 1


def test_chain_calls_through_with_passthrough_middleware() -> None:
    """A passthrough middleware doesn't block the chain from reaching terminal."""
    terminal = FakeChainTerminal()

    async def passthrough(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        return await next_call(request)

    assert chain_calls_through_to_terminal(terminal, [passthrough]) is True
    assert terminal.call_count == 1


def test_chain_calls_through_with_short_circuit_middleware_returns_false() -> None:
    """A short-circuiting middleware prevents the chain from reaching transport.

    No production middleware in the Tier-12 set does this, but the helper
    correctly reports it so tests that *expect* short-circuit behavior can
    assert against it.
    """
    terminal = FakeChainTerminal()

    async def short_circuit(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        return RpcResponse(response=httpx.Response(status_code=418, content=b""))

    assert chain_calls_through_to_terminal(terminal, [short_circuit]) is False
    assert terminal.call_count == 0


def test_chain_calls_through_with_multiple_middlewares_runs_all_in_order() -> None:
    """All middlewares get a chance to observe the request before transport."""
    terminal = FakeChainTerminal()
    call_order: list[str] = []

    def make_recorder(label: str):
        async def mw(request: RpcRequest, next_call: NextCall) -> RpcResponse:
            call_order.append(label)
            return await next_call(request)

        return mw

    middlewares = [make_recorder("A"), make_recorder("B"), make_recorder("C")]
    assert chain_calls_through_to_terminal(terminal, middlewares) is True
    assert call_order == ["A", "B", "C"]
    assert terminal.call_count == 1


def test_chain_terminal_receives_context() -> None:
    """The terminal receives the request context object."""
    terminal = FakeChainTerminal()

    async def stuff_context(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        request.context["log_label"] = "test-label"
        request.context["disable_internal_retries"] = True
        return await next_call(request)

    async def driver() -> None:
        # We can't use ``chain_calls_through_to_terminal`` here because
        # we want to assert on the *content* of the recorded call, not
        # just whether it happened.
        chain = build_chain([stuff_context], terminal)
        await chain(make_request())

    asyncio.run(driver())
    assert terminal.call_count == 1
    context = terminal.calls[0]["context"]
    assert context["log_label"] == "test-label"
    assert context["disable_internal_retries"] is True
