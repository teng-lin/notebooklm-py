"""Direct characterization tests for :class:`SemaphoreMiddleware`.

B0 moved the live RPC gate to ``CallSupervisor``; the module docstring says
this helper stays "for direct middleware compatibility tests", and until now
there were none. These pin what a caller composing it directly still gets:

- the slot is held for exactly the inner leg (``next_call``), including any
  retries a middleware nested inside it performs — the "one slot per logical
  RPC" backpressure contract;
- the slot is released when ``next_call`` raises, so a failing call cannot
  leak capacity;
- the measured queue wait is written to the shared request context under the
  documented key, so a host can forward it to ``ClientMetrics`` without the
  middleware knowing metric names.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

from notebooklm._web.transport.middleware.core import (
    NextCall,
    RpcRequest,
    RpcResponse,
    build_chain,
)
from notebooklm._web.transport.middleware.semaphore import (
    RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS,
    RPC_QUEUE_WAIT_CONTEXT_KEY,
    SemaphoreMiddleware,
)
from tests._fixtures.chain import make_request

pytestmark = pytest.mark.asyncio


def _terminal(on_call: NextCall | None = None) -> NextCall:
    async def terminal(request: RpcRequest) -> RpcResponse:
        if on_call is not None:
            await on_call(request)
        return RpcResponse(
            response=httpx.Response(status_code=200, content=b"ok"),
            context=request.context,
        )

    return terminal


async def test_the_legacy_key_alias_still_points_at_the_canonical_context_key() -> None:
    assert RPC_QUEUE_WAIT_CONTEXT_KEY == RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS


async def test_a_no_op_gate_passes_the_response_through_and_records_a_wait() -> None:
    chain = build_chain(
        [SemaphoreMiddleware(contextlib.nullcontext)],
        _terminal(),
    )
    request = make_request()

    result = await chain(request)

    assert result.response.status_code == 200
    recorded = request.context[RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS]
    assert isinstance(recorded, float)
    assert recorded >= 0.0


async def test_the_slot_is_held_for_the_whole_inner_leg() -> None:
    """A one-slot gate serializes two concurrent calls end to end."""
    semaphore = asyncio.Semaphore(1)
    concurrent = 0
    peak = 0
    release = asyncio.Event()

    async def observing(_request: RpcRequest) -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await release.wait()
        concurrent -= 1

    chain = build_chain([SemaphoreMiddleware(lambda: semaphore)], _terminal(observing))
    first = asyncio.create_task(chain(make_request()))
    second = asyncio.create_task(chain(make_request()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert peak == 1
    assert semaphore.locked()

    release.set()
    await asyncio.gather(first, second)
    assert peak == 1
    assert not semaphore.locked()


async def test_retries_inside_the_gate_do_not_release_the_slot() -> None:
    """A nested retry re-enters the inner chain without re-queueing."""
    semaphore = asyncio.Semaphore(1)
    attempts = 0
    locked_during_attempts: list[bool] = []

    async def retrying(request: RpcRequest, next_call: NextCall) -> RpcResponse:
        nonlocal attempts
        for _ in range(3):
            attempts += 1
            locked_during_attempts.append(semaphore.locked())
        return await next_call(request)

    chain = build_chain(
        [SemaphoreMiddleware(lambda: semaphore), retrying],
        _terminal(),
    )

    await chain(make_request())

    assert attempts == 3
    assert locked_during_attempts == [True, True, True]
    assert not semaphore.locked()


async def test_a_failing_call_still_releases_the_slot() -> None:
    semaphore = asyncio.Semaphore(1)

    async def failing(_request: RpcRequest) -> RpcResponse:
        raise RuntimeError("transport blew up")

    chain = build_chain([SemaphoreMiddleware(lambda: semaphore)], failing)

    with pytest.raises(RuntimeError, match="transport blew up"):
        await chain(make_request())

    assert not semaphore.locked()


async def test_the_recorded_wait_reflects_time_spent_queued() -> None:
    semaphore = asyncio.Semaphore(1)
    release = asyncio.Event()
    first_context: dict = {}
    second_context: dict = {}

    async def holding(_request: RpcRequest) -> None:
        await release.wait()

    chain = build_chain([SemaphoreMiddleware(lambda: semaphore)], _terminal(holding))
    first = asyncio.create_task(chain(make_request(context=first_context)))
    await asyncio.sleep(0)
    second = asyncio.create_task(chain(make_request(context=second_context)))
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.gather(first, second)

    # The queued call waited for the holder; the holder did not.
    assert (
        second_context[RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS]
        > first_context[RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS]
    )
