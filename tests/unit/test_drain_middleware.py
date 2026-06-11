"""Unit tests for :class:`DrainMiddleware` (Tier-12 PR 12.5).

Pins the contract documented in ``src/notebooklm/_middleware/drain.py``
and ADR-0009 §"Chain ordering":

- Pass-through identity: the middleware brackets ``next_call`` but does
  not transform request or response.
- Counter accounting: ``begin_transport_post`` increments the
  ``TransportDrainTracker`` in-flight counter before ``next_call``;
  ``finish_transport_post`` decrements it after. Net effect at steady
  state is zero, but the counter rises by 1 during ``next_call``.
- Failure path: if ``next_call`` raises, ``finish_transport_post``
  STILL fires (via ``try/finally``) so the counter never orphans.
- Drain admission: with the tracker in draining mode and the current
  task at depth 0, ``begin_transport_post`` raises ``RuntimeError``
  carrying the ``log_label``. The exception propagates out of the
  middleware (no swallow).
- ``log_label`` propagation: the value comes from
  ``request.context["log_label"]``; a missing key falls back to a
  defensive sentinel.

The tests use the canonical chain fixtures + a real
``TransportDrainTracker`` instance (not a mock) so the begin/finish
condition-variable + per-task depth bookkeeping is exercised end-to-end.
"""

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
from notebooklm._middleware.drain import DrainMiddleware
from notebooklm._transport_drain import TransportDrainTracker

# The ``tests/`` package chain is complete; ``tests._fixtures.chain`` is the
# fully-qualified import path documented in ``tests/_fixtures/__init__.py``.
from tests._fixtures.chain import make_request


def _terminal_returning(response: httpx.Response) -> NextCall:
    """Build a chain-terminal coroutine that wraps ``response``.

    The chain leaf normally adapts to ``Kernel.post``; this helper
    short-circuits that adaptation so tests can exercise the middleware
    without booting a real transport. ``request.context`` is propagated to the
    ``RpcResponse`` so any outer middleware (or test assertion) sees the
    same context object.
    """

    async def terminal(request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=response, context=request.context)

    return terminal


@pytest.fixture
def tracker() -> TransportDrainTracker:
    """Fresh tracker per test — counters start at zero, not draining."""
    return TransportDrainTracker()


