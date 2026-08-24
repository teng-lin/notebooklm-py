"""Public artifact-generation helpers.

The client methods on ``client.artifacts`` raise
:class:`~notebooklm.exceptions.RateLimitError` when Google rejects a
synchronous generation kickoff with a user-displayable rate-limit or quota
error (v0.8.0, #1342). This module provides the same retry policy used by the
CLI — retrying on a raised ``RateLimitError`` — so Python API callers do not
need to duplicate the backoff loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from ._deadline import Monotonic, RuntimeDeadline, Sleep
from .exceptions import (
    ArtifactInProgressTimeoutError,
    ArtifactPendingTimeoutError,
    ArtifactTimeoutError,
    RateLimitError,
)
from .types import GenerationState, GenerationStatus

RATE_LIMIT_RETRY_INITIAL_DELAY = 60.0
RATE_LIMIT_RETRY_MAX_DELAY = 300.0
RATE_LIMIT_RETRY_BACKOFF_MULTIPLIER = 2.0

_GenerationCallable = Callable[[], Awaitable[GenerationStatus | None]]
_RetrySleep = Callable[[float], Awaitable[object]]


@dataclass(frozen=True)
class RateLimitRetryEvent:
    """Details passed to ``with_rate_limit_retry`` retry callbacks.

    ``retry_number`` is the 1-based retry being scheduled.
    ``next_attempt_number`` is the 1-based generation attempt after the
    callback and sleep complete.
    """

    result: GenerationStatus
    next_attempt_number: int
    total_attempts: int
    retry_number: int
    max_retries: int
    delay: float


_RetryCallback = Callable[[RateLimitRetryEvent], object | Awaitable[object]]
_WorkflowGenerate = Callable[[RuntimeDeadline], Awaitable[Any]]
_WorkflowWait = Callable[
    [str, str, RuntimeDeadline, float, float | None],
    Awaitable[Any],
]
_FacadeGenerate = Callable[[], Awaitable[Any]]
_FacadeWait = Callable[..., Awaitable[Any]]
_AwaitableFactory = Callable[[], Awaitable[Any]]
_WaitContext = Callable[[str, str], AbstractAsyncContextManager[None]]


# ``asyncio`` keeps only weak references to tasks. Timeout cleanup must retain
# cancellation-suppressing children until they eventually settle, while still
# returning at the caller's hard deadline.
_DETACHED_TIMEOUT_TASKS: set[asyncio.Future[Any]] = set()


def calculate_backoff_delay(
    attempt: int,
    initial_delay: float = RATE_LIMIT_RETRY_INITIAL_DELAY,
    max_delay: float = RATE_LIMIT_RETRY_MAX_DELAY,
    multiplier: float = RATE_LIMIT_RETRY_BACKOFF_MULTIPLIER,
) -> float:
    """Calculate the capped exponential delay for a retry attempt.

    ``attempt`` is zero-indexed, so ``attempt=0`` yields ``initial_delay``.
    The delay grows by ``multiplier`` until capped at ``max_delay``.
    """
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError("attempt must be a non-negative integer")

    delay = initial_delay * (multiplier**attempt)
    return min(delay, max_delay)


async def _run_rate_limit_retry(
    generate_fn: _GenerationCallable,
    *,
    max_retries: int,
    initial_delay: float = RATE_LIMIT_RETRY_INITIAL_DELAY,
    max_delay: float = RATE_LIMIT_RETRY_MAX_DELAY,
    multiplier: float = RATE_LIMIT_RETRY_BACKOFF_MULTIPLIER,
    sleep: _RetrySleep | None = None,
    on_retry: _RetryCallback | None = None,
) -> GenerationStatus | None:
    """Run the shared artifact-generation retry loop.

    The public :func:`with_rate_limit_retry` helper and the private
    :class:`~notebooklm._artifacts.ArtifactsAPI` workflow entry both delegate
    here.  Keeping the loop in one primitive preserves the public helper while
    ensuring internal ``_app`` orchestration no longer wraps a facade call in a
    second execution authority.

    The callable is always invoked at least once. A retry is scheduled only
    when an attempt raises :class:`~notebooklm.exceptions.RateLimitError` — the
    ADR-0019 "async kickoff" contract where a synchronous rate-limit refusal
    propagates as an exception (v0.8.0, #1342). A *returned* ``GenerationStatus``
    — including one whose ``is_rate_limited`` property is true — is no longer a
    retry signal and is returned immediately.

    Successful statuses, non-rate-limit failures, returned rate-limited statuses,
    and ``None`` return immediately. Non-``RateLimitError`` exceptions propagate
    unchanged.

    When the retry budget is exhausted, the final attempt's ``RateLimitError``
    is re-raised.

    Args:
        generate_fn: Async callable that starts an artifact-generation request.
        max_retries: Number of retries after the initial attempt.
        initial_delay: Delay before the first retry, in seconds.
        max_delay: Maximum delay cap, in seconds.
        multiplier: Exponential backoff multiplier.
        sleep: Async sleep function. Defaults to ``asyncio.sleep``.
        on_retry: Optional callback invoked before each retry sleep. The
            event's ``result`` is a synthesized
            ``GenerationStatus(status="failed", error_code="USER_DISPLAYABLE_ERROR")``
            standing in for the caught ``RateLimitError`` so the callback shape
            is uniform.

    Returns:
        The first returned result (the callable may still return ``None``).

    Raises:
        ValueError: If retry or delay parameters are invalid.
        RateLimitError: When the retry budget is exhausted (the final attempt's
            ``RateLimitError`` is re-raised).
    """
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    if initial_delay < 0:
        raise ValueError("initial_delay must be non-negative")
    if max_delay < 0:
        raise ValueError("max_delay must be non-negative")
    if multiplier <= 0:
        raise ValueError("multiplier must be positive")

    sleep_func = asyncio.sleep if sleep is None else sleep

    attempt = 0
    while True:
        # ``event_result`` carries the rate-limited GenerationStatus passed to
        # ``on_retry``. It is synthesized from the caught ``RateLimitError`` so
        # the callback shape is uniform — a *returned* status is never a retry
        # signal (v0.8.0, #1342).
        try:
            result = await generate_fn()
        except RateLimitError as exc:
            if attempt >= max_retries:
                raise
            # This branch is reached only because a ``RateLimitError`` was
            # caught, so the synthesized status must read as rate-limited for
            # ``on_retry`` consumers (``event.result.is_rate_limited``). Fall
            # back to the ``USER_DISPLAYABLE_ERROR`` sentinel when the exception
            # carries no ``rpc_code`` rather than dropping ``error_code`` to
            # ``None`` (which would force brittle message-substring matching).
            event_result = GenerationStatus(
                task_id="",
                status=GenerationState.FAILED,
                error=str(exc),
                error_code=(
                    str(exc.rpc_code) if exc.rpc_code is not None else "USER_DISPLAYABLE_ERROR"
                ),
            )
        else:
            # Any returned result (success, non-rate-limit failure, a returned
            # rate-limited status, or ``None``) returns immediately — only a
            # raised ``RateLimitError`` drives a retry (#1342).
            return result

        delay = calculate_backoff_delay(
            attempt,
            initial_delay=initial_delay,
            max_delay=max_delay,
            multiplier=multiplier,
        )
        if on_retry is not None:
            event = RateLimitRetryEvent(
                result=event_result,
                next_attempt_number=attempt + 2,
                total_attempts=max_retries + 1,
                retry_number=attempt + 1,
                max_retries=max_retries,
                delay=delay,
            )
            callback_result = on_retry(event)
            if inspect.isawaitable(callback_result):
                await callback_result
        await sleep_func(delay)
        attempt += 1


def _generation_task_id(result: Any) -> str | None:
    """Return the compatibility task id used by optional workflow polling."""
    if isinstance(result, dict):
        return result.get("artifact_id") or result.get("task_id")
    task_id = getattr(result, "task_id", None)
    return task_id if isinstance(task_id, str) and task_id else None


def _generation_status(result: Any) -> str | None:
    """Return a normalized kickoff status for caller-local timeout context."""
    raw_status = (
        result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
    )
    value = getattr(raw_status, "value", raw_status)
    if not isinstance(value, str):
        return None
    if value == "processing":
        return "in_progress"
    return value


def _caller_artifact_timeout_error(
    notebook_id: str,
    task_id: str,
    timeout: float,
    kickoff_result: Any,
    observed_transitions: Sequence[GenerationStatus],
) -> ArtifactTimeoutError:
    """Build a typed timeout when this caller expires before a shared poll."""
    observed_history = tuple(
        status
        for transition in observed_transitions
        if (status := _generation_status(transition)) is not None
    )
    kickoff_status = _generation_status(kickoff_result)
    last_status = next(reversed(observed_history), kickoff_status)
    history = observed_history or ((kickoff_status,) if kickoff_status is not None else ())
    error_type = (
        ArtifactInProgressTimeoutError if "in_progress" in history else ArtifactPendingTimeoutError
    )
    return error_type(
        notebook_id,
        task_id,
        timeout,
        last_status=last_status,
        status_history=history,
        status_transitions=observed_transitions,
    )


@contextlib.asynccontextmanager
async def _null_wait_context(_message: str, _resume_hint: str) -> AsyncIterator[None]:
    yield


async def _run_deadline_generation_workflow(
    generate_fn: _WorkflowGenerate,
    wait_fn: _WorkflowWait,
    *,
    notebook_id: str,
    timeout: float,
    max_retries: int,
    wait: bool,
    artifact_type: str,
    wait_message: str,
    initial_interval: float | None = None,
    on_retry: _RetryCallback | None = None,
    on_wait_start: Callable[[str], None] | None = None,
    wait_context: _WaitContext | None = None,
    sleep: Sleep | None = None,
    monotonic: Monotonic | None = None,
) -> Any:
    """Own one absolute deadline for kickoff retry and optional polling."""
    resolved_monotonic = monotonic or asyncio.get_running_loop().time
    resolved_sleep = sleep or asyncio.sleep
    deadline = RuntimeDeadline.start(timeout, monotonic=resolved_monotonic)

    async def _generate() -> Any:
        if deadline.expired():
            raise TimeoutError(deadline.timeout_message(f"{artifact_type} generation"))
        return await generate_fn(deadline)

    async def _sleep(delay: float) -> None:
        bounded = deadline.clamp_sleep(delay)
        if delay > 0.0 and bounded <= 0.0:
            raise TimeoutError(deadline.timeout_message(f"{artifact_type} generation"))
        await resolved_sleep(bounded)
        if deadline.expired():
            raise TimeoutError(deadline.timeout_message(f"{artifact_type} generation"))

    result = await _run_rate_limit_retry(
        _generate,
        max_retries=max_retries,
        sleep=_sleep,
        on_retry=on_retry,
    )
    task_id = _generation_task_id(result)
    if not wait or result is None or task_id is None:
        return result

    if on_wait_start is not None:
        on_wait_start(task_id)
    context = wait_context or _null_wait_context
    async with context(wait_message, f"notebooklm artifact poll {task_id}"):
        return await wait_fn(notebook_id, task_id, deadline, timeout, initial_interval)


async def _await_with_deadline(
    awaitable_factory: _AwaitableFactory,
    deadline: RuntimeDeadline,
    artifact_type: str,
) -> Any:
    remaining = deadline.remaining()
    if remaining <= 0.0:
        raise TimeoutError(deadline.timeout_message(f"{artifact_type} generation"))
    completed, result = await _await_before_timeout(awaitable_factory(), remaining)
    if completed:
        return result
    raise TimeoutError(deadline.timeout_message(f"{artifact_type} generation"))


async def _await_before_timeout(awaitable: Awaitable[Any], timeout: float) -> tuple[bool, Any]:
    """Distinguish a child result from this caller's own hard timeout boundary.

    Cancellation is requested but deliberately not awaited: a custom child
    that suppresses ``CancelledError`` must not hold this caller open past its
    aggregate budget. Its eventual exception is still consumed by a callback.
    """
    task = asyncio.ensure_future(awaitable)

    def _cancel_without_wait() -> None:
        def _consume_result(done_task: asyncio.Future[Any]) -> None:
            try:
                if not done_task.cancelled():
                    done_task.exception()
            finally:
                _DETACHED_TIMEOUT_TASKS.discard(done_task)

        _DETACHED_TIMEOUT_TASKS.add(task)
        task.add_done_callback(_consume_result)
        task.cancel()

    try:
        done, _pending = await asyncio.wait((task,), timeout=timeout)
    except BaseException:
        _cancel_without_wait()
        raise
    if task in done:
        return True, task.result()
    _cancel_without_wait()
    return False, None


async def _run_generation_workflow(
    generate_fn: _FacadeGenerate,
    wait_for_completion: _FacadeWait | None,
    *,
    notebook_id: str,
    timeout: float,
    max_retries: int,
    wait: bool,
    artifact_type: str,
    wait_message: str,
    initial_interval: float | None = None,
    on_retry: _RetryCallback | None = None,
    on_wait_start: Callable[[str], None] | None = None,
    wait_context: _WaitContext | None = None,
) -> Any:
    """Adapt public facade callables into the single private workflow owner.

    This entry is intentionally underscore-private and absent from ``__all__``.
    ``_app`` supplies already-resolved public namespace methods, preserving the
    established adapter monkeypatch seam without owning retry, clocks, or wait
    dispatch itself.
    """

    kickoff_result: Any = None
    observed_transitions: list[GenerationStatus] = []

    async def _generate(deadline: RuntimeDeadline) -> Any:
        nonlocal kickoff_result
        kickoff_result = await _await_with_deadline(generate_fn, deadline, artifact_type)
        return kickoff_result

    async def _wait(
        resolved_notebook_id: str,
        task_id: str,
        deadline: RuntimeDeadline,
        caller_timeout: float,
        caller_interval: float | None,
    ) -> Any:
        if wait_for_completion is None:
            raise RuntimeError("artifact wait callable is required when wait=True")
        # The public waiter owns its typed poll timeout and shielded shared
        # poll, but a follower deliberately inherits the leader's poll budget.
        # Keep this workflow's own hard budget as well so a slow poll RPC or a
        # longer-lived leader cannot overrun the generate caller. Preserve an
        # inner typed timeout; translate only this caller-local expiry.
        remaining = deadline.remaining()
        if remaining <= 0.0:
            raise _caller_artifact_timeout_error(
                resolved_notebook_id,
                task_id,
                caller_timeout,
                kickoff_result,
                observed_transitions,
            )
        wait_kwargs: dict[str, Any] = {"timeout": remaining}
        if caller_interval is not None:
            wait_kwargs["initial_interval"] = caller_interval
        wait_kwargs["on_status_change"] = observed_transitions.append
        completed, result = await _await_before_timeout(
            wait_for_completion(resolved_notebook_id, task_id, **wait_kwargs),
            remaining,
        )
        if completed:
            return result
        raise _caller_artifact_timeout_error(
            resolved_notebook_id,
            task_id,
            caller_timeout,
            kickoff_result,
            observed_transitions,
        )

    return await _run_deadline_generation_workflow(
        _generate,
        _wait,
        notebook_id=notebook_id,
        timeout=timeout,
        max_retries=max_retries,
        wait=wait,
        artifact_type=artifact_type,
        wait_message=wait_message,
        initial_interval=initial_interval,
        on_retry=on_retry,
        on_wait_start=on_wait_start,
        wait_context=wait_context,
    )


async def with_rate_limit_retry(
    generate_fn: _GenerationCallable,
    *,
    max_retries: int,
    initial_delay: float = RATE_LIMIT_RETRY_INITIAL_DELAY,
    max_delay: float = RATE_LIMIT_RETRY_MAX_DELAY,
    multiplier: float = RATE_LIMIT_RETRY_BACKOFF_MULTIPLIER,
    sleep: _RetrySleep | None = None,
    on_retry: _RetryCallback | None = None,
) -> GenerationStatus | None:
    """Run an artifact-generation callable with rate-limit retry.

    This remains the supported standalone helper for Python callers. Internal
    application workflows enter through ``ArtifactsAPI`` so their retry sleep,
    semantic kickoff, and optional wait share one private absolute deadline.
    """
    return await _run_rate_limit_retry(
        generate_fn,
        max_retries=max_retries,
        initial_delay=initial_delay,
        max_delay=max_delay,
        multiplier=multiplier,
        sleep=sleep,
        on_retry=on_retry,
    )


__all__ = [
    "RATE_LIMIT_RETRY_BACKOFF_MULTIPLIER",
    "RATE_LIMIT_RETRY_INITIAL_DELAY",
    "RATE_LIMIT_RETRY_MAX_DELAY",
    "RateLimitRetryEvent",
    "calculate_backoff_delay",
    "with_rate_limit_retry",
]
