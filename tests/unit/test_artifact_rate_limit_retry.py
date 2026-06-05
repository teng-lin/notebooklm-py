"""Unit tests for public artifact-generation rate-limit retry helpers."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from notebooklm.artifacts import (
    RATE_LIMIT_RETRY_MAX_DELAY,
    RateLimitRetryEvent,
    calculate_backoff_delay,
    with_rate_limit_retry,
)
from notebooklm.exceptions import RateLimitError, RPCError
from notebooklm.types import GenerationStatus


def _rate_limited_status() -> GenerationStatus:
    return GenerationStatus(
        task_id="",
        status="failed",
        error="Rate limited",
        error_code="USER_DISPLAYABLE_ERROR",
    )


class TestCalculateBackoffDelay:
    def test_exponential_backoff_with_cap(self) -> None:
        assert calculate_backoff_delay(0, initial_delay=60.0) == 60.0
        assert calculate_backoff_delay(1, initial_delay=60.0) == 120.0
        assert calculate_backoff_delay(2, initial_delay=60.0) == 240.0
        assert (
            calculate_backoff_delay(10, initial_delay=60.0, max_delay=RATE_LIMIT_RETRY_MAX_DELAY)
            == RATE_LIMIT_RETRY_MAX_DELAY
        )

    def test_custom_multiplier(self) -> None:
        assert calculate_backoff_delay(1, initial_delay=10.0, multiplier=3.0) == 30.0

    @pytest.mark.parametrize("attempt", [-1, 1.5, True])
    def test_rejects_invalid_attempt(self, attempt: Any) -> None:
        with pytest.raises(ValueError, match="attempt must be a non-negative integer"):
            calculate_backoff_delay(attempt)


class TestWithRateLimitRetry:
    @pytest.mark.asyncio
    async def test_returns_success_without_retry(self) -> None:
        success = GenerationStatus(task_id="task_123", status="pending")
        generate_fn = AsyncMock(return_value=success)

        result = await with_rate_limit_retry(generate_fn, max_retries=3)

        assert result == success
        assert generate_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_returned_rate_limited_status_returns_immediately(self) -> None:
        # v0.8.0 (#1342): a *returned* rate-limited status is no longer a retry
        # signal — only a raised RateLimitError drives a retry. The returned
        # status is surfaced immediately, with no sleep and no on_retry event.
        rate_limited = _rate_limited_status()
        generate_fn = AsyncMock(return_value=rate_limited)
        events: list[RateLimitRetryEvent] = []

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_rate_limit_retry(
                generate_fn,
                max_retries=3,
                on_retry=events.append,
            )

        assert result == rate_limited
        assert generate_fn.call_count == 1
        mock_sleep.assert_not_awaited()
        assert events == []

    @pytest.mark.asyncio
    async def test_does_not_retry_non_rate_limit_failure(self) -> None:
        failed = GenerationStatus(task_id="", status="failed", error="Bad request")
        generate_fn = AsyncMock(return_value=failed)

        result = await with_rate_limit_retry(generate_fn, max_retries=3)

        assert result == failed
        assert generate_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_supports_async_retry_callback_and_custom_sleep(self) -> None:
        # The retry is driven by a raised RateLimitError (#1342); a custom sleep
        # and an async on_retry callback are both honored.
        success = GenerationStatus(task_id="task_123", status="pending")
        generate_fn = AsyncMock(
            side_effect=[
                RateLimitError("Rate limited", rpc_code="USER_DISPLAYABLE_ERROR"),
                success,
            ]
        )
        on_retry = AsyncMock()
        sleep = AsyncMock()

        result = await with_rate_limit_retry(
            generate_fn,
            max_retries=1,
            initial_delay=2.0,
            sleep=sleep,
            on_retry=on_retry,
        )

        assert result == success
        sleep.assert_awaited_once_with(2.0)
        on_retry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retries_raised_rate_limit_error_then_returns_success(self) -> None:
        # The ADR-0019 "async kickoff" path (e.g. ``retry_failed``) raises
        # RateLimitError on a synchronous refusal rather than returning a
        # rate-limited status; the helper must back off and retry that too.
        success = GenerationStatus(task_id="task_123", status="in_progress")
        generate_fn = AsyncMock(
            side_effect=[
                RateLimitError("Rate limited", rpc_code="USER_DISPLAYABLE_ERROR"),
                success,
            ]
        )
        events: list[RateLimitRetryEvent] = []

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await with_rate_limit_retry(
                generate_fn,
                max_retries=3,
                on_retry=events.append,
            )

        assert result == success
        assert generate_fn.call_count == 2
        mock_sleep.assert_awaited_once_with(60.0)
        # The callback event carries a synthesized rate-limited status so the
        # callback shape is uniform across the returned-status and raised paths.
        assert len(events) == 1
        assert events[0].result.error_code == "USER_DISPLAYABLE_ERROR"
        assert events[0].result.error == "Rate limited"
        assert events[0].retry_number == 1

    @pytest.mark.asyncio
    async def test_synthesized_event_is_rate_limited_without_rpc_code(self) -> None:
        # A RateLimitError with no rpc_code must still produce a callback event
        # whose result reads as rate-limited (uniform-callback contract), so we
        # don't fall back to brittle message-substring matching.
        success = GenerationStatus(task_id="task_123", status="in_progress")
        generate_fn = AsyncMock(side_effect=[RateLimitError("429 from gateway"), success])
        events: list[RateLimitRetryEvent] = []

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await with_rate_limit_retry(generate_fn, max_retries=2, on_retry=events.append)

        assert result == success
        assert len(events) == 1
        assert events[0].result.error_code == "USER_DISPLAYABLE_ERROR"
        assert events[0].result.is_rate_limited is True

    @pytest.mark.asyncio
    async def test_reraises_rate_limit_error_when_budget_exhausted(self) -> None:
        error = RateLimitError("Rate limited", rpc_code="USER_DISPLAYABLE_ERROR")
        generate_fn = AsyncMock(side_effect=error)

        with (
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(RateLimitError) as exc_info,
        ):
            await with_rate_limit_retry(generate_fn, max_retries=2)

        assert exc_info.value is error
        assert generate_fn.call_count == 3
        assert [call.args[0] for call in mock_sleep.await_args_list] == [60.0, 120.0]

    @pytest.mark.asyncio
    async def test_does_not_retry_non_rate_limit_exception(self) -> None:
        # A non-RateLimitError refusal (e.g. a plain RPCError) propagates
        # immediately without consuming the retry budget.
        error = RPCError("Not retryable", rpc_code="USER_DISPLAYABLE_ERROR")
        generate_fn = AsyncMock(side_effect=error)

        with pytest.raises(RPCError) as exc_info:
            await with_rate_limit_retry(generate_fn, max_retries=3)

        assert exc_info.value is error
        assert generate_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_validates_retry_parameters(self) -> None:
        generate_fn = AsyncMock()

        with pytest.raises(ValueError, match="max_retries"):
            await with_rate_limit_retry(generate_fn, max_retries=-1)
        with pytest.raises(ValueError, match="initial_delay"):
            await with_rate_limit_retry(generate_fn, max_retries=0, initial_delay=-1.0)
        with pytest.raises(ValueError, match="max_delay"):
            await with_rate_limit_retry(generate_fn, max_retries=0, max_delay=-1.0)
        with pytest.raises(ValueError, match="multiplier"):
            await with_rate_limit_retry(generate_fn, max_retries=0, multiplier=0.0)
