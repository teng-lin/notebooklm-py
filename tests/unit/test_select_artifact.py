"""Unit tests for the centralized ``ArtifactsAPI._select_artifact`` helper.

These tests cover the behavior contract the rest of the artifact-download
plumbing relies on:

* filter candidates by ``type_code`` (other types must be ignored);
* skip non-completed artifacts (status != ``ArtifactStatus.COMPLETED``);
* honour the explicit ``artifact_id`` match (and raise when it misses);
* without an explicit id, return the **latest** completed artifact sorted by
  the raw API timestamp at ``a[15][0]``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._row_adapters import ArtifactRow
from notebooklm.rpc.types import ArtifactStatus, ArtifactTypeCode
from notebooklm.types import ArtifactNotReadyError


def _artifact(
    artifact_id: str,
    type_code: ArtifactTypeCode,
    status: ArtifactStatus,
    timestamp: int | None = 0,
) -> list:
    """Build a minimal artifact list shaped like ``_list_raw`` output.

    The layout matches what the production code reads:

    * index 0  -> id
    * index 2  -> type code
    * index 4  -> status code
    * index 15 -> ``[timestamp_seconds, ...]`` (raw API timestamp; the helper
      sorts on ``a[15][0]``)
    """
    a: list = [artifact_id, "Title", type_code, None, status]
    a.extend([None] * 10)  # pad to index 14 inclusive
    a.append([timestamp] if timestamp is not None else None)  # index 15
    return a


@pytest.fixture
def api() -> ArtifactsAPI:
    """Build an ArtifactsAPI with no-op runtime / mind-map — only the helper is exercised."""
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    mock_core = MagicMock()
    mock_core.rpc_call = AsyncMock()
    return ArtifactsAPI(
        mock_core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )


class TestSelectArtifactFiltering:
    """``type_code`` and status filters."""

    def test_filters_by_type_code_skipping_other_types(self, api: ArtifactsAPI) -> None:
        """Only artifacts matching the requested ``type_code`` are considered."""
        candidates = [
            _artifact("audio_1", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 100),
            _artifact("video_1", ArtifactTypeCode.VIDEO, ArtifactStatus.COMPLETED, 200),
            _artifact("report_1", ArtifactTypeCode.REPORT, ArtifactStatus.COMPLETED, 300),
        ]

        result = api._select_artifact(
            candidates,
            artifact_id=None,
            type_name="Video",
            no_result_error_key="video",
            type_code=ArtifactTypeCode.VIDEO,
        )

        assert result[0] == "video_1"

    def test_filters_out_non_completed_artifacts(self, api: ArtifactsAPI) -> None:
        """Pending / processing / failed artifacts are not selectable."""
        candidates = [
            _artifact("audio_pending", ArtifactTypeCode.AUDIO, ArtifactStatus.PENDING, 100),
            _artifact("audio_processing", ArtifactTypeCode.AUDIO, ArtifactStatus.PROCESSING, 200),
            _artifact("audio_failed", ArtifactTypeCode.AUDIO, ArtifactStatus.FAILED, 300),
            _artifact("audio_done", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 50),
        ]

        result = api._select_artifact(
            candidates,
            artifact_id=None,
            type_name="Audio",
            no_result_error_key="audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        assert result[0] == "audio_done"

    def test_raises_when_no_matching_completed_candidate(self, api: ArtifactsAPI) -> None:
        """Empty candidate list (after filtering) raises ``ArtifactNotReadyError``."""
        candidates = [
            # Wrong type
            _artifact("video_1", ArtifactTypeCode.VIDEO, ArtifactStatus.COMPLETED, 100),
            # Right type but not completed
            _artifact("audio_pending", ArtifactTypeCode.AUDIO, ArtifactStatus.PENDING, 200),
        ]

        with pytest.raises(ArtifactNotReadyError):
            api._select_artifact(
                candidates,
                artifact_id=None,
                type_name="Audio",
                no_result_error_key="audio",
                type_code=ArtifactTypeCode.AUDIO,
            )

    def test_skips_malformed_entries_silently(self, api: ArtifactsAPI) -> None:
        """Non-list / too-short entries are silently skipped, not exploded on."""
        candidates: list = [
            "not_a_list",  # wrong shape
            [],  # too short
            [None, None, None],  # too short (no a[4])
            _artifact("ok", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 100),
        ]

        result = api._select_artifact(
            candidates,
            artifact_id=None,
            type_name="Audio",
            no_result_error_key="audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        assert result[0] == "ok"

    def test_adapter_selector_returns_artifact_row(self, api: ArtifactsAPI) -> None:
        """New internal selector returns the adapter while preserving raw selector behavior."""
        candidates = [
            _artifact("old", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 100),
            _artifact("newest", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 999),
        ]

        result = api._listing.select_completed_artifact_row(
            candidates,
            artifact_id=None,
            type_name="Audio",
            no_result_error_key="audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        assert isinstance(result, ArtifactRow)
        assert result.id == "newest"


class TestSelectArtifactExplicitId:
    """Explicit-ID match preserves the original behavior."""

    def test_explicit_id_match_returns_that_artifact(self, api: ArtifactsAPI) -> None:
        """The artifact with the matching id wins, even if older."""
        candidates = [
            _artifact("a", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 500),
            _artifact("b", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 100),
        ]

        result = api._select_artifact(
            candidates,
            artifact_id="b",
            type_name="Audio",
            no_result_error_key="audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        assert result[0] == "b"

    def test_explicit_id_miss_raises(self, api: ArtifactsAPI) -> None:
        """Explicit id that does not match any candidate raises."""
        candidates = [
            _artifact("a", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 500),
        ]

        with pytest.raises(ArtifactNotReadyError):
            api._select_artifact(
                candidates,
                artifact_id="nonexistent",
                type_name="Audio",
                no_result_error_key="audio",
                type_code=ArtifactTypeCode.AUDIO,
            )

    def test_explicit_id_only_searches_within_type(self, api: ArtifactsAPI) -> None:
        """An id that exists but belongs to a different type still misses."""
        candidates = [
            _artifact("shared_id", ArtifactTypeCode.VIDEO, ArtifactStatus.COMPLETED, 100),
            _artifact("audio_only", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 200),
        ]

        # Asking for "shared_id" while filtering for AUDIO should miss.
        with pytest.raises(ArtifactNotReadyError):
            api._select_artifact(
                candidates,
                artifact_id="shared_id",
                type_name="Audio",
                no_result_error_key="audio",
                type_code=ArtifactTypeCode.AUDIO,
            )


class TestSelectArtifactSortByTimestamp:
    """Sort key ``a[15][0]`` is the raw API timestamp — preserve it."""

    def test_returns_latest_by_timestamp_at_index_15_position_0(self, api: ArtifactsAPI) -> None:
        """When multiple completed artifacts exist, the one with the largest
        ``a[15][0]`` wins — regardless of input order."""
        candidates = [
            _artifact("old", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 100),
            _artifact("newest", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 999),
            _artifact("middle", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 500),
        ]

        result = api._select_artifact(
            candidates,
            artifact_id=None,
            type_name="Audio",
            no_result_error_key="audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        assert result[0] == "newest"

    def test_handles_missing_or_malformed_timestamps_gracefully(self, api: ArtifactsAPI) -> None:
        """Artifacts whose ``a[15]`` is missing / not-a-list / empty sort as 0
        (so a real timestamp still wins) — no exception."""
        candidates = [
            _artifact("with_ts", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 100),
        ]

        # Artifact without index 15 at all.
        short: list = ["short_one", "Title", ArtifactTypeCode.AUDIO, None, ArtifactStatus.COMPLETED]
        candidates.append(short)

        # Artifact with a non-list at index 15.
        bad_shape: list = list(
            _artifact("bad_shape", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 0)
        )
        bad_shape[15] = "not_a_list"
        candidates.append(bad_shape)

        # Artifact with an empty list at index 15.
        empty_ts: list = list(
            _artifact("empty_ts", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 0)
        )
        empty_ts[15] = []
        candidates.append(empty_ts)

        result = api._select_artifact(
            candidates,
            artifact_id=None,
            type_name="Audio",
            no_result_error_key="audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        # The only artifact with a real timestamp wins.
        assert result[0] == "with_ts"

    def test_handles_none_at_timestamp_position_without_typeerror(self, api: ArtifactsAPI) -> None:
        """``a[15][0] is None`` must not crash the sort with ``TypeError``.

        Python 3 refuses to compare ``None`` against ``int``; if the API
        ever emits ``[null, ...]`` at index 15 the sort must coerce that
        to ``0`` rather than blow up at runtime.
        """
        # Artifact with a real timestamp.
        with_ts = _artifact("with_ts", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 100)
        # Artifact with a None timestamp at position 0 of index 15.
        none_ts: list = _artifact("none_ts", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 0)
        none_ts[15] = [None, "extra"]

        candidates = [with_ts, none_ts]

        result = api._select_artifact(
            candidates,
            artifact_id=None,
            type_name="Audio",
            no_result_error_key="audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        # The artifact with a real timestamp wins (None coerces to 0).
        assert result[0] == "with_ts"


class TestSelectArtifactErrorKeys:
    """Verify ``ArtifactNotReadyError.artifact_type`` for both raise paths.

    This locks in the asymmetry that ``download_video`` relies on: the
    explicit-id-miss path derives its key from ``type_name`` (lowercased,
    spaces->underscores) while the empty-list path uses the caller-supplied
    ``no_result_error_key`` verbatim.
    """

    def test_explicit_id_miss_error_key_derived_from_type_name(self, api: ArtifactsAPI) -> None:
        """ID-miss path: ``artifact_type == type_name.lower().replace(' ', '_')``."""
        candidates = [
            _artifact("real_id", ArtifactTypeCode.VIDEO, ArtifactStatus.COMPLETED, 100),
        ]

        with pytest.raises(ArtifactNotReadyError) as exc_info:
            api._select_artifact(
                candidates,
                artifact_id="nonexistent",
                type_name="Video",
                no_result_error_key="video_overview",
                type_code=ArtifactTypeCode.VIDEO,
            )

        assert exc_info.value.artifact_type == "video"
        assert exc_info.value.artifact_id == "nonexistent"

    def test_explicit_id_miss_with_space_in_type_name(self, api: ArtifactsAPI) -> None:
        """Spaces in ``type_name`` become underscores (e.g. "Slide deck" -> "slide_deck")."""
        candidates = [
            _artifact("real_id", ArtifactTypeCode.SLIDE_DECK, ArtifactStatus.COMPLETED, 100),
        ]

        with pytest.raises(ArtifactNotReadyError) as exc_info:
            api._select_artifact(
                candidates,
                artifact_id="nonexistent",
                type_name="Slide deck",
                no_result_error_key="slide_deck",
                type_code=ArtifactTypeCode.SLIDE_DECK,
            )

        assert exc_info.value.artifact_type == "slide_deck"

    def test_no_result_error_key_used_verbatim(self, api: ArtifactsAPI) -> None:
        """Empty-list path: ``artifact_type == no_result_error_key`` verbatim.

        ``download_video`` exploits this to raise ``video_overview`` for the
        empty-list case while still raising ``video`` for explicit-id miss.
        """
        with pytest.raises(ArtifactNotReadyError) as exc_info:
            api._select_artifact(
                [],
                artifact_id=None,
                type_name="Video",
                no_result_error_key="video_overview",
                type_code=ArtifactTypeCode.VIDEO,
            )

        assert exc_info.value.artifact_type == "video_overview"
        assert exc_info.value.artifact_id is None

    def test_error_type_for_id_miss_vs_empty_list(self, api: ArtifactsAPI) -> None:
        """End-to-end asymmetry: id-miss uses ``type_name``, empty-list uses key.

        Locks in the contract ``download_video`` depends on:
        same call shape, same ``type_name`` / ``no_result_error_key`` pair,
        but the raised ``artifact_type`` differs between the two paths.
        """
        candidates = [
            _artifact("a", ArtifactTypeCode.VIDEO, ArtifactStatus.COMPLETED, 100),
        ]

        # Path 1: explicit-id miss — derived from type_name ("Video" -> "video").
        with pytest.raises(ArtifactNotReadyError) as exc_info:
            api._select_artifact(
                candidates,
                artifact_id="nonexistent",
                type_name="Video",
                no_result_error_key="video_overview",
                type_code=ArtifactTypeCode.VIDEO,
            )
        assert exc_info.value.artifact_type == "video"

        # Path 2: empty list — uses ``no_result_error_key`` verbatim.
        with pytest.raises(ArtifactNotReadyError) as exc_info:
            api._select_artifact(
                [],
                artifact_id=None,
                type_name="Video",
                no_result_error_key="video_overview",
                type_code=ArtifactTypeCode.VIDEO,
            )
        assert exc_info.value.artifact_type == "video_overview"


class TestSelectArtifactDoesNotMutateInput:
    """The helper must not mutate the caller's candidate list."""

    def test_input_list_unchanged_after_selection(self, api: ArtifactsAPI) -> None:
        """The raw artifact list passed in must be unchanged (order preserved)."""
        original = [
            _artifact("old", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 100),
            _artifact("newest", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 999),
            _artifact("middle", ArtifactTypeCode.AUDIO, ArtifactStatus.COMPLETED, 500),
        ]
        # Snapshot of (id, timestamp) tuples in input order.
        snapshot = [(a[0], a[15][0]) for a in original]

        api._select_artifact(
            original,
            artifact_id=None,
            type_name="Audio",
            no_result_error_key="audio",
            type_code=ArtifactTypeCode.AUDIO,
        )

        after = [(a[0], a[15][0]) for a in original]
        assert after == snapshot
