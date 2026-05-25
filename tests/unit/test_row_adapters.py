"""Tests for ``notebooklm._row_adapters.ArtifactRow``.

The adapter centralises position knowledge for the ``LIST_ARTIFACTS`` row
shape so consumers (``Artifact.from_api_response`` and
``ArtifactListingService.select_artifact``) read named properties instead
of open-coding ``data[2]`` / ``data[4]`` / ``data[15]``. See
``docs/improvement.md`` §6.2 for the motivation and
``src/notebooklm/_row_adapters.py`` for the position contract.

These tests cover three layers:

1. **Position-contract pin** — the canary that fails loudly if anyone
   edits a position constant. When this fails, the diff is the
   wire-shape change signal Google has rotated something.
2. **Shape handling** — missing trailing positions return sensible
   defaults; deep descent goes through ``safe_index`` so strict-mode
   drift raises ``UnknownRPCMethodError``.
3. **matches_type** — the predicate used by ``select_artifact``.
"""

from __future__ import annotations

import pytest

from notebooklm._row_adapters import ArtifactRow
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc.types import ArtifactStatus, ArtifactTypeCode

# ---------------------------------------------------------------------------
# 1. Position-contract pin (the canary)
# ---------------------------------------------------------------------------


class TestPositionContract:
    """If any of these assertions fail, Google has likely reshaped the wire.

    Changing a position constant is the *only* legitimate reason for one
    of these tests to need updating. When that happens, the failing diff
    serves as the audit trail for the wire-shape change.
    """

    def test_id_position_is_0(self) -> None:
        assert ArtifactRow._ID_POS == 0

    def test_title_position_is_1(self) -> None:
        assert ArtifactRow._TITLE_POS == 1

    def test_type_position_is_2(self) -> None:
        assert ArtifactRow._TYPE_POS == 2

    def test_status_position_is_4(self) -> None:
        assert ArtifactRow._STATUS_POS == 4

    def test_options_position_is_9(self) -> None:
        assert ArtifactRow._OPTIONS_POS == 9

    def test_timestamp_position_is_15(self) -> None:
        assert ArtifactRow._TIMESTAMP_POS == 15

    def test_all_positions_at_once(self) -> None:
        """A single dict pin so a sweeping reshape (e.g. all positions
        shift by one because Google inserted a new leading element)
        fails with one informative assertion rather than six."""
        assert (
            ArtifactRow._ID_POS,
            ArtifactRow._TITLE_POS,
            ArtifactRow._TYPE_POS,
            ArtifactRow._STATUS_POS,
            ArtifactRow._OPTIONS_POS,
            ArtifactRow._TIMESTAMP_POS,
        ) == (0, 1, 2, 4, 9, 15)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _full_row(
    artifact_id: str = "art_id",
    title: str = "Title",
    type_code: int = ArtifactTypeCode.AUDIO,
    status: int = ArtifactStatus.COMPLETED,
    variant: int | None = None,
    timestamp: int | None = 1_700_000_000,
) -> list:
    """Build a full 16-element row matching the ``LIST_ARTIFACTS`` shape.

    Mirrors the helper used in ``tests/unit/test_select_artifact.py`` so
    fixtures stay consistent across the artifact-adapter test surface.
    """
    row: list = [artifact_id, title, type_code, None, status]
    # Pad positions 5..8.
    row.extend([None] * 4)
    # Position 9: options block — ``[unused, [variant]]``.
    if variant is None:
        row.append(None)
    else:
        row.append([None, [variant]])
    # Pad positions 10..14.
    row.extend([None] * 5)
    # Position 15: ``[timestamp, ...]``.
    if timestamp is None:
        row.append(None)
    else:
        row.append([timestamp])
    return row


# ---------------------------------------------------------------------------
# 2. Shape handling (sensible defaults for short/malformed rows)
# ---------------------------------------------------------------------------


