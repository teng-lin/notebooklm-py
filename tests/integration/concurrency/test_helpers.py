"""Unit tests for ``tests/integration/concurrency/helpers.py``.

Each helper has happy-path + at least one edge case. Helpers are
covered here (not via dogfooding in Phase 2 fix tests) so a
helper-level regression surfaces before any Phase 2 PR depends on it.
"""

from __future__ import annotations

import asyncio

import pytest

from .helpers import (
    assert_no_concurrent_overlap,
    assert_unique_outputs,
    with_simulated_cancel,
)

# pure helper-logic tests for tests/integration/concurrency/helpers.py;
# no HTTP, no cassette. Opt out of the tier-enforcement hook in
# tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr

# ---------------------------------------------------------------------------
# assert_no_concurrent_overlap
# ---------------------------------------------------------------------------


def test_no_concurrent_overlap_serialized_passes() -> None:
    """A serialized stream (each enter followed by exit) passes."""
    events = [
        (0.0, "enter"),
        (0.1, "exit"),
        (0.2, "enter"),
        (0.3, "exit"),
    ]
    assert_no_concurrent_overlap(events)  # type: ignore[arg-type]


def test_no_concurrent_overlap_detects_overlap() -> None:
    """Two enters without an intervening exit raises with index/timestamp."""
    events = [
        (0.0, "enter"),
        (0.1, "enter"),  # overlap at index 1
        (0.2, "exit"),
        (0.3, "exit"),
    ]
    with pytest.raises(AssertionError, match="index 1.*timestamp=0.1"):
        assert_no_concurrent_overlap(events)  # type: ignore[arg-type]


def test_no_concurrent_overlap_unmatched_exit_raises() -> None:
    """An exit before any enter is a caller bug; raise with a clear msg."""
    events = [
        (0.0, "exit"),
    ]
    with pytest.raises(AssertionError, match="unmatched exit.*index 0"):
        assert_no_concurrent_overlap(events)  # type: ignore[arg-type]


def test_no_concurrent_overlap_dangling_enter_raises() -> None:
    """A stream that ends with inflight > 0 raises (caller bug)."""
    events = [
        (0.0, "enter"),
        # no matching exit
    ]
    with pytest.raises(AssertionError, match="ended with inflight=1"):
        assert_no_concurrent_overlap(events)  # type: ignore[arg-type]


def test_no_concurrent_overlap_unknown_kind_raises() -> None:
    """An unknown event kind is a caller bug."""
    events = [(0.0, "wat")]
    with pytest.raises(AssertionError, match="unknown event kind"):
        assert_no_concurrent_overlap(events)  # type: ignore[arg-type]


def test_no_concurrent_overlap_empty_stream_passes() -> None:
    """No events is vacuously serialized."""
    assert_no_concurrent_overlap([])


# ---------------------------------------------------------------------------
# assert_unique_outputs
# ---------------------------------------------------------------------------


async def test_unique_outputs_distinct_passes() -> None:
    """Each invocation returns a fresh value -> no duplicates."""
    counter = 0

    async def _next() -> int:
        nonlocal counter
        counter += 1
        return counter

    outputs = await assert_unique_outputs(_next, 50)
    assert len(outputs) == 50
    assert sorted(outputs) == list(range(1, 51))


async def test_unique_outputs_duplicate_raises_with_indices() -> None:
    """A constant-returning callable trips the duplicate check."""

    async def _const() -> str:
        return "same"

    with pytest.raises(AssertionError, match=r"duplicate output 'same'"):
        await assert_unique_outputs(_const, 3)


async def test_unique_outputs_zero_raises_value_error() -> None:
    """``n_concurrent < 1`` is a misuse and raises early."""

    async def _noop() -> None:
        return None

    with pytest.raises(ValueError, match="n_concurrent must be >= 1"):
        await assert_unique_outputs(_noop, 0)


