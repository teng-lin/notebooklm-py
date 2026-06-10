"""Unit tests for the transport-neutral ``notebooklm._app.generate_retry`` core.

These pin the relocated generate retry/wait business logic at the ``_app``
boundary (independent of the Click adapter):

* :func:`calculate_backoff_delay` exponential-backoff math and
  :func:`generate_with_retry` retry-on-rate-limit loop — moved from the former
  ``tests/unit/cli/test_generate.py`` ``TestCalculateBackoffDelay`` /
  ``TestGenerateWithRetry`` / ``TestExtractTaskIdDirect`` /
  ``TestGenerateWithRetryConsoleOutput`` classes (they already called the
  function directly through the ``_app.generate_retry`` core);
* the ``_format_status_message`` spinner-line formatter;
* net-new direct coverage for :func:`generation_outcome_from_status` outcome
  classification and :func:`handle_generation_result` (None / rate-limited /
  wait-path / task-id extraction precedence) against a ``MagicMock`` client.

No Click / ``CliRunner`` — every test calls the ``_app`` function directly. The
CLI ``--json`` / console-rendering assertions stay in
``tests/unit/cli/test_generate.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._app.generate_retry import (
    RETRY_MAX_DELAY,
    GenerationOutcome,
    _extract_generation_task_id,
    _extract_task_id,
    _format_status_message,
    calculate_backoff_delay,
    generate_with_retry,
    generation_outcome_from_status,
    handle_generation_result,
)
from notebooklm.exceptions import RateLimitError
from notebooklm.types import GenerationStatus

# ---------------------------------------------------------------------------
# calculate_backoff_delay — exponential backoff math (moved, pure).
# ---------------------------------------------------------------------------


class TestCalculateBackoffDelay:
    """Tests for the calculate_backoff_delay helper function."""

    def test_initial_delay(self):
        """Test that first attempt uses initial delay."""
        delay = calculate_backoff_delay(0, initial_delay=60.0)
        assert delay == 60.0

    def test_exponential_backoff(self):
        """Test that delay increases exponentially."""
        assert calculate_backoff_delay(0, initial_delay=60.0) == 60.0
        assert calculate_backoff_delay(1, initial_delay=60.0) == 120.0
        assert calculate_backoff_delay(2, initial_delay=60.0) == 240.0

    def test_max_delay_cap(self):
        """Test that delay is capped at max_delay."""
        delay = calculate_backoff_delay(10, initial_delay=60.0, max_delay=300.0)
        assert delay == 300.0

    def test_custom_multiplier(self):
        """Test custom backoff multiplier."""
        delay = calculate_backoff_delay(1, initial_delay=10.0, multiplier=3.0)
        assert delay == 30.0


# ---------------------------------------------------------------------------
# generate_with_retry — retry-on-rate-limit loop (moved, pure).
# ---------------------------------------------------------------------------


class TestGenerateWithRetry:
    """Tests for the generate_with_retry helper function."""

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self):
        """Test that successful generation doesn't trigger retry."""
        success_result = GenerationStatus(
            task_id="task_123", status="pending", error=None, error_code=None
        )
        generate_fn = AsyncMock(return_value=success_result)

        result = await generate_with_retry(generate_fn, max_retries=3, artifact_type="audio")

        assert result == success_result
        assert generate_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_rate_limit(self):
        """Test that a raised RateLimitError triggers retry (#1342)."""
        success_result = GenerationStatus(
            task_id="task_123", status="pending", error=None, error_code=None
        )
        generate_fn = AsyncMock(
            side_effect=[
                RateLimitError("Rate limited", rpc_code="USER_DISPLAYABLE_ERROR"),
                success_result,
            ]
        )

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await generate_with_retry(generate_fn, max_retries=3, artifact_type="audio")

        assert result == success_result
        assert generate_fn.call_count == 2
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_exhausted_reraises(self):
        """v0.8.0 (#1342): exhausting the budget re-raises the RateLimitError."""
        error = RateLimitError("Rate limited", rpc_code="USER_DISPLAYABLE_ERROR")
        generate_fn = AsyncMock(side_effect=error)

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(RateLimitError) as exc_info,
        ):
            await generate_with_retry(generate_fn, max_retries=2, artifact_type="audio")

        assert exc_info.value is error
        assert generate_fn.call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_returned_rate_limited_status_returns_without_retry(self):
        """v0.8.0 (#1342): a returned rate-limited status is no longer a retry signal."""
        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        generate_fn = AsyncMock(return_value=rate_limited)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await generate_with_retry(generate_fn, max_retries=3, artifact_type="audio")

        assert result == rate_limited
        assert generate_fn.call_count == 1
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_retry_when_max_retries_zero(self):
        """Test that max_retries=0 means no retry attempts (re-raises immediately)."""
        error = RateLimitError("Rate limited", rpc_code="USER_DISPLAYABLE_ERROR")
        generate_fn = AsyncMock(side_effect=error)

        with pytest.raises(RateLimitError):
            await generate_with_retry(generate_fn, max_retries=0, artifact_type="audio")

        assert generate_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_delays_increase_exponentially(self):
        """Verify delays follow exponential backoff pattern (60s, 120s, 240s)."""
        error = RateLimitError("Rate limited", rpc_code="USER_DISPLAYABLE_ERROR")
        generate_fn = AsyncMock(side_effect=error)

        with (
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(RateLimitError),
        ):
            await generate_with_retry(generate_fn, max_retries=3, artifact_type="audio")

        # Verify delays: 60s, 120s, 240s (3 retries = 3 sleeps)
        delays = [call[0][0] for call in mock_sleep.call_args_list]
        assert delays == [60.0, 120.0, 240.0]

    @pytest.mark.asyncio
    async def test_retry_delay_caps_at_max(self):
        """Verify delay caps at 300s even with many retries."""
        error = RateLimitError("Rate limited", rpc_code="USER_DISPLAYABLE_ERROR")
        generate_fn = AsyncMock(side_effect=error)

        with (
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(RateLimitError),
        ):
            await generate_with_retry(generate_fn, max_retries=10, artifact_type="audio")

        # Verify no delay exceeds RETRY_MAX_DELAY (300s)
        delays = [call[0][0] for call in mock_sleep.call_args_list]
        assert len(delays) == 10  # 10 retries = 10 sleeps
        for delay in delays:
            assert delay <= RETRY_MAX_DELAY
        # Later delays should be capped at 300
        assert delays[-1] == RETRY_MAX_DELAY

    @pytest.mark.asyncio
    async def test_retry_fires_on_retry_sink(self):
        """The ``on_retry`` callback is invoked once per retry (moved from the
        former CLI console-output test, retargeted at the injected sink)."""
        success_result = GenerationStatus(
            task_id="task_123", status="pending", error=None, error_code=None
        )
        generate_fn = AsyncMock(
            side_effect=[
                RateLimitError("Rate limited", rpc_code="USER_DISPLAYABLE_ERROR"),
                success_result,
            ]
        )
        retry_sink = MagicMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await generate_with_retry(
                generate_fn,
                max_retries=1,
                artifact_type="audio",
                on_retry=retry_sink,
            )

        assert result == success_result
        retry_sink.assert_called_once()


