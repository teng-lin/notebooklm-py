"""Shared polling helpers for CLI wait commands."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Generic, TypeVar

from ..error_handler import emit_cancelled_and_exit
from ..rendering import console

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


@contextlib.asynccontextmanager
async def status_with_elapsed(
    message: str,
    *,
    json_output: bool = False,
    resume_hint: str | None = None,
) -> AsyncIterator[None]:
    """Show a transient Rich status spinner with an elapsed-seconds counter.

    The context manager is a no-op in JSON mode so stdout remains parseable.
    In text mode it updates the spinner once per second and cancels that
    ticker when the wrapped block exits. If ``resume_hint`` is provided,
    ``KeyboardInterrupt`` is converted to the CLI's structured/friendly
    cancellation response; otherwise the interrupt propagates unchanged.
    """

    @contextlib.contextmanager
    def _sigint_guard() -> Iterator[None]:
        try:
            yield
        except KeyboardInterrupt:
            if resume_hint is None:
                raise
            emit_cancelled_and_exit(resume_hint, json_output=json_output)

    if json_output:
        with _sigint_guard():
            yield
        return

    start = time.monotonic()
    with console.status(message) as status:

        async def _ticker() -> None:
            while True:
                await asyncio.sleep(1.0)
                elapsed = int(time.monotonic() - start)
                status.update(f"{message} [{elapsed}s elapsed]")

        ticker_task = asyncio.create_task(_ticker())
        try:
            with _sigint_guard():
                yield
        finally:
            ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker_task


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


__all__ = ["PollResult", "poll_until", "status_with_elapsed"]