class TestRequiredPositionsAcceptShortRows:
    """Top-level positions tolerate short rows in BOTH soft and strict modes.

    This is the historical ``Artifact.from_api_response`` contract: a
    minimal row like ``["id", "title", 1, None, 3]`` must read fine
    even though positions 9 and 15 are absent.
    """

    def test_empty_row_yields_default_id_and_title(self) -> None:
        row = ArtifactRow([])
        assert row.id == ""
        assert row.title == ""

    def test_empty_row_yields_default_type_and_status(self) -> None:
        row = ArtifactRow([])
        assert row.type_code == 0
        assert row.status == 0

    def test_id_coerced_to_string(self) -> None:
        """Defensive: a non-string id is stringified."""
        row = ArtifactRow([12345, "Title"])
        assert row.id == "12345"

    def test_title_coerced_to_string(self) -> None:
        row = ArtifactRow(["id", 999])
        assert row.title == "999"

    def test_non_int_type_code_falls_back_to_zero(self) -> None:
        """A non-int at position 2 normalises to ``0`` rather than
        leaking ``None`` past the ``type_code: int`` contract."""
        row = ArtifactRow(["id", "title", None, None, 3])
        assert row.type_code == 0

    def test_non_int_status_falls_back_to_zero(self) -> None:
        row = ArtifactRow(["id", "title", 1, None, None])
        assert row.status == 0

    def test_minimal_row_no_variant_no_timestamp(self) -> None:
        """The smallest meaningful row: positions 0..4 present, 9 and 15 absent."""
        row = ArtifactRow(["art_minimal", "Audio", 1, None, 3])
        assert row.id == "art_minimal"
        assert row.title == "Audio"
        assert row.type_code == 1
        assert row.status == 3
        assert row.variant is None
        assert row.created_at_raw is None
        assert row.created_at is None


class TestVariantDescent:
    """``data[9][1][0]`` descent — used to distinguish QUIZ vs FLASHCARDS."""

    def test_variant_extracted_from_options_block(self) -> None:
        row = ArtifactRow(_full_row(type_code=ArtifactTypeCode.QUIZ, variant=2))
        assert row.variant == 2

    def test_flashcards_variant(self) -> None:
        row = ArtifactRow(_full_row(type_code=ArtifactTypeCode.QUIZ, variant=1))
        assert row.variant == 1

    def test_missing_options_position_returns_none(self) -> None:
        """Short row without position 9 yields ``None`` (no strict-mode raise)."""
        row = ArtifactRow(["id", "title", 4, None, 3])
        assert row.variant is None

    def test_options_block_is_none_returns_none_softly(self) -> None:
        """``data[9] = None`` (older cassette shape) degrades silently —
        preserves the legacy ``isinstance(data[9], list)`` guard so the
        adapter never invokes ``safe_index`` against a non-list root."""
        raw = _full_row(variant=None)  # already puts None at position 9
        assert raw[ArtifactRow._OPTIONS_POS] is None
        row = ArtifactRow(raw)
        assert row.variant is None

    def test_non_int_variant_falls_back_to_none(self) -> None:
        """A string at ``[9][1][0]`` is not a valid variant code."""
        raw = _full_row()
        raw[ArtifactRow._OPTIONS_POS] = [None, ["not_an_int"]]
        row = ArtifactRow(raw)
        assert row.variant is None


