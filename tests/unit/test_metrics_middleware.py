"""Unit tests for :class:`MetricsMiddleware` (Tier-12 PR 12.4).

Pins the contract documented in
``src/notebooklm/_middleware/metrics.py`` and ADR-0009 §"Chain ordering":

- Pass-through identity (the middleware is a pure observer; it must not
  mutate ``RpcRequest`` or transform the ``RpcResponse``).
- On success: increment ``rpc_calls_succeeded`` + ``rpc_latency_seconds_total``
  and ``await metrics.emit_rpc_event`` with a ``status="success"`` event
  carrying the ``rpc_method`` name, the request id from ``_logging``, and
  the elapsed wall-clock duration.
- On failure: increment ``rpc_calls_failed`` + ``rpc_latency_seconds_total``,
  emit a ``status="error"`` event with ``error_type = type(exc).__name__``,
  and re-raise the exact same exception instance.
- Skip emission entirely when ``request.context["rpc_method"]`` is absent
  (chat-side path). This is the regression guard for the pre-PR-12.4
  invariant that chat requests do not show up in RPC counters.
- ``asyncio.CancelledError`` is a :class:`BaseException`, not
  :class:`Exception`; the middleware lets it propagate without any
  metrics side-effects.

The tests use the canonical chain fixtures (``make_request`` + ``build_chain``)
from ``tests/_fixtures/chain.py`` so the substrate matches every other
middleware test in the Tier-12 set.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

# pytest puts ``tests/`` on ``sys.path``; ``_fixtures.chain`` is the
# canonical import path documented in ``tests/_fixtures/__init__.py``.
from _fixtures.chain import make_request
from notebooklm._client_metrics import ClientMetrics
from notebooklm._logging import get_request_id, reset_request_id, set_request_id
from notebooklm._middleware.core import (
    NextCall,
    RpcRequest,
    RpcResponse,
    build_chain,
)
from notebooklm._middleware.metrics import MetricsMiddleware
from notebooklm._types.common import RpcTelemetryEvent


def _make_terminal_returning(response: httpx.Response) -> NextCall:
    """Build a terminal-shaped callable that returns ``RpcResponse(response)``.

    The chain leaf normally returns the ``httpx.Response`` from
    ``Kernel.post``; this helper short-circuits that step so tests can
    drive the chain without booting a real transport. ``request.context``
    is propagated to the response so any middleware above the leaf
    observes the same context object.
    """

    async def terminal(request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=response, context=request.context)

    return terminal


@pytest.fixture
def metrics() -> ClientMetrics:
    """Fresh ``ClientMetrics`` per test — counters start at zero."""
    return ClientMetrics(on_rpc_event=None)


@pytest.mark.asyncio
async def test_success_increments_counters_and_emits_event(
    metrics: ClientMetrics,
) -> None:
    """Happy path: counters bump and event fires with status="success".

    Verifies the four observable side-effects on success: (1) the
    ``rpc_calls_succeeded`` counter increments by exactly 1, (2) the
    ``rpc_latency_seconds_total`` accumulator grows by a non-negative
    amount, (3) ``emit_rpc_event`` fires exactly once with the expected
    field values, and (4) the response forwarded to the caller is
    identity-equal to what the terminal returned.
    """
    captured: list[RpcTelemetryEvent] = []

    async def capture(event: RpcTelemetryEvent) -> None:
        captured.append(event)

    metrics._on_rpc_event = capture

    expected_response = httpx.Response(status_code=200, content=b"ok")
    middleware = MetricsMiddleware(metrics)
    chain = build_chain([middleware], _make_terminal_returning(expected_response))

    request = make_request(
        context={"log_label": "RPC LIST_NOTEBOOKS", "rpc_method": "LIST_NOTEBOOKS"}
    )
    result = await chain(request)

    assert result.response is expected_response
    snap = metrics._metrics
    assert snap.rpc_calls_succeeded == 1
    assert snap.rpc_calls_failed == 0
    assert snap.rpc_latency_seconds_total >= 0.0

    assert len(captured) == 1
    event = captured[0]
    assert event.method == "LIST_NOTEBOOKS"
    assert event.status == "success"
    assert event.elapsed_seconds >= 0.0
    assert event.error_type is None


@pytest.mark.asyncio
async def test_failure_increments_counters_emits_error_and_reraises(
    metrics: ClientMetrics,
) -> None:
    """If ``next_call`` raises, emit ``status="error"`` and re-raise.

    Pins three invariants: (1) the exact exception instance propagates
    (``is``-equal — the middleware never wraps or swallows), (2) the
    ``error_type`` event field carries the bare class name, and
    (3) ``rpc_calls_failed`` increments by 1 (NOT ``rpc_calls_succeeded``).
    """
    boom = RuntimeError("transport blew up")

    async def failing_terminal(_request: RpcRequest) -> RpcResponse:
        raise boom

    captured: list[RpcTelemetryEvent] = []

    async def capture(event: RpcTelemetryEvent) -> None:
        captured.append(event)

    metrics._on_rpc_event = capture

    middleware = MetricsMiddleware(metrics)
    chain = build_chain([middleware], failing_terminal)
    request = make_request(
        context={"log_label": "RPC LIST_NOTEBOOKS", "rpc_method": "LIST_NOTEBOOKS"}
    )

    with pytest.raises(RuntimeError) as exc_info:
        await chain(request)

    assert exc_info.value is boom

    snap = metrics._metrics
    assert snap.rpc_calls_succeeded == 0
    assert snap.rpc_calls_failed == 1
    assert snap.rpc_latency_seconds_total >= 0.0

    assert len(captured) == 1
    event = captured[0]
    assert event.method == "LIST_NOTEBOOKS"
    assert event.status == "error"
    assert event.error_type == "RuntimeError"
    assert event.elapsed_seconds >= 0.0


@pytest.mark.asyncio
async def test_skips_emit_when_rpc_method_absent(
    metrics: ClientMetrics,
) -> None:
    """Chat-side path (``rpc_method`` absent) is a pure pass-through.

    Pins the regression guard for the pre-PR-12.4 invariant: requests
    flowing through the chain WITHOUT ``rpc_method`` in context must not
    appear in the RPC counters or telemetry stream. The chat streaming
    path (``_chat.transport.send_authed_post``) is the production caller
    that exercises this branch — chat requests have never been counted
    as RPCs and continue not to be.
    """
    captured: list[RpcTelemetryEvent] = []

    async def capture(event: RpcTelemetryEvent) -> None:
        captured.append(event)

    metrics._on_rpc_event = capture

    expected_response = httpx.Response(status_code=200, content=b"chat-ok")
    middleware = MetricsMiddleware(metrics)
    chain = build_chain([middleware], _make_terminal_returning(expected_response))

    # log_label present, rpc_method ABSENT — exact shape produced by
    # ``Session._perform_authed_post`` for the chat path (which
    # defaults ``rpc_method=None``).
    request = make_request(context={"log_label": "chat.ask"})
    result = await chain(request)

    assert result.response is expected_response
    snap = metrics._metrics
    assert snap.rpc_calls_succeeded == 0
    assert snap.rpc_calls_failed == 0
    assert snap.rpc_latency_seconds_total == 0.0
    assert captured == []


@pytest.mark.asyncio
async def test_skips_emit_when_rpc_method_is_none(
    metrics: ClientMetrics,
) -> None:
    """Explicit ``rpc_method=None`` in context is treated the same as absent.

    ``Session._perform_authed_post`` populates the context with
    ``"rpc_method": rpc_method`` where the kwarg defaults to ``None``.
    The middleware's ``context.get("rpc_method")`` returns ``None`` in
    both cases, but pin the explicit-None case in a separate test so a
    future refactor that changes the population logic (e.g. omitting the
    key entirely when ``None``) doesn't silently change semantics.
    """
    captured: list[RpcTelemetryEvent] = []

    async def capture(event: RpcTelemetryEvent) -> None:
        captured.append(event)

    metrics._on_rpc_event = capture

    expected_response = httpx.Response(status_code=200, content=b"ok")
    middleware = MetricsMiddleware(metrics)
    chain = build_chain([middleware], _make_terminal_returning(expected_response))

    request = make_request(context={"log_label": "chat.ask", "rpc_method": None})
    await chain(request)

    assert metrics._metrics.rpc_calls_succeeded == 0
    assert metrics._metrics.rpc_calls_failed == 0
    assert captured == []


@pytest.mark.asyncio
async def test_cancelled_error_bypasses_all_metrics(
    metrics: ClientMetrics,
) -> None:
    """``asyncio.CancelledError`` propagates without touching metrics state.

    ``CancelledError`` is a :class:`BaseException`, not
    :class:`Exception`; the middleware's ``except Exception`` clause is
    deliberately narrow so cooperative-cancellation signals (also
    ``KeyboardInterrupt``, ``SystemExit``) skip the metrics path
    entirely. Pinning this in a test guards against a future
    widening-to-``BaseException`` regression that would inflate the
    failed-call counter on every benign task cancellation.
    """
    captured: list[RpcTelemetryEvent] = []

    async def capture(event: RpcTelemetryEvent) -> None:
        captured.append(event)

    metrics._on_rpc_event = capture

    async def cancelling_terminal(_request: RpcRequest) -> RpcResponse:
        raise asyncio.CancelledError()

    middleware = MetricsMiddleware(metrics)
    chain = build_chain([middleware], cancelling_terminal)
    request = make_request(
        context={"log_label": "RPC LIST_NOTEBOOKS", "rpc_method": "LIST_NOTEBOOKS"}
    )

    with pytest.raises(asyncio.CancelledError):
        await chain(request)

    snap = metrics._metrics
    assert snap.rpc_calls_failed == 0
    assert snap.rpc_calls_succeeded == 0
    assert snap.rpc_latency_seconds_total == 0.0
    assert captured == []


@pytest.mark.asyncio
async def test_event_carries_current_request_id(
    metrics: ClientMetrics,
) -> None:
    """Event ``request_id`` reflects the active ``contextvar`` at emit time.

    ``RpcExecutor.rpc_call`` mints (or inherits) a request
    id via ``set_request_id()`` BEFORE invoking the chain, and the
    middleware's call to ``get_request_id()`` reads that contextvar. Pin
    the propagation by setting the id explicitly in test scope and
    asserting it appears on the event.
    """
    captured: list[RpcTelemetryEvent] = []

    async def capture(event: RpcTelemetryEvent) -> None:
        captured.append(event)

    metrics._on_rpc_event = capture

    expected_response = httpx.Response(status_code=200, content=b"ok")
    middleware = MetricsMiddleware(metrics)
    chain = build_chain([middleware], _make_terminal_returning(expected_response))

    request = make_request(
        context={"log_label": "RPC LIST_NOTEBOOKS", "rpc_method": "LIST_NOTEBOOKS"}
    )
    token = set_request_id("test-req-id-7f2a")
    try:
        assert get_request_id() == "test-req-id-7f2a"
        await chain(request)
    finally:
        # Restore the prior reqid context. ``ContextVar`` tokens are not
        # cleared when the function frame exits — they must be explicitly
        # ``reset()``-ed (see ``ContextVar.reset`` docs). pytest-asyncio
        # gives each async test its own task + ``copy_context()`` snapshot,
        # so a leak here usually doesn't affect sibling tests in practice,
        # but the disciplined cleanup is to reset the token we minted.
        reset_request_id(token)

    assert len(captured) == 1
    assert captured[0].request_id == "test-req-id-7f2a"


@pytest.mark.asyncio
async def test_no_callback_still_increments_counters(
    metrics: ClientMetrics,
) -> None:
    """When ``on_rpc_event`` is ``None``, counters still increment.

    Pins the contract that the counter side of the middleware is
    independent of the callback side — applications that opt out of the
    ``on_rpc_event`` channel still see ``metrics_snapshot()`` track
    RPC volume. ``ClientMetrics.emit_rpc_event`` no-ops when
    ``_on_rpc_event is None``; the increment runs unconditionally.
    """
    # No on_rpc_event registered; the fixture default is None.
    assert metrics._on_rpc_event is None

    expected_response = httpx.Response(status_code=200, content=b"ok")
    middleware = MetricsMiddleware(metrics)
    chain = build_chain([middleware], _make_terminal_returning(expected_response))

    request = make_request(
        context={"log_label": "RPC LIST_NOTEBOOKS", "rpc_method": "LIST_NOTEBOOKS"}
    )
    await chain(request)

    snap = metrics._metrics
    assert snap.rpc_calls_succeeded == 1
    assert snap.rpc_latency_seconds_total >= 0.0


@pytest.mark.asyncio
async def test_pass_through_does_not_mutate_request(
    metrics: ClientMetrics,
) -> None:
    """Middleware does not mutate the ``RpcRequest`` instance it receives.

    ``RpcRequest`` is a frozen dataclass so attribute mutation raises
    ``FrozenInstanceError``, but the ``context`` dict is mutable by
    reference. The middleware reads ``context.get("rpc_method")`` and
    must not write back. Pin this by snapshotting context keys before
    the call and asserting equality after.
    """
    observed_request: dict[str, Any] = {}

    async def terminal(request: RpcRequest) -> RpcResponse:
        observed_request["instance"] = request
        observed_request["context_keys"] = set(request.context)
        return RpcResponse(
            response=httpx.Response(status_code=200, content=b""),
            context=request.context,
        )

    middleware = MetricsMiddleware(metrics)
    chain = build_chain([middleware], terminal)

    context_before = {
        "log_label": "RPC LIST_NOTEBOOKS",
        "rpc_method": "LIST_NOTEBOOKS",
        "disable_internal_retries": False,
    }
    request = make_request(context=dict(context_before))  # defensive copy
    await chain(request)

    assert observed_request["instance"] is request
    assert observed_request["context_keys"] == set(context_before)
    # No new keys leaked back into the request context.
    assert set(request.context) == set(context_before)
