"""Integration tests for the authed-post middleware chain.

:func:`notebooklm._middleware.core.build_chain` is wired by
:func:`compose_client_internals` against the chain leaf on
:class:`MiddlewareChainHost`
(:meth:`MiddlewareChainHost._authed_post_chain_terminal`), which
consumes the populated ``RpcRequest.url`` / ``headers`` / ``body``
envelope and delegates directly to ``Kernel.post`` — the transport
seam under both :meth:`RuntimeTransport.perform_authed_post` AND
``RpcExecutor._execute_once``. The ``NotebookLMClient._perform_authed_post``
compatibility forward was deleted in Wave 11c of session-decoupling;
tests now drive the canonical collaborator method directly.

These tests verify the wiring contract from
ADR-0009 §"RpcRequest.context keys":

1. Both call paths (``RuntimeTransport.perform_authed_post`` directly
   and the ``RpcExecutor._execute_once`` keyword shape) flow through
   the chain terminal to the transport.
2. ``RpcRequest.context`` carries ``build_request`` / ``log_label`` /
   ``disable_internal_retries`` for retry/rebuild metadata while the terminal
   reads the envelope itself.
3. The leaf returns an :class:`RpcResponse` wrapping the
   :class:`httpx.Response` from the transport.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from notebooklm._middleware.core import (
    Middleware,
    NextCall,
    RpcRequest,
    RpcResponse,
    build_chain,
)
from notebooklm._transport_errors import TransportServerError
from notebooklm.client import NotebookLMClient
from tests._helpers.client_factory import build_client_shell_for_tests


def _make_core() -> NotebookLMClient:
    """Build a ``NotebookLMClient`` instance without opening an HTTP client.

    ``NotebookLMClient.__init__`` is event-loop-agnostic, so we can construct an
    instance in synchronous test setup. Tests replace ``Kernel.post`` directly
    so no real HTTP call fires.
    """
    auth = MagicMock()
    auth.storage_path = None
    auth.authuser = 0
    auth.account_email = None
    auth.csrf_token = "csrf-token"
    auth.session_id = "session-id"
    return build_client_shell_for_tests(auth=auth)


class FakeKernelPost:
    """Programmable stub for ``Kernel.post``."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.response = response or httpx.Response(status_code=200, content=b"")
        self.calls: list[dict[str, Any]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def post(
        self,
        url: str,
        *,
        headers: Any,
        body: bytes,
        read_timeout: float | None = None,
    ) -> httpx.Response:
        self.calls.append(
            {"url": url, "headers": headers, "body": body, "read_timeout": read_timeout}
        )
        return self.response


def _swap_kernel_post(core: NotebookLMClient, fake: FakeKernelPost) -> None:
    core._collaborators.kernel.post = fake.post  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_chain_routes_perform_authed_post_to_transport() -> None:
    """``RuntimeTransport.perform_authed_post`` flows through the chain.

    Covers direct callers of ``RuntimeTransport.perform_authed_post``:
    the chat path in ``_chat/transport.py:64`` and any first-party
    caller via ``client._composed.transport.perform_authed_post``.
    """
    expected_response = httpx.Response(status_code=200, content=b"chain-routed")
    fake = FakeKernelPost(response=expected_response)
    core = _make_core()
    _swap_kernel_post(core, fake)

    def build_request(snapshot: Any) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://fake/url", b"body", None)

    response = await core._composed.transport.perform_authed_post(
        build_request=build_request,
        log_label="test-log-label",
        disable_internal_retries=False,
    )

    assert response is expected_response
    assert fake.call_count == 1
    call = fake.calls[0]
    assert call["url"] == "https://fake/url"
    assert call["headers"] == {}
    assert call["body"] == b"body"
    assert call.get("read_timeout") is None


@pytest.mark.asyncio
async def test_chain_routes_rpc_executor_path_to_transport() -> None:
    """``RpcExecutor._execute_once`` → ``perform_authed_post`` flows through the chain too.

    ``RpcExecutor._execute_once`` calls
    ``self._transport.perform_authed_post(...)`` (Wave 4 of
    session-decoupling: the executor takes :class:`RuntimeTransport`
    directly instead of reaching through NotebookLMClient). Routing both paths
    through one seam is the whole point of wiring at
    ``perform_authed_post`` rather than at each call site.

    We exercise the route by calling ``perform_authed_post`` with the
    keyword shape ``RpcExecutor._execute_once`` uses (the
    ``log_label=f"RPC {method.name}"`` template)
    and asserting the chain leaf hands those exact kwargs to the
    transport. We do NOT spin up a full ``RpcExecutor`` here because that
    pulls in the idempotency registry and encoder fixtures; the seam
    invariant is "the chain receives whatever ``perform_authed_post``
    receives," which a direct call validates without the extra surface.
    """
    expected_response = httpx.Response(status_code=200, content=b"rpc-path")
    fake = FakeKernelPost(response=expected_response)
    core = _make_core()
    _swap_kernel_post(core, fake)

    def build_request(snapshot: Any) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://fake/rpc", b"rpc-body", {"X-Goog-AuthUser": "0"})

    response = await core._composed.transport.perform_authed_post(
        build_request=build_request,
        log_label="RPC LIST_NOTEBOOKS",
        disable_internal_retries=True,
    )

    assert response is expected_response
    assert fake.call_count == 1
    call = fake.calls[0]
    assert call["url"] == "https://fake/rpc"
    assert call["headers"] == {"X-Goog-AuthUser": "0"}
    assert call["body"] == b"rpc-body"
    assert call.get("read_timeout") is None


@pytest.mark.asyncio
async def test_chain_terminal_reads_context_keys() -> None:
    """``RpcRequest.context`` carries the three keys the terminal reads.

    Drives the terminal adapter directly with a hand-built ``RpcRequest``
    so we can assert the contract independently of
    :meth:`RuntimeTransport.perform_authed_post`'s context-construction code.
    This is what every middleware PR 12.3–12.8 will rely on when it
    builds a chain over ``[*middlewares, ...]`` and lets the leaf adapt
    the request into a transport call.
    """
    expected_response = httpx.Response(status_code=204, content=b"")
    fake = FakeKernelPost(response=expected_response)
    core = _make_core()
    _swap_kernel_post(core, fake)

    request = RpcRequest(
        url="https://fake/ctx",
        headers={"X-Test": "yes"},
        body=b"ctx-body",
        context={
            "log_label": "context-test",
            "disable_internal_retries": False,
        },
    )

    result = await core._composed.chain_host._authed_post_chain_terminal(request)

    assert isinstance(result, RpcResponse)
    assert result.response is expected_response
    # The ``RpcResponse.context`` propagates the same dict the request
    # carried, so middlewares above the leaf can read additions a deeper
    # link made. The terminal adapter leaves the dict unchanged.
    assert result.context is request.context
    assert fake.call_count == 1
    assert fake.calls[0] == {
        "url": "https://fake/ctx",
        "headers": {"X-Test": "yes"},
        "body": b"ctx-body",
        "read_timeout": None,
    }


@pytest.mark.asyncio
async def test_chain_terminal_disable_internal_retries_defaults_false() -> None:
    """When ``context`` omits ``disable_internal_retries`` the leaf reads ``False``.

    ``perform_authed_post`` always populates the key, but the leaf
    defends against a missing entry so middlewares that build a request
    without the key cannot trip the leaf with a ``KeyError``.
    """
    fake = FakeKernelPost()
    core = _make_core()
    _swap_kernel_post(core, fake)

    request = RpcRequest(
        url="https://fake/no-retry-flag",
        headers={},
        body=b"",
        context={
            "log_label": "default-flag",
        },
    )

    await core._composed.chain_host._authed_post_chain_terminal(request)

    assert fake.call_count == 1
    assert fake.calls[0]["url"] == "https://fake/no-retry-flag"


@pytest.mark.asyncio
async def test_chain_terminal_log_label_defaults_for_direct_calls() -> None:
    """Direct terminal calls without context metadata still map errors safely."""
    core = _make_core()

    async def raise_network_error(
        url: str,
        *,
        headers: Any,
        body: bytes,
        read_timeout: float | None = None,
    ) -> httpx.Response:
        request = httpx.Request("POST", url, headers=dict(headers), content=body)
        raise httpx.RequestError("boom", request=request)

    core._collaborators.kernel.post = raise_network_error  # type: ignore[method-assign]
    request = RpcRequest(
        url="https://fake/no-log-label",
        headers={},
        body=b"",
        context={},
    )

    with pytest.raises(TransportServerError, match="<unknown-chain-call> network error"):
        await core._composed.chain_host._authed_post_chain_terminal(request)


@pytest.mark.asyncio
async def test_chain_seeded_with_final_adr_009_ordering() -> None:
    """``NotebookLMClient.__init__`` seeds the chain with the FINAL ADR-0009 ordering.

    PR 12.3 landed ``TracingMiddleware`` at the innermost position; PR 12.4
    prepended ``MetricsMiddleware``; PR 12.5 prepended ``DrainMiddleware``
    outermost; PR 12.6 inserted ``ErrorInjectionMiddleware`` between
    Metrics and Tracing; PR 12.7 inserted ``RetryMiddleware`` between
    Metrics and ErrorInjection; PR 12.8 inserted ``AuthRefreshMiddleware``
    between Retry and ErrorInjection; PR 12.9 inserted
    ``SemaphoreMiddleware`` between Metrics and Retry (codex catch — see
    ADR-0009 close-out notes). The list now reads the final ADR-0009
    ordering
    ``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]``
    (outermost → innermost).

    Order rationale (per ADR-0009):
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
    from notebooklm._middleware.auth_refresh import AuthRefreshMiddleware
    from notebooklm._middleware.drain import DrainMiddleware
    from notebooklm._middleware.error_injection import ErrorInjectionMiddleware
    from notebooklm._middleware.metrics import MetricsMiddleware
    from notebooklm._middleware.retry import RetryMiddleware
    from notebooklm._middleware.semaphore import SemaphoreMiddleware
    from notebooklm._middleware.tracing import TracingMiddleware

    core = _make_core()
    assert len(core._composed.middlewares) == 7
    assert isinstance(core._composed.middlewares[0], DrainMiddleware)
    assert isinstance(core._composed.middlewares[1], MetricsMiddleware)
    assert isinstance(core._composed.middlewares[2], SemaphoreMiddleware)
    assert isinstance(core._composed.middlewares[3], RetryMiddleware)
    assert isinstance(core._composed.middlewares[4], AuthRefreshMiddleware)
    assert isinstance(core._composed.middlewares[5], ErrorInjectionMiddleware)
    assert isinstance(core._composed.middlewares[6], TracingMiddleware)


@pytest.mark.asyncio
async def test_chain_with_test_middleware_observes_request_and_response() -> None:
    """A test middleware can observe the request and response around the leaf.

    Demonstrates the contract every middleware PR 12.3–12.8 will rely on:
    insert a middleware into the chain, drive a request through, and
    assert the middleware saw both the inbound request and the outbound
    response. This is the wire-up smoke test for middleware extractions.

    Builds the chain locally (rather than mutating ``core._composed.middlewares``
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
    fake = FakeKernelPost(response=expected_response)
    core = _make_core()
    _swap_kernel_post(core, fake)

    # Build a chain with one observer middleware around the production
    # terminal. This per-test composition validates the leaf's contract
    # against ``build_chain`` without mutating ``NotebookLMClient.__init__``'s
    # production chain.
    chain: NextCall = build_chain([observer], core._composed.chain_host._authed_post_chain_terminal)

    request = RpcRequest(
        url="https://fake/observe",
        headers={},
        body=b"",
        context={
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
    assert fake.calls[0]["url"] == "https://fake/observe"


@pytest.mark.asyncio
async def test_chain_terminal_forwards_read_timeout_context() -> None:
    """Per-request read timeout context reaches the concrete streaming POST."""
    expected_response = httpx.Response(status_code=200, content=b"read-timeout")
    fake = FakeKernelPost(response=expected_response)
    core = _make_core()
    _swap_kernel_post(core, fake)

    request = RpcRequest(
        url="https://fake/read-timeout",
        headers={},
        body=b"",
        context={
            "log_label": "read-timeout-test",
            "read_timeout": 123.0,
        },
    )

    result = await core._composed.chain_host._authed_post_chain_terminal(request)

    assert result.response is expected_response
    assert fake.calls[0].get("read_timeout") == 123.0


def test_build_chain_empty_returns_terminal_unchanged() -> None:
    """:func:`build_chain` returns the terminal unchanged when ``middlewares`` is empty.

    Pins the contract that ``_middleware.build_chain([], terminal) is terminal``
    so the ``chain_host._authed_post_chain is
    chain_host._authed_post_chain_terminal`` invariant from
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
    """The keyword-only signature of ``perform_authed_post`` is unchanged.

    Many call sites pass the three kwargs by name, including the RPC executor,
    chat transport, and integration tests. The chain wiring inside the body
    must NOT change the public-ish signature; this guard catches an
    accidental rename. The NotebookLMClient-level ``_perform_authed_post`` forward
    was deleted in Wave 11c of session-decoupling; the signature contract
    now lives on the canonical collaborator method
    (``RuntimeTransport.perform_authed_post``).
    """
    import inspect

    from notebooklm._runtime.transport import RuntimeTransport

    sig = inspect.signature(RuntimeTransport.perform_authed_post)
    params = sig.parameters
    assert "build_request" in params
    assert "log_label" in params
    assert "disable_internal_retries" in params
    # All three are keyword-only — the ``*`` separator in the production
    # signature is what makes this true.
    assert params["build_request"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["log_label"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["disable_internal_retries"].kind is inspect.Parameter.KEYWORD_ONLY
