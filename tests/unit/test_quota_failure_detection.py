"""Tests for quota/daily-limit failure detection during artifact polling.

Regression tests for GitHub issue #239: when a daily quota is reached
(e.g. Cinematics limit) the generation task silently polled until timeout
instead of failing quickly with a helpful error message.

Root causes:
1. poll_status() returned status="pending" when the artifact disappeared
   from the list (the API removes quota-rejected artifacts).
2. wait_for_completion() had no mechanism to detect a sustained run of
   "artifact not found" responses and would spin until timeout.
3. A quota rejection at generation time had to fail fast instead of polling
   to a timeout.

Where a failure reason does and does not come from (#2134 / #2188 / #2193)
--------------------------------------------------------------------------
Root cause 3 used to read "failed artifacts had no error message surfaced to
the caller", and the tests below tried to surface one from the artifact row.
No such field exists. ``Artifact`` in ``docs/android/schema.proto`` has no error
or failure field at all: index 3 is ``sources`` and index 5 is
``isPubliclyReadable``, and #2134 deleted the reader that pretended otherwise.

A reason exists in exactly one place — the ``google.rpc.Status`` on the
``CreateArtifact`` RPC at generation time, which is why ``RETRY_ARTIFACT``
exists at all: the resource remembers nothing, so retry is the only affordance
left. Two consequences are pinned here:

* ``TestRejectionAtGenerationTime`` — a rejected ``CreateArtifact`` reaches the
  ``generate_*`` caller as an exception, through the REAL decoder, so #239's
  fail-fast guarantee has a regression test again.
* ``TestLateFailureHasNoReason`` — an artifact accepted at create time that
  only later flips to FAILED carries ``error=None`` forever, and the user-facing
  string comes from the generic ``"{Type} generation failed"`` fallback in
  ``_app/generate_retry.py``.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.generate_retry import generation_outcome_from_status
from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.wire.decoder import decode_response, extract_rpc_result
from notebooklm.exceptions import ArtifactFeatureUnavailableError, RateLimitError, RPCError
from notebooklm.rpc.types import ArtifactStatus, RPCMethod
from notebooklm.types import GenerationStatus
from tests._fixtures.rpc_error_frames import (
    CREATE_ARTIFACT_METHOD_ID,
    LIVE_CREATE_ARTIFACT_INVALID_ARGUMENT_BODY,
    LIVE_RETRY_ARTIFACT_NOT_FOUND_BODY,
    LIVE_REVISE_SLIDE_NOT_FOUND_BODY,
    raw_batchexecute_body,
    user_displayable_rejection_chunks,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_api(rpc_call: AsyncMock | None = None):
    """Return an ArtifactsAPI with mocked runtime + mind-map services.

    ``rpc_call`` overrides the RPC seam so a test can answer with a real
    decoded response instead of a bare mock return value.
    """
    from notebooklm._web.mind_maps import NoteBackedMindMapService
    from notebooklm._web.notes import NoteService
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(
        rpc_call=rpc_call if rpc_call is not None else AsyncMock(),
        operation_scope=MagicMock(side_effect=lambda _label: _noop_operation_scope()),
    )
    # ``ArtifactsAPI`` constructs its own ``PollRegistry`` internally; the fake
    # core does not need to provide one.
    mind_maps = MagicMock(spec=NoteBackedMindMapService)
    note_service = MagicMock(spec=NoteService)
    notebooks = MagicMock()
    notebooks.get_source_ids = AsyncMock(return_value=[])
    return WebArtifactsAPI(
        rpc=core,
        supervisor=core,
        notebooks=notebooks,
        mind_maps=mind_maps,
        note_service=note_service,
    )


@asynccontextmanager
async def _noop_operation_scope():
    yield None


def _art(artifact_id: str, status: int, artifact_type: int = 1, sources: list | None = None):
    """Build a constructed artifact row; index 3 models ``Artifact.sources`` (#2134)."""
    return [artifact_id, "Title", artifact_type, sources, status]


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
# wait_for_completion: detects quota failure via consecutive not-found
# ---------------------------------------------------------------------------


class TestWaitForCompletionQuotaDetection:
    """wait_for_completion fails fast when artifact disappears from list."""

    @pytest.mark.asyncio
    async def test_consecutive_not_found_returns_removed(self):
        """After max_not_found consecutive not-found polls, returns removed.

        Regression for issue #1168: a delisted artifact is reported with a
        distinct ``"removed"`` status, *not* ``"failed"``, so callers do not
        conflate a transient list omission with a genuine terminal failure.
        """
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

        assert result.is_removed is True
        # A removal is NOT a terminal FAILED artifact — see issue #1168.
        assert result.is_failed is False
        assert result.status == "removed"
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

        assert result.is_removed is True
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
        # A genuine terminal FAILED artifact must NOT be reported as removed
        # (issue #1168: the two states must stay disjoint).
        assert result.is_removed is False
        assert result.error == "Some server error"

    @pytest.mark.asyncio
    async def test_removed_status_invokes_status_change_callback(self):
        """on_status_change fires once with the synthesized removed status."""
        api = _make_api()
        api.poll_status = AsyncMock(
            return_value=GenerationStatus(task_id="task_abc", status="not_found")
        )
        observed: list[str] = []

        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.01,
            max_interval=0.01,
            max_not_found=3,
            min_not_found_window=0.0,
            on_status_change=lambda status: observed.append(status.status),
        )

        assert result.is_removed is True
        # The terminal "removed" status is the last status emitted, exactly once.
        assert observed[-1] == "removed"
        assert observed.count("removed") == 1

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
        assert result.is_removed is True

    @pytest.mark.asyncio
    async def test_transient_omissions_reset_accumulators_and_complete(self):
        """A flickering artifact that keeps reappearing is NOT declared removed.

        Regression for issue #1198: previously ``total_not_found`` accumulated
        across the whole poll and never reset on reappearance, so an artifact
        with sporadic brief omissions during an otherwise-healthy generation
        could trip the total threshold and be fabricated into a terminal
        ``"removed"`` before it ever completed. Now every reappearance resets
        the not-found accumulators, so ``"removed"`` requires a *sustained*
        absence — a flapping artifact polls through to completion instead.
        """
        api = _make_api()
        # 7 not-found omissions interleaved with in_progress sightings, then a
        # genuine completion. With max_not_found=3 the OLD cumulative total
        # threshold was 6, so the pre-fix loop would have returned "removed" at
        # the 6th not-found (before completing). With the reset, each in_progress
        # wipes the counter, so no removal trigger ever fires.
        responses = []
        for _ in range(7):
            responses.append(GenerationStatus(task_id="task_abc", status="not_found"))
            responses.append(GenerationStatus(task_id="task_abc", status="in_progress"))
        responses.append(GenerationStatus(task_id="task_abc", status="completed"))
        api.poll_status = AsyncMock(side_effect=responses)

        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.01,
            max_interval=0.01,
            max_not_found=3,
            min_not_found_window=0.0,
        )

        assert result.is_complete is True
        assert result.is_removed is False
        # All 15 responses were consumed: the loop never short-circuited to
        # "removed" despite 7 total not-found polls (> the old total threshold).
        assert api.poll_status.call_count == 15

    @pytest.mark.asyncio
    async def test_sustained_not_found_with_blocking_window_still_removed(self):
        """Sustained absence still triggers removal via the total fallback even
        when ``min_not_found_window`` blocks the consecutive trigger.

        Guards the issue #1198 change: resetting the counter on reappearance
        must not weaken detection of a *genuinely* delisted artifact that never
        comes back. With a large window the time-gated trigger is suppressed,
        but the window-independent trigger still fires once the consecutive run
        reaches ``max_not_found * 2`` and reports ``"removed"``.
        """
        api = _make_api()
        api.poll_status = AsyncMock(
            return_value=GenerationStatus(task_id="task_abc", status="not_found")
        )

        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.01,
            max_interval=0.01,
            max_not_found=3,
            min_not_found_window=9999.0,  # blocks the consecutive trigger
        )

        assert result.is_removed is True
        assert result.is_failed is False
        # Fires on the total path at max_not_found * 2 = 6 consecutive not-founds.
        assert api.poll_status.call_count == 6

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
# GenerationStatus.is_removed property (issue #1168)
# ---------------------------------------------------------------------------


