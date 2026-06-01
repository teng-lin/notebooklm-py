"""Shared polling helpers for CLI wait commands."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class PollResult(Generic[T]):
    """Result returned by :func:`poll_until`.

    Attributes:
        value: Last fetched value. This is the completed value on success and
            the most recent non-terminal value on timeout.
        attempts: Number of times ``fetch`` was awaited.
        elapsed: Seconds elapsed according to ``time.monotonic``.
        timed_out: Whether the timeout was reached before ``is_done`` returned
            true.
    """

    value: T
    attempts: int
    elapsed: float
    timed_out: bool


async def poll_until(
    fetch: Callable[[], Awaitable[T]],
    is_done: Callable[[T], bool],
    *,
    timeout: float,
    interval: float,
) -> PollResult[T]:
    """Poll ``fetch`` until ``is_done`` returns true or ``timeout`` elapses.

    ``fetch`` is called immediately, then after each ``interval`` while the
    value remains non-terminal. Timeout is reported as a ``PollResult`` with
    ``timed_out=True`` and the last fetched value. ``asyncio.CancelledError``
    and other exceptions from ``fetch`` or ``is_done`` are not swallowed, so
    caller-level cancellation and domain errors keep their normal semantics.
    """
    if timeout < 0:
        raise ValueError("timeout must be non-negative")
    if interval <= 0:
        raise ValueError("interval must be positive")

    start = time.monotonic()
    attempts = 0

    while True:
        value = await fetch()
        attempts += 1
        elapsed = time.monotonic() - start

        if is_done(value):
            return PollResult(value=value, attempts=attempts, elapsed=elapsed, timed_out=False)
        if elapsed >= timeout:
            return PollResult(value=value, attempts=attempts, elapsed=elapsed, timed_out=True)

        await asyncio.sleep(min(interval, timeout - elapsed))


__all__ = ["PollResult", "poll_until"]
