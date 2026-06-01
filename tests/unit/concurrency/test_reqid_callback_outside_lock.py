"""Regression tests for the reqid-callback-outside-lock contract (P1-19).

Before P1-19, :meth:`ReqidCounter.next_reqid` invoked the ``on_lock_wait``
callback under :attr:`ReqidCounter._lock`. A misbehaving callback —
slow telemetry sink, attempted re-entry, ``asyncio.sleep(0)`` that
yielded to another coroutine waiting on the same reqid lock — would
serialise the lock-wait recording with the increment itself,
unnecessarily widening the critical section.

The fix reorders so the callback fires AFTER ``_lock.release()``:

1. Acquire lock + measure wait time.
2. Mutate ``_value`` under the lock.
3. Release the lock.
4. Invoke the ``on_lock_wait`` callback with the measured wait time.

The increment semantics (monotonic, distinct values under concurrent
``asyncio.gather``) are unchanged. The reorder is purely a
"don't-hold-the-lock-while-emitting-telemetry" hardening pass.

Acceptance:
- The callback observes a *releasable* lock (i.e. can acquire it itself
  in the callback body without deadlocking) → proves the release happened
  first.
- The pre-increment-value snapshot the callback sees is the
  *post-increment* value (proves increment happened first).
- ``next_reqid`` is still atomic under ``asyncio.gather``: two
  concurrent calls produce distinct values.
"""

from __future__ import annotations

import asyncio

import pytest

from notebooklm._reqid_counter import ReqidCounter


@pytest.mark.asyncio
async def test_on_lock_wait_runs_after_lock_release() -> None:
    """The callback must observe a releasable lock.

    Set up a callback that tries to acquire ``counter._lock`` itself. If
    the callback ran *inside* the lock, the re-entry would deadlock; the
    test fails via timeout. If the callback runs *outside* the lock (the
    P1-19 fix), the re-acquire succeeds immediately.
    """
    counter = ReqidCounter()

    callback_could_acquire: list[bool] = []

    def on_lock_wait(_wait_seconds: float) -> None:
        # If we got here while ``_lock`` was still held, this acquire
        # would block forever. Use ``locked()`` — a synchronous probe —
        # to capture the state without blocking.
        lock = counter._lock
        assert lock is not None
        callback_could_acquire.append(not lock.locked())

    counter._on_lock_wait = on_lock_wait

    await counter.next_reqid()

    assert callback_could_acquire == [True], (
        "on_lock_wait callback observed the lock as STILL HELD — "
        "the callback must run AFTER lock.release(), not before."
    )


@pytest.mark.asyncio
async def test_on_lock_wait_observes_post_increment_value() -> None:
    """The increment must happen under the lock, before the callback.

    Capture the counter value from inside the callback. The fix's
    ordering — ``_value += step`` *inside* the lock, callback *outside* —
    means the callback sees the already-incremented value.

    This guards against a "fix" that releases the lock too early and
    races the increment with the callback.
    """
    counter = ReqidCounter()
    baseline = counter.value

    observed_values: list[int] = []

    def on_lock_wait(_wait_seconds: float) -> None:
        observed_values.append(counter.value)

    counter._on_lock_wait = on_lock_wait

    new_value = await counter.next_reqid()

    assert new_value == baseline + 100000  # default step
    assert observed_values == [new_value], (
        f"callback observed counter={observed_values}, "
        f"expected post-increment value [{new_value}]. "
        "The increment must complete under the lock before the callback fires."
    )


@pytest.mark.asyncio
async def test_next_reqid_remains_atomic_under_gather() -> None:
    """The lock-ordering fix must not break concurrent monotonicity.

    Two ``asyncio.gather``-ed ``next_reqid`` calls must still produce
    distinct, monotonic values. This is the existing contract — added
    here as a regression guard that the P1-19 reorder doesn't widen any
    race window in ``_value += step``.
    """
    counter = ReqidCounter()
    baseline = counter.value

    results = await asyncio.gather(
        counter.next_reqid(),
        counter.next_reqid(),
        counter.next_reqid(),
    )

    assert len(set(results)) == 3, f"expected 3 distinct values, got {results}"
    assert sorted(results) == [baseline + i * 100000 for i in (1, 2, 3)], (
        f"expected monotonic +100000 sequence after baseline {baseline}, got {sorted(results)}"
    )
