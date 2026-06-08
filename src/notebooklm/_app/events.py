"""Transport-neutral progress-reporting seam.

Long-running ``_app`` operations (downloads, artifact generation, polling)
need to report progress without knowing whether the consumer is a Rich
progress bar, an MCP progress token, or ``/dev/null``. They emit
:class:`ProgressEvent` values into a caller-supplied :class:`ProgressSink`;
each adapter implements the sink in its own vocabulary.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ProgressEvent:
    """One progress update from a long-running operation.

    Attributes:
        message: Human-readable description of the current step (e.g.
            ``"Polling artifact status"``).
        kind: Optional coarse machine-readable label for the event so a sink
            can route or filter (e.g. ``"poll"`` / ``"download"`` / ``"done"``).
            ``None`` when the operation does not tag its events.
        pct: Optional completion fraction in the closed range ``[0.0, 1.0]``.
            ``None`` when progress is indeterminate (e.g. a poll with no known
            total). Adapters that render a percentage should treat ``None`` as
            "spinner / indeterminate".
    """

    message: str
    kind: str | None = None
    pct: float | None = None


@runtime_checkable
class ProgressSink(Protocol):
    """A consumer of :class:`ProgressEvent` values.

    Adapters implement this to render progress in their own surface. Sinks
    MUST treat :meth:`emit` as best-effort and side-effect-only: it returns
    nothing and must not raise into the operation it is observing (a sink that
    fails should swallow or log its own error rather than abort the work).
    """

    def emit(self, event: ProgressEvent) -> None:
        """Report a single progress event. Best-effort; returns nothing."""
        ...
