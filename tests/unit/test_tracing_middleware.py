"""Unit tests for :class:`notebooklm._middleware_tracing.TracingMiddleware`.

PR 12.3 of the Tier-12/13 greenfield migration lands ``TracingMiddleware``
as the innermost middleware in the chain (ADR-009 §"Chain ordering"). The
middleware is a pure observer: it logs one "starting" record before
``next_call`` and one "completed"/"failed" record after, without
transforming the request or response.

These tests verify:

1. The middleware satisfies the :class:`Middleware` Protocol shape and
   wires through the chain (smoke test via the shared
   :func:`chain_calls_through_to_terminal` fixture).
2. Two log records are emitted around a successful call: one BEFORE
   ``next_call`` ("starting"), one AFTER ("completed" with
   ``status_code`` and ``duration_ms``).
3. ``rpc_method`` / ``log_label`` are propagated from
   ``request.context`` into the log record's ``extra`` mapping (so
   structured-logging consumers see them as ``LogRecord`` attributes).
4. The response returned by the middleware is the exact instance
   returned by ``next_call`` (identity-equal, no rewrap).
5. The request is unchanged after the middleware runs (frozen dataclass
   enforces this at the type level; this test asserts it at runtime to
   guard against a future maintainer relaxing the frozen invariant).
6. On exception from ``next_call``, the middleware emits a "failed"
   record (with ``duration_ms`` and ``exception_type``) and re-raises
   the original exception unchanged.
7. The middleware does NOT raise ``KeyError`` when ``rpc_method`` is
   absent from ``request.context`` (``RpcExecutor.rpc_call`` populates
   it in Tier 13; PR 12.2's wiring does not).

The tests use stdlib :func:`caplog` to capture log records — no
production logger reconfiguration leaks across tests. The chain is
driven directly via :func:`build_chain` rather than going through
``Session`` so the unit boundary is the middleware itself, not the
full client.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

# pytest puts ``tests/`` on ``sys.path``; ``_fixtures.chain`` is the canonical
# import path documented in ``tests/_fixtures/__init__.py``.
from _fixtures.chain import (
    FakeChainTerminal,
    chain_calls_through_to_terminal,
    make_request,
)
from notebooklm._middleware import (
    Middleware,
    RpcRequest,
    RpcResponse,
    build_chain,
)
from notebooklm._middleware_tracing import TracingMiddleware

_TRACE_LOGGER = "notebooklm.middleware.tracing"


# ---------------------------------------------------------------------------
# Protocol / wire-up.
# ---------------------------------------------------------------------------


def test_tracing_middleware_satisfies_protocol() -> None:
    """``TracingMiddleware`` is assignable into ``Middleware`` (Protocol check).

    Static check at runtime: assignment to a ``Middleware``-typed variable
    is the mypy-visible equivalent of "satisfies the Protocol." If a
    future change adds a positional arg to ``__call__``, this assignment
    fails type-check; the runtime assertion guards against the runtime
    invariant too.
    """
    middleware: Middleware = TracingMiddleware()
    assert middleware is not None


def test_tracing_middleware_calls_through_to_transport() -> None:
    """Chain of ``[TracingMiddleware()]`` reaches the terminal exactly once.

    Uses the shared :func:`chain_calls_through_to_terminal` fixture from
    ``tests/_fixtures/chain.py`` — the canonical wire-up smoke test for
    every middleware PR per ADR-009 §"Per-position rationale" and master
    plan line 105.
    """
    terminal = FakeChainTerminal()
    assert chain_calls_through_to_terminal(terminal, [TracingMiddleware()])
    assert terminal.call_count == 1


# ---------------------------------------------------------------------------
# Logging behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_starting_and_completed_records_on_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two records around a successful call: one "starting", one "completed".

    Verifies the per-attempt visibility requirement from ADR-009 §"Chain
    ordering" (Tracing innermost — "logs every actual HTTP attempt").
    """
    expected_response = httpx.Response(status_code=200, content=b"ok")

    async def terminal(_request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=expected_response, context={})

    chain = build_chain([TracingMiddleware()], terminal)

    with caplog.at_level(logging.DEBUG, logger=_TRACE_LOGGER):
        result = await chain(
            make_request(
                context={"rpc_method": "LIST_NOTEBOOKS", "log_label": "RPC LIST_NOTEBOOKS"}
            )
        )

    assert result.response is expected_response

    records = [r for r in caplog.records if r.name == _TRACE_LOGGER]
    assert len(records) == 2

    starting, completed = records
    assert starting.getMessage() == "rpc starting: RPC LIST_NOTEBOOKS"
    assert starting.rpc_method == "LIST_NOTEBOOKS"
    assert starting.log_label == "RPC LIST_NOTEBOOKS"
    # ``starting`` is emitted BEFORE the call returns, so no status_code /
    # duration_ms yet (LogRecord lacks those attributes — accessing them
    # would raise ``AttributeError`` rather than return ``None``).
    assert not hasattr(starting, "status_code")
    assert not hasattr(starting, "duration_ms")

    assert completed.getMessage() == "rpc completed: RPC LIST_NOTEBOOKS -> 200"
    assert completed.rpc_method == "LIST_NOTEBOOKS"
    assert completed.log_label == "RPC LIST_NOTEBOOKS"
    assert completed.status_code == 200
    assert isinstance(completed.duration_ms, float)
    assert completed.duration_ms >= 0.0


