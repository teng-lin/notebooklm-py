"""Focused tests for the shared runtime deadline helpers."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from notebooklm._deadline import RuntimeDeadline, await_with_deadline


@pytest.mark.parametrize("timeout", [None, float("inf"), float("-inf"), float("nan")])
def test_from_timeout_disables_absent_and_non_finite_budgets(timeout: float | None) -> None:
    assert RuntimeDeadline.from_timeout(timeout) is None


def test_from_timeout_starts_a_finite_budget_with_the_supplied_clock() -> None:
    deadline = RuntimeDeadline.from_timeout(3, monotonic=lambda: 11.0)

    assert deadline is not None
    assert deadline.timeout == 3.0
    assert deadline.started_at == 11.0


@pytest.mark.asyncio
async def test_await_with_deadline_uses_the_owner_timeout_translation() -> None:
    class _OwnerTimeout(RuntimeError):
        pass

    awaitable = asyncio.sleep(1)
    with pytest.raises(_OwnerTimeout):
        await await_with_deadline(
            awaitable,
            RuntimeDeadline.start(0.0),
            on_timeout=_OwnerTimeout,
        )

    assert inspect.getcoroutinestate(awaitable) == inspect.CORO_CLOSED


@pytest.mark.asyncio
async def test_await_with_deadline_leaves_caller_owned_future_open() -> None:
    pending = asyncio.get_running_loop().create_future()

    with pytest.raises(TimeoutError):
        await await_with_deadline(
            pending,
            RuntimeDeadline.start(0.0),
            on_timeout=TimeoutError,
        )

    assert not pending.done()
    pending.cancel()
