"""Small, transport-neutral evidence contract for the local fault scenarios."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScenarioResult:
    """Mutable evidence shared with the runner, including during cancellation."""

    backend: str
    scenario: str
    operation_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def record(self, kind: str, **payload: Any) -> None:
        """Append a snapshot of JSON-safe evidence in observation order."""
        if not isinstance(kind, str) or not kind:
            raise ValueError("event kind must be a nonempty string")
        # Copy nested containers so subsequent mutation cannot rewrite history.
        # Reject NaN/infinity too: reports are portable JSON, not Python reprs.
        event = json.loads(json.dumps({"kind": kind, **payload}, allow_nan=False))
        self.events.append(event)

    def require(self, name: str, condition: bool) -> None:
        """Record one invariant and preserve all evidence when it fails."""
        if not isinstance(name, str) or not name:
            raise ValueError("check name must be a nonempty string")
        if name in self.checks:
            raise ValueError(f"duplicate scenario check: {name}")
        if not isinstance(condition, bool):
            raise TypeError("scenario check condition must be bool")
        self.checks[name] = condition
        self.record("check", name=name, passed=condition)
        if not condition:
            raise ScenarioFailure(self)


class ScenarioFailure(AssertionError):
    """An invariant failed; ``result`` includes evidence up to that failure."""

    def __init__(self, result: ScenarioResult) -> None:
        self.result = result
        failed = ", ".join(name for name, passed in result.checks.items() if not passed)
        super().__init__(f"{result.backend}/{result.scenario} [{result.operation_id}]: {failed}")
