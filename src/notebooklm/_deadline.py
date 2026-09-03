"""Small runtime deadline helper shared by retry and polling loops.

Architecture mapping note: this module owns the narrow internal
deadline/sleep-clamp primitive used by retry middleware, artifact polling,
and source polling.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

Monotonic = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]
_T = TypeVar("_T")


@dataclass(frozen=True)
class RuntimeDeadline:
    """Track an aggregate timeout against a monotonic clock."""

    timeout: float
    started_at: float
    monotonic: Monotonic

    @classmethod
    def start(cls, timeout: float, *, monotonic: Monotonic | None = None) -> RuntimeDeadline:
        """Capture a monotonic start time for ``timeout`` seconds."""
        resolved_monotonic = time.monotonic if monotonic is None else monotonic
        return cls(
            timeout=float(timeout),
            started_at=resolved_monotonic(),
            monotonic=resolved_monotonic,
        )

    @classmethod
    def from_timeout(
        cls,
        timeout: float | None,
        *,
        monotonic: Monotonic | None = None,
    ) -> RuntimeDeadline | None:
        """Start a deadline unless ``timeout`` disables the aggregate budget."""
        if timeout is None or not math.isfinite(float(timeout)):
            return None
        return cls.start(float(timeout), monotonic=monotonic)

    def now(self) -> float:
        """Return the current monotonic timestamp."""
        return self.monotonic()

    def elapsed(self) -> float:
        """Return seconds elapsed since the deadline was started."""
        return self.now() - self.started_at

    def remaining(self) -> float:
        """Return non-negative seconds left before the timeout expires."""
        return max(0.0, self.timeout - self.elapsed())

    def expired(self) -> bool:
        """Return ``True`` once elapsed time reaches the aggregate timeout."""
        return self.remaining() <= 0.0

    def exceeded(self) -> bool:
        """Return ``True`` once elapsed time moves past the aggregate timeout."""
        return self.elapsed() > self.timeout

    def clamp_sleep(self, requested: float) -> float:
        """Clamp a requested sleep duration to the remaining timeout budget."""
        return max(0.0, min(float(requested), self.remaining()))

    def timeout_message(self, operation: str) -> str:
        """Build a consistent timeout message for diagnostics."""
        return f"{operation} timed out after {self.timeout:.1f}s"


async def await_with_deadline(
    awaitable: Awaitable[_T],
    deadline: RuntimeDeadline | None,
    *,
    on_timeout: Callable[[], BaseException],
) -> _T:
    """Await within ``deadline`` and normalize timeout translation.

    An already-expired budget closes coroutine objects before refusing them,
    avoiding an unawaited-coroutine warning while leaving non-closeable
    awaitables such as caller-owned futures untouched.
    """
    if deadline is None:
        return await awaitable
    remaining = deadline.remaining()
    if remaining <= 0.0:
        close = getattr(awaitable, "close", None)
        if callable(close):
            cast(Callable[[], object], close)()
        raise on_timeout() from None
    try:
        return await asyncio.wait_for(awaitable, timeout=remaining)
    except asyncio.TimeoutError:
        # Python 3.10 keeps asyncio.TimeoutError separate from the built-in
        # spelling used by the runtime's public timeout policy.
        raise on_timeout() from None


__all__ = ["Monotonic", "RuntimeDeadline", "Sleep", "await_with_deadline"]