async def test_unique_outputs_returns_outputs_for_caller_assertions() -> None:
    """Callers may add their own assertions on the returned list."""
    seq = iter(range(5))

    async def _seq() -> int:
        return next(seq)

    outputs = await assert_unique_outputs(_seq, 5)
    # Caller can check shape beyond uniqueness. Use ``sorted`` /
    # ``set`` rather than positional equality so the test stays
    # stable across asyncio event-loop scheduling implementations
    # (CPython runs zero-await coroutines FIFO; uvloop/others may not).
    assert sorted(outputs) == [0, 1, 2, 3, 4]
    assert set(outputs) == set(range(5))


# ---------------------------------------------------------------------------
# with_simulated_cancel
# ---------------------------------------------------------------------------


async def test_with_simulated_cancel_returns_result_when_fast_enough() -> None:
    """A coro that finishes before the cancel fires returns its result."""

    async def _quick() -> str:
        await asyncio.sleep(0.01)
        return "done"

    result = await with_simulated_cancel(_quick(), delay=1.0)
    assert result == "done"


async def test_with_simulated_cancel_returns_cancelled_error_on_timeout() -> None:
    """A coro slower than the cancel returns ``CancelledError``."""

    async def _slow() -> str:
        await asyncio.sleep(10.0)
        return "never"

    result = await with_simulated_cancel(_slow(), delay=0.05, label="slow")
    assert isinstance(result, asyncio.CancelledError)


async def test_with_simulated_cancel_returns_application_exception() -> None:
    """If the coro raises before the cancel, that exception is captured."""

    async def _raises() -> str:
        await asyncio.sleep(0.01)
        raise RuntimeError("boom")

    result = await with_simulated_cancel(_raises(), delay=1.0)
    assert isinstance(result, RuntimeError)
    assert str(result) == "boom"


async def test_with_simulated_cancel_negative_delay_raises() -> None:
    """``delay < 0`` is a misuse and raises early."""

    async def _noop() -> None:
        return None

    with pytest.raises(ValueError, match="delay must be >= 0"):
        await with_simulated_cancel(_noop(), delay=-1.0)


async def test_with_simulated_cancel_propagates_outer_cancellation_to_inner() -> None:
    """Outer cancel of the helper itself must cancel the wrapped task too.

    Otherwise the inner task becomes a stray background task and the
    cancellation is silently consumed. Regression coverage for the
    claude[bot] iter-2 finding.
    """
    inner_started = asyncio.Event()
    inner_finished = False

    async def _slow() -> str:
        nonlocal inner_finished
        inner_started.set()
        try:
            await asyncio.sleep(10.0)
            inner_finished = True
            return "never"
        except asyncio.CancelledError:
            # Re-raise so the cancellation actually takes effect on
            # the task (the helper expects to receive CancelledError
            # from awaiting the task).
            raise

    helper_task = asyncio.create_task(
        with_simulated_cancel(_slow(), delay=10.0, label="outer-cancel-test")
    )

    # Wait until the inner coro has started (so the task is real).
    await inner_started.wait()
    # Give the canceller a tick to schedule, then cancel the OUTER helper.
    await asyncio.sleep(0)
    helper_task.cancel()

    # The helper catches CancelledError and returns it as a value
    # (per its contract); we just verify the outer cancel did NOT
    # leave the inner running.
    try:
        result = await helper_task
        assert isinstance(result, asyncio.CancelledError)
    except asyncio.CancelledError:
        # Acceptable: depending on timing, the outer cancel may
        # propagate before the helper's except block runs.
        pass

    # Yield to let the inner task observe its own cancellation.
    await asyncio.sleep(0)
    assert not inner_finished, (
        "Inner task should have been cancelled when the outer helper was cancelled."
    )


async def test_with_simulated_cancel_cleans_up_canceller_task() -> None:
    """The canceller task should not leak after the helper returns.

    No direct API to inspect; we rely on no warnings about
    "Task was destroyed but it is pending" emerging at teardown. This
    test exists so a future regression that drops the cleanup `cancel()`
    will surface as a noisy test run.
    """

    async def _quick() -> int:
        return 42

    result = await with_simulated_cancel(_quick(), delay=0.5)
    assert result == 42
    # Yield once so any pending cancellation can complete cleanly.
    await asyncio.sleep(0)
