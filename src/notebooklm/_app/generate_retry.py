"""Transport-neutral artifact-generation retry + wait orchestration.

This is the retry/wait half of the Click-free ``generate`` core (the sibling
:mod:`notebooklm._app.generate` owns plan-building + the executor). Production
retry/wait execution lives behind the artifact facade; this module retains
presentation-only result projection, compatibility constants, and the typed
:class:`GenerationOutcome`, the status-extraction helpers, and the spinner
status-line formatter. Splitting this out keeps each module under the
ADR-0008 module-size budget while leaving a single import surface
(``_app.generate`` re-exports everything callers need).

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import artifacts as artifact_retry
from ..types import GenerationStatus

# Retry constants re-exported from the public ``artifacts`` retry helper so the
# CLI service adapter (and its tests) keep their established import seam.
RETRY_INITIAL_DELAY = artifact_retry.RATE_LIMIT_RETRY_INITIAL_DELAY
RETRY_MAX_DELAY = artifact_retry.RATE_LIMIT_RETRY_MAX_DELAY
RETRY_BACKOFF_MULTIPLIER = artifact_retry.RATE_LIMIT_RETRY_BACKOFF_MULTIPLIER

# Compatibility export for callers that imported the old CLI-local helper.
calculate_backoff_delay = artifact_retry.calculate_backoff_delay

# Typical-duration hints for the spinner status line.
# Empirical observation; the API exposes no progress channel so these are
# user-facing wall-clock heuristics, not authoritative ETAs. Missing keys fall
# back to no hint — the spinner still renders kind + elapsed seconds.
_TYPICAL_DURATIONS: dict[str, str] = {
    "audio": "typically 2-5 min",
    "video": "typically 5-15 min",
    "cinematic-video": "typically 30-40 min",
    "slide-deck": "typically 1-3 min",
    "quiz": "typically 30-60 sec",
    "flashcards": "typically 30-60 sec",
    "infographic": "typically 1-3 min",
    "data-table": "typically 30-90 sec",
    "mind-map": "typically 30-90 sec",
    "report": "typically 1-3 min",
}


@dataclass(frozen=True)
class GenerationOutcome:
    """Typed result of generation orchestration for command-layer rendering."""

    status: str
    artifact_type: str
    task_id: str | None = None
    url: str | None = None
    error: str | None = None
    error_code: str = "GENERATION_FAILED"
    hint: str | None = None
    raw_status: Any = None

    @property
    def exit_code(self) -> int:
        return 1 if self.status in {"failed", "rate_limited"} else 0


def _format_status_message(artifact_type: str, elapsed: float | None = None) -> str:
    """Build the spinner status line for a long-running generation.

    Kind + typical-duration hint + optional elapsed timer. ``elapsed`` is
    ``None`` on first paint and an integer seconds value once the periodic
    ticker starts updating.
    """
    hint = _TYPICAL_DURATIONS.get(artifact_type)
    suffix = f" ({hint})" if hint else ""
    base = f"Waiting for {artifact_type} generation{suffix}..."
    if elapsed is None:
        return base
    return f"{base} [{int(elapsed)}s elapsed]"


def _extract_task_id(status: Any) -> str | None:
    """Extract task ID from various status formats.

    Handles GenerationStatus objects (anything exposing ``task_id``) and dicts
    with ``task_id``/``artifact_id`` keys. The facade returns typed statuses,
    so no raw positional payload ever reaches this helper.
    """
    if hasattr(status, "task_id"):
        return status.task_id
    if isinstance(status, dict):
        return status.get("task_id") or status.get("artifact_id")
    return None


def generation_outcome_from_status(status: Any, artifact_type: str) -> GenerationOutcome:
    """Map a generation status payload to a command-renderable outcome."""
    is_complete = hasattr(status, "is_complete") and status.is_complete
    is_failed = hasattr(status, "is_failed") and status.is_failed
    # A ``removed`` status (artifact delisted by the server) is distinct from
    # ``failed`` at the API layer, but the CLI surfaces both as a non-zero-exit
    # error since neither produced a usable artifact.
    is_removed = hasattr(status, "is_removed") and status.is_removed

    if is_failed or is_removed:
        return GenerationOutcome(
            status="failed",
            artifact_type=artifact_type,
            task_id=_extract_task_id(status),
            error=getattr(status, "error", None) or f"{artifact_type.title()} generation failed",
            raw_status=status,
        )

    if is_complete:
        return GenerationOutcome(
            status="completed",
            artifact_type=artifact_type,
            task_id=getattr(status, "task_id", None),
            url=getattr(status, "url", None),
            raw_status=status,
        )

    return GenerationOutcome(
        status="pending",
        artifact_type=artifact_type,
        task_id=_extract_task_id(status),
        raw_status=status,
    )


def generation_outcome_from_result(result: Any, artifact_type: str) -> GenerationOutcome:
    """Project a kickoff/final result without owning retry or wait execution."""
    if result is None:
        return GenerationOutcome(
            status="failed",
            artifact_type=artifact_type,
            error=f"{artifact_type.title()} generation failed",
        )
    if isinstance(result, GenerationStatus) and result.is_rate_limited:
        return GenerationOutcome(
            status="rate_limited",
            artifact_type=artifact_type,
            task_id=result.task_id,
            error=f"{artifact_type.title()} generation rate limited by Google.",
            error_code="RATE_LIMITED",
            hint=(
                "Daily quota may be exceeded. Try again in 1-24 hours, "
                "or use --retry N to retry automatically."
            ),
            raw_status=result,
        )
    return generation_outcome_from_status(result, artifact_type)


__all__ = [
    "RETRY_BACKOFF_MULTIPLIER",
    "RETRY_INITIAL_DELAY",
    "RETRY_MAX_DELAY",
    "GenerationOutcome",
    "calculate_backoff_delay",
    "generation_outcome_from_result",
    "generation_outcome_from_status",
]
