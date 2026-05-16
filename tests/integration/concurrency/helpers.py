"""Reusable assertion helpers for the Tier 7 concurrency harness.

Three helpers, each scoped to a recurring assertion shape that
appears across the Phase 2 fix tests. Add new helpers here only when
they would be repeated in two or more Phase 2 tests — single-use
assertions stay inline in their test module.

Helpers
-------
``assert_no_concurrent_overlap(events)``
    Given a stream of ``(timestamp, kind)`` events from a section that
    is supposed to be serialized (a lock, a per-resource queue, a
    per-conversation guard), asserts that no two ``enter`` events
    occur without an intervening ``exit``. Used by Phase 2 tests for
    `_refresh_lock`, the conversation-cache lock, the
    per-task polling dedupe leader, etc.

``assert_unique_outputs(make_coro, n_concurrent)``
    Asserts N concurrent invocations of an async callable produce N
    distinct outputs. Used by tests that verify monotonic/unique IDs
    under fan-out (e.g. `next_reqid` regression coverage).

``with_simulated_cancel(coro, delay, *, label=None)``
    Creates a task from ``coro`` and schedules ``task.cancel()`` after
    ``delay`` seconds. Returns the task's result, or the captured
    exception (including ``CancelledError``). Used by tests for
    `asyncio.shield` regressions across the concurrency-hardening work:
    cancel one waiter mid-flight, assert sibling work survives.

Non-helpers
-----------
Module-level fixtures live in ``conftest.py``. ``helpers.py`` exposes
plain functions so per-bug tests can import them explicitly. Tests
that live UNDER ``tests/integration/concurrency/`` use the relative
form (``from .helpers import ...``); tests at other locations should
use whatever import path pytest's rootdir/pythonpath discovery
exposes for their layout.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Literal, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

EventKind = Literal["enter", "exit"]
"""Discriminator for ``assert_no_concurrent_overlap`` events."""


def assert_no_concurrent_overlap(
    events: Iterable[tuple[float, EventKind]],
) -> None:
    """Replay an enter/exit event stream and assert peak depth == 1.

    Parameters
    ----------
    events:
        Iterable of ``(timestamp, kind)`` pairs where ``kind`` is
        ``"enter"`` or ``"exit"``. Events are replayed in input
        order — the timestamps are recorded for the assertion message
        but do not need to be monotonic (the caller is responsible for
        ordering or pre-sorting if needed).

    Raises
    ------
    AssertionError
        If at any replay step the inflight count exceeds 1, OR if an
        ``exit`` is observed when the inflight count is already 0
        (caller bug — unmatched exit). The error message identifies
        the offending event by index and timestamp so a failing test
        can point at the offending coroutine in its log capture.

    Notes
    -----
    This helper does not record the events itself — the caller is
    expected to instrument the section under test, typically via a
    list shared across coroutines and protected by the asyncio
    event-loop's single-thread invariant. Example pattern::

        events: list[tuple[float, EventKind]] = []

        async def serialized_section():
            events.append((time.monotonic(), "enter"))
            try:
                await do_work()
            finally:
                events.append((time.monotonic(), "exit"))

        await asyncio.gather(*[serialized_section() for _ in range(10)])
        assert_no_concurrent_overlap(events)
    """
    inflight = 0
    for index, (timestamp, kind) in enumerate(events):
        if kind == "enter":
            inflight += 1
            if inflight > 1:
                raise AssertionError(
                    f"concurrent overlap detected at event index {index} "
                    f"(timestamp={timestamp}): inflight={inflight} > 1. "
                    f"The section is not serialized."
                )
        elif kind == "exit":
            if inflight == 0:
                raise AssertionError(
                    f"unmatched exit at event index {index} "
                    f"(timestamp={timestamp}): no matching enter. "
                    f"Caller instrumentation bug."
                )
            inflight -= 1
        else:
            raise AssertionError(
                f"unknown event kind {kind!r} at index {index}; expected 'enter' or 'exit'."
            )
    if inflight != 0:
        raise AssertionError(
            f"event stream ended with inflight={inflight} (expected 0). "
            f"Some enters had no matching exit."
        )


async def assert_unique_outputs(
    make_coro: Callable[[], Awaitable[T]],
    n_concurrent: int,
) -> list[T]:
    """Run ``make_coro()`` ``n_concurrent`` times via gather, assert distinct.

    Parameters
    ----------
    make_coro:
        Zero-argument callable that returns a fresh coroutine on each
        call. Required to be a callable rather than a coroutine
        because each gather slot needs its own coroutine instance.
    n_concurrent:
        Number of concurrent invocations to fan out. Must be >= 1.

    Returns
    -------
    list[T]
        The collected outputs in completion order. Returned so the
        caller can assert additional shape properties (e.g.
        monotonicity) beyond uniqueness.

    Raises
    ------
    AssertionError
        If the collected outputs contain any duplicate. Error message
        identifies the duplicate value and the duplicated indices.
    ValueError
        If ``n_concurrent < 1``.

    Notes
    -----
    Outputs must be hashable. For non-hashable outputs (lists, dicts),
    the caller should write a custom assertion rather than try to
    coerce here.
    """
    if n_concurrent < 1:
        raise ValueError(f"n_concurrent must be >= 1, got {n_concurrent}")
    coros = [make_coro() for _ in range(n_concurrent)]
    outputs: list[T] = await asyncio.gather(*coros)
    seen: dict[Any, int] = {}
    for index, value in enumerate(outputs):
        if value in seen:
            raise AssertionError(
                f"duplicate output {value!r} at indices {seen[value]} and "
                f"{index} (of {n_concurrent} concurrent invocations)."
            )
        seen[value] = index
    return outputs


async def with_simulated_cancel(
    coro: Awaitable[T],
    delay: float,
    *,
    label: str | None = None,
) -> T | BaseException:
    """Run ``coro`` and cancel it after ``delay`` seconds.

    Parameters
    ----------
    coro:
        The awaitable to wrap. Will be scheduled as a task immediately.
    delay:
        Seconds to wait before issuing ``task.cancel()``. Must be
        >= 0. Use a small positive value (50ms-500ms) for most tests.
    label:
        Optional string to embed in the diagnostic log line emitted
        when the cancel fires. Helpful when a test wraps several
        coroutines under cancel and needs to identify which one in
        its log capture.

    Returns
    -------
    T | BaseException
        The task's result if it completed before the cancel fired,
        OR the captured exception (typically ``asyncio.CancelledError``,
        but any exception the coro raises is captured and returned).
        Exceptions are RETURNED, not raised, so the caller can assert
        on the exception type without a ``pytest.raises`` block.

    Raises
    ------
    ValueError
        If ``delay < 0``.

    Notes
    -----
    Returning the exception (rather than raising) is intentional:
    Phase 2 cancellation tests typically want to assert
    "the leader survived; the canceled follower received
    ``CancelledError``" — returning makes that two assertions, not
    one ``pytest.raises`` block per scenario.

    Footgun: if ``T`` is itself a ``BaseException`` subclass (a
    coroutine that legitimately RETURNS an exception object rather
    than raising), the caller cannot distinguish "coro returned an
    exception value" from "coro raised". Phase 2 tests that need
    that distinction should write a custom assertion rather than
    rely on ``isinstance(result, BaseException)``.
    """
    if delay < 0:
        # Close the coro so misuse doesn't trigger
        # ``RuntimeWarning: coroutine '...' was never awaited``.
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[union-attr]
        raise ValueError(f"delay must be >= 0, got {delay}")

    task: asyncio.Task[T] = asyncio.create_task(_as_coroutine(coro))

    async def _cancel_after_delay() -> None:
        await asyncio.sleep(delay)
        if not task.done():
            logger.info(
                "with_simulated_cancel: firing cancel after %.3fs (label=%s)",
                delay,
                label,
            )
            task.cancel()

    canceller = asyncio.create_task(_cancel_after_delay())
    try:
        return await task
    except BaseException as exc:  # noqa: BLE001 — we explicitly return any exception
        # Outer-cancellation safety: if THIS helper's invoking task is
        # cancelled (vs. the inner ``task`` being cancelled by our own
        # canceller), propagate the cancel into ``task`` so it doesn't
        # become a stray background task. The exception is still
        # returned per the helper's contract.
        if isinstance(exc, asyncio.CancelledError) and not task.done():
            task.cancel()
        return exc
    finally:
        # Best-effort cleanup; the canceller has either fired or is
        # mid-sleep. Cancel it to avoid a stray task at test teardown.
        if not canceller.done():
            canceller.cancel()
            try:
                await canceller
            except BaseException:  # noqa: BLE001 — cleanup must not propagate
                pass


async def _as_coroutine(awaitable: Awaitable[T]) -> T:
    """Wrap an arbitrary awaitable in a coroutine for ``create_task``.

    ``asyncio.create_task`` requires a coroutine, but callers may pass
    any awaitable (a future, a generator-coroutine wrapper). This
    trivial wrapper bridges the gap.
    """
    return await awaitable
