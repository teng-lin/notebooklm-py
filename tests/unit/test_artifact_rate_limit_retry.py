"""Unit tests for public artifact-generation rate-limit retry helpers."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from notebooklm._idempotency import (
    bound_operation_journal_entries,
    call_unconfirmed_on_transport_loss,
)
from notebooklm._web.artifact.generation import ArtifactGenerationService
from notebooklm._web.wire.decoder import extract_rpc_result
from notebooklm.artifacts import (
    RATE_LIMIT_RETRY_MAX_DELAY,
    RateLimitRetryEvent,
    calculate_backoff_delay,
    with_rate_limit_retry,
)
from notebooklm.exceptions import RateLimitError, RPCError
from notebooklm.outcomes import CommitState, RecoveryAction
from notebooklm.rpc import RPCMethod
from notebooklm.types import GenerationStatus
from tests._fixtures.rpc_error_frames import user_displayable_rejection_chunks


def _rate_limited_status() -> GenerationStatus:
    return GenerationStatus(
        task_id="",
        status="failed",
        error="Rate limited",
        error_code="USER_DISPLAYABLE_ERROR",
    )


def _decoded_refusal() -> RateLimitError:
    with pytest.raises(RateLimitError) as captured:
        extract_rpc_result(
            user_displayable_rejection_chunks(RPCMethod.CREATE_ARTIFACT.value),
            RPCMethod.CREATE_ARTIFACT.value,
        )
    return captured.value


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
    async def test_e4_unconfirmed_rate_limit_is_not_replayed_by_outer_helper(self) -> None:
        """A fake transport's unknown-commit verdict must survive outer orchestration."""
        error = RateLimitError("response lost after artifact create dispatch")
        transport_calls = 0

        async def fake_transport() -> GenerationStatus:
            nonlocal transport_calls
            transport_calls += 1

            async def dispatched_write() -> GenerationStatus:
                raise error

            return await call_unconfirmed_on_transport_loss(
                dispatched_write,
                method="fake.CreateArtifact",
                what="the fake artifact create",
            )

        sleep = AsyncMock()
        with pytest.raises(RateLimitError) as raised:
            await with_rate_limit_retry(fake_transport, max_retries=1, sleep=sleep)

        assert raised.value is error
        assert getattr(error, "unconfirmed", False) is True
        assert transport_calls == 1
        sleep.assert_not_awaited()

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
                _decoded_refusal(),
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
                _decoded_refusal(),
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
        assert "Resource exhausted" in (events[0].result.error or "")
        assert events[0].retry_number == 1

    @pytest.mark.asyncio
    async def test_naked_gateway_rate_limit_is_not_replayed(self) -> None:
        error = RateLimitError("429 from gateway")
        generate_fn = AsyncMock(side_effect=error)
        events: list[RateLimitRetryEvent] = []

        with (
            patch("asyncio.sleep", new_callable=AsyncMock) as sleep,
            pytest.raises(RateLimitError) as captured,
        ):
            await with_rate_limit_retry(generate_fn, max_retries=2, on_retry=events.append)

        assert captured.value is error
        assert generate_fn.await_count == 1
        sleep.assert_not_awaited()
        assert events == []

    @pytest.mark.asyncio
    async def test_reraises_rate_limit_error_when_budget_exhausted(self) -> None:
        error = _decoded_refusal()
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
    async def test_nested_confirmed_generation_is_not_replayed_by_outer_helper(self) -> None:
        class ScriptedRpc:
            def __init__(self) -> None:
                self.results: list[Any] = [
                    [["artifact-a", None, None, None, 2]],
                    _decoded_refusal(),
                ]
                self.calls = 0

            async def rpc_call(self, method: Any, params: Any, **kwargs: Any) -> Any:
                del method, params
                self.calls += 1
                (entry,) = bound_operation_journal_entries()
                entry.mark_dispatched()
                result = self.results.pop(0)
                if isinstance(result, BaseException):
                    entry.record(result.commit_state, "decoded refusal")
                    raise result
                return result

        rpc = ScriptedRpc()
        service = ArtifactGenerationService(
            rpc=rpc,
            notebooks=AsyncMock(),
            note_service=AsyncMock(),
        )

        async def outer_generation() -> GenerationStatus:
            confirmed = await with_rate_limit_retry(
                lambda: service.generate_audio("nb-a", source_ids=["source-a"]),
                max_retries=1,
                sleep=AsyncMock(),
            )
            assert confirmed.task_id == "artifact-a"
            return await service.generate_audio("nb-b", source_ids=["source-b"])

        with pytest.raises(RateLimitError) as captured:
            await with_rate_limit_retry(outer_generation, max_retries=2, sleep=AsyncMock())

        assert rpc.calls == 2
        assert captured.value.commit_state is CommitState.CONFIRMED
        assert captured.value.operation_metadata.known_resource_ids == ("artifact-a",)
        assert (
            captured.value.operation_metadata.recovery_action
            is RecoveryAction.INSPECT_AND_RECONCILE
        )

    @pytest.mark.asyncio
    async def test_inner_exhaustion_does_not_restart_from_outer_retry_budget(self) -> None:
        class RefusingRpc:
            def __init__(self) -> None:
                self.calls = 0

            async def rpc_call(self, method: Any, params: Any, **kwargs: Any) -> Any:
                del method, params
                self.calls += 1
                (entry,) = bound_operation_journal_entries()
                entry.mark_dispatched()
                error = _decoded_refusal()
                entry.record(CommitState.REJECTED, "decoded refusal")
                raise error

        rpc = RefusingRpc()
        service = ArtifactGenerationService(
            rpc=rpc,
            notebooks=AsyncMock(),
            note_service=AsyncMock(),
        )

        async def outer_generation() -> GenerationStatus:
            return await with_rate_limit_retry(
                lambda: service.generate_audio("nb-a", source_ids=["source-a"]),
                max_retries=1,
                sleep=AsyncMock(),
            )

        with pytest.raises(RateLimitError):
            await with_rate_limit_retry(outer_generation, max_retries=3, sleep=AsyncMock())

        assert rpc.calls == 2

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
