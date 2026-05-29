"""Public artifact-generation helpers.

The client methods on ``client.artifacts`` return ``GenerationStatus`` objects
directly when Google rejects generation with a user-displayable rate-limit or
quota error. This module provides the same retry policy used by the CLI so
Python API callers do not need to duplicate the backoff loop.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .types import GenerationStatus

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

    The callable is always invoked at least once. Retries happen only when the
    result is a ``GenerationStatus`` whose ``is_rate_limited`` property is
    true. Successful statuses, non-rate-limit failures, and ``None`` return
    immediately.

    Args:
        generate_fn: Async callable that starts an artifact-generation request.
        max_retries: Number of retries after the initial attempt.
        initial_delay: Delay before the first retry, in seconds.
        max_delay: Maximum delay cap, in seconds.
        multiplier: Exponential backoff multiplier.
        sleep: Async sleep function. Defaults to ``asyncio.sleep``.
        on_retry: Optional callback invoked before each retry sleep.

    Returns:
        The first non-rate-limited result, or the final rate-limited result
        when the retry budget is exhausted.

    Raises:
        ValueError: If retry or delay parameters are invalid.
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
        result = await generate_fn()

        if not isinstance(result, GenerationStatus) or not result.is_rate_limited:
            return result

        if attempt >= max_retries:
            return result

        delay = calculate_backoff_delay(
            attempt,
            initial_delay=initial_delay,
            max_delay=max_delay,
            multiplier=multiplier,
        )
        if on_retry is not None:
            event = RateLimitRetryEvent(
                result=result,
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


__all__ = [
    "RATE_LIMIT_RETRY_BACKOFF_MULTIPLIER",
    "RATE_LIMIT_RETRY_INITIAL_DELAY",
    "RATE_LIMIT_RETRY_MAX_DELAY",
    "RateLimitRetryEvent",
    "calculate_backoff_delay",
    "with_rate_limit_retry",
]
