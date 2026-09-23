"""Tests for explicit quota rejection and unresolved artifact absence.

A listing miss does not prove quota rejection (#2432). Only server evidence
can establish a generation refusal; a missing task may still complete later.

Where a failure reason does and does not come from (#2134 / #2188 / #2193)
--------------------------------------------------------------------------
Earlier tests tried to surface a failure reason from the artifact row.
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
# wait_for_completion: only explicit server failures establish failure
# ---------------------------------------------------------------------------


class TestWaitForCompletionQuotaDetection:
    @pytest.mark.asyncio
    async def test_absence_does_not_invent_quota_evidence(self):
        api = _make_api()
        api.poll_status = AsyncMock(
            return_value=GenerationStatus(task_id="task_abc", status="not_found")
        )
        observed = []

        with pytest.raises(TimeoutError) as caught:
            await api.wait_for_completion(
                "nb1",
                "task_abc",
                initial_interval=0.001,
                max_interval=0.001,
                timeout=0.02,
                on_status_change=observed.append,
            )

        assert caught.value.last_status == "not_found"
        assert [status.status for status in observed] == ["not_found"]
        assert all(status.error is None and not status.is_rate_limited for status in observed)

    @pytest.mark.asyncio
    async def test_normal_failure_still_returns_failed(self):
        api = _make_api()
        failed = GenerationStatus(task_id="task_abc", status="failed", error="Server failure")
        api.poll_status = AsyncMock(return_value=failed)

        result = await api.wait_for_completion("nb1", "task_abc")

        assert result is failed
        assert not result.is_removed

    @pytest.mark.asyncio
    async def test_prolonged_absence_then_completion_emits_no_removal(self):
        api = _make_api()
        completed = GenerationStatus(task_id="task_abc", status="completed")
        api.poll_status = AsyncMock(
            side_effect=[
                GenerationStatus(task_id="task_abc", status="not_found") for _ in range(10)
            ]
            + [completed]
        )
        observed = []

        result = await api.wait_for_completion(
            "nb1",
            "task_abc",
            initial_interval=0.001,
            max_interval=0.001,
            on_status_change=observed.append,
        )

        assert result is completed
        assert [status.status for status in observed] == ["not_found", "completed"]


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
    async def test_late_failure_preserves_the_absent_semantic_reason(self):
        """A reasonless poll stays semantic; the CLI owns its fallback message."""
        api = _make_api()
        api._list_raw = AsyncMock(return_value=[_art("task_abc", ArtifactStatus.FAILED)])

        status = await api.poll_status("nb1", "task_abc")
        outcome = generation_outcome_from_status(status, "audio")

        assert outcome.status == "failed"
        assert outcome.error is None

    def test_a_server_reason_would_win_over_the_fallback(self):
        """The fallback is a fallback: a real reason is preferred if one exists.

        Nothing on the artifact resource can populate ``error`` today (that is
        the point of the test above), so this pins the *precedence* rather than
        a reachable path — if a future RPC is ever shown to carry a reason,
        wiring it into ``GenerationStatus.error`` is all that is required.
        """
        status = GenerationStatus(task_id="x", status="failed", error="Daily limit reached")

        assert generation_outcome_from_status(status, "audio").error == "Daily limit reached"