@pytest.mark.asyncio
async def test_brackets_next_call_with_begin_finish(
    tracker: TransportDrainTracker,
) -> None:
    """Counter rises during ``next_call`` and returns to zero after.

    Verifies the in-flight bookkeeping covers exactly the chain inner
    leg: the count is 0 before chain entry, 1 while the terminal is
    awaiting, and 0 after the chain returns. Capturing inside the
    terminal makes the during-call value observable.
    """
    seen_in_flight: list[int] = []

    async def observing_terminal(request: RpcRequest) -> RpcResponse:
        seen_in_flight.append(tracker._in_flight_posts)
        return RpcResponse(
            response=httpx.Response(status_code=200, content=b"ok"),
            context=request.context,
        )

    middleware = DrainMiddleware(tracker)
    chain = build_chain([middleware], observing_terminal)
    request = make_request(context={"log_label": "RPC LIST_NOTEBOOKS"})

    assert tracker._in_flight_posts == 0
    await chain(request)
    assert seen_in_flight == [1]
    assert tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_finish_fires_on_exception(
    tracker: TransportDrainTracker,
) -> None:
    """If ``next_call`` raises, the counter still decrements via ``finally``.

    Pins the load-bearing invariant that orphaning a token would stall
    ``drain()`` forever — the ``try/finally`` in DrainMiddleware exists
    precisely to make ``finish_transport_post`` fire on the failure
    path. The exception itself propagates unchanged (not swallowed).
    """
    boom = RuntimeError("transport blew up")

    async def failing_terminal(_request: RpcRequest) -> RpcResponse:
        raise boom

    middleware = DrainMiddleware(tracker)
    chain = build_chain([middleware], failing_terminal)
    request = make_request(context={"log_label": "RPC LIST_NOTEBOOKS"})

    with pytest.raises(RuntimeError) as exc_info:
        await chain(request)

    assert exc_info.value is boom
    assert tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_draining_top_level_request_is_rejected(
    tracker: TransportDrainTracker,
) -> None:
    """A drained tracker raises ``RuntimeError`` on top-level admission.

    Flips ``tracker._draining`` via the public ``drain()`` API (with a
    zero-in-flight short-circuit so the call returns immediately) and
    asserts that the next chain invocation surfaces the standard
    "client is draining" ``RuntimeError`` from
    ``begin_transport_post``. The terminal must NOT be reached.
    """
    terminal_ran = False

    async def must_not_run(_request: RpcRequest) -> RpcResponse:
        nonlocal terminal_ran
        terminal_ran = True
        return RpcResponse(
            response=httpx.Response(status_code=200, content=b""),
            context={},
        )

    middleware = DrainMiddleware(tracker)
    chain = build_chain([middleware], must_not_run)

    # ``drain(timeout=0)`` with no in-flight posts short-circuits the
    # condition wait and just flips ``_draining = True``. (The
    # ``assert_bound_loop`` short-circuit is a no-op when bound_loop is
    # None, which it is for a bare-constructed tracker.)
    await tracker.drain(timeout=0)

    request = make_request(context={"log_label": "RPC LIST_NOTEBOOKS"})
    with pytest.raises(RuntimeError, match="draining") as exc_info:
        await chain(request)

    assert "RPC LIST_NOTEBOOKS" in str(exc_info.value)
    assert terminal_ran is False
    assert tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_nested_call_admitted_after_drain_starts(
    tracker: TransportDrainTracker,
) -> None:
    """An admitted operation's nested chain call passes through after drain.

    The drain-admission semantic — load-bearing because source-upload
    operations issue inner RPCs from within their admitted outer scope
    — keys on ``asyncio.current_task()``'s depth, not on a new-call
    sentinel. If the task already has depth > 0 from an admitted outer
    begin, a NEW chain invocation issued by that task DURING drain
    must still admit. Tests this path by:

    1. Manually admitting one operation (depth=1 on the current task).
    2. Flipping the tracker into draining mode.
    3. Driving the chain — which calls ``begin_transport_post`` again,
       lifting depth to 2 — and asserting the terminal runs.
    4. Decrementing back to depth=0 to leave the tracker clean.
    """
    terminal_ran = False

    async def inner_terminal(request: RpcRequest) -> RpcResponse:
        nonlocal terminal_ran
        terminal_ran = True
        return RpcResponse(
            response=httpx.Response(status_code=200, content=b"ok"),
            context=request.context,
        )

    middleware = DrainMiddleware(tracker)
    chain = build_chain([middleware], inner_terminal)

    # Outer admission lifts the current task's depth to 1.
    outer_token = await tracker.begin_transport_post("outer.upload")

    # Flip into draining mode without releasing the outer slot.
    async with tracker.get_drain_condition():
        tracker._draining = True

    try:
        await chain(make_request(context={"log_label": "RPC GET_NOTEBOOK"}))
    finally:
        # Restore the outer slot regardless of test outcome.
        await tracker.finish_transport_post(outer_token)

    assert terminal_ran is True
    assert tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_missing_log_label_falls_back_to_sentinel(
    tracker: TransportDrainTracker,
) -> None:
    """A request with no ``log_label`` in context admits with a sentinel label.

    ``RuntimeTransport.perform_authed_post`` always populates ``log_label``,
    so this case only arises for ``__new__``-built fixtures driving the
    chain raw. The middleware should still admit + count rather than
    raising ``KeyError`` — pinning this guards against a regression
    that would surface as a flaky test-fixture failure under a
    seemingly-unrelated refactor.
    """
    middleware = DrainMiddleware(tracker)
    chain = build_chain([middleware], _terminal_returning(httpx.Response(200, content=b"")))

    request = make_request(context={})  # explicitly no log_label
    await chain(request)

    assert tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_pass_through_does_not_mutate_request(
    tracker: TransportDrainTracker,
) -> None:
    """Middleware does not mutate the ``RpcRequest`` instance.

    ``RpcRequest`` is a frozen dataclass so attribute mutation raises
    ``FrozenInstanceError``, but ``context`` is mutable by reference.
    DrainMiddleware reads ``context["log_label"]`` and must not write
    back. Pin by snapshotting context keys before the call and asserting
    equality after.
    """
    observed: dict[str, object] = {}

    async def terminal(request: RpcRequest) -> RpcResponse:
        observed["instance"] = request
        observed["context_keys"] = set(request.context)
        return RpcResponse(
            response=httpx.Response(200, content=b""),
            context=request.context,
        )

    middleware = DrainMiddleware(tracker)
    chain = build_chain([middleware], terminal)

    context_before = {
        "log_label": "RPC LIST_NOTEBOOKS",
        "rpc_method": "LIST_NOTEBOOKS",
        "disable_internal_retries": False,
    }
    request = make_request(context=dict(context_before))  # defensive copy
    await chain(request)

    assert observed["instance"] is request
    assert observed["context_keys"] == set(context_before)
    # No new keys leaked back into the request context.
    assert set(request.context) == set(context_before)


@pytest.mark.asyncio
async def test_drain_after_chain_finishes_does_not_block(
    tracker: TransportDrainTracker,
) -> None:
    """Once the chain returns, ``drain()`` resolves immediately.

    End-to-end smoke: run a chain call, then call ``drain()`` with a
    finite timeout. If the middleware correctly fires
    ``finish_transport_post`` after the terminal returns, the counter
    is back at zero and drain's ``wait_for(in_flight==0)`` short-
    circuits without blocking. A timeout here would indicate the
    counter was orphaned.
    """
    middleware = DrainMiddleware(tracker)
    chain = build_chain([middleware], _terminal_returning(httpx.Response(200, content=b"")))

    await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    # If the counter is orphaned, this ``wait_for`` raises TimeoutError.
    await asyncio.wait_for(tracker.drain(timeout=1.0), timeout=1.5)
    assert tracker._in_flight_posts == 0