@pytest.mark.asyncio
async def test_starting_record_is_emitted_before_next_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The "starting" record is emitted *before* the terminal runs.

    Validated by recording the log count inside the terminal: at that
    point exactly one record has been emitted (the "starting" one); the
    "completed" record cannot land until ``next_call`` returns.
    """
    log_count_during_terminal: list[int] = []

    async def terminal(_request: RpcRequest) -> RpcResponse:
        log_count_during_terminal.append(sum(1 for r in caplog.records if r.name == _TRACE_LOGGER))
        return RpcResponse(response=httpx.Response(status_code=200, content=b""), context={})

    chain = build_chain([TracingMiddleware()], terminal)

    with caplog.at_level(logging.DEBUG, logger=_TRACE_LOGGER):
        await chain(make_request(context={"log_label": "ordering-check"}))

    assert log_count_during_terminal == [1]


@pytest.mark.asyncio
async def test_rpc_method_absent_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``rpc_method`` missing from context is fine — middleware logs ``None``.

    ``RpcExecutor.rpc_call`` (Tier 13) populates ``context["rpc_method"]``;
    PR 12.2's wiring does not, so the empty-chain path through
    ``Session._perform_authed_post`` only carries ``log_label`` /
    ``build_request`` / ``disable_internal_retries``. The middleware
    must handle this gracefully (``.get()`` returns ``None``, no
    ``KeyError``).
    """

    async def terminal(_request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=httpx.Response(status_code=204, content=b""), context={})

    chain = build_chain([TracingMiddleware()], terminal)

    with caplog.at_level(logging.DEBUG, logger=_TRACE_LOGGER):
        # Context has ``log_label`` but no ``rpc_method``.
        await chain(make_request(context={"log_label": "no-rpc-method"}))

    records = [r for r in caplog.records if r.name == _TRACE_LOGGER]
    assert len(records) == 2
    assert records[0].rpc_method is None
    assert records[1].rpc_method is None
    assert records[1].log_label == "no-rpc-method"
    assert records[1].status_code == 204


