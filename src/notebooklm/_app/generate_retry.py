"""Transport-neutral artifact-generation retry + wait orchestration.

This is the retry/wait half of the Click-free ``generate`` core (the sibling
:mod:`notebooklm._app.generate` owns typed-request dispatch). It holds the
retry-with-backoff loop, the wait-for-completion orchestration, the typed
:class:`GenerationOutcome`, and the status-extraction helpers. Splitting this out keeps each module under the
ADR-0008 module-size budget while leaving a single import surface
(``_app.generate`` re-exports everything callers need).

The long-running progress seams are neutral callables: ``wait_start_sink`` is a
point notification; ``wait_context`` spans the awaited poll with an enter/exit
boundary (a spinner in the CLI). Neither signature carries a transport type, so
the adapter wires its Rich-coupled implementations in and this core stays
presentation-neutral.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .. import artifacts as artifact_retry
from ..types import GenerationStatus
from .generation_requests import GenerationKind

if TYPE_CHECKING:
    from ..client import NotebookLMClient

# Retry constants re-exported from the public ``artifacts`` retry helper so the
# CLI service adapter (and its tests) keep their established import seam.
RETRY_INITIAL_DELAY = artifact_retry.RATE_LIMIT_RETRY_INITIAL_DELAY
RETRY_MAX_DELAY = artifact_retry.RATE_LIMIT_RETRY_MAX_DELAY
RETRY_BACKOFF_MULTIPLIER = artifact_retry.RATE_LIMIT_RETRY_BACKOFF_MULTIPLIER

# Compatibility export for callers that imported the old CLI-local helper.
calculate_backoff_delay = artifact_retry.calculate_backoff_delay


@dataclass(frozen=True)
class GenerationOutcome:
    """Semantic result of generation orchestration, independent of rendering."""

    status: Literal["failed", "rate_limited", "completed", "pending"]
    kind: GenerationKind
    task_id: str | None = None
    url: str | None = None
    error: str | None = None
    raw_status: Any = None


@dataclass(frozen=True)
class GenerationWaitStarted:
    """Semantic notification emitted immediately before generation polling."""

    kind: GenerationKind
    task_id: str
    elapsed: float


async def generate_with_retry(
    generate_fn: Callable[[], Awaitable[GenerationStatus | None]],
    max_retries: int,
    on_retry: Callable[[artifact_retry.RateLimitRetryEvent], None] | None = None,
) -> GenerationStatus | None:
    """Generate artifact with retry on rate limit.

    Retries the generation call with exponential backoff when rate limited.
    Always makes at least one attempt, even when max_retries=0.

    Args:
        generate_fn: Async function that performs the generation.
        max_retries: Maximum number of retries (0 = no retry, just one attempt).
        on_retry: Optional command-layer callback for retry notices.

    Returns:
        GenerationStatus or None if generation failed.
    """
    return await artifact_retry.with_rate_limit_retry(
        generate_fn,
        max_retries=max_retries,
        on_retry=on_retry,
    )


@contextlib.asynccontextmanager
async def _null_wait_context(_event: GenerationWaitStarted) -> AsyncIterator[None]:
    yield


def _extract_generation_task_id(result: Any) -> str | None:
    """Extract the task ID used to wait after a generation-start response.

    Generation-start dicts historically prefer ``artifact_id`` over
    ``task_id``. Keep that precedence separate from final status rendering,
    where ``_extract_task_id`` preserves the existing ``task_id``-first order.
    The facade ``generate_*`` methods return typed ``GenerationStatus``
    objects, so no raw positional payload ever reaches this helper.
    """
    if isinstance(result, GenerationStatus):
        return result.task_id
    if isinstance(result, dict):
        return result.get("artifact_id") or result.get("task_id")
    return None


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


def generation_outcome_from_status(status: Any, kind: GenerationKind) -> GenerationOutcome:
    """Map a generation status payload to a semantic outcome."""
    is_complete = hasattr(status, "is_complete") and status.is_complete
    is_failed = hasattr(status, "is_failed") and status.is_failed
    # A ``removed`` status (artifact delisted by the server) is distinct from
    # ``failed`` at the API layer, but the CLI surfaces both as a non-zero-exit
    # error since neither produced a usable artifact.
    is_removed = hasattr(status, "is_removed") and status.is_removed

    if is_failed or is_removed:
        return GenerationOutcome(
            status="failed",
            kind=kind,
            task_id=_extract_task_id(status),
            error=getattr(status, "error", None),
            raw_status=status,
        )

    if is_complete:
        return GenerationOutcome(
            status="completed",
            kind=kind,
            task_id=getattr(status, "task_id", None),
            url=getattr(status, "url", None),
            raw_status=status,
        )

    return GenerationOutcome(
        status="pending",
        kind=kind,
        task_id=_extract_task_id(status),
        raw_status=status,
    )


async def handle_generation_result(
    client: NotebookLMClient,
    notebook_id: str,
    result: Any,
    kind: GenerationKind,
    wait: bool = False,
    timeout: float = 300.0,
    interval: float | None = None,
    wait_context: (
        Callable[[GenerationWaitStarted], AbstractAsyncContextManager[None]] | None
    ) = None,
    wait_start_sink: Callable[[GenerationWaitStarted], None] | None = None,
) -> GenerationOutcome:
    """Handle generation result with optional waiting and typed outcome mapping.

    Consolidates the common pattern across all generate commands:

    - Check for None/failed result
    - Optionally wait for completion
    - Return a typed outcome for the command layer to render

    Args:
        client: The NotebookLM client.
        notebook_id: The notebook ID.
        result: The generation result from artifacts API.
        kind: The generation variant being executed.
        wait: Whether to wait for completion.
        timeout: Timeout forwarded to ``wait_for_completion``. Callers supply
            per-command defaults; media generators use longer budgets while
            generic artifact waits remain at 300s.
        interval: Polling interval in seconds. ``None`` (default) lets
            ``wait_for_completion`` use its built-in default
            (``initial_interval=2.0``); when supplied, the value is forwarded
            as ``initial_interval`` so callers can tighten or loosen the
            cadence.
        wait_context: Optional adapter span receiving a frozen semantic event.
        wait_start_sink: Optional point notification receiving that same event.

    Returns:
        GenerationOutcome describing the final status.
    """
    if result is None:
        return GenerationOutcome(
            status="failed",
            kind=kind,
        )

    # Check for rate limiting (result exists but failed due to rate limit)
    if isinstance(result, GenerationStatus) and result.is_rate_limited:
        return GenerationOutcome(
            status="rate_limited",
            kind=kind,
            task_id=result.task_id,
            error=result.error,
            raw_status=result,
        )

    status: Any = result
    task_id = _extract_generation_task_id(result)

    # Wait for completion if requested
    if wait and task_id:
        event = GenerationWaitStarted(kind=kind, task_id=task_id, elapsed=0.0)
        if wait_start_sink is not None:
            wait_start_sink(event)
        wait_kwargs: dict[str, Any] = {"timeout": timeout}
        if interval is not None:
            wait_kwargs["initial_interval"] = interval
        context = wait_context or _null_wait_context
        async with context(event):
            status = await client.artifacts.wait_for_completion(notebook_id, task_id, **wait_kwargs)

    return generation_outcome_from_status(status, kind)


__all__ = [
    "RETRY_BACKOFF_MULTIPLIER",
    "RETRY_INITIAL_DELAY",
    "RETRY_MAX_DELAY",
    "GenerationOutcome",
    "GenerationWaitStarted",
    "calculate_backoff_delay",
    "generate_with_retry",
    "generation_outcome_from_status",
    "handle_generation_result",
]
