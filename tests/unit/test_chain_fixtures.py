"""Unit tests for the ``tests/_fixtures/chain.py`` test substrate.

Exercises the three helpers that PR 12.1 lands so every subsequent
middleware PR (12.3–12.8) has a uniform test substrate:

- ``FakeAuthedPost`` — programmable stub matching
  ``AuthedTransport.perform_authed_post``.
- ``make_request`` — factory for ``RpcRequest`` with benign defaults.
- ``chain_calls_through_to_authed_post`` — assertion helper that builds a
  chain over a ``FakeAuthedPost`` terminal and reports whether the
  transport was reached.

These tests verify the fixtures themselves so middleware-extraction PRs
can trust the substrate without re-discovering its shape.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

# Match the import idiom documented in ``tests/_fixtures/__init__.py``:
# pytest puts ``tests/`` on ``sys.path``, so ``_fixtures.chain`` is the
# canonical import path for these helpers.
from _fixtures.chain import (
    FakeAuthedPost,
    chain_calls_through_to_authed_post,
    make_request,
)
from notebooklm._middleware import (
    NextCall,
    RpcRequest,
    RpcResponse,
    build_chain,
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
# FakeAuthedPost
# ---------------------------------------------------------------------------


def test_fake_authed_post_records_calls() -> None:
    transport = FakeAuthedPost()

    async def driver() -> httpx.Response:
        return await transport.perform_authed_post(
            build_request=lambda snap: ("https://x", b"", None),
            log_label="my-label",
            disable_internal_retries=True,
        )

    resp = asyncio.run(driver())
    assert resp.status_code == 200
    assert transport.was_called is True
    assert transport.call_count == 1
    assert transport.calls[0]["log_label"] == "my-label"
    assert transport.calls[0]["disable_internal_retries"] is True


def test_fake_authed_post_default_response_is_fresh_each_call() -> None:
    """Default 200/empty response is constructed per call (not shared)."""
    transport = FakeAuthedPost()

    async def driver() -> tuple[httpx.Response, httpx.Response]:
        a = await transport.perform_authed_post(
            build_request=lambda s: ("https://x", b"", None),
            log_label="a",
        )
        b = await transport.perform_authed_post(
            build_request=lambda s: ("https://x", b"", None),
            log_label="b",
        )
        return a, b

    a, b = asyncio.run(driver())
    assert a is not b  # fresh instances per call
    assert a.status_code == 200
    assert b.status_code == 200


def test_fake_authed_post_explicit_response_is_returned() -> None:
    canned = httpx.Response(status_code=204, content=b"")
    transport = FakeAuthedPost(response=canned)

    async def driver() -> httpx.Response:
        return await transport.perform_authed_post(
            build_request=lambda s: ("https://x", b"", None),
            log_label="lbl",
        )

    result = asyncio.run(driver())
    assert result is canned


def test_fake_authed_post_response_factory_produces_per_call_responses() -> None:
    counter = {"n": 0}

    def factory() -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(status_code=200 + counter["n"], content=b"")

    transport = FakeAuthedPost(response_factory=factory)

    async def driver() -> list[int]:
        results: list[int] = []
        for _ in range(3):
            resp = await transport.perform_authed_post(
                build_request=lambda s: ("https://x", b"", None),
                log_label="lbl",
            )
            results.append(resp.status_code)
        return results

    statuses = asyncio.run(driver())
    assert statuses == [201, 202, 203]


def test_fake_authed_post_raises_when_configured() -> None:
    transport = FakeAuthedPost(raises=httpx.RequestError("boom"))

    async def driver() -> None:
        await transport.perform_authed_post(
            build_request=lambda s: ("https://x", b"", None),
            log_label="lbl",
        )

    with pytest.raises(httpx.RequestError, match="boom"):
        asyncio.run(driver())
    # The call is still recorded — the fake records first, then raises.
    assert transport.call_count == 1


# ---------------------------------------------------------------------------
# chain_calls_through_to_authed_post
# ---------------------------------------------------------------------------


def test_chain_calls_through_with_no_middlewares() -> None:
    """Empty chain still reaches the transport (terminal call)."""
    transport = FakeAuthedPost()
    assert chain_calls_through_to_authed_post(transport, []) is True
    assert transport.call_count == 1


def test_chain_calls_through_with_passthrough_middleware() -> None:
    """A passthrough middleware doesn't block the chain from reaching transport."""
    transport = FakeAuthedPost()

    async def passthrough(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        return await next_call(request)

    assert chain_calls_through_to_authed_post(transport, [passthrough]) is True
    assert transport.call_count == 1


def test_chain_calls_through_with_short_circuit_middleware_returns_false() -> None:
    """A short-circuiting middleware prevents the chain from reaching transport.

    No production middleware in the Tier-12 set does this, but the helper
    correctly reports it so tests that *expect* short-circuit behavior can
    assert against it.
    """
    transport = FakeAuthedPost()

    async def short_circuit(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        return RpcResponse(response=httpx.Response(status_code=418, content=b""))

    assert chain_calls_through_to_authed_post(transport, [short_circuit]) is False
    assert transport.call_count == 0


def test_chain_calls_through_with_multiple_middlewares_runs_all_in_order() -> None:
    """All middlewares get a chance to observe the request before transport."""
    transport = FakeAuthedPost()
    call_order: list[str] = []

    def make_recorder(label: str):
        async def mw(request: RpcRequest, next_call: NextCall) -> RpcResponse:
            call_order.append(label)
            return await next_call(request)

        return mw

    middlewares = [make_recorder("A"), make_recorder("B"), make_recorder("C")]
    assert chain_calls_through_to_authed_post(transport, middlewares) is True
    assert call_order == ["A", "B", "C"]
    assert transport.call_count == 1


def test_chain_terminal_reads_context_keys_for_transport_call() -> None:
    """The terminal forwards ``build_request`` / ``log_label`` from request.context.

    PR 12.2's chain leaf will do exactly this on real production data.
    The fixture mirrors the contract so middleware PRs can assert against
    a realistic chain shape.
    """
    transport = FakeAuthedPost()

    sentinel_build_request = object()

    async def stuff_context(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        # Middleware populates the keys the terminal expects.
        request.context["build_request"] = sentinel_build_request
        request.context["log_label"] = "test-label"
        request.context["disable_internal_retries"] = True
        return await next_call(request)

    async def driver() -> None:
        # We can't use ``chain_calls_through_to_authed_post`` here because
        # we want to assert on the *content* of the recorded call, not
        # just whether it happened.
        async def terminal(req: RpcRequest) -> RpcResponse:
            ctx = req.context
            resp = await transport.perform_authed_post(
                build_request=ctx["build_request"],
                log_label=ctx["log_label"],
                disable_internal_retries=ctx["disable_internal_retries"],
            )
            return RpcResponse(response=resp, context=ctx)

        chain = build_chain([stuff_context], terminal)
        await chain(make_request())

    asyncio.run(driver())
    assert transport.call_count == 1
    call = transport.calls[0]
    assert call["build_request"] is sentinel_build_request
    assert call["log_label"] == "test-label"
    assert call["disable_internal_retries"] is True
