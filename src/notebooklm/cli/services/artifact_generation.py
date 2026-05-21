"""CLI-internal services for artifact generation commands."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ...client import NotebookLMClient
from ...types import GenerationStatus
from ..error_handler import output_error
from ..rendering import console, json_output_response
from .polling import status_with_elapsed

# Retry constants
RETRY_INITIAL_DELAY = 60.0  # seconds
RETRY_MAX_DELAY = 300.0  # 5 minutes
RETRY_BACKOFF_MULTIPLIER = 2.0


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


_status_with_elapsed = status_with_elapsed


def calculate_backoff_delay(
    attempt: int,
    initial_delay: float = RETRY_INITIAL_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    multiplier: float = RETRY_BACKOFF_MULTIPLIER,
) -> float:
    """Calculate exponential backoff delay for a retry attempt.

    Args:
        attempt: The current attempt number (0-indexed).
        initial_delay: Initial delay in seconds.
        max_delay: Maximum delay cap in seconds.
        multiplier: Backoff multiplier.

    Returns:
        Delay in seconds for this attempt.
    """
    delay = initial_delay * (multiplier**attempt)
    return min(delay, max_delay)


async def generate_with_retry(
    generate_fn: Callable[[], Awaitable[GenerationStatus | None]],
    max_retries: int,
    artifact_type: str,
    json_output: bool = False,
) -> GenerationStatus | None:
    """Generate artifact with retry on rate limit.

    Retries the generation call with exponential backoff when rate limited.
    Always makes at least one attempt, even when max_retries=0.

    Args:
        generate_fn: Async function that performs the generation.
        max_retries: Maximum number of retries (0 = no retry, just one attempt).
        artifact_type: Display name for progress messages.
        json_output: Whether to suppress console output.

    Returns:
        GenerationStatus or None if generation failed.
    """
    for attempt in range(max_retries + 1):
        result = await generate_fn()

        # Return immediately if not rate limited (success or other failure)
        if not isinstance(result, GenerationStatus) or not result.is_rate_limited:
            return result

        # Rate limited with no retries left
        if attempt >= max_retries:
            return result

        # Wait before retry
        delay = calculate_backoff_delay(attempt)
        if not json_output:
            console.print(
                f"[yellow]{artifact_type.title()} rate limited. "
                f"Retrying in {int(delay)}s (attempt {attempt + 2}/{max_retries + 1})...[/yellow]"
            )
        await asyncio.sleep(delay)

    # Unreachable, but satisfies type checker
    return None


async def handle_generation_result(
    client: NotebookLMClient,
    notebook_id: str,
    result: Any,
    artifact_type: str,
    wait: bool = False,
    json_output: bool = False,
    timeout: float = 300.0,
    interval: float | None = None,
) -> GenerationStatus | None:
    """Handle generation result with optional waiting and output formatting.

    Consolidates common pattern across all generate commands:
    - Check for None/failed result
    - Optionally wait for completion
    - Output status in JSON or console format

    Args:
        client: The NotebookLM client.
        notebook_id: The notebook ID.
        result: The generation result from artifacts API.
        artifact_type: Display name for the artifact type (e.g., "audio", "video").
        wait: Whether to wait for completion.
        json_output: Whether to output as JSON.
        timeout: Timeout for waiting (default: 300s).
        interval: Polling interval in seconds. ``None`` (default) lets
            ``wait_for_completion`` use its built-in default
            (``initial_interval=2.0``); when supplied, the value is forwarded
            as ``initial_interval`` so callers can tighten or loosen the
            cadence.

    Returns:
        Final GenerationStatus, or None if generation failed.
    """
    # Both failure branches route through ``output_error`` so the exit code
    # is non-zero in both text and JSON modes. ``output_error`` always raises
    # ``SystemExit`` — these calls never return. Runtime failures use
    # ``output_error`` rather than ``click.ClickException``; the latter is
    # reserved for input-validation boundaries (CLI argument parsing).
    if not result:
        output_error(
            f"{artifact_type.title()} generation failed",
            "GENERATION_FAILED",
            json_output,
            1,
        )

    # Check for rate limiting (result exists but failed due to rate limit)
    if isinstance(result, GenerationStatus) and result.is_rate_limited:
        output_error(
            f"{artifact_type.title()} generation rate limited by Google.",
            "RATE_LIMITED",
            json_output,
            1,
            hint=(
                "Daily quota may be exceeded. Try again in 1-24 hours, "
                "or use --retry N to retry automatically."
            ),
        )

    status: Any = result
    task_id = _extract_generation_task_id(result)

    # Wait for completion if requested
    if wait and task_id:
        if not json_output:
            console.print(f"[yellow]Generating {artifact_type}...[/yellow] Task: {task_id}")
        wait_kwargs: dict[str, Any] = {"timeout": timeout}
        if interval is not None:
            wait_kwargs["initial_interval"] = interval
        # Wrap the blocking poll in a transient spinner so interactive users see
        # progress feedback during long generations. The status
        # line includes the artifact kind, a typical-duration hint, and a
        # live elapsed-seconds counter. No-op under --json.
        #
        # The ``resume_hint`` plumbs the canonical M2 cancellation message
        # (``Cancelled. Resume with: notebooklm artifact poll <task_id>``)
        # so Ctrl-C during the wait surfaces the resume command instead of
        # a Python KeyboardInterrupt traceback. See ``cli/error_handler.py``
        # ``emit_cancelled_and_exit``.
        async with status_with_elapsed(
            _format_status_message(artifact_type),
            json_output=json_output,
            resume_hint=f"notebooklm artifact poll {task_id}",
        ):
            status = await client.artifacts.wait_for_completion(notebook_id, task_id, **wait_kwargs)

    # Output status
    _output_generation_status(status, artifact_type, json_output)

    return status if isinstance(status, GenerationStatus) else None


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


def _output_generation_status(status: Any, artifact_type: str, json_output: bool) -> None:
    """Output generation status in appropriate format.

    The terminal ``is_failed`` branch routes through ``output_error`` so
    failures observed after a ``--wait`` produce a non-zero exit in both
    text and JSON modes.
    """
    is_complete = hasattr(status, "is_complete") and status.is_complete
    is_failed = hasattr(status, "is_failed") and status.is_failed

    if is_failed:
        output_error(
            getattr(status, "error", None) or f"{artifact_type.title()} generation failed",
            "GENERATION_FAILED",
            json_output,
            1,
        )

    if json_output:
        if is_complete:
            json_output_response(
                {
                    "task_id": getattr(status, "task_id", None),
                    "status": "completed",
                    "url": getattr(status, "url", None),
                }
            )
        else:
            task_id = _extract_task_id(status)
            json_output_response({"task_id": task_id, "status": "pending"})
    else:
        if is_complete:
            url = getattr(status, "url", None)
            if url:
                console.print(f"[green]{artifact_type.title()} ready:[/green] {url}")
            else:
                console.print(f"[green]{artifact_type.title()} ready[/green]")
        else:
            task_id = _extract_task_id(status)
            console.print(f"[yellow]Started:[/yellow] {task_id or status}")
