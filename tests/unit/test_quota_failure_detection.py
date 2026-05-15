"""Tests for quota/daily-limit failure detection during artifact polling.

Regression tests for GitHub issue #239: when a daily quota is reached
(e.g. Cinematics limit) the generation task silently polled until timeout
instead of failing quickly with a helpful error message.

Root causes:
1. poll_status() returned status="pending" when the artifact disappeared
   from the list (the API removes quota-rejected artifacts).
2. wait_for_completion() had no mechanism to detect a sustained run of
   "artifact not found" responses and would spin until timeout.
3. Failed artifacts had no error message surfaced to the caller.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm.rpc.types import ArtifactStatus
from notebooklm.types import GenerationStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_api():
    """Return an ArtifactsAPI with mocked core + notes."""
    core = MagicMock()
    core.rpc_call = AsyncMock()
    core.get_source_ids = AsyncMock(return_value=[])
    # ClientCore._pending_polls (T7.E2) — real dict so the leader/follower
    # dedupe in ``wait_for_completion`` can ``dict.get(key)`` against it.
    core._pending_polls = {}
    notes = MagicMock()
    notes.list_mind_maps = AsyncMock(return_value=[])
    notes.create = AsyncMock(return_value=MagicMock(id="note_1"))
    return ArtifactsAPI(core, notes_api=notes)


def _art(artifact_id: str, status: int, artifact_type: int = 1, error_at_3: str | None = None):
    """Build a minimal raw artifact list entry."""
    entry = [artifact_id, "Title", artifact_type, error_at_3, status]
    return entry


# ---------------------------------------------------------------------------
# poll_status: returns "not_found" when artifact absent from list
# ---------------------------------------------------------------------------


class TestPollStatusNotFound:
    """poll_status distinguishes missing artifacts from pending ones."""

    @pytest.mark.asyncio
    async def test_missing_artifact_returns_not_found(self):
        """Artifact absent from list → status 'not_found', not 'pending'."""
        api = _make_api()
        api._list_raw = AsyncMock(return_value=[_art("other_id", ArtifactStatus.PROCESSING)])

        result = await api.poll_status("nb1", "missing_task_id")

        assert result.status == "not_found"
        assert result.is_not_found is True
        assert result.is_pending is False

    @pytest.mark.asyncio
    async def test_empty_list_returns_not_found(self):
        """Empty artifact list → status 'not_found'."""
        api = _make_api()
        api._list_raw = AsyncMock(return_value=[])

        result = await api.poll_status("nb1", "task_abc")

        assert result.status == "not_found"
        assert result.is_not_found is True

    @pytest.mark.asyncio
    async def test_found_artifact_returns_correct_status(self):
        """Artifact present in list → actual status propagated."""
        api = _make_api()
        api._list_raw = AsyncMock(return_value=[_art("task_abc", ArtifactStatus.PROCESSING)])

        result = await api.poll_status("nb1", "task_abc")

        assert result.status == "in_progress"
        assert result.is_not_found is False

    @pytest.mark.asyncio
    async def test_completed_artifact_status(self):
        """Completed non-media artifact (report) returns 'completed'."""
        api = _make_api()
        # Type 2 = REPORT (non-media, no URL check required)
        api._list_raw = AsyncMock(
            return_value=[_art("task_abc", ArtifactStatus.COMPLETED, artifact_type=2)]
        )

        result = await api.poll_status("nb1", "task_abc")

        assert result.status == "completed"
        assert result.is_complete is True

    @pytest.mark.asyncio
    async def test_failed_artifact_returns_failed_status(self):
        """Artifact with status=FAILED returns 'failed'."""
        api = _make_api()
        api._list_raw = AsyncMock(return_value=[_art("task_abc", ArtifactStatus.FAILED)])

        result = await api.poll_status("nb1", "task_abc")

        assert result.status == "failed"
        assert result.is_failed is True


# ---------------------------------------------------------------------------
# poll_status: extracts error message from failed artifacts
# ---------------------------------------------------------------------------


class TestPollStatusErrorExtraction:
    """poll_status surfaces error details from failed artifacts."""

    @pytest.mark.asyncio
    async def test_error_string_at_index_3_is_surfaced(self):
        """When art[3] is a non-empty string, it becomes error in GenerationStatus."""
        api = _make_api()
        api._list_raw = AsyncMock(
            return_value=[_art("task_abc", ArtifactStatus.FAILED, error_at_3="Daily limit reached")]
        )

        result = await api.poll_status("nb1", "task_abc")

        assert result.status == "failed"
        assert result.error == "Daily limit reached"

    @pytest.mark.asyncio
    async def test_no_error_at_index_3_error_is_none(self):
        """When art[3] is None, error field remains None."""
        api = _make_api()
        api._list_raw = AsyncMock(return_value=[_art("task_abc", ArtifactStatus.FAILED)])

        result = await api.poll_status("nb1", "task_abc")

        assert result.status == "failed"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_art5_fallback_end_to_end(self):
        """Error in art[5] is surfaced through poll_status (end-to-end, not just helper)."""
        api = _make_api()
        # art[3] is None, error is in art[5] nested list
        art = ["task_abc", "Title", 1, None, ArtifactStatus.FAILED, ["Veo daily limit hit"]]
        api._list_raw = AsyncMock(return_value=[art])

        result = await api.poll_status("nb1", "task_abc")

        assert result.status == "failed"
        assert result.error == "Veo daily limit hit"

    @pytest.mark.asyncio
    async def test_error_extraction_only_for_failed_status(self):
        """Error extraction is skipped for non-failed statuses."""
        api = _make_api()
        # art[3] has content, but status is PROCESSING — should not surface error
        art = _art("task_abc", ArtifactStatus.PROCESSING, error_at_3="some stray text")
        api._list_raw = AsyncMock(return_value=[art])

        result = await api.poll_status("nb1", "task_abc")

        # Status is in_progress; error should not be set
        assert result.status == "in_progress"
        assert result.error is None


# ---------------------------------------------------------------------------
# wait_for_completion: detects quota failure via consecutive not-found
# ---------------------------------------------------------------------------


class TestWaitForCompletionQuotaDetection:
    """wait_for_completion fails fast when artifact disappears from list."""

    @pytest.mark.asyncio
    async def test_consecutive_not_found_raises_failed(self):
        """After max_not_found consecutive not-found polls, returns failed."""
        api = _make_api()
        # Always return not_found
        api.poll_status = AsyncMock(
            return_value=GenerationStatus(task_id="task_abc", status="not_found")
        )

        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.01,
            max_interval=0.01,
            max_not_found=3,
            min_not_found_window=0.0,
        )

        assert result.is_failed is True
        assert result.error is not None
        assert "quota" in result.error.lower() or "limit" in result.error.lower()

    @pytest.mark.asyncio
    async def test_not_found_then_found_resets_counter(self):
        """Prove consecutive counter resets: with max_not_found=2, a
        [not_found, pending, not_found, completed] sequence should succeed
        because the pending response resets the consecutive counter."""
        api = _make_api()
        responses = [
            GenerationStatus(task_id="task_abc", status="not_found"),
            GenerationStatus(task_id="task_abc", status="pending"),
            GenerationStatus(task_id="task_abc", status="not_found"),
            GenerationStatus(task_id="task_abc", status="completed"),
        ]
        api.poll_status = AsyncMock(side_effect=responses)

        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.01,
            max_interval=0.01,
            max_not_found=2,
            min_not_found_window=0.0,
        )

        assert result.is_complete is True
        # All 4 calls were made (counter was reset after the pending)
        assert api.poll_status.call_count == 4

    @pytest.mark.asyncio
    async def test_sustained_not_found_fails_before_timeout(self):
        """Sustained not-found responses fail fast, not at timeout."""
        api = _make_api()
        api.poll_status = AsyncMock(
            return_value=GenerationStatus(task_id="task_abc", status="not_found")
        )

        import time

        start = time.monotonic()
        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.01,
            max_interval=0.01,
            timeout=60.0,  # Long timeout — should NOT reach it
            max_not_found=3,
            min_not_found_window=0.0,
        )
        elapsed = time.monotonic() - start

        assert result.is_failed is True
        # Should complete well before the 60s timeout
        assert elapsed < 5.0, f"Expected fast failure, took {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_normal_failure_still_returns_failed(self):
        """A FAILED status from poll_status propagates normally."""
        api = _make_api()
        api.poll_status = AsyncMock(
            return_value=GenerationStatus(
                task_id="task_abc",
                status="failed",
                error="Some server error",
            )
        )

        result = await api.wait_for_completion(
            "nb1", "task_abc", initial_interval=0.01, max_interval=0.01
        )

        assert result.is_failed is True
        assert result.error == "Some server error"

    @pytest.mark.asyncio
    async def test_timeout_includes_last_status(self):
        """TimeoutError message includes the last observed status."""
        api = _make_api()
        api.poll_status = AsyncMock(
            return_value=GenerationStatus(task_id="task_abc", status="in_progress")
        )

        with pytest.raises(TimeoutError) as exc_info:
            await api.wait_for_completion(
                "nb1",
                "task_abc",
                initial_interval=0.01,
                max_interval=0.01,
                timeout=0.05,
            )

        assert "in_progress" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_max_not_found_default_is_5(self):
        """Default max_not_found is 5 consecutive polls."""
        api = _make_api()
        call_count = 0

        async def side_effect(notebook_id, task_id):
            nonlocal call_count
            call_count += 1
            return GenerationStatus(task_id=task_id, status="not_found")

        api.poll_status = AsyncMock(side_effect=side_effect)

        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.01,
            max_interval=0.01,
            min_not_found_window=0.0,
        )

        # Should have polled exactly 5 times (default max_not_found=5)
        assert call_count == 5
        assert result.is_failed is True

    @pytest.mark.asyncio
    async def test_flickering_artifact_triggers_total_failure(self):
        """Oscillating found/not-found (flickering) triggers failure via
        total_not_found even though consecutive never reaches max_not_found."""
        api = _make_api()
        # With max_not_found=3, total threshold is 6.  Alternate to keep
        # consecutive at most 1, but accumulate 6 total not-founds.
        responses = []
        for _ in range(6):
            responses.append(GenerationStatus(task_id="task_abc", status="not_found"))
            responses.append(GenerationStatus(task_id="task_abc", status="pending"))
        # The 6th not_found should trigger total_not_found >= 3*2 = 6
        # Actually it fires at the not_found before the pending resets consecutive,
        # so let's just add enough not_founds interleaved.
        # Sequence: nf, pending, nf, pending, nf, pending, nf, pending, nf, pending, nf
        # total_not_found hits 6 at the 6th nf (index 10)
        responses_flickering = []
        for i in range(12):
            if i % 2 == 0:
                responses_flickering.append(
                    GenerationStatus(task_id="task_abc", status="not_found")
                )
            else:
                responses_flickering.append(GenerationStatus(task_id="task_abc", status="pending"))

        api.poll_status = AsyncMock(side_effect=responses_flickering)

        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.01,
            max_interval=0.01,
            max_not_found=3,
            min_not_found_window=0.0,
        )

        assert result.is_failed is True
        assert result.error is not None
        assert "removed by the server" in result.error

    @pytest.mark.asyncio
    async def test_last_status_set_before_timeout(self):
        """Timeout message includes actual status even on immediate timeout."""
        api = _make_api()
        api.poll_status = AsyncMock(
            return_value=GenerationStatus(task_id="task_abc", status="in_progress")
        )

        with pytest.raises(TimeoutError) as exc_info:
            await api.wait_for_completion(
                "nb1",
                "task_abc",
                initial_interval=0.01,
                max_interval=0.01,
                timeout=0.0,  # Immediate timeout after first poll
            )

        # last_status should be set even though we timed out on first iteration
        assert "in_progress" in str(exc_info.value)
        assert "None" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_min_not_found_window_prevents_false_positive(self):
        """Not-found failure is deferred until min_not_found_window elapses."""
        api = _make_api()
        call_count = 0

        async def side_effect(notebook_id, task_id):
            nonlocal call_count
            call_count += 1
            if call_count <= 5:
                return GenerationStatus(task_id=task_id, status="not_found")
            return GenerationStatus(task_id=task_id, status="completed")

        api.poll_status = AsyncMock(side_effect=side_effect)

        # With a large window, consecutive threshold alone won't trigger
        # because the window hasn't elapsed.  But after enough polls,
        # the total count (5*2=10) would trigger on the total path.
        # Use max_not_found=5, min_not_found_window=9999 so consecutive
        # trigger is blocked, but total triggers at 10.
        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.01,
            max_interval=0.01,
            max_not_found=5,
            min_not_found_window=9999.0,
        )

        # Should complete since we return completed at poll 6
        assert result.is_complete is True
        assert call_count == 6


# ---------------------------------------------------------------------------
# GenerationStatus.is_not_found property
# ---------------------------------------------------------------------------


class TestGenerationStatusIsNotFound:
    """GenerationStatus.is_not_found correctly identifies the new status."""

    def test_is_not_found_true_for_not_found_status(self):
        status = GenerationStatus(task_id="x", status="not_found")
        assert status.is_not_found is True

    def test_is_not_found_false_for_pending(self):
        status = GenerationStatus(task_id="x", status="pending")
        assert status.is_not_found is False

    def test_is_not_found_false_for_in_progress(self):
        status = GenerationStatus(task_id="x", status="in_progress")
        assert status.is_not_found is False

    def test_is_not_found_false_for_completed(self):
        status = GenerationStatus(task_id="x", status="completed")
        assert status.is_not_found is False

    def test_is_not_found_false_for_failed(self):
        status = GenerationStatus(task_id="x", status="failed")
        assert status.is_not_found is False

    def test_is_rate_limited_matches_limit_exceeded_phrase(self):
        """is_rate_limited now also matches 'limit exceeded' in error text."""
        status = GenerationStatus(
            task_id="x",
            status="failed",
            error="Daily limit exceeded for cinematic videos",
        )
        assert status.is_rate_limited is True

    def test_is_not_failed_while_not_found(self):
        """not_found is a separate state from failed."""
        status = GenerationStatus(task_id="x", status="not_found")
        assert status.is_failed is False
        assert status.is_complete is False
        assert status.is_pending is False


# ---------------------------------------------------------------------------
# _extract_artifact_error helper
# ---------------------------------------------------------------------------


class TestExtractArtifactError:
    """Unit tests for the static _extract_artifact_error helper."""

    def test_string_at_index_3_is_returned(self):
        art = ["id", "title", 1, "Quota exceeded", 4]
        result = ArtifactsAPI._extract_artifact_error(art)
        assert result == "Quota exceeded"

    def test_none_at_index_3_returns_none(self):
        art = ["id", "title", 1, None, 4]
        result = ArtifactsAPI._extract_artifact_error(art)
        assert result is None

    def test_empty_string_at_index_3_returns_none(self):
        art = ["id", "title", 1, "   ", 4]
        result = ArtifactsAPI._extract_artifact_error(art)
        assert result is None

    def test_short_artifact_no_index_3_returns_none(self):
        art = ["id", "title"]
        result = ArtifactsAPI._extract_artifact_error(art)
        assert result is None

    def test_nested_string_at_index_5_is_returned(self):
        """Error text in art[5] as nested list is extracted."""
        art = ["id", "title", 1, None, 4, ["Daily cinematic limit reached"]]
        result = ArtifactsAPI._extract_artifact_error(art)
        assert result == "Daily cinematic limit reached"

    def test_deeply_nested_string_at_index_5(self):
        """Error text in art[5] as doubly nested list is extracted."""
        art = ["id", "title", 1, None, 4, [["Veo quota exhausted"]]]
        result = ArtifactsAPI._extract_artifact_error(art)
        assert result == "Veo quota exhausted"

    def test_index_3_takes_priority_over_index_5(self):
        """When both art[3] and art[5] contain strings, art[3] wins."""
        art = ["id", "title", 1, "Primary error", 4, ["Secondary error"]]
        result = ArtifactsAPI._extract_artifact_error(art)
        assert result == "Primary error"
