"""CLI-internal services for artifact generation commands."""

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from ... import artifacts as artifact_retry
from ...client import NotebookLMClient
from ...types import GenerationStatus

# Retry constants
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

    Mirrors the format suggested in the audit — kind + typical-duration
    hint + optional elapsed timer. ``elapsed`` is ``None`` on first paint and
    an integer seconds value once the periodic ticker starts updating.
    """
    hint = _TYPICAL_DURATIONS.get(artifact_type)
    suffix = f" ({hint})" if hint else ""
    base = f"Waiting for {artifact_type} generation{suffix}..."
    if elapsed is None:
        return base
    return f"{base} [{int(elapsed)}s elapsed]"


async def generate_with_retry(
    generate_fn: Callable[[], Awaitable[GenerationStatus | None]],
    max_retries: int,
    artifact_type: str,
    on_retry: Callable[[artifact_retry.RateLimitRetryEvent], None] | None = None,
) -> GenerationStatus | None:
    """Generate artifact with retry on rate limit.

    Retries the generation call with exponential backoff when rate limited.
    Always makes at least one attempt, even when max_retries=0.

    Args:
        generate_fn: Async function that performs the generation.
        max_retries: Maximum number of retries (0 = no retry, just one attempt).
        artifact_type: Display name for progress messages.
        on_retry: Optional command-layer callback for retry notices.

    Returns:
        GenerationStatus or None if generation failed.
    """

    return await artifact_retry.with_rate_limit_retry(
        generate_fn,
        max_retries=max_retries,
        on_retry=on_retry,
    )


async def handle_generation_result(
    client: NotebookLMClient,
    notebook_id: str,
    result: Any,
    artifact_type: str,
    wait: bool = False,
    timeout: float = 300.0,
    interval: float | None = None,
    wait_context: Callable[[str, str], AbstractAsyncContextManager[None]] | None = None,
    wait_start_sink: Callable[[str], None] | None = None,
) -> GenerationOutcome:
    """Handle generation result with optional waiting and typed outcome mapping.

    Consolidates common pattern across all generate commands:
    - Check for None/failed result
    - Optionally wait for completion
    - Return a typed outcome for the command layer to render

    Args:
        client: The NotebookLM client.
        notebook_id: The notebook ID.
        result: The generation result from artifacts API.
        artifact_type: Display name for the artifact type (e.g., "audio", "video").
        wait: Whether to wait for completion.
        timeout: Timeout forwarded to ``wait_for_completion``. Callers supply
            per-command defaults; media generators use longer budgets while
            generic artifact waits remain at 300s.
        interval: Polling interval in seconds. ``None`` (default) lets
            ``wait_for_completion`` use its built-in default
            (``initial_interval=2.0``); when supplied, the value is forwarded
            as ``initial_interval`` so callers can tighten or loosen the
            cadence.

    Returns:
        GenerationOutcome describing the final status.
    """
    if not result:
        return GenerationOutcome(
            status="failed",
            artifact_type=artifact_type,
            error=f"{artifact_type.title()} generation failed",
        )

    # Check for rate limiting (result exists but failed due to rate limit)
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

    status: Any = result
    task_id = _extract_generation_task_id(result)

    # Wait for completion if requested
    if wait and task_id:
        if wait_start_sink is not None:
            wait_start_sink(task_id)
        wait_kwargs: dict[str, Any] = {"timeout": timeout}
        if interval is not None:
            wait_kwargs["initial_interval"] = interval
        context = wait_context or _null_wait_context
        async with context(
            _format_status_message(artifact_type),
            f"notebooklm artifact poll {task_id}",
        ):
            status = await client.artifacts.wait_for_completion(notebook_id, task_id, **wait_kwargs)

    return generation_outcome_from_status(status, artifact_type)


@contextlib.asynccontextmanager
async def _null_wait_context(_message: str, _resume_hint: str) -> AsyncIterator[None]:
    yield


def _extract_generation_task_id(result: Any) -> str | None:
    """Extract the task ID used to wait after a generation-start response.

    Generation-start dicts historically prefer ``artifact_id`` over
    ``task_id``. Keep that precedence separate from final status rendering,
    where ``_extract_task_id`` preserves the existing ``task_id``-first order.
    """
    if isinstance(result, GenerationStatus):
        return result.task_id
    if isinstance(result, dict):
        return result.get("artifact_id") or result.get("task_id")
    if isinstance(result, list) and len(result) > 0 and isinstance(result[0], str):
        return result[0]
    return None


def _extract_task_id(status: Any) -> str | None:
    """Extract task ID from various status formats.

    Handles GenerationStatus objects, dicts with task_id/artifact_id keys,
    and lists where the first element is an ID string.
    """
    if hasattr(status, "task_id"):
        return status.task_id
    if isinstance(status, dict):
        return status.get("task_id") or status.get("artifact_id")
    if isinstance(status, list) and len(status) > 0 and isinstance(status[0], str):
        return status[0]
    return None


def generation_outcome_from_status(status: Any, artifact_type: str) -> GenerationOutcome:
    """Map a generation status payload to a command-renderable outcome."""
    is_complete = hasattr(status, "is_complete") and status.is_complete
    is_failed = hasattr(status, "is_failed") and status.is_failed

    if is_failed:
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


__all__ = [
    "GenerationOutcome",
    "calculate_backoff_delay",
    "generate_with_retry",
    "generation_outcome_from_status",
    "handle_generation_result",
]