# ---------------------------------------------------------------------------
# Task-id extraction (moved from TestExtractTaskIdDirect, pure).
# ---------------------------------------------------------------------------


class TestExtractTaskId:
    """Direct tests for _extract_task_id() covering object/dict paths.

    The raw positional-list path is gone: the facade ``generate_*`` /
    ``wait_for_completion`` methods return typed ``GenerationStatus`` objects
    (or dicts at this seam), so no raw RPC payload reaches the extractor.
    """

    def test_extract_from_dict_task_id(self):
        result = _extract_task_id({"task_id": "t1", "status": "pending"})
        assert result == "t1"

    def test_extract_from_dict_artifact_id(self):
        result = _extract_task_id({"artifact_id": "a1"})
        assert result == "a1"

    def test_extract_from_object_with_task_id(self):
        status = MagicMock()
        status.task_id = "task_obj"
        result = _extract_task_id(status)
        assert result == "task_obj"


class TestExtractGenerationTaskId:
    """Generation-start task-id extraction prefers ``artifact_id`` over ``task_id``."""

    def test_dict_prefers_artifact_id(self):
        result = _extract_generation_task_id({"artifact_id": "a1", "task_id": "t1"})
        assert result == "a1"

    def test_dict_falls_back_to_task_id(self):
        result = _extract_generation_task_id({"task_id": "t1"})
        assert result == "t1"

    def test_generation_status_uses_task_id(self):
        status = GenerationStatus(task_id="gs_1", status="pending", error=None, error_code=None)
        assert _extract_generation_task_id(status) == "gs_1"

    def test_unhandled_returns_none(self):
        assert _extract_generation_task_id(42) is None

    def test_raw_positional_list_is_not_decoded(self):
        """A raw positional list yields ``None`` — the list-sniffing path is gone.

        ``_app`` consumes typed facade returns only; an RPC-shaped list must
        not be silently decoded above the facade.
        """
        assert _extract_generation_task_id(["task_raw", "x"]) is None
        assert _extract_task_id(["task_raw", "x"]) is None


