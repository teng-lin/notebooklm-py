"""Integration tests for the empty middleware chain wired into ``Session``.

PR 12.2 of the Tier-12/13 greenfield migration wires
:func:`notebooklm._middleware.build_chain` into
:meth:`Session.__init__` with an empty middleware list. The chain leaf
(:meth:`Session._authed_post_chain_terminal`) reads
``build_request`` / ``log_label`` / ``disable_internal_retries`` from
``RpcRequest.context`` and delegates to
:meth:`AuthedTransport.perform_authed_post` — the shared seam covering both
:meth:`Session._perform_authed_post` AND ``RpcExecutor.execute`` (which
calls ``self._owner._perform_authed_post`` at ``_core_rpc.py:275``).

These tests verify the wiring contract from
``.sisyphus/plans/tier-12-13-greenfield-migration.md`` line 160 and ADR-009
§"RpcRequest.context keys":

1. Both call paths (``Session._perform_authed_post`` directly and
   ``RpcExecutor.execute`` indirectly) flow through the empty chain to the
   transport.
2. ``RpcRequest.context`` carries ``build_request`` / ``log_label`` /
   ``disable_internal_retries`` exactly as the leaf expects them.
3. The leaf returns an :class:`RpcResponse` wrapping the
   :class:`httpx.Response` from the transport.

The terminal adapter resolves ``self._get_authed_transport()`` per
invocation so swapping the transport mid-test (the idiom used here) still
affects live behavior — a property the existing
``test_core_transport.py`` test suite already relies on for its
monkeypatch surface.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

# pytest puts ``tests/`` on ``sys.path``; ``_fixtures.chain`` is the
# canonical import path documented in ``tests/_fixtures/__init__.py``.
from _fixtures.chain import FakeAuthedPost
from notebooklm._middleware import (
    Middleware,
    NextCall,
    RpcRequest,
    RpcResponse,
    build_chain,
)
from notebooklm._session import Session


def _make_core() -> Session:
    """Build a ``Session`` instance without opening an HTTP client.

    ``Session.__init__`` is event-loop-agnostic, so we can construct an
    instance in synchronous test setup. The transport is then swapped in
    each test for a :class:`FakeAuthedPost` so no real HTTP call fires.
    """
    auth = MagicMock()
    auth.storage_path = None
    auth.authuser = 0
    auth.account_email = None
    auth.csrf_token = "csrf-token"
    auth.session_id = "session-id"
    return Session(auth=auth)


def _swap_transport(core: Session, fake: FakeAuthedPost) -> None:
    """Replace ``core._get_authed_transport`` with a callable returning ``fake``.

    The chain terminal calls ``self._get_authed_transport()`` per
    invocation; overriding the bound method on the instance is sufficient
    because Python's attribute lookup finds the instance attribute before
    the class method. This keeps the swap local to the test (no shared
    module-level monkeypatch).
    """
    core._get_authed_transport = lambda: fake  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_empty_chain_routes_perform_authed_post_to_transport() -> None:
    """``Session._perform_authed_post`` flows through the empty chain to transport.

    Covers the first of the two call paths from master plan line 160:
    direct callers of ``Session._perform_authed_post`` (the chat path
    in ``_chat_transport.py:64`` and any first-party caller via
    ``client._core._perform_authed_post``).
    """
    expected_response = httpx.Response(status_code=200, content=b"chain-routed")
    fake = FakeAuthedPost(response=expected_response)
    core = _make_core()
    _swap_transport(core, fake)

    def build_request(snapshot: Any) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://fake/url", b"body", None)

    response = await core._perform_authed_post(
        build_request=build_request,
        log_label="test-log-label",
        disable_internal_retries=False,
    )

    assert response is expected_response
    assert fake.call_count == 1
    call = fake.calls[0]
    assert call["build_request"] is build_request
    assert call["log_label"] == "test-log-label"
    assert call["disable_internal_retries"] is False


@pytest.mark.asyncio
async def test_empty_chain_routes_rpc_executor_path_to_transport() -> None:
    """``RpcExecutor.execute`` → ``_perform_authed_post`` flows through the chain too.

    Covers the second of the two call paths from master plan line 160:
    ``RpcExecutor.execute`` (``_core_rpc.py:275``) calls
    ``self._owner._perform_authed_post(...)`` which is precisely
    :meth:`Session._perform_authed_post`. Routing both paths through
    one seam is the whole point of wiring at ``_perform_authed_post``
    rather than at each call site.

    We exercise the route by calling ``_perform_authed_post`` with the
    keyword shape ``RpcExecutor.execute`` uses (the
    ``log_label=f"RPC {method.name}"`` template at ``_core_rpc.py:277``)
    and asserting the chain leaf hands those exact kwargs to the
    transport. We do NOT spin up a full ``RpcExecutor`` here because that
    pulls in the idempotency registry and encoder fixtures; the seam
    invariant is "the chain receives whatever ``_perform_authed_post``
    receives," which a direct call validates without the extra surface.
    """
    expected_response = httpx.Response(status_code=200, content=b"rpc-path")
    fake = FakeAuthedPost(response=expected_response)
    core = _make_core()
    _swap_transport(core, fake)

    def build_request(snapshot: Any) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://fake/rpc", b"rpc-body", {"X-Goog-AuthUser": "0"})

    response = await core._perform_authed_post(
        build_request=build_request,
        log_label="RPC LIST_NOTEBOOKS",
        disable_internal_retries=True,
    )

    assert response is expected_response
    assert fake.call_count == 1
    call = fake.calls[0]
    assert call["build_request"] is build_request
    assert call["log_label"] == "RPC LIST_NOTEBOOKS"
    # The ``disable_internal_retries`` bool resolved by
    # ``_idempotency.resolve_effective_disable_internal_retries`` upstream
    # propagates through the chain unchanged.
    assert call["disable_internal_retries"] is True


@pytest.mark.asyncio
async def test_chain_terminal_reads_context_keys() -> None:
    """``RpcRequest.context`` carries the three keys the terminal reads.

    Drives the terminal adapter directly with a hand-built ``RpcRequest``
    so we can assert the contract independently of
    :meth:`Session._perform_authed_post`'s context-construction code.
    This is what every middleware PR 12.3–12.8 will rely on when it
    builds a chain over ``[*middlewares, ...]`` and lets the leaf adapt
    the request into a transport call.
    """
    expected_response = httpx.Response(status_code=204, content=b"")
    fake = FakeAuthedPost(response=expected_response)
    core = _make_core()
    _swap_transport(core, fake)

    def build_request(snapshot: Any) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://fake/ctx", b"ctx-body", None)

    request = RpcRequest(
        url="",
        headers={},
        body=b"",
        context={
            "build_request": build_request,
            "log_label": "context-test",
            "disable_internal_retries": False,
        },
    )

    result = await core._authed_post_chain_terminal(request)

    assert isinstance(result, RpcResponse)
    assert result.response is expected_response
    # The ``RpcResponse.context`` propagates the same dict the request
    # carried, so middlewares above the leaf can read additions a deeper
    # link made. The empty chain leaves the dict unchanged.
    assert result.context is request.context
    assert fake.call_count == 1
    assert fake.calls[0]["build_request"] is build_request
    assert fake.calls[0]["log_label"] == "context-test"
    assert fake.calls[0]["disable_internal_retries"] is False


@pytest.mark.asyncio
async def test_chain_terminal_disable_internal_retries_defaults_false() -> None:
    """When ``context`` omits ``disable_internal_retries`` the leaf reads ``False``.

    ``_perform_authed_post`` always populates the key, but the leaf
    defends against a missing entry so middlewares that build a request
    without the key (e.g. a future ``Session.transport_post`` raw-POST
    seam, master plan section 3) cannot trip the leaf with a
    ``KeyError``.
    """
    fake = FakeAuthedPost()
    core = _make_core()
    _swap_transport(core, fake)

    def build_request(snapshot: Any) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://fake/no-retry-flag", b"", None)

    request = RpcRequest(
        url="",
        headers={},
        body=b"",
        context={
            "build_request": build_request,
            "log_label": "default-flag",
        },
    )

    await core._authed_post_chain_terminal(request)

    assert fake.call_count == 1
    assert fake.calls[0]["disable_internal_retries"] is False


@pytest.mark.asyncio
async def test_chain_seeded_with_final_adr_009_ordering() -> None:
    """``Session.__init__`` seeds the chain with the FINAL ADR-009 ordering.

    PR 12.3 landed ``TracingMiddleware`` at the innermost position; PR 12.4
    prepended ``MetricsMiddleware``; PR 12.5 prepended ``DrainMiddleware``
    outermost; PR 12.6 inserted ``ErrorInjectionMiddleware`` between
    Metrics and Tracing; PR 12.7 inserted ``RetryMiddleware`` between
    Metrics and ErrorInjection; PR 12.8 inserted ``AuthRefreshMiddleware``
    between Retry and ErrorInjection; PR 12.9 inserted
    ``SemaphoreMiddleware`` between Metrics and Retry (codex catch — see
    ADR-009 close-out notes). The list now reads the final ADR-009
    ordering
    ``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]``
    (outermost → innermost).

    Order rationale (per ADR-009):
    - Drain outermost — every in-flight call counts toward shutdown wait
    - Metrics outside Semaphore — latency includes queue wait
    - Semaphore outside Retry — retry attempts stay in one slot
    - Retry outside AuthRefresh — orthogonal failure modes
    - AuthRefresh outside ErrorInjection — test-injected 401s exercise refresh
    - ErrorInjection inside Retry — synthetic transient failures trigger retry
    - Tracing innermost — logs actual HTTP attempts including retries

    The list is exposed as ``self._middlewares`` so the cleanup audit can
    verify ordering by inspecting the production attribute directly.
    """
    from notebooklm._middleware_auth_refresh import AuthRefreshMiddleware
    from notebooklm._middleware_drain import DrainMiddleware
    from notebooklm._middleware_error_injection import ErrorInjectionMiddleware
    from notebooklm._middleware_metrics import MetricsMiddleware
    from notebooklm._middleware_retry import RetryMiddleware
    from notebooklm._middleware_semaphore import SemaphoreMiddleware
    from notebooklm._middleware_tracing import TracingMiddleware

    core = _make_core()
    assert len(core._middlewares) == 7
    assert isinstance(core._middlewares[0], DrainMiddleware)
    assert isinstance(core._middlewares[1], MetricsMiddleware)
    assert isinstance(core._middlewares[2], SemaphoreMiddleware)
    assert isinstance(core._middlewares[3], RetryMiddleware)
    assert isinstance(core._middlewares[4], AuthRefreshMiddleware)
    assert isinstance(core._middlewares[5], ErrorInjectionMiddleware)
    assert isinstance(core._middlewares[6], TracingMiddleware)


@pytest.mark.asyncio
async def test_chain_with_test_middleware_observes_request_and_response() -> None:
    """A test middleware can observe the request and response around the leaf.

    Demonstrates the contract every middleware PR 12.3–12.8 will rely on:
    insert a middleware into the chain, drive a request through, and
    assert the middleware saw both the inbound request and the outbound
    response. This is the wire-up smoke test for middleware extractions.

    Builds the chain locally (rather than mutating ``core._middlewares``
    in-place) because production code does not yet support hot-swapping
    the chain — that's a PR 12.3 concern when ``TracingMiddleware`` lands.
    """
    observed: dict[str, Any] = {}

    async def observer(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        observed["request"] = request
        response = await next_call(request)
        observed["response"] = response
        return response

    expected_response = httpx.Response(status_code=200, content=b"observed")
    fake = FakeAuthedPost(response=expected_response)
    core = _make_core()
    _swap_transport(core, fake)

    # Build a chain with one observer middleware around the production
    # terminal. The production chain stays empty; this is a per-test
    # composition that validates the leaf's contract against
    # ``build_chain`` rather than ``Session.__init__``.
    chain: NextCall = build_chain([observer], core._authed_post_chain_terminal)

    def build_request(snapshot: Any) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://fake/observe", b"", None)

    request = RpcRequest(
        url="",
        headers={},
        body=b"",
        context={
            "build_request": build_request,
            "log_label": "observer-test",
            "disable_internal_retries": False,
        },
    )

    result = await chain(request)

    assert observed["request"] is request
    assert isinstance(observed["response"], RpcResponse)
    assert observed["response"].response is expected_response
    assert result.response is expected_response
    assert fake.call_count == 1


@pytest.mark.asyncio
async def test_perform_authed_post_works_on_new_built_fixture() -> None:
    """A ``__new__``-built fixture can still call ``_perform_authed_post``.

    Mirrors the ``_ensure_observability_state`` /
    ``_ensure_auth_coord`` / ``_ensure_lifecycle`` backfill pattern: a
    test that constructs ``Session.__new__(Session)`` (skipping
    ``__init__``) must still be able to drive the authed-POST path.
    The :meth:`Session._ensure_authed_post_chain` helper backfills
    ``_middlewares`` and ``_authed_post_chain`` lazily so the first call
    succeeds. This regression guard catches the path before any
    middleware PR (12.3+) inadvertently moves the chain build out of
    the backfill.
    """
    core = Session.__new__(Session)
    fake = FakeAuthedPost(response=httpx.Response(status_code=200, content=b"new"))
    _swap_transport(core, fake)

    def build_request(snapshot: Any) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://fake/new", b"", None)

    response = await core._perform_authed_post(
        build_request=build_request,
        log_label="new-fixture",
        disable_internal_retries=False,
    )

    assert response.status_code == 200
    assert fake.call_count == 1


def test_build_chain_empty_returns_terminal_unchanged() -> None:
    """:func:`build_chain` returns the terminal unchanged when ``middlewares`` is empty.

    Pins the contract that ``_middleware.build_chain([], terminal) is terminal``
    so :meth:`Session.__init__`'s ``self._authed_post_chain is
    self._authed_post_chain_terminal`` invariant from
    :func:`test_chain_is_empty_by_default` does not silently flip if
    ``build_chain``'s identity behavior changes. Synchronous test —
    no event-loop overhead.
    """

    async def terminal(request: RpcRequest) -> RpcResponse:
        return RpcResponse(
            response=httpx.Response(status_code=200, content=b""),
            context=request.context,
        )

    middlewares: list[Middleware] = []
    chain = build_chain(middlewares, terminal)
    assert chain is terminal


def test_perform_authed_post_signature_unchanged() -> None:
    """The keyword-only signature of ``_perform_authed_post`` is unchanged.

    Many call sites pass the three kwargs by name
    (``_core_rpc.py:275``, ``_chat_transport.py:64``, integration tests).
    The chain wiring inside the body must NOT change the public-ish
    signature; this guard catches an accidental rename.
    """
    import inspect

    sig = inspect.signature(Session._perform_authed_post)
    params = sig.parameters
    assert "build_request" in params
    assert "log_label" in params
    assert "disable_internal_retries" in params
    # All three are keyword-only — the ``*`` separator in the production
    # signature is what makes this true.
    assert params["build_request"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["log_label"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["disable_internal_retries"].kind is inspect.Parameter.KEYWORD_ONLY