class TestTimestampDescent:
    """``data[15][0]`` descent — used for ``created_at`` and sort key."""

    def test_created_at_raw_returns_int_seconds(self) -> None:
        row = ArtifactRow(_full_row(timestamp=1_700_000_000))
        assert row.created_at_raw == 1_700_000_000

    def test_created_at_converts_to_datetime(self) -> None:
        row = ArtifactRow(_full_row(timestamp=1_700_000_000))
        assert row.created_at is not None
        assert row.created_at.timestamp() == 1_700_000_000

    def test_missing_timestamp_position_returns_none(self) -> None:
        row = ArtifactRow(["id", "title", 1, None, 3])
        assert row.created_at_raw is None
        assert row.created_at is None

    def test_timestamp_block_is_none_degrades_softly(self) -> None:
        """``data[15] = None`` returns ``None`` without raising even in
        strict mode (legacy ``isinstance(data[15], list)`` guard)."""
        raw = _full_row(timestamp=None)  # explicit None at position 15
        assert raw[ArtifactRow._TIMESTAMP_POS] is None
        row = ArtifactRow(raw)
        assert row.created_at_raw is None

    def test_timestamp_block_is_non_list_degrades_softly(self) -> None:
        raw = _full_row(timestamp=0)
        raw[ArtifactRow._TIMESTAMP_POS] = "not_a_list"
        row = ArtifactRow(raw)
        assert row.created_at_raw is None
        assert row.created_at is None

    def test_timestamp_block_empty_returns_none_in_both_modes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``data[15] = []`` is an accepted edge case (some cassettes
        legitimately produce this), not strict-mode drift. The adapter
        short-circuits an empty envelope so ``safe_index`` is never
        invoked against it — preserves the legacy
        ``len(a) > 15 and isinstance(a[15], list) and a[15]`` contract
        that ``tests/unit/test_select_artifact.py
        ::test_handles_missing_or_malformed_timestamps_gracefully``
        depends on."""
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        raw = _full_row(timestamp=0)
        raw[ArtifactRow._TIMESTAMP_POS] = []
        row = ArtifactRow(raw)
        assert row.created_at_raw is None  # no exception in strict mode

        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "0")
        # No DeprecationWarning either — short-circuit avoids safe_index entirely.
        assert ArtifactRow(raw).created_at_raw is None

    def test_none_at_timestamp_position_zero(self) -> None:
        """``data[15] = [None, ...]`` is NOT a drift signal — it is the
        legacy ``[None, "extra"]`` shape that the sort key falsy-coerces
        to ``0``. The adapter exposes that as ``created_at_raw is None``
        and lets the caller's ``or 0`` do the coercion."""
        raw = _full_row(timestamp=0)
        raw[ArtifactRow._TIMESTAMP_POS] = [None, "extra"]
        row = ArtifactRow(raw)
        assert row.created_at_raw is None


