"""Unit tests for the Tier-12 middleware-chain type scaffolding.

Exercises the shapes defined in ``src/notebooklm/_middleware/core.py`` and the
public-ish aliases in ``src/notebooklm/_request_types.py``:

- ``RpcRequest`` / ``RpcResponse`` dataclass round-trip (construction,
  ``dataclasses.replace`` equivalence, frozen-mutation guard).
- ``Middleware: Protocol`` structural typing — a plain async callable
  satisfies the Protocol without inheriting from it.
- ``build_chain`` composition order — leftmost middleware in the sequence
  becomes the outermost wrapper (matches ADR-0009 chain ordering).
- ``BuildRequestResult`` value semantics and request materialization bridges.

These tests target the type scaffolding and materialization contracts from
Tier-12/13. No production chain is wired (PR 12.2 does that), so the tests
build chains over fake terminals and observe behavior directly.
"""

from __future__ import annotations

import asyncio
import dataclasses

import httpx
import pytest

from notebooklm._middleware.core import (
    Middleware,
    NextCall,
    RpcRequest,
    RpcResponse,
    build_chain,
    materialize_rpc_request,
)
from notebooklm._request_types import (
    AuthSnapshot,
    BuildRequest,
    BuildRequestResult,
    materialize_build_request,
)

# ---------------------------------------------------------------------------
# RpcRequest / RpcResponse dataclass shape
# ---------------------------------------------------------------------------


def _make_response(status: int = 200) -> httpx.Response:
    """Fully-buffered ``httpx.Response`` for tests — no live network call."""
    return httpx.Response(status_code=status, content=b"")


def test_rpc_request_construction_with_defaults_and_overrides() -> None:
    req = RpcRequest(
        url="https://example/batchexecute?authuser=0&_reqid=100000",
        headers={"X-Goog-AuthUser": "0"},
        body=b"f.req=...",
    )
    assert req.url == "https://example/batchexecute?authuser=0&_reqid=100000"
    assert req.headers == {"X-Goog-AuthUser": "0"}
    assert req.body == b"f.req=..."
    # ``context`` defaults to an empty dict, fresh per instance.
    assert req.context == {}
    other = RpcRequest(url="https://x", headers={}, body=b"")
    assert other.context is not req.context  # independent dict instances


def test_rpc_request_is_frozen() -> None:
    req = RpcRequest(url="https://x", headers={}, body=b"")
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.url = "https://y"


def test_rpc_request_replace_returns_new_instance() -> None:
    req = RpcRequest(url="https://x", headers={"a": "1"}, body=b"", context={"k": "v"})
    new = dataclasses.replace(req, url="https://y")
    assert new.url == "https://y"
    assert req.url == "https://x"  # original untouched
    # ``replace`` keeps the same context dict by reference (intentional —
    # ADR-0009 §"Per-request behavior"). Mutating ``new.context`` is visible
    # on ``req.context`` because they are the same object.
    assert new.context is req.context


def test_rpc_response_construction() -> None:
    resp = _make_response()
    rpc_resp = RpcResponse(response=resp, context={"trace_id": "abc"})
    assert rpc_resp.response is resp
    assert rpc_resp.context == {"trace_id": "abc"}


def test_rpc_response_is_frozen() -> None:
    rpc_resp = RpcResponse(response=_make_response())
    with pytest.raises(dataclasses.FrozenInstanceError):
        rpc_resp.context = {"x": 1}


# ---------------------------------------------------------------------------
# Middleware Protocol — structural typing check
# ---------------------------------------------------------------------------


async def _passthrough_middleware(request: RpcRequest, next_call: NextCall) -> RpcResponse:
    return await next_call(request)


def test_async_callable_satisfies_middleware_protocol() -> None:
    """A bare async function with the right shape *is* a ``Middleware``.

    ``Middleware`` is a ``Protocol`` with a single ``__call__``; structural
    typing accepts any async callable whose parameters and return type
    match.
    """
    mw: Middleware = _passthrough_middleware  # mypy will fail if wrong shape
    assert callable(mw)