class TestGenerationStatusIsRemoved:
    """is_removed identifies a delisted artifact, distinct from failed."""

    def test_is_removed_true_for_removed_status(self):
        status = GenerationStatus(task_id="x", status="removed")
        assert status.is_removed is True

    def test_removed_is_not_failed(self):
        """A removed artifact is not a terminal FAILED artifact."""
        status = GenerationStatus(task_id="x", status="removed")
        assert status.is_failed is False
        assert status.is_complete is False
        assert status.is_pending is False
        assert status.is_not_found is False

    def test_failed_is_not_removed(self):
        """A terminal FAILED artifact is not reported as removed."""
        status = GenerationStatus(task_id="x", status="failed")
        assert status.is_removed is False

    def test_other_statuses_are_not_removed(self):
        for value in ("pending", "in_progress", "completed", "not_found"):
            assert GenerationStatus(task_id="x", status=value).is_removed is False

    def test_removed_with_quota_error_is_rate_limited(self):
        """A removal carrying quota wording stays rate-limit-retryable."""
        status = GenerationStatus(
            task_id="x",
            status="removed",
            error="artifact was removed; daily quota/rate limit was exceeded",
        )
        assert status.is_rate_limited is True

    def test_removed_without_quota_wording_is_not_rate_limited(self):
        status = GenerationStatus(task_id="x", status="removed", error="just gone")
        assert status.is_rate_limited is False


