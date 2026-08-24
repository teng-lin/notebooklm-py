"""Unit tests for the transport-neutral ``notebooklm._app.generate_retry`` core.

These pin generation presentation/projection logic at the ``_app``
boundary (independent of the Click adapter):

* :func:`calculate_backoff_delay` compatibility math;
* the ``_format_status_message`` spinner-line formatter;
* direct coverage for result/outcome classification.

No Click / ``CliRunner`` — every test calls the ``_app`` function directly. The
CLI ``--json`` / console-rendering assertions stay in
``tests/unit/cli/test_generate.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from notebooklm._app.generate_retry import (
    GenerationOutcome,
    _extract_task_id,
    _format_status_message,
    calculate_backoff_delay,
    generation_outcome_from_result,
    generation_outcome_from_status,
)
from notebooklm._deadline import RuntimeDeadline
from notebooklm.artifacts import _await_with_deadline
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


@pytest.mark.asyncio
async def test_expired_deadline_does_not_create_awaitable() -> None:
    called = False

    async def operation() -> object:
        nonlocal called
        called = True
        return object()

    deadline = RuntimeDeadline.start(0.0, monotonic=lambda: 1.0)

    with pytest.raises(TimeoutError, match="audio generation timed out"):
        await _await_with_deadline(operation, deadline, "audio")

    assert called is False


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


def test_raw_positional_list_is_not_decoded() -> None:
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


class TestGenerationOutcomeFromResult:
    def test_none_result_is_failed(self) -> None:
        outcome = generation_outcome_from_result(None, "audio")
        assert outcome.status == "failed"
        assert outcome.error == "Audio generation failed"

    def test_rate_limited_status_is_projected(self) -> None:
        status = GenerationStatus(
            task_id="t1",
            status="failed",
            error="quota",
            error_code="USER_DISPLAYABLE_ERROR",
        )
        outcome = generation_outcome_from_result(status, "audio")
        assert outcome.status == "rate_limited"
        assert outcome.error_code == "RATE_LIMITED"