class _ClassMiddleware:
    """Class-based middleware — also satisfies the Protocol structurally."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, request: RpcRequest, next_call: NextCall) -> RpcResponse:
        self.calls += 1
        return await next_call(request)


def test_class_with_async_call_satisfies_middleware_protocol() -> None:
    mw: Middleware = _ClassMiddleware()
    assert callable(mw)


# ---------------------------------------------------------------------------
# build_chain ordering — leftmost-is-outermost (the ADR-0009 contract)
# ---------------------------------------------------------------------------


def _ordering_recorder(label: str, order_log: list[str]) -> Middleware:
    """Make a middleware that records its label before *and* after recursing.

    Used by the composition-order tests below: a chain ``[A, B, C]`` over
    terminal ``T`` should produce the log
    ``['A-pre', 'B-pre', 'C-pre', 'T', 'C-post', 'B-post', 'A-post']``.
    """

    async def _middleware(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        order_log.append(f"{label}-pre")
        response = await next_call(request)
        order_log.append(f"{label}-post")
        return response

    return _middleware


def test_build_chain_empty_middlewares_returns_terminal_unchanged() -> None:
    terminal_calls = 0

    async def terminal(request: RpcRequest) -> RpcResponse:
        nonlocal terminal_calls
        terminal_calls += 1
        return RpcResponse(response=_make_response())

    chain = build_chain([], terminal)
    asyncio.run(chain(RpcRequest(url="https://x", headers={}, body=b"")))
    assert terminal_calls == 1
    # And ``build_chain([], t)`` should literally return ``t`` — the
    # implementation skips the wrapper loop entirely.
    assert chain is terminal


def test_build_chain_three_middleware_call_order() -> None:
    """First in list is outermost; last in list is innermost.

    Matches ADR-0009 chain ordering: ``[Drain, Metrics, Semaphore, Retry,
    AuthRefresh, ErrorInjection, Tracing]`` → Drain (index 0) is the outermost
    wrapper, Tracing (index 6) wraps the terminal directly.
    """
    log: list[str] = []

    async def terminal(request: RpcRequest) -> RpcResponse:
        log.append("T")
        return RpcResponse(response=_make_response())

    chain = build_chain(
        [
            _ordering_recorder("A", log),
            _ordering_recorder("B", log),
            _ordering_recorder("C", log),
        ],
        terminal,
    )
    asyncio.run(chain(RpcRequest(url="https://x", headers={}, body=b"")))

    assert log == [
        "A-pre",  # outermost middleware enters
        "B-pre",
        "C-pre",  # innermost middleware enters
        "T",  # terminal runs
        "C-post",  # unwinds inside-out
        "B-post",
        "A-post",
    ]


def test_build_chain_preserves_class_middleware_state_across_calls() -> None:
    """Class-based middlewares keep their own state across chain invocations."""
    mw = _ClassMiddleware()

    async def terminal(request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=_make_response())

    chain = build_chain([mw], terminal)
    asyncio.run(chain(RpcRequest(url="https://x", headers={}, body=b"")))
    asyncio.run(chain(RpcRequest(url="https://y", headers={}, body=b"")))
    assert mw.calls == 2


def test_build_chain_middleware_can_transform_request() -> None:
    """A middleware that uses ``dataclasses.replace`` propagates the new request."""
    seen_urls: list[str] = []

    async def rewrite(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        new_request = dataclasses.replace(request, url=request.url + "?rewritten")
        return await next_call(new_request)

    async def terminal(request: RpcRequest) -> RpcResponse:
        seen_urls.append(request.url)
        return RpcResponse(response=_make_response())

    chain = build_chain([rewrite], terminal)
    asyncio.run(chain(RpcRequest(url="https://x", headers={}, body=b"")))
    assert seen_urls == ["https://x?rewritten"]


def test_build_chain_short_circuit_middleware_can_skip_terminal() -> None:
    """A middleware may return without calling ``next_call``.

    No production middleware in the Tier-12 set does this, but the protocol
    permits it (and certain test middlewares need it — e.g. a "deny all
    requests" canary).
    """
    terminal_calls = 0
    short_circuit_response = _make_response(status=418)

    async def short_circuit(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        return RpcResponse(response=short_circuit_response)

    async def terminal(request: RpcRequest) -> RpcResponse:
        nonlocal terminal_calls
        terminal_calls += 1
        return RpcResponse(response=_make_response())

    chain = build_chain([short_circuit], terminal)
    result = asyncio.run(chain(RpcRequest(url="https://x", headers={}, body=b"")))
    assert terminal_calls == 0
    assert result.response is short_circuit_response


def test_build_chain_each_call_gets_independent_closures() -> None:
    """Defensive guard against the late-binding closure bug in loops.

    Without the ``make_wrapper`` helper in ``build_chain``, every wrapped
    middleware would close over the *final* loop variable. This test would
    pick that up — each middleware would see the same (last) ``mw``.
    """
    log: list[str] = []

    async def terminal(request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=_make_response())

    chain = build_chain(
        [
            _ordering_recorder("first", log),
            _ordering_recorder("second", log),
            _ordering_recorder("third", log),
        ],
        terminal,
    )
    asyncio.run(chain(RpcRequest(url="https://x", headers={}, body=b"")))
    # If the closure bug were present, we'd see "third-pre" three times in
    # a row (every wrapped middleware closed over the same final label).
    assert log == [
        "first-pre",
        "second-pre",
        "third-pre",
        "third-post",
        "second-post",
        "first-post",
    ]


# ---------------------------------------------------------------------------
# _request_types.py — public aliases for AuthSnapshot / BuildRequest / BuildRequestResult
# ---------------------------------------------------------------------------


def test_auth_snapshot_round_trip() -> None:
    """``AuthSnapshot`` keeps value semantics through the request-types export."""
    snap = AuthSnapshot(
        csrf_token="csrf-1",
        session_id="sid-1",
        authuser=0,
        account_email=None,
    )
    assert snap.csrf_token == "csrf-1"
    assert snap.session_id == "sid-1"
    assert snap.authuser == 0
    assert snap.account_email is None
    # Equality is value-based (frozen dataclass).
    assert snap == AuthSnapshot(
        csrf_token="csrf-1",
        session_id="sid-1",
        authuser=0,
        account_email=None,
    )


def test_auth_snapshot_export_is_same_object_as_request_types_export() -> None:
    """``AuthSnapshot`` is owned by the request-types Module."""
    from notebooklm._request_types import AuthSnapshot as RequestTypesAuthSnapshot

    assert AuthSnapshot is RequestTypesAuthSnapshot


def test_build_request_alias_is_callable_type() -> None:
    """``BuildRequest`` is a callable type alias.

    Runtime check: a function with the right shape can be assigned to a
    ``BuildRequest``-annotated variable without mypy complaint, and the
    function is callable as documented.
    """
    snap = AuthSnapshot(csrf_token="csrf", session_id="sid", authuser=0, account_email=None)

    def factory(s: AuthSnapshot) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://x", b"body", {"X-Goog-AuthUser": str(s.authuser)})

    callable_factory: BuildRequest = factory
    url, body, headers = callable_factory(snap)
    assert url == "https://x"
    assert body == b"body"
    assert headers == {"X-Goog-AuthUser": "0"}


def test_build_request_export_is_same_object_as_request_types_export() -> None:
    """``BuildRequest`` is owned by the request-types Module."""
    from notebooklm._request_types import BuildRequest as RequestTypesBuildRequest

    assert BuildRequest is RequestTypesBuildRequest


def test_build_request_result_value_semantics() -> None:
    """``BuildRequestResult`` is frozen + value-equal."""
    r1 = BuildRequestResult(url="https://x", body=b"body", headers={"a": "1"})
    r2 = BuildRequestResult(url="https://x", body=b"body", headers={"a": "1"})
    assert r1 == r2
    with pytest.raises(dataclasses.FrozenInstanceError):
        r1.url = "https://y"


def test_build_request_result_accepts_none_headers() -> None:
    r = BuildRequestResult(url="https://x", body=b"", headers=None)
    assert r.headers is None


def test_materialize_build_request_normalizes_str_body_and_copies_headers() -> None:
    """Legacy tuple builders become the named bytes-body request shape."""
    snap = AuthSnapshot(csrf_token="csrf", session_id="sid", authuser=1, account_email=None)
    original_headers = {"X-Goog-AuthUser": "1"}

    def factory(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
        return (
            f"https://example.test/batchexecute?authuser={snapshot.authuser}",
            "f.req=%5B%5D&",
            original_headers,
        )

    result = materialize_build_request(factory, snap)

    assert result == BuildRequestResult(
        url="https://example.test/batchexecute?authuser=1",
        body=b"f.req=%5B%5D&",
        headers={"X-Goog-AuthUser": "1"},
    )
    assert result.headers is not original_headers
    original_headers["X-Goog-AuthUser"] = "2"
    assert result.headers == {"X-Goog-AuthUser": "1"}


def test_materialize_build_request_preserves_bytes_body_and_none_headers() -> None:
    snap = AuthSnapshot(csrf_token="csrf", session_id="sid", authuser=0, account_email=None)

    def factory(snapshot: AuthSnapshot) -> tuple[str, bytes, None]:
        return ("https://example.test/batchexecute", b"raw-bytes", None)

    result = materialize_build_request(factory, snap)

    assert result.body == b"raw-bytes"
    assert result.headers is None


def test_materialize_rpc_request_populates_envelope_and_preserves_context_identity() -> None:
    """The future middleware envelope keeps ADR-0009's shared context object."""
    snap = AuthSnapshot(csrf_token="csrf", session_id="sid", authuser=0, account_email=None)

    def factory(snapshot: AuthSnapshot) -> tuple[str, str, None]:
        return (
            f"https://example.test/batchexecute?authuser={snapshot.authuser}",
            "f.req=body&",
            None,
        )

    context = {"build_request": factory, "log_label": "RPC TEST_METHOD"}
    request = materialize_rpc_request(build_request=factory, snapshot=snap, context=context)

    assert request.url == "https://example.test/batchexecute?authuser=0"
    assert request.headers == {}
    assert request.body == b"f.req=body&"
    assert request.context is context

    request.context["trace_id"] = "trace-1"
    assert context["trace_id"] == "trace-1"


def test_request_types_all_contains_only_public_names() -> None:
    """``__all__`` is the canonical public-within-package export list."""
    from notebooklm import _request_types

    assert "AuthSnapshot" in _request_types.__all__
    assert "BuildRequest" in _request_types.__all__
    assert "BuildRequestResult" in _request_types.__all__
    assert "materialize_build_request" in _request_types.__all__
    assert all(not name.startswith("_") for name in _request_types.__all__)


def test_middleware_all_contains_only_public_names() -> None:
    """``_middleware.__all__`` exports the chain contract helpers."""
    from notebooklm import _middleware

    assert "Middleware" in _middleware.__all__
    assert "NextCall" in _middleware.__all__
    assert "RpcRequest" in _middleware.__all__
    assert "RpcResponse" in _middleware.__all__
    assert "build_chain" in _middleware.__all__
    assert "materialize_rpc_request" in _middleware.__all__
    assert all(not name.startswith("_") for name in _middleware.__all__)