# ---------------------------------------------------------------------------
# #239 / #2193: a rejection at generation time reaches the caller
# ---------------------------------------------------------------------------


class TestRejectionAtGenerationTime:
    """A ``CreateArtifact`` rejection fails fast, through the real decoder.

    These tests deliberately do **not** stub the decode step. The rejection is
    handed to the production decoder as the server sent it, and the assertion
    is on what ``generate_audio`` raises — the chain decoder → ``rpc_call`` →
    ``ArtifactGenerationService`` → ``ArtifactsAPI`` is exactly where the #239
    regression lived, and stubbing the decoder would have hidden it.
    """

    @staticmethod
    def _api_answering_with(decoded):
        """Build an API whose RPC seam runs ``decoded(method_id)`` for real."""

        async def rpc_call(method, *_args, **kwargs):
            method_id = getattr(method, "value", method)
            return decoded(method_id, **kwargs)

        return _make_api(rpc_call=AsyncMock(side_effect=rpc_call))

    @pytest.mark.asyncio
    async def test_quota_rejection_raises_rate_limit_error_to_the_caller(self):
        """#239: a UserDisplayableError rejection raises before any polling."""
        api = self._api_answering_with(
            lambda method_id, **_kw: extract_rpc_result(
                user_displayable_rejection_chunks(method_id), method_id
            )
        )

        with pytest.raises(RateLimitError) as exc_info:
            await api.generate_audio("nb1")

        assert exc_info.value.rpc_code == "USER_DISPLAYABLE_ERROR"
        message = str(exc_info.value)
        # The condition and the remedy are both named. Both halves of this
        # sentence are CLIENT-authored: the recorded rejection carries no
        # server text at all (see USER_DISPLAYABLE_RATE_LIMIT_STATUS).
        assert "quota" in message.lower()
        assert "retry" in message.lower()
        # The upstream gRPC label is kept for diagnosis; the raw code is not.
        assert "Resource exhausted" in message

    @pytest.mark.asyncio
    async def test_quota_rejection_reaches_the_caller_only_from_create_artifact(self):
        """The rejection travels on ``CREATE_ARTIFACT``, not a later poll."""
        seen: list[str] = []

        def decoded(method_id, **_kw):
            seen.append(method_id)
            return extract_rpc_result(user_displayable_rejection_chunks(method_id), method_id)

        api = self._api_answering_with(decoded)

        with pytest.raises(RateLimitError):
            await api.generate_audio("nb1")

        assert seen == [RPCMethod.CREATE_ARTIFACT.value]

    @pytest.mark.asyncio
    async def test_live_captured_invalid_argument_rejection_reports_the_server_status(self):
        """The server's own status survives to the caller (#2188).

        Drives the verbatim body a live ``CREATE_ARTIFACT`` returned for an
        Audio Overview on a source-less notebook (2026-08-13) through
        ``decode_response``. Before #2188 the ``allow_null=True`` decode
        swallowed the ``[3]`` and the caller reported "Audio generation is
        unavailable" — a client-invented reason that contradicted the one the
        server actually gave.
        """
        api = self._api_answering_with(
            lambda method_id, **kwargs: decode_response(
                LIVE_CREATE_ARTIFACT_INVALID_ARGUMENT_BODY,
                method_id,
                allow_null=kwargs.get("allow_null", False),
                raise_on_null_status=kwargs.get("raise_on_null_status", False),
            )
        )

        with pytest.raises(RPCError) as exc_info:
            await api.generate_audio("nb1")

        assert not isinstance(exc_info.value, ArtifactFeatureUnavailableError)
        assert exc_info.value.rpc_code == 3
        assert "invalid argument" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "call"),
        [
            pytest.param(
                LIVE_RETRY_ARTIFACT_NOT_FOUND_BODY,
                lambda api: api.retry_failed("nb1", "no-such-artifact-id"),
                id="retry_artifact",
            ),
            pytest.param(
                LIVE_REVISE_SLIDE_NOT_FOUND_BODY,
                lambda api: api.revise_slide("nb1", "no-such-artifact-id", 0, "tweak it"),
                id="revise_slide",
            ),
        ],
    )
    async def test_retry_and_revise_also_report_the_server_status(self, body, call):
        """The other two opt-in call sites are evidence-backed too (#2188).

        Live probe 2026-08-13: both answer ``[5]`` NOT_FOUND for an unknown
        artifact id. Before the opt-in each reported "… generation is
        unavailable", which says nothing about the id being wrong.
        """
        api = self._api_answering_with(
            lambda method_id, **kwargs: decode_response(
                body,
                method_id,
                allow_null=kwargs.get("allow_null", False),
                raise_on_null_status=kwargs.get("raise_on_null_status", False),
            )
        )

        with pytest.raises(RPCError) as exc_info:
            await call(api)

        assert not isinstance(exc_info.value, ArtifactFeatureUnavailableError)
        assert exc_info.value.rpc_code == 5
        assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_null_result_without_a_status_still_reports_feature_unavailable(self):
        """A reasonless null keeps its existing "unavailable" surface.

        The server sometimes answers a create with nothing at all — no payload
        and no status. There is no reason to report there, so the client's own
        wording remains the honest ceiling and must not regress into a bare
        decode error.
        """
        body = raw_batchexecute_body(
            [["wrb.fr", CREATE_ARTIFACT_METHOD_ID, None, None, None, None, "generic"]]
        )
        api = self._api_answering_with(
            lambda method_id, **kwargs: decode_response(
                body,
                method_id,
                allow_null=kwargs.get("allow_null", False),
                raise_on_null_status=kwargs.get("raise_on_null_status", False),
            )
        )

        with pytest.raises(ArtifactFeatureUnavailableError) as exc_info:
            await api.generate_audio("nb1")

        assert exc_info.value.artifact_type == "audio"


