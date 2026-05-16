"""Concurrency test for ``ClientCore.next_reqid``.

Covers 100 concurrent ``next_reqid()`` callers (via
``asyncio.gather``) must each see a unique, monotonic counter value. Without
the ``asyncio.Lock`` guard, the underlying read-modify-write would race and
produce duplicate values — exactly the bug Google's chat backend rejects
when two concurrent ``ChatAPI.ask`` calls happen on the same client.
"""

import asyncio

import pytest

from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens


def _make_core() -> ClientCore:
    auth = AuthTokens(
        cookies={"SID": "test"},
        csrf_token="test_csrf",
        session_id="test_session",
    )
    return ClientCore(auth=auth)


@pytest.mark.asyncio
async def test_next_reqid_concurrent_unique_and_monotonic() -> None:
    """100 concurrent ``next_reqid()`` calls produce 100 distinct values."""
    core = _make_core()
    step = 100000
    baseline = core._reqid_counter  # 100000

    results = await asyncio.gather(*[core.next_reqid(step=step) for _ in range(100)])

    # Uniqueness: no two callers got the same value.
    assert len(set(results)) == 100, (
        f"Expected 100 unique reqids under contention, got {len(set(results))}"
    )

    # Strict monotonicity when sorted: each value is exactly ``step`` above
    # the previous one, anchored at ``baseline + step``.
    sorted_results = sorted(results)
    expected = [baseline + step * (i + 1) for i in range(100)]
    assert sorted_results == expected, (
        f"Expected values {expected[:3]}...{expected[-1]} (step={step}); "
        f"got {sorted_results[:3]}...{sorted_results[-1]}"
    )

    # The counter ends exactly where the largest call landed.
    assert core._reqid_counter == expected[-1]


@pytest.mark.asyncio
async def test_next_reqid_concurrent_custom_step() -> None:
    """The custom-``step`` path is also race-free under contention."""
    core = _make_core()
    step = 7  # small step exercises the increment math under heavy contention
    baseline = core._reqid_counter

    results = await asyncio.gather(*[core.next_reqid(step=step) for _ in range(50)])

    assert len(set(results)) == 50
    assert sorted(results) == [baseline + step * (i + 1) for i in range(50)]