# ---------------------------------------------------------------------------
# _format_status_message — spinner status line (moved, pure).
# ---------------------------------------------------------------------------


class TestFormatStatusMessage:
    def test_known_kind_includes_typical_hint(self):
        msg = _format_status_message("cinematic-video")
        assert "cinematic-video" in msg
        assert "typically" in msg
        assert msg.endswith("...")

    def test_unknown_kind_omits_hint(self):
        msg = _format_status_message("unknown-kind")
        assert "unknown-kind" in msg
        assert "(" not in msg, f"unknown kind should NOT add a hint, got: {msg!r}"

    def test_with_elapsed_appends_seconds(self):
        msg = _format_status_message("audio", elapsed=42.7)
        assert "[42s elapsed]" in msg


# ---------------------------------------------------------------------------
# generation_outcome_from_status — outcome classification (net-new direct).
# ---------------------------------------------------------------------------


class TestGenerationOutcomeFromStatus:
    def test_completed_with_url(self):
        status = GenerationStatus(
            task_id="t1",
            status="completed",
            error=None,
            error_code=None,
            url="https://example.com/a.mp3",
        )
        outcome = generation_outcome_from_status(status, "audio")
        assert outcome.status == "completed"
        assert outcome.url == "https://example.com/a.mp3"
        assert outcome.task_id == "t1"
        assert outcome.exit_code == 0

    def test_failed_uses_error_message(self):
        status = GenerationStatus(task_id="t1", status="failed", error="boom", error_code="X")
        outcome = generation_outcome_from_status(status, "audio")
        assert outcome.status == "failed"
        assert outcome.error == "boom"
        assert outcome.exit_code == 1

    def test_failed_without_error_message_uses_default(self):
        status = GenerationStatus(task_id="t1", status="failed", error=None, error_code="X")
        outcome = generation_outcome_from_status(status, "audio")
        assert outcome.status == "failed"
        assert outcome.error == "Audio generation failed"

    def test_removed_is_classified_as_failed(self):
        """A ``removed`` artifact has no usable result → surfaced as failed.

        Uses a real ``GenerationStatus(status="removed")`` (``is_removed`` is
        True, ``is_failed``/``is_complete`` False) rather than a hand-rolled
        mock so the predicate wiring is exercised faithfully.
        """
        removed = GenerationStatus(task_id="t1", status="removed", error=None, error_code=None)
        outcome = generation_outcome_from_status(removed, "video")
        assert outcome.status == "failed"
        assert outcome.error == "Video generation failed"

    def test_pending_when_neither_complete_nor_failed(self):
        status = GenerationStatus(task_id="t1", status="pending", error=None, error_code=None)
        outcome = generation_outcome_from_status(status, "audio")
        assert outcome.status == "pending"
        assert outcome.task_id == "t1"
        assert outcome.exit_code == 0