class TestStrictModeOnDeepDrift:
    """When a present position has a *malformed inner shape*, strict mode raises."""

    def test_options_block_with_too_short_inner_raises_strict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``data[9] = [single_element]`` lacks ``[9][1]`` — strict mode
        surfaces this as ``UnknownRPCMethodError`` because the descent
        through index 1 fails on a real list (not a None envelope)."""
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        raw = _full_row()
        raw[ArtifactRow._OPTIONS_POS] = [None]  # length 1, no [1]
        row = ArtifactRow(raw)
        with pytest.raises(UnknownRPCMethodError):
            _ = row.variant

    def test_options_block_with_too_short_inner_soft_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "0")
        raw = _full_row()
        raw[ArtifactRow._OPTIONS_POS] = [None]
        row = ArtifactRow(raw)
        with pytest.warns(DeprecationWarning):
            assert row.variant is None


# ---------------------------------------------------------------------------
# 3. matches_type predicate
# ---------------------------------------------------------------------------


class TestMatchesType:
    def test_matches_when_type_codes_align(self) -> None:
        row = ArtifactRow(_full_row(type_code=ArtifactTypeCode.AUDIO))
        assert row.matches_type(ArtifactTypeCode.AUDIO) is True

    def test_rejects_mismatched_type_code(self) -> None:
        row = ArtifactRow(_full_row(type_code=ArtifactTypeCode.VIDEO))
        assert row.matches_type(ArtifactTypeCode.AUDIO) is False

    def test_completed_only_accepts_completed_artifact(self) -> None:
        row = ArtifactRow(
            _full_row(type_code=ArtifactTypeCode.AUDIO, status=ArtifactStatus.COMPLETED)
        )
        assert row.matches_type(ArtifactTypeCode.AUDIO, completed_only=True) is True

    def test_completed_only_rejects_pending_artifact(self) -> None:
        row = ArtifactRow(
            _full_row(type_code=ArtifactTypeCode.AUDIO, status=ArtifactStatus.PENDING)
        )
        assert row.matches_type(ArtifactTypeCode.AUDIO, completed_only=True) is False

    def test_completed_only_rejects_processing_artifact(self) -> None:
        row = ArtifactRow(
            _full_row(type_code=ArtifactTypeCode.AUDIO, status=ArtifactStatus.PROCESSING)
        )
        assert row.matches_type(ArtifactTypeCode.AUDIO, completed_only=True) is False

    def test_completed_only_rejects_failed_artifact(self) -> None:
        row = ArtifactRow(_full_row(type_code=ArtifactTypeCode.AUDIO, status=ArtifactStatus.FAILED))
        assert row.matches_type(ArtifactTypeCode.AUDIO, completed_only=True) is False

    def test_completed_only_false_accepts_any_status(self) -> None:
        """Without ``completed_only``, status is ignored — used by listing
        paths that want every artifact of a given type regardless of
        readiness."""
        row = ArtifactRow(
            _full_row(type_code=ArtifactTypeCode.AUDIO, status=ArtifactStatus.PROCESSING)
        )
        assert row.matches_type(ArtifactTypeCode.AUDIO) is True

    def test_int_type_code_argument_works(self) -> None:
        """Callers passing a raw ``int`` (not the enum) still match."""
        row = ArtifactRow(_full_row(type_code=ArtifactTypeCode.AUDIO))
        assert row.matches_type(1) is True  # ArtifactTypeCode.AUDIO == 1

    def test_completed_only_on_short_row_returns_false(self) -> None:
        """A row too short to carry status (``len <= 4``) reads status as
        ``0``; ``completed_only`` then rejects it. Documents that the
        ``select_artifact`` filter is safe against short rows even when
        the candidate-list length-guard in the caller is relaxed."""
        row = ArtifactRow(["id", "title", 1])  # no position 4
        assert row.status == 0
        assert row.matches_type(1, completed_only=True) is False
        # Without completed_only, the type alone matches.
        assert row.matches_type(1) is True


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestImmutability:
    """The adapter is frozen so the wrapped row can't be swapped out."""

    def test_cannot_assign_to_raw(self) -> None:
        """``dataclasses.FrozenInstanceError`` is an ``AttributeError``
        subclass, so the narrower expectation here both pins the contract
        and serves as a real signal — if the assignment raised something
        else entirely (e.g. ``ValueError``) the test would now fail."""
        row = ArtifactRow([])
        with pytest.raises(AttributeError):
            row._raw = [1, 2, 3]  # type: ignore[misc]

    def test_does_not_mutate_wrapped_row(self) -> None:
        """Reading properties is side-effect-free — the wrapped row is
        not modified by sort key computation or type matching."""
        raw = _full_row(timestamp=1_700_000_000, variant=2)
        snapshot = list(raw)
        row = ArtifactRow(raw)

        # Touch every property.
        _ = row.id
        _ = row.title
        _ = row.type_code
        _ = row.status
        _ = row.variant
        _ = row.created_at_raw
        _ = row.created_at
        row.matches_type(ArtifactTypeCode.AUDIO, completed_only=True)

        assert raw == snapshot


# ---------------------------------------------------------------------------
# Method-ID plumbing (verifies safe_index gets enough context for drift logs)
# ---------------------------------------------------------------------------


class TestMethodIdPropagation:
    """``safe_index`` includes ``method_id`` and ``source`` in its drift
    logs / strict-mode exceptions — verify the adapter wires those
    through correctly."""

    def test_strict_mode_exception_carries_method_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        raw = _full_row()
        raw[ArtifactRow._OPTIONS_POS] = [None]  # forces inner drift
        row = ArtifactRow(raw)
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            _ = row.variant
        # method_id default is RPCMethod.LIST_ARTIFACTS.value == "gArtLc".
        assert exc_info.value.method_id == "gArtLc"
        assert "ArtifactRow.variant" in str(exc_info.value)

    def test_custom_method_id_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Callers wrapping a row that came from a non-LIST_ARTIFACTS
        method can override ``method_id`` so drift diagnostics point at
        the correct RPC."""
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        raw = _full_row()
        raw[ArtifactRow._OPTIONS_POS] = [None]
        row = ArtifactRow(raw, method_id="custom_method")
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            _ = row.variant
        assert exc_info.value.method_id == "custom_method"
