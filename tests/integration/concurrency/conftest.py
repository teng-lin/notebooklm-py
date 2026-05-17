"""Fixtures for the Tier 7 concurrency integration harness.

Three fixtures, all explicitly named and class-based — no clever hooks,
no monkeypatching. Phase 2 fix PRs consume these to TDD red-green
against specific Tier 7 bugs (refresh races, semaphore gating,
cancellation propagation, etc.).

Fixtures
--------
``mock_transport_concurrent``
    A class-based ``httpx.AsyncBaseTransport`` that records peak in-flight
    concurrency and supports per-request controllable response timing.
    See ``ConcurrentMockTransport`` for the full method surface.

``barrier_factory``
    Returns a callable ``make_barrier(n)`` that produces a
    ``EventBarrier`` for deterministic interleaving of N coroutines.
    Each barrier is a one-shot synchronization point: every arriver
    awaits ``arrive()``; once N have arrived, all are released.
    Built on ``asyncio.Event`` (not ``asyncio.Barrier``) so behavior is
    identical on Python 3.10 (the project's minimum supported version).

``cancellation_helper``
    Wraps a coroutine in ``asyncio.wait_for`` and emits a structured
    diagnostic on cancellation: which coroutine label, what timeout,
    and the captured traceback. Used by per-fix tests that need to
    distinguish "the bug under test deadlocked" from "the test timed
    out for an unrelated reason."

Non-goals
---------
- NOT a load tester. Peak-inflight assertions are coarse (>= 80 of 100)
  because asyncio task scheduling is not perfectly parallel.
- NOT a property-based generator (no Hypothesis dep added).
- NOT a thread-pool stress harness (no real threads — pure asyncio).
- pytest-xdist + asyncio: each xdist worker has its own event loop, so
  fixture state is never shared across workers. Tests that assert on
  process-global state must mark themselves ``@pytest.mark.xdist_group``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
import pytest

from notebooklm.rpc import RPCMethod

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Default response payload
# ---------------------------------------------------------------------------
# A minimal valid batchexecute response that decodes to ``[]`` for
# ``LIST_NOTEBOOKS``. Reused by ``ConcurrentMockTransport`` whenever the
# response queue is empty so tests don't have to enqueue 100 identical
# responses for a 100-way fan-out.
_DEFAULT_RPC_ID = RPCMethod.LIST_NOTEBOOKS.value


def _default_rpc_response_text(rpc_id: str = _DEFAULT_RPC_ID) -> str:
    """Build a minimal valid batchexecute response that decodes to ``[]``."""
    inner = json.dumps([])
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def install_post_as_stream(
    monkeypatch: pytest.MonkeyPatch | None,
    http_client: Any,
    fake_post: Callable[..., Awaitable[Any]],
) -> None:
    """Adapt legacy fake ``post`` callbacks to the streaming RPC POST API."""

    @asynccontextmanager
    async def fake_stream(method: str, url: str, **kwargs: Any) -> Any:
        response = await fake_post(url, **kwargs)
        if type(response) is httpx.Response:
            yield response
            return

        text = getattr(response, "text", "")
        payload = text.encode("utf-8") if isinstance(text, str) else bytes(text or b"")
        raw_status = getattr(response, "status_code", 200)
        status = raw_status if isinstance(raw_status, int) else 200
        try:
            raw_headers = getattr(response, "headers", None)
        except AttributeError:
            raw_headers = None
        try:
            headers = dict(raw_headers) if raw_headers else None
        except (TypeError, AttributeError):
            headers = None
        yield httpx.Response(
            status_code=status,
            headers=headers,
            content=payload,
            request=httpx.Request("POST", url),
        )

    if monkeypatch is not None:
        monkeypatch.setattr(http_client, "stream", fake_stream)
    else:
        http_client.stream = fake_stream


# ---------------------------------------------------------------------------
# ConcurrentMockTransport
# ---------------------------------------------------------------------------


@dataclass
class _InflightTracker:
    """Plain counter pair. Single-threaded asyncio — no lock needed."""

    current: int = 0
    peak: int = 0

    def enter(self) -> None:
        self.current += 1
        if self.current > self.peak:
            self.peak = self.current

    def exit(self) -> None:
        # Hard assert: a double-`finally` or transport-reuse bug would
        # silently drive `current` negative and quietly invalidate every
        # peak assertion downstream. As a test helper we want loud
        # failure, not silent corruption.
        assert self.current > 0, "exit() called more times than enter()"
        self.current -= 1


class ConcurrentMockTransport(httpx.AsyncBaseTransport):
    """Mock transport that records concurrent in-flight requests.

    Designed for asyncio fan-out tests: every ``handle_async_request``
    increments an in-flight counter, awaits a configurable delay (so
    sibling tasks can pile up at the same await point), then decrements
    and returns a queued (or default) response.

    Methods
    -------
    queue_response(response_or_factory)
        Append a response to the FIFO queue. May be:
          - ``httpx.Response`` instance.
          - Tuple ``(status_code, text)`` for convenience.
          - Callable ``(httpx.Request) -> httpx.Response`` for per-request
            shaping (e.g. echoing the URL, returning an error for one URL).
        When the queue is empty, ``_default_rpc_response_text`` is used.
    set_delay(seconds)
        Set the per-request artificial delay (default ``0.05``s — long
        enough that a 100-way ``asyncio.gather`` reliably stacks all 100
        callers at the await point before any returns).
    get_inflight_count()
        Current in-flight request count.
    get_peak_inflight()
        High-water mark observed since construction (or last ``reset()``).
    request_count()
        Total requests served so far.
    captured_requests()
        Snapshot of every request observed (for assertions on URL,
        headers, body — useful in per-fix PRs).
    reset()
        Clear counters, queued responses, and captured request history.
        The configured ``_delay`` is preserved (a session-scoped consumer
        wants its delay choice to survive reset). Call ``set_delay(...)``
        explicitly if you need to reset the timing too.

    Thread-safety note
    ------------------
    All state mutation happens on the asyncio event loop's single thread.
    No locks are needed. If a future test wires this into a real
    threadpool, add an ``asyncio.Lock`` around the counter mutations.
    """

    # Type alias kept inline so readers don't have to scroll for it.
    _ResponseFactory = Callable[[httpx.Request], httpx.Response]
    _QueuedResponse = httpx.Response | tuple[int, str] | _ResponseFactory

    def __init__(self, *, default_delay: float = 0.05) -> None:
        self._tracker = _InflightTracker()
        # ``deque`` for O(1) FIFO popleft. ``list.pop(0)`` shifts the whole
        # backing array each dequeue; immaterial at ~100 items but the wrong
        # data structure for a queue.
        self._queue: deque[ConcurrentMockTransport._QueuedResponse] = deque()
        self._delay: float = default_delay
        self._captured: list[httpx.Request] = []
        self._request_count: int = 0

    # -- configuration -------------------------------------------------

    def queue_response(self, response_or_factory: _QueuedResponse) -> None:
        """Append a response to the FIFO queue.

        Acceptable shapes documented on the class docstring.
        """
        self._queue.append(response_or_factory)

    def set_delay(self, seconds: float) -> None:
        """Set the artificial per-request delay.

        ``0`` is allowed but defeats the purpose of fan-out tests:
        without a delay, requests complete before the next coroutine
        even enters the transport. Use a small positive value (50ms is
        the default) for fan-out work.
        """
        if seconds < 0:
            raise ValueError(f"delay must be >= 0, got {seconds}")
        self._delay = seconds

    # -- observation ---------------------------------------------------

    def get_inflight_count(self) -> int:
        return self._tracker.current

    def get_peak_inflight(self) -> int:
        return self._tracker.peak

    def request_count(self) -> int:
        return self._request_count

    def captured_requests(self) -> list[httpx.Request]:
        # Defensive copy so callers can iterate without racing future
        # in-flight requests (which would mutate the underlying list
        # via append).
        return list(self._captured)

    def reset(self) -> None:
        self._tracker = _InflightTracker()
        self._queue.clear()
        self._captured.clear()
        self._request_count = 0

    # -- AsyncBaseTransport ABI ---------------------------------------

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._captured.append(request)
        self._request_count += 1
        self._tracker.enter()
        try:
            if self._delay > 0:
                # The yield point that lets sibling coroutines stack up
                # in the in-flight counter. Every concurrent caller hits
                # ``enter()`` synchronously before any reaches this
                # ``await``, so peak-inflight reflects the gather width.
                await asyncio.sleep(self._delay)
            return self._next_response(request)
        finally:
            self._tracker.exit()

    # -- internal ------------------------------------------------------

    def _next_response(self, request: httpx.Request) -> httpx.Response:
        if not self._queue:
            return httpx.Response(200, text=_default_rpc_response_text())
        item = self._queue.popleft()
        if isinstance(item, httpx.Response):
            return item
        if isinstance(item, tuple):
            status, text = item
            return httpx.Response(status, text=text)
        if callable(item):
            return item(request)
        raise TypeError(
            f"Unsupported queued response type: {type(item).__name__}. "
            "Pass an httpx.Response, a (status, text) tuple, or a callable."
        )


@pytest.fixture
def mock_transport_concurrent() -> ConcurrentMockTransport:
    """A fresh ``ConcurrentMockTransport`` per test."""
    return ConcurrentMockTransport()


# ---------------------------------------------------------------------------
# barrier_factory
# ---------------------------------------------------------------------------


@dataclass
class EventBarrier:
    """One-shot N-arrival barrier built on ``asyncio.Event``.

    Every arriver calls ``await arrive()``. The first ``N - 1`` arrivers
    suspend at ``event.wait()``; the Nth arrival sets the event,
    releasing all of them simultaneously.

    Re-arming is intentionally NOT supported. Per-test barriers are
    cheap; spawn a fresh one for each synchronization point. This
    matches the guidance in the existing ``test_concurrency_refresh_race``
    suite which uses one ``asyncio.Event`` per checkpoint.
    """

    n: int
    _event: asyncio.Event = field(default_factory=asyncio.Event)
    _arrived: int = 0

    async def arrive(self) -> None:
        self._arrived += 1
        if self._arrived >= self.n:
            self._event.set()
        await self._event.wait()

    @property
    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def arrived_count(self) -> int:
        return self._arrived


@pytest.fixture
def barrier_factory() -> Callable[[int], EventBarrier]:
    """Return a callable that builds N-arrival ``EventBarrier`` instances.

    Usage::

        async def test_thing(barrier_factory):
            barrier = barrier_factory(3)
            await asyncio.gather(
                worker(barrier),
                worker(barrier),
                worker(barrier),
            )

    Implementation note: the factory is the fixture (not a barrier
    instance) so a single test can spawn multiple independent
    synchronization points without re-fixturing.
    """

    def _make(n: int) -> EventBarrier:
        if n <= 0:
            raise ValueError(f"barrier arrivals must be >= 1, got {n}")
        return EventBarrier(n=n)

    return _make


# ---------------------------------------------------------------------------
# cancellation_helper
# ---------------------------------------------------------------------------


CancellationHelper = Callable[..., Awaitable[Any]]


@pytest.fixture
def cancellation_helper() -> CancellationHelper:
    """Wrap a coroutine in ``asyncio.wait_for`` with diagnostic on cancel.

    Signature::

        await cancellation_helper(coro, timeout=5.0, label="my-coro")

    On ``asyncio.TimeoutError`` (Python 3.10) / ``TimeoutError`` (3.11+),
    logs the label and timeout via the harness logger then re-raises.
    The re-raise preserves test failure semantics — the helper's job is
    to surface *which* coroutine deadlocked, not to swallow the failure.

    The diagnostic is stderr-friendly and includes the traceback so a
    CI failure with several pending coroutines is debuggable from the
    log alone.
    """

    async def _run(
        coro: Awaitable[T],
        *,
        timeout: float = 5.0,
        label: str = "<unlabeled>",
    ) -> T:
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            tb = traceback.format_exc()
            logger.error(
                "cancellation_helper: coroutine %r timed out after %.3fs\n%s",
                label,
                timeout,
                tb,
            )
            raise

    return _run
