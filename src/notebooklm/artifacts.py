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

from .exceptions import RateLimitError
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

    The callable is always invoked at least once. A retry is scheduled when an
    attempt signals a rate limit in **either** of these two ways:

    * it returns a ``GenerationStatus`` whose ``is_rate_limited`` property is
      true (the legacy ``generate_*`` path, which swallows a rate-limit refusal
      into ``status="failed"`` until v0.8.0); **or**
    * it raises :class:`~notebooklm.exceptions.RateLimitError` (the ADR-0019
      "async kickoff" path used by ``retry_failed``, where a synchronous
      refusal propagates as an exception rather than a returned status).

    Successful statuses, non-rate-limit failures, and ``None`` return
    immediately. Non-``RateLimitError`` exceptions propagate unchanged.

    When the retry budget is exhausted, the final outcome is surfaced the same
    way it arrived: a returned rate-limited ``GenerationStatus`` is returned,
    and a raised ``RateLimitError`` is re-raised. (Removing the returned-status
    path is the v0.8.0 break — not done here.)

    Args:
        generate_fn: Async callable that starts an artifact-generation request.
        max_retries: Number of retries after the initial attempt.
        initial_delay: Delay before the first retry, in seconds.
        max_delay: Maximum delay cap, in seconds.
        multiplier: Exponential backoff multiplier.
        sleep: Async sleep function. Defaults to ``asyncio.sleep``.
        on_retry: Optional callback invoked before each retry sleep. The
            event's ``result`` is the returned rate-limited ``GenerationStatus``
            for the returned-status path, or a synthesized
            ``GenerationStatus(status="failed", error_code="USER_DISPLAYABLE_ERROR")``
            standing in for the raised ``RateLimitError`` so the callback shape
            is uniform.

    Returns:
        The first non-rate-limited result, or the final rate-limited
        ``GenerationStatus`` when the retry budget is exhausted on the
        returned-status path.

    Raises:
        ValueError: If retry or delay parameters are invalid.
        RateLimitError: When the retry budget is exhausted on the raised-error
            path (the final attempt's ``RateLimitError`` is re-raised).
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
        # ``on_retry``. For the raised-error path it is synthesized from the
        # caught exception so the callback shape stays uniform across both
        # rate-limit signals.
        event_result: GenerationStatus
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
                status="failed",
                error=str(exc),
                error_code=(
                    str(exc.rpc_code) if exc.rpc_code is not None else "USER_DISPLAYABLE_ERROR"
                ),
            )
        else:
            if not isinstance(result, GenerationStatus) or not result.is_rate_limited:
                return result
            if attempt >= max_retries:
                return result
            event_result = result

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


__all__ = [
    "RATE_LIMIT_RETRY_BACKOFF_MULTIPLIER",
    "RATE_LIMIT_RETRY_INITIAL_DELAY",
    "RATE_LIMIT_RETRY_MAX_DELAY",
    "RateLimitRetryEvent",
    "calculate_backoff_delay",
    "with_rate_limit_retry",
]