def test_generation_outcome_exit_code_rate_limited():
    outcome = GenerationOutcome(status="rate_limited", artifact_type="audio")
    assert outcome.exit_code == 1


# ---------------------------------------------------------------------------
# handle_generation_result — None / rate-limited / wait-path (net-new direct).
# ---------------------------------------------------------------------------


class TestHandleGenerationResult:
    @pytest.mark.asyncio
    async def test_none_result_is_failed(self):
        client = MagicMock()
        outcome = await handle_generation_result(client, "nb_1", None, "audio")
        assert outcome.status == "failed"
        assert outcome.error == "Audio generation failed"

    @pytest.mark.asyncio
    async def test_rate_limited_status_maps_to_rate_limited_outcome(self):
        client = MagicMock()
        rate_limited = GenerationStatus(
            task_id="t1",
            status="failed",
            error="rl",
            error_code="USER_DISPLAYABLE_ERROR",
        )
        outcome = await handle_generation_result(client, "nb_1", rate_limited, "audio")
        assert outcome.status == "rate_limited"
        assert outcome.error_code == "RATE_LIMITED"
        assert outcome.hint is not None

    @pytest.mark.asyncio
    async def test_no_wait_returns_pending_outcome(self):
        client = MagicMock()
        client.artifacts = MagicMock()
        client.artifacts.wait_for_completion = AsyncMock()
        start = {"task_id": "t1", "status": "processing"}
        outcome = await handle_generation_result(client, "nb_1", start, "audio", wait=False)
        assert outcome.status == "pending"
        client.artifacts.wait_for_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wait_polls_for_completion(self):
        client = MagicMock()
        client.artifacts = MagicMock()
        completed = GenerationStatus(
            task_id="t1",
            status="completed",
            error=None,
            error_code=None,
            url="https://example.com/a.mp3",
        )
        client.artifacts.wait_for_completion = AsyncMock(return_value=completed)
        start = {"artifact_id": "t1", "status": "processing"}

        wait_start_sink = MagicMock()
        outcome = await handle_generation_result(
            client,
            "nb_1",
            start,
            "audio",
            wait=True,
            wait_start_sink=wait_start_sink,
        )
        assert outcome.status == "completed"
        assert outcome.url == "https://example.com/a.mp3"
        client.artifacts.wait_for_completion.assert_awaited_once()
        wait_start_sink.assert_called_once_with("t1")

    @pytest.mark.asyncio
    async def test_wait_context_spans_the_poll(self):
        client = MagicMock()
        client.artifacts = MagicMock()
        completed = GenerationStatus(task_id="t1", status="completed", error=None, error_code=None)
        client.artifacts.wait_for_completion = AsyncMock(return_value=completed)
        entered = {"flag": False}

        @asynccontextmanager
        async def _ctx(_message, _resume_hint):
            entered["flag"] = True
            yield

        await handle_generation_result(
            client,
            "nb_1",
            {"task_id": "t1"},
            "audio",
            wait=True,
            wait_context=_ctx,
        )
        assert entered["flag"] is True

    @pytest.mark.asyncio
    async def test_wait_forwards_interval_as_initial_interval(self):
        client = MagicMock()
        client.artifacts = MagicMock()
        completed = GenerationStatus(task_id="t1", status="completed", error=None, error_code=None)
        client.artifacts.wait_for_completion = AsyncMock(return_value=completed)

        await handle_generation_result(
            client,
            "nb_1",
            {"task_id": "t1"},
            "audio",
            wait=True,
            timeout=123.0,
            interval=5.0,
        )
        _args, kwargs = client.artifacts.wait_for_completion.await_args
        assert kwargs["timeout"] == 123.0
        assert kwargs["initial_interval"] == 5.0