# ---------------------------------------------------------------------------
# #2193: an artifact that fails LATE carries no reason — pin the fallback
# ---------------------------------------------------------------------------


class TestLateFailureHasNoReason:
    """An accepted-then-FAILED artifact has no reason, and the CLI says so."""

    @pytest.mark.asyncio
    async def test_failed_row_polls_to_status_failed_with_no_error(self):
        """``Artifact`` has no failure field, so ``error`` stays ``None``."""
        api = _make_api()
        api._list_raw = AsyncMock(return_value=[_art("task_abc", ArtifactStatus.FAILED)])

        status = await api.poll_status("nb1", "task_abc")

        assert status.is_failed is True
        assert status.error is None
        assert status.error_code is None

    @pytest.mark.asyncio
    async def test_late_failure_renders_the_generic_fallback_message(self):
        """The reasonless poll result still gives the user *something*.

        Links a late-FAILED poll to ``generate_retry.generation_outcome_from_status``,
        whose ``or f"{artifact_type.title()} generation failed"`` fallback is the
        only thing standing between the user and an empty error string.
        """
        api = _make_api()
        api._list_raw = AsyncMock(return_value=[_art("task_abc", ArtifactStatus.FAILED)])

        status = await api.poll_status("nb1", "task_abc")
        outcome = generation_outcome_from_status(status, "audio")

        assert outcome.status == "failed"
        assert outcome.error == "Audio generation failed"

    def test_a_server_reason_would_win_over_the_fallback(self):
        """The fallback is a fallback: a real reason is preferred if one exists.

        Nothing on the artifact resource can populate ``error`` today (that is
        the point of the test above), so this pins the *precedence* rather than
        a reachable path — if a future RPC is ever shown to carry a reason,
        wiring it into ``GenerationStatus.error`` is all that is required.
        """
        status = GenerationStatus(task_id="x", status="failed", error="Daily limit reached")

        assert generation_outcome_from_status(status, "audio").error == "Daily limit reached"