@pytest.mark.asyncio
async def test_empty_context_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even with a fully-empty ``context`` dict, both records emit with ``None`` fields.

    Edge case: ``make_request()`` defaults ``context`` to ``{}``. The
    middleware should not require any specific key.
    """

    async def terminal(_request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=httpx.Response(status_code=200, content=b""), context={})

    chain = build_chain([TracingMiddleware()], terminal)

    with caplog.at_level(logging.DEBUG, logger=_TRACE_LOGGER):
        await chain(make_request())

    records = [r for r in caplog.records if r.name == _TRACE_LOGGER]
    assert len(records) == 2
    for record in records:
        assert record.rpc_method is None
        assert record.log_label is None


# ---------------------------------------------------------------------------
# Pass-through invariants.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_response_passthrough_identity() -> None:
    """The middleware returns the exact ``RpcResponse`` produced by ``next_call``.

    Pure observer contract: no rewrap, no replace. Identity equality
    (``is``) catches a future maintainer who accidentally wraps the
    response in a new dataclass; value equality would not.
    """
    sentinel_response = RpcResponse(
        response=httpx.Response(status_code=201, content=b"made"),
        context={"trace_id": "abc-123"},
    )

    async def terminal(_request: RpcRequest) -> RpcResponse:
        return sentinel_response

    chain = build_chain([TracingMiddleware()], terminal)
    result = await chain(make_request())

    assert result is sentinel_response


@pytest.mark.asyncio
async def test_request_is_not_mutated() -> None:
    """The middleware does not mutate ``request`` — frozen dataclass + identity.

    The frozen ``RpcRequest`` dataclass enforces immutability at the
    type level (assigning to a field raises ``FrozenInstanceError``).
    This test asserts at runtime that the terminal receives the *same*
    ``RpcRequest`` instance the middleware was given — no
    ``dataclasses.replace`` happened in between.
    """
    received_requests: list[RpcRequest] = []

    async def terminal(request: RpcRequest) -> RpcResponse:
        received_requests.append(request)
        return RpcResponse(response=httpx.Response(status_code=200, content=b""), context={})

    chain = build_chain([TracingMiddleware()], terminal)
    sent = make_request(context={"log_label": "no-mutation"})
    await chain(sent)

    assert len(received_requests) == 1
    assert received_requests[0] is sent


# ---------------------------------------------------------------------------
# Failure path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_emits_failed_record_and_reraises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If ``next_call`` raises, emit a "failed" record and re-raise unchanged.

    Verifies the never-swallow contract: the propagated exception is the
    exact instance raised by the terminal (``is``-equal), and a
    ``WARNING``-level record with ``exception_type`` and ``duration_ms``
    is emitted before the re-raise.
    """
    boom = RuntimeError("transport-failed")

    async def terminal(_request: RpcRequest) -> RpcResponse:
        raise boom

    chain = build_chain([TracingMiddleware()], terminal)

    with (
        caplog.at_level(logging.DEBUG, logger=_TRACE_LOGGER),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await chain(make_request(context={"log_label": "boom-label"}))

    assert exc_info.value is boom

    records = [r for r in caplog.records if r.name == _TRACE_LOGGER]
    # "starting" + "failed".
    assert len(records) == 2

    failed = records[1]
    assert failed.levelno == logging.WARNING
    assert "rpc failed: boom-label" in failed.getMessage()
    assert failed.log_label == "boom-label"
    assert failed.exception_type == "RuntimeError"
    assert isinstance(failed.duration_ms, float)
    assert failed.duration_ms >= 0.0


@pytest.mark.asyncio
async def test_cancelled_error_bypasses_failed_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``asyncio.CancelledError`` propagates without emitting a "failed" record.

    ``CancelledError`` is a :class:`BaseException` subclass (Python 3.8+), and
    the ``except Exception`` clause in :class:`TracingMiddleware` is
    deliberately narrow: cooperative-cancellation signals
    (``CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit``) are caller-
    initiated unwinds, not RPC failures, so they bypass the failure-trace
    path entirely. Only the "starting" record lands.

    Pinning this in a test guards against a future maintainer widening the
    ``except`` to ``BaseException`` (or adding a bare ``except``), which
    would silently turn benign cancellations into noisy "failed" warnings
    and inflate the duration_ms latency histogram.
    """

    async def terminal(_request: RpcRequest) -> RpcResponse:
        raise asyncio.CancelledError()

    chain = build_chain([TracingMiddleware()], terminal)

    with (
        caplog.at_level(logging.DEBUG, logger=_TRACE_LOGGER),
        pytest.raises(asyncio.CancelledError),
    ):
        await chain(make_request(context={"log_label": "cancel-test"}))

    records = [r for r in caplog.records if r.name == _TRACE_LOGGER]
    assert len(records) == 1
    assert "rpc starting" in records[0].getMessage()
    assert records[0].log_label == "cancel-test"
