"""Small runtime deadline helper shared by retry and polling loops.

Architecture mapping note: this module owns the narrow internal
deadline/sleep-clamp primitive used by retry middleware, artifact polling,
and source polling.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Monotonic = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]


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


__all__ = ["Monotonic", "RuntimeDeadline", "Sleep"]
