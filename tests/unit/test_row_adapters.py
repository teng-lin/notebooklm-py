"""Tests for ``notebooklm._row_adapters`` (``ArtifactRow`` + ``NoteRow`` + ``SourceRow``).

The adapters centralise position knowledge for the ``LIST_ARTIFACTS``,
``GET_NOTES_AND_MIND_MAPS``, and source row shapes so consumers
(``Artifact.from_api_response``, ``ArtifactListingService.select_artifact``,
``NoteService.classify_row``, ``NotesAPI._parse_note``,
``Source.from_api_response``, ``SourceLister._parse_source``,
``NotebooksAPI.get_source_ids``) read named properties instead of
open-coding ``data[2]`` / ``data[4]`` / ``data[15]`` / ``row[1][1]`` /
``row[1][4]`` / ``data[0][0]`` / ``metadata[4]``. See
``docs/improvement.md`` §6.2 for the motivation and
``src/notebooklm/_row_adapters.py`` for the position contracts.

These tests cover three layers per adapter:

1. **Position-contract pin** — the canary that fails loudly if anyone
   edits a position constant. When this fails, the diff is the
   wire-shape change signal Google has rotated something.
2. **Shape handling** — missing trailing positions return sensible
   defaults; deep descent goes through ``safe_index`` so strict-mode
   drift raises ``UnknownRPCMethodError``.
3. **Predicate / domain helpers** — ``matches_type`` for artifacts,
   ``is_deleted`` / ``is_mind_map_content`` for notes, multi-shape
   dispatch (``from_unknown_shape``) for sources.
"""

from __future__ import annotations

import json

import pytest

from notebooklm._row_adapters import ArtifactRow, NoteRow, SourceRow, SourceRowShape
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc.types import ArtifactStatus, ArtifactTypeCode, SourceStatus

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


# ===========================================================================
# NoteRow — note / mind-map row adapter for GET_NOTES_AND_MIND_MAPS
# ===========================================================================


class TestNoteRowPositionContract:
    """The canary for the ``GET_NOTES_AND_MIND_MAPS`` row shape.

    These pin tests fail loudly if anyone edits a position constant.
    When that happens, the failing diff IS the audit trail for the
    Google-side wire reshape. See
    ``src/notebooklm/_row_adapters.py:NoteRow`` for the shape contract.
    """

    def test_id_position_is_0(self) -> None:
        assert NoteRow._ID_POS == 0

    def test_content_position_is_1(self) -> None:
        assert NoteRow._CONTENT_POS == 1

    def test_status_position_is_2(self) -> None:
        assert NoteRow._STATUS_POS == 2

    def test_inner_content_position_is_1(self) -> None:
        assert NoteRow._INNER_CONTENT_POS == 1

    def test_inner_title_position_is_4(self) -> None:
        assert NoteRow._INNER_TITLE_POS == 4

    def test_deleted_sentinel_is_2(self) -> None:
        assert NoteRow._DELETED_SENTINEL == 2

    def test_all_positions_at_once(self) -> None:
        """A single tuple pin so a sweeping reshape (e.g. Google inserts
        a new leading element shifting every position by one) fails with
        one informative assertion rather than six."""
        assert (
            NoteRow._ID_POS,
            NoteRow._CONTENT_POS,
            NoteRow._STATUS_POS,
            NoteRow._INNER_CONTENT_POS,
            NoteRow._INNER_TITLE_POS,
            NoteRow._DELETED_SENTINEL,
        ) == (0, 1, 2, 1, 4, 2)


# ---------------------------------------------------------------------------
# NoteRow helpers — fixtures matching the in-the-wild shape varieties
# ---------------------------------------------------------------------------


def _legacy_note_row(
    note_id: str = "note_id",
    content: str = "Plain note body",
) -> list:
    """Legacy shape: ``[id, content_string]``.

    Older rows arrive in this shape; the adapter must keep extracting
    content from position 1 directly and return ``""`` for title.
    """
    return [note_id, content]


def _current_note_row(
    note_id: str = "note_id",
    content: str = "Plain note body",
    title: str = "Note Title",
    metadata: object = None,
) -> list:
    """Current shape: ``[id, [id, content, metadata, None, title]]``.

    Standard production shape since the metadata envelope rollout —
    content at ``raw[1][1]``, title at ``raw[1][4]``.
    """
    return [note_id, [note_id, content, metadata, None, title]]


def _deleted_note_row(note_id: str = "note_id") -> list:
    """Soft-deletion sentinel: ``[id, None, 2]``."""
    return [note_id, None, 2]


# ---------------------------------------------------------------------------
# NoteRow — id and is_deleted
# ---------------------------------------------------------------------------


class TestNoteRowId:
    def test_id_extracted_from_position_0(self) -> None:
        assert NoteRow(_legacy_note_row(note_id="abc")).id == "abc"

    def test_id_extracted_from_current_shape(self) -> None:
        assert NoteRow(_current_note_row(note_id="xyz")).id == "xyz"

    def test_id_extracted_from_deleted_row(self) -> None:
        """Deleted rows still expose their id so callers can correlate
        the deletion with prior reads."""
        assert NoteRow(_deleted_note_row(note_id="gone")).id == "gone"

    def test_id_empty_for_empty_row(self) -> None:
        assert NoteRow([]).id == ""

    def test_id_coerced_to_string(self) -> None:
        """A non-string id is stringified — defensive against drift in
        position 0's type."""
        assert NoteRow([12345, "body"]).id == "12345"


class TestNoteRowIsDeleted:
    """Centralised ``row[1] is None and row[2] == 2`` check."""

    def test_canonical_deleted_shape(self) -> None:
        assert NoteRow(_deleted_note_row()).is_deleted is True

    def test_deleted_with_trailing_metadata(self) -> None:
        """Some cassettes carry trailing metadata after the sentinel —
        the adapter should still classify it as deleted."""
        row = [*_deleted_note_row(), {"extra": True}]
        assert NoteRow(row).is_deleted is True

    def test_legacy_active_row_not_deleted(self) -> None:
        assert NoteRow(_legacy_note_row()).is_deleted is False

    def test_current_active_row_not_deleted(self) -> None:
        assert NoteRow(_current_note_row()).is_deleted is False

    def test_status_zero_not_deleted(self) -> None:
        """Status ``0`` at position 2 is not the soft-delete sentinel."""
        assert NoteRow(["id", None, 0]).is_deleted is False

    def test_status_other_int_not_deleted(self) -> None:
        assert NoteRow(["id", None, 5]).is_deleted is False

    def test_content_not_none_not_deleted(self) -> None:
        """A row with ``row[2] == 2`` but content present is NOT
        deleted — both conditions are required."""
        assert NoteRow(["id", "content", 2]).is_deleted is False

    def test_short_row_not_deleted(self) -> None:
        """Rows too short to carry position 2 are never deleted."""
        assert NoteRow([]).is_deleted is False
        assert NoteRow(["id"]).is_deleted is False
        assert NoteRow(["id", None]).is_deleted is False


# ---------------------------------------------------------------------------
# NoteRow — multi-shape content dispatch (the whole point of the adapter)
# ---------------------------------------------------------------------------


class TestNoteRowContentLegacyShape:
    """Legacy shape: ``row[1]`` is the content string directly."""

    def test_content_from_legacy_shape(self) -> None:
        assert NoteRow(_legacy_note_row(content="legacy body")).content == "legacy body"

    def test_empty_string_content_returned(self) -> None:
        """An empty content string is a *valid* legacy payload — must
        not collapse to ``None``."""
        assert NoteRow(["id", ""]).content == ""


class TestNoteRowContentCurrentShape:
    """Current shape: ``row[1][1]`` is the content string via the envelope."""

    def test_content_from_current_shape(self) -> None:
        row = _current_note_row(content="nested body")
        assert NoteRow(row).content == "nested body"

    def test_content_with_full_envelope(self) -> None:
        row = ["nid", ["nid", "body", {"meta": 1}, None, "Title"]]
        assert NoteRow(row).content == "body"


class TestNoteRowContentDegradation:
    """Unknown / short / mistyped slots return ``None`` in soft mode."""

    def test_empty_row_returns_none(self) -> None:
        assert NoteRow([]).content is None

    def test_id_only_row_returns_none(self) -> None:
        assert NoteRow(["id"]).content is None

    def test_deleted_row_content_is_none(self) -> None:
        assert NoteRow(_deleted_note_row()).content is None

    def test_int_at_position_1_returns_none(self) -> None:
        """A non-str/non-list slot is not extractable content."""
        assert NoteRow(["id", 123]).content is None

    def test_dict_at_position_1_returns_none(self) -> None:
        """Dicts are not a recognised shape variant — soft-degrade."""
        assert NoteRow(["id", {"oops": True}]).content is None

    def test_inner_non_string_content_returns_none(self) -> None:
        """The inner envelope is long enough for ``[1]`` indexing but
        the value at ``inner[1]`` is not a string — ``safe_index``
        succeeds and the ``isinstance(value, str)`` filter degrades
        the result to ``None`` rather than leaking a non-string past
        the ``content: str | None`` contract. Closes claude[bot]'s
        Issue 2 from #1028's first review."""
        assert NoteRow(["id", ["inner_id", 99]]).content is None
        assert NoteRow(["id", ["inner_id", None]]).content is None


class TestNoteRowShortInnerIsNotDrift:
    """Short inner envelopes are a legitimate production shape, not drift.

    Some cassettes legitimately carry rows like ``[id, [id, content]]``
    (length-2 inner with no metadata/title slots — predates the title
    rollout). The adapter MUST length-guard these before invoking
    ``safe_index`` so strict mode never raises on a real production
    shape. This is the key behavioural difference from
    :class:`ArtifactRow`, whose options-block descent has no
    length-guard because every production options block is length 2.
    """

    def test_inner_length_1_returns_none_in_strict_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        # ``[id_only]`` is below the content slot — length-guarded to
        # ``None`` without invoking ``safe_index``, so strict mode does
        # NOT raise.
        assert NoteRow(["id", ["id_only"]]).content is None

    def test_inner_length_2_returns_content_in_strict_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Length-2 inner is just barely long enough to carry the
        content slot at position 1 — extracts cleanly."""
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        assert NoteRow(["id", ["id", "the body"]]).content == "the body"

    def test_inner_length_2_title_returns_empty_in_strict_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The length-2 inner lacks a title slot — length-guarded to
        ``""`` without invoking ``safe_index``."""
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        assert NoteRow(["id", ["id", "the body"]]).title == ""

    def test_empty_inner_returns_none_in_strict_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        assert NoteRow(["id", []]).content is None
        assert NoteRow(["id", []]).title == ""


# ---------------------------------------------------------------------------
# NoteRow — title (current-shape only)
# ---------------------------------------------------------------------------


class TestNoteRowTitle:
    def test_title_extracted_from_current_shape(self) -> None:
        row = _current_note_row(title="My Title")
        assert NoteRow(row).title == "My Title"

    def test_title_empty_for_legacy_shape(self) -> None:
        """Legacy ``[id, content_string]`` has no title slot — return
        ``""`` rather than guessing at one."""
        assert NoteRow(_legacy_note_row()).title == ""

    def test_title_empty_for_deleted_row(self) -> None:
        assert NoteRow(_deleted_note_row()).title == ""

    def test_title_empty_for_short_row(self) -> None:
        assert NoteRow([]).title == ""
        assert NoteRow(["id"]).title == ""

    def test_title_empty_when_inner_too_short(self) -> None:
        """``inner = [id, content]`` (length 2) predates the title
        slot — degrade to ``""`` without raising, since this is a
        legitimate variant of the current shape (not drift)."""
        assert NoteRow(["id", ["id", "content"]]).title == ""

    def test_title_empty_when_inner_has_no_title_slot(self) -> None:
        """``inner = [id, content, meta, None]`` (length 4) is still
        below the title slot at position 4."""
        assert NoteRow(["id", ["id", "content", None, None]]).title == ""

    def test_title_empty_when_inner_title_is_not_str(self) -> None:
        """A non-string at ``[1][4]`` (rare drift case) falls back to
        ``""`` rather than leaking ``None`` past the ``title: str``
        contract."""
        row = ["id", ["id", "content", None, None, 999]]
        assert NoteRow(row).title == ""


# ---------------------------------------------------------------------------
# NoteRow — mind-map content detection
# ---------------------------------------------------------------------------


class TestNoteRowIsMindMapContent:
    """The ``"children":`` / ``"nodes":`` substring discriminator."""

    def test_children_key_classifies_as_mind_map(self) -> None:
        assert NoteRow.is_mind_map_content(json.dumps({"children": []})) is True

    def test_nodes_key_classifies_as_mind_map(self) -> None:
        assert NoteRow.is_mind_map_content(json.dumps({"nodes": []})) is True

    def test_plain_text_not_mind_map(self) -> None:
        assert NoteRow.is_mind_map_content("Just a plain note body") is False

    def test_other_json_not_mind_map(self) -> None:
        """JSON without the mind-map discriminator keys is not a mind
        map — the predicate is intentionally narrow."""
        assert NoteRow.is_mind_map_content(json.dumps({"title": "x"})) is False

    def test_none_content_not_mind_map(self) -> None:
        assert NoteRow.is_mind_map_content(None) is False

    def test_empty_string_not_mind_map(self) -> None:
        assert NoteRow.is_mind_map_content("") is False

    def test_plain_text_with_children_substring_not_mind_map(self) -> None:
        """The ``startswith("{")`` guard prevents false positives on
        plain note bodies that happen to contain the substring
        ``"children":`` verbatim — gemini review feedback on #1028.
        Without the guard a user-typed note like ``My "children": Alice``
        would be misclassified as a mind map and silently filtered out
        of :meth:`NotesAPI.list`."""
        assert NoteRow.is_mind_map_content('My "children": Alice and Bob') is False

    def test_plain_text_with_nodes_substring_not_mind_map(self) -> None:
        assert NoteRow.is_mind_map_content('Graph "nodes": twelve in total') is False

    def test_json_array_with_children_key_not_mind_map(self) -> None:
        """Mind-map payloads are always JSON *objects*, never arrays.
        A JSON array starting with ``[`` is rejected even if it
        contains the discriminator substring."""
        assert NoteRow.is_mind_map_content('[{"children": []}]') is False


class TestNoteRowIsMindMap:
    """The instance-property convenience wrapper around
    :meth:`NoteRow.is_mind_map_content`."""

    def test_legacy_mind_map_row(self) -> None:
        row = NoteRow(_legacy_note_row(content=json.dumps({"children": []})))
        assert row.is_mind_map is True

    def test_current_mind_map_row(self) -> None:
        row = NoteRow(_current_note_row(content=json.dumps({"nodes": []})))
        assert row.is_mind_map is True

    def test_plain_note_row_not_mind_map(self) -> None:
        assert NoteRow(_legacy_note_row(content="plain body")).is_mind_map is False
        assert NoteRow(_current_note_row(content="plain body")).is_mind_map is False

    def test_deleted_row_not_mind_map(self) -> None:
        assert NoteRow(_deleted_note_row()).is_mind_map is False

    def test_empty_row_not_mind_map(self) -> None:
        assert NoteRow([]).is_mind_map is False


# ---------------------------------------------------------------------------
# NoteRow — immutability + method_id propagation
# ---------------------------------------------------------------------------


class TestNoteRowImmutability:
    """The adapter is frozen so the wrapped row can't be swapped out."""

    def test_cannot_assign_to_raw(self) -> None:
        # ``dataclasses.FrozenInstanceError`` is a subclass of
        # ``AttributeError`` — narrowing matches the ArtifactRow test
        # convention (coderabbit nit on #1028).
        row = NoteRow([])
        with pytest.raises(AttributeError):
            row._raw = [1, 2, 3]  # type: ignore[misc]

    def test_does_not_mutate_wrapped_row(self) -> None:
        """Reading every property is side-effect-free — the wrapped row
        is not modified by classification or extraction."""
        raw = _current_note_row(content="body", title="Title")
        snapshot = [raw[0], list(raw[1])]
        row = NoteRow(raw)

        # Touch every property.
        _ = row.id
        _ = row.is_deleted
        _ = row.content
        _ = row.title
        _ = row.is_mind_map

        assert raw[0] == snapshot[0]
        assert raw[1] == snapshot[1]


class TestNoteRowMethodIdField:
    """The adapter exposes ``method_id`` for callers that need to tag
    diagnostics with the RPC the row came from. Public (no leading
    underscore) to mirror :class:`ArtifactRow`'s post-#1026 convention.

    Drift-triggering inputs cannot be synthesised through the
    content / title descents (length-guards short-circuit before
    ``safe_index`` is reached), so this test pins the field default
    and override behaviour instead of trying to provoke a raise.
    """

    def test_default_method_id_is_get_notes_and_mind_maps(self) -> None:
        row = NoteRow(["id", "body"])
        # ``GET_NOTES_AND_MIND_MAPS.value`` per ``rpc/types.py``.
        assert row.method_id == "cFji9"

    def test_custom_method_id_can_be_supplied(self) -> None:
        """Callers wrapping a row that came from a non-default RPC can
        override ``method_id`` so any future drift diagnostics name
        the correct method."""
        row = NoteRow(["id", "body"], method_id="custom_note_rpc")
        assert row.method_id == "custom_note_rpc"


# ===========================================================================
# SourceRow
# ===========================================================================


# ---------------------------------------------------------------------------
# 1. Position-contract pin (the canary)
# ---------------------------------------------------------------------------


class TestSourceRowPositionContract:
    """If any of these assertions fail, Google has likely reshaped the wire.

    The constants cover both the top-level entry layout (id envelope,
    title, metadata, status block) AND the metadata sub-list layout
    (timestamp, type code, url precedence chain). Changing any of these
    is the *only* legitimate reason for one of these tests to need
    updating — the failing diff is the audit trail.
    """

    def test_top_level_positions(self) -> None:
        """Entry-level positions: id, title, metadata, status block."""
        assert SourceRow._ID_POS == 0
        assert SourceRow._TITLE_POS == 1
        assert SourceRow._METADATA_POS == 2
        assert SourceRow._STATUS_BLOCK_POS == 3
        assert SourceRow._STATUS_INNER_POS == 1

    def test_metadata_positions(self) -> None:
        """Metadata-sub-list positions: bare-url, timestamp, type, yt, url."""
        assert SourceRow._META_BARE_URL_POS == 0
        assert SourceRow._META_TIMESTAMP_POS == 2
        assert SourceRow._META_TYPE_POS == 4
        assert SourceRow._META_YOUTUBE_POS == 5
        assert SourceRow._META_URL_POS == 7

    def test_id_envelope_positions(self) -> None:
        """Id-envelope positions: plain id at [0]; drive-backed at [2][0]."""
        assert SourceRow._ID_ENVELOPE_PLAIN_POS == 0
        assert SourceRow._ID_ENVELOPE_DRIVE_PAYLOAD_POS == 2
        assert SourceRow._ID_ENVELOPE_DRIVE_INNER_POS == 0

    def test_list_first_position_is_neutral(self) -> None:
        """``_LIST_FIRST_POS`` is a neutral "first element" index used by
        URL helpers — kept separate from ``_ID_ENVELOPE_PLAIN_POS`` so a
        future id-envelope reshape doesn't accidentally break URL
        extraction (claude review feedback on #1029)."""
        assert SourceRow._LIST_FIRST_POS == 0

    def test_all_positions_at_once(self) -> None:
        """A single dict pin so a sweeping reshape fails with one
        informative assertion rather than many."""
        assert (
            SourceRow._ID_POS,
            SourceRow._TITLE_POS,
            SourceRow._METADATA_POS,
            SourceRow._STATUS_BLOCK_POS,
            SourceRow._STATUS_INNER_POS,
            SourceRow._META_BARE_URL_POS,
            SourceRow._META_TIMESTAMP_POS,
            SourceRow._META_TYPE_POS,
            SourceRow._META_YOUTUBE_POS,
            SourceRow._META_URL_POS,
            SourceRow._ID_ENVELOPE_PLAIN_POS,
            SourceRow._ID_ENVELOPE_DRIVE_PAYLOAD_POS,
            SourceRow._ID_ENVELOPE_DRIVE_INNER_POS,
            SourceRow._LIST_FIRST_POS,
        ) == (0, 1, 2, 3, 1, 0, 2, 4, 5, 7, 0, 2, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metadata(
    *,
    timestamp: int | float | None = 1_700_000_000,
    type_code: int | None = 5,
    canonical_url: str | None = "https://example.com/canonical",
    youtube_url: str | None = None,
    bare_url: str | None = None,
) -> list:
    """Build a metadata sub-list matching the documented layout.

    Mirrors ``Source.from_api_response`` test fixtures so the positions
    here stay in lockstep with the production code.
    """
    md: list = [bare_url, None]
    md.append([timestamp] if timestamp is not None else None)
    md.append(None)
    md.append(type_code)
    if youtube_url is not None:
        md.append([youtube_url, "ytid", "channel"])
    else:
        md.append(None)
    md.append(None)
    if canonical_url is not None:
        md.append([canonical_url])
    else:
        md.append(None)
    return md


def _entry(
    *,
    source_id: str = "src_test",
    title: str | None = "Source Title",
    drive_backed: bool = False,
    plain_id_string: bool = False,
    status_code: int | None = None,
    metadata: list | None = None,
) -> list:
    """Build a medium-nested entry: ``[[id], title, metadata, [None, status]]``."""
    if plain_id_string:
        id_envelope: object = source_id
    elif drive_backed:
        id_envelope = [None, True, [source_id]]
    else:
        id_envelope = [source_id]
    entry: list = [id_envelope, title]
    if metadata is None:
        entry.append(_metadata())
    else:
        entry.append(metadata)
    if status_code is not None:
        entry.append([None, status_code])
    return entry


# ---------------------------------------------------------------------------
# 2. Shape-dispatch normalization
# ---------------------------------------------------------------------------


class TestSourceRowShapeDispatch:
    """``SourceRow.from_unknown_shape`` normalizes all three wire shapes."""

    def test_deeply_nested_shape(self) -> None:
        """``[[[[id], title, metadata, ...]]]`` → DEEPLY_NESTED.

        Honors ``url_allow_bare_http=True`` because the deeply-nested
        ``ADD_SOURCE`` shape historically allowed a bare URL at
        ``metadata[0]`` as a fallback. The internal ``_raw`` points at
        the unwrapped entry so :attr:`id` reads cleanly.
        """
        data = [[[["id_deep"], "Deep", _metadata(canonical_url="https://deep.example/")]]]
        row = SourceRow.from_unknown_shape(data)
        assert row.shape is SourceRowShape.DEEPLY_NESTED
        assert row.url_allow_bare_http is True
        assert row.id == "id_deep"
        assert row.title == "Deep"
        assert row.url == "https://deep.example/"

    def test_medium_nested_shape(self) -> None:
        """``[[[id], title, metadata, ...]]`` → MEDIUM_NESTED."""
        data = [[["id_med"], "Med", _metadata(canonical_url="https://med.example/")]]
        row = SourceRow.from_unknown_shape(data)
        assert row.shape is SourceRowShape.MEDIUM_NESTED
        assert row.url_allow_bare_http is False
        assert row.id == "id_med"
        assert row.title == "Med"
        assert row.url == "https://med.example/"

    def test_flat_shape(self) -> None:
        """``[id, title]`` → FLAT; metadata-dependent props degrade."""
        row = SourceRow.from_unknown_shape(["id_flat", "Flat"])
        assert row.shape is SourceRowShape.FLAT
        assert row.url_allow_bare_http is False
        assert row.id == "id_flat"
        assert row.title == "Flat"
        # Flat rows have no metadata block, so url/type/timestamp are absent.
        assert row.metadata is None
        assert row.url is None
        assert row.type_code is None
        assert row.created_at_raw is None

    def test_empty_data_raises_value_error(self) -> None:
        """Empty/non-list data fails fast (mirrors legacy ``Source.from_api_response``)."""
        with pytest.raises(ValueError, match="Invalid source data"):
            SourceRow.from_unknown_shape([])

    def test_non_list_data_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid source data"):
            SourceRow.from_unknown_shape("not a list")  # type: ignore[arg-type]

    def test_from_entry_records_entry_shape(self) -> None:
        """Pre-extracted entries get ``shape=ENTRY``; ``url_allow_bare_http`` stays False.

        This is the path used by ``SourceLister._parse_source`` and
        ``NotebooksAPI.get_source_ids`` after they walk the response
        envelope themselves.
        """
        entry = _entry(source_id="entry_id", title="Entry")
        row = SourceRow.from_entry(entry)
        assert row.shape is SourceRowShape.ENTRY
        assert row.url_allow_bare_http is False
        assert row.id == "entry_id"
        assert row.title == "Entry"

    def test_dispatch_method_id_override(self) -> None:
        """Explicit ``method_id`` propagates to the wrapped row."""
        row = SourceRow.from_unknown_shape(
            [[["id"], "T", _metadata()]],
            method_id="my_custom_method",
        )
        assert row.method_id == "my_custom_method"

    def test_dispatch_default_method_id_is_get_notebook(self) -> None:
        """Default ``method_id`` is ``GET_NOTEBOOK`` for source-list diagnostics."""
        row = SourceRow.from_unknown_shape([[["id"], "T", _metadata()]])
        # GET_NOTEBOOK == "rLM1Ne"
        assert row.method_id == "rLM1Ne"


# ---------------------------------------------------------------------------
# 3. Id-envelope decoding (drive-backed + edge cases)
# ---------------------------------------------------------------------------


class TestSourceRowId:
    """:attr:`SourceRow.id` handles three id-envelope variants."""

    def test_typical_wrapped_id(self) -> None:
        """``[[id], title, ...]`` is the common case."""
        row = SourceRow.from_entry(_entry(source_id="typical"))
        assert row.id == "typical"
        assert row.has_id is True

    def test_drive_backed_id_at_index_2(self) -> None:
        """Drive-backed entries nest the id as ``[None, True, [id]]``."""
        row = SourceRow.from_entry(_entry(source_id="drv42", drive_backed=True))
        assert row.id == "drv42"
        assert row.has_id is True

    def test_bare_string_id(self) -> None:
        """Some flat-shaped rows put the id directly at ``self._raw[0]``."""
        row = SourceRow(_raw=["bare_id", "T"])
        assert row.id == "bare_id"
        assert row.has_id is True

    def test_empty_id_envelope_returns_empty(self) -> None:
        """``[[], title, ...]`` — id envelope is an empty list."""
        row = SourceRow(_raw=[[], "T"])
        assert row.id == ""
        assert row.has_id is False

    def test_missing_id_position_returns_empty(self) -> None:
        """``self._raw == []`` — no id envelope at all."""
        row = SourceRow(_raw=[])
        assert row.id == ""
        assert row.has_id is False

    def test_drive_backed_with_empty_inner_returns_empty(self) -> None:
        """``[None, True, []]`` — drive payload is empty."""
        row = SourceRow(_raw=[[None, True, []], "T"])
        assert row.id == ""
        assert row.has_id is False

    def test_drive_backed_with_inner_none_returns_empty(self) -> None:
        """``[None, True, [None]]`` — drive inner element is ``None``.

        Both :attr:`id` and :attr:`has_id` must return falsy values so
        :class:`notebooklm._source_listing.SourceLister` skips the row
        (matching legacy ``_extract_source_id`` which returned ``None``
        from ``raw_id[2][0] is None``).
        """
        row = SourceRow(_raw=[[None, True, [None]], "T"])
        assert row.id == ""
        assert row.has_id is False

    def test_integer_id_is_stringified(self) -> None:
        """Defensive: non-string ids stringify (mirrors ``Source(id=str(src_id))``)."""
        row = SourceRow(_raw=[[12345], "T"])
        assert row.id == "12345"

    def test_drive_backed_id_through_deeply_nested_dispatch(self) -> None:
        """Drive-backed id-envelope inside the DEEPLY_NESTED wire shape.

        Combines the two dispatch axes: the extra outer wrapper +
        ``[None, True, [id]]`` id envelope. The production
        ``ADD_SOURCE`` path produces this exact combination for
        drive-backed sources. Covers the test gap noted in claude
        review feedback on #1029.
        """
        deep_drive = [
            [
                [
                    [None, True, ["drive_in_deep"]],
                    "Drive Title",
                    _metadata(canonical_url="https://drive.example/"),
                ]
            ]
        ]
        row = SourceRow.from_unknown_shape(deep_drive)
        assert row.shape is SourceRowShape.DEEPLY_NESTED
        assert row.id == "drive_in_deep"
        assert row.title == "Drive Title"
        assert row.url == "https://drive.example/"

    def test_drive_backed_id_through_medium_nested_dispatch(self) -> None:
        """Drive-backed id-envelope inside the MEDIUM_NESTED wire shape."""
        med_drive = [
            [
                [None, True, ["drive_in_med"]],
                "Drive Title Med",
                _metadata(canonical_url="https://drive-med.example/"),
            ]
        ]
        row = SourceRow.from_unknown_shape(med_drive)
        assert row.shape is SourceRowShape.MEDIUM_NESTED
        assert row.id == "drive_in_med"
        assert row.title == "Drive Title Med"
        assert row.url == "https://drive-med.example/"


# ---------------------------------------------------------------------------
# 4. URL precedence (metadata[7] > metadata[5] > metadata[0])
# ---------------------------------------------------------------------------


class TestSourceRowUrlPrecedence:
    """:attr:`SourceRow.url` precedence matches legacy ``_extract_source_url``."""

    def test_canonical_url_at_metadata_7(self) -> None:
        row = SourceRow.from_entry(
            _entry(
                metadata=_metadata(
                    canonical_url="https://canonical.example/",
                    youtube_url="https://youtube.example/v",
                )
            )
        )
        # [7] wins over [5].
        assert row.url == "https://canonical.example/"

    def test_youtube_url_at_metadata_5_when_7_absent(self) -> None:
        row = SourceRow.from_entry(
            _entry(
                metadata=_metadata(
                    canonical_url=None,
                    youtube_url="https://youtube.example/v",
                )
            )
        )
        assert row.url == "https://youtube.example/v"

    def test_youtube_block_first_element_non_string_skipped(self) -> None:
        """``metadata[5][0]`` must be a string to be honored."""
        md = _metadata(canonical_url=None)
        md[SourceRow._META_YOUTUBE_POS] = [42, "ytid"]
        row = SourceRow.from_entry(_entry(metadata=md))
        assert row.url is None

    def test_bare_url_at_metadata_0_ignored_when_allow_bare_http_false(self) -> None:
        """ENTRY / MEDIUM_NESTED shapes never honor ``metadata[0]`` —
        unrelated content can live there."""
        row = SourceRow.from_entry(
            _entry(
                metadata=_metadata(
                    canonical_url=None,
                    youtube_url=None,
                    bare_url="https://bare.example/",
                )
            )
        )
        assert row.url is None

    def test_bare_url_at_metadata_0_honored_for_deeply_nested(self) -> None:
        """Only the deeply-nested ``ADD_SOURCE`` shape lets a bare
        ``http(s)://...`` at ``metadata[0]`` act as the URL."""
        md = _metadata(canonical_url=None, youtube_url=None, bare_url="https://bare.example/")
        data = [[[["id"], "T", md]]]
        row = SourceRow.from_unknown_shape(data)
        assert row.shape is SourceRowShape.DEEPLY_NESTED
        assert row.url == "https://bare.example/"

    def test_bare_url_non_http_ignored_even_when_allowed(self) -> None:
        """``metadata[0]`` is only honored when it starts with ``http``."""
        md = _metadata(canonical_url=None, youtube_url=None, bare_url="ftp://bare.example/")
        data = [[[["id"], "T", md]]]
        row = SourceRow.from_unknown_shape(data)
        assert row.url is None

    def test_url_returns_none_when_metadata_absent(self) -> None:
        row = SourceRow(_raw=[["id"], "T"])
        assert row.url is None

    def test_url_returns_none_when_all_slots_empty(self) -> None:
        md = _metadata(canonical_url=None, youtube_url=None)
        row = SourceRow.from_entry(_entry(metadata=md))
        assert row.url is None


# ---------------------------------------------------------------------------
# 5. type_code and metadata access
# ---------------------------------------------------------------------------


class TestSourceRowTypeCodeAndMetadata:
    def test_type_code_from_metadata_4(self) -> None:
        row = SourceRow.from_entry(_entry(metadata=_metadata(type_code=9)))
        assert row.type_code == 9

    def test_type_code_none_when_metadata_too_short(self) -> None:
        """Metadata shorter than 5 elements → ``type_code`` is ``None``."""
        row = SourceRow.from_entry(_entry(metadata=[None, None, [1700000000], None]))
        assert row.type_code is None

    def test_type_code_none_when_not_int(self) -> None:
        md = _metadata()
        md[SourceRow._META_TYPE_POS] = "not_an_int"
        row = SourceRow.from_entry(_entry(metadata=md))
        assert row.type_code is None

    def test_type_code_none_when_metadata_absent(self) -> None:
        row = SourceRow(_raw=[["id"], "T"])
        assert row.type_code is None

    def test_metadata_returns_list_when_present(self) -> None:
        md = _metadata()
        row = SourceRow.from_entry(_entry(metadata=md))
        assert row.metadata is md  # adapter doesn't copy

    def test_metadata_returns_none_when_non_list(self) -> None:
        row = SourceRow(_raw=[["id"], "T", "not_a_list"])
        assert row.metadata is None


# ---------------------------------------------------------------------------
# 6. Timestamp descent (delegated to safe_index for the deep step)
# ---------------------------------------------------------------------------


class TestSourceRowTimestamp:
    def test_created_at_raw_returns_int(self) -> None:
        row = SourceRow.from_entry(_entry(metadata=_metadata(timestamp=1_700_000_000)))
        assert row.created_at_raw == 1_700_000_000

    def test_created_at_converts_to_datetime(self) -> None:
        row = SourceRow.from_entry(_entry(metadata=_metadata(timestamp=1_700_000_000)))
        assert row.created_at is not None
        assert row.created_at.timestamp() == 1_700_000_000

    def test_missing_timestamp_block_returns_none(self) -> None:
        row = SourceRow.from_entry(_entry(metadata=_metadata(timestamp=None)))
        assert row.created_at_raw is None
        assert row.created_at is None

    def test_empty_timestamp_block_returns_none_in_both_modes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``metadata[2] = []`` is an accepted soft edge-case (mirrors
        :class:`ArtifactRow` timestamp handling)."""
        md = _metadata()
        md[SourceRow._META_TIMESTAMP_POS] = []
        row = SourceRow.from_entry(_entry(metadata=md))

        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        assert row.created_at_raw is None  # no exception in strict mode

    def test_non_list_timestamp_block_returns_none(self) -> None:
        md = _metadata()
        md[SourceRow._META_TIMESTAMP_POS] = "not_a_list"
        row = SourceRow.from_entry(_entry(metadata=md))
        assert row.created_at_raw is None

    def test_metadata_absent_returns_none(self) -> None:
        row = SourceRow(_raw=[["id"], "T"])
        assert row.created_at_raw is None

    def test_metadata_too_short_for_timestamp_returns_none(self) -> None:
        row = SourceRow.from_entry(_entry(metadata=[None, None]))
        assert row.created_at_raw is None

    def test_non_numeric_timestamp_returns_none(self) -> None:
        md = _metadata()
        md[SourceRow._META_TIMESTAMP_POS] = ["not_numeric"]
        row = SourceRow.from_entry(_entry(metadata=md))
        assert row.created_at_raw is None


# ---------------------------------------------------------------------------
# 7. Status decoding (used by SourceLister)
# ---------------------------------------------------------------------------


class TestSourceRowStatus:
    """:attr:`SourceRow.status` mirrors legacy ``SourceLister._extract_status``."""

    def test_status_ready_when_status_block_absent(self) -> None:
        row = SourceRow.from_entry(_entry(status_code=None))
        assert row.status == SourceStatus.READY

    def test_status_processing(self) -> None:
        row = SourceRow.from_entry(_entry(status_code=SourceStatus.PROCESSING))
        assert row.status == SourceStatus.PROCESSING

    def test_status_error(self) -> None:
        row = SourceRow.from_entry(_entry(status_code=SourceStatus.ERROR))
        assert row.status == SourceStatus.ERROR

    def test_status_preparing(self) -> None:
        row = SourceRow.from_entry(_entry(status_code=SourceStatus.PREPARING))
        assert row.status == SourceStatus.PREPARING

    def test_unknown_status_falls_back_to_ready(self) -> None:
        """Status codes outside the known enum coerce to READY."""
        row = SourceRow.from_entry(_entry(status_code=999))
        assert row.status == SourceStatus.READY

    def test_non_list_status_block_falls_back_to_ready(self) -> None:
        entry = _entry()
        entry.append("not_a_list")  # status block at position 3
        row = SourceRow.from_entry(entry)
        assert row.status == SourceStatus.READY

    def test_short_status_block_falls_back_to_ready(self) -> None:
        entry = _entry()
        entry.append([None])  # status block too short — no [1]
        row = SourceRow.from_entry(entry)
        assert row.status == SourceStatus.READY

    def test_non_int_status_code_falls_back_to_ready(self) -> None:
        """Non-int status codes (None, str, etc.) fall back via the
        ``SourceStatus(...)`` ValueError path (claude review feedback on
        #1029 — switching from explicit membership tuple to try/except
        retains this behavior for any non-enum value)."""
        for bad_code in (None, "not_a_status", []):
            entry = _entry()
            entry.append([None, bad_code])  # whatever-type status code at [3][1]
            row = SourceRow.from_entry(entry)
            assert row.status == SourceStatus.READY, f"failed for {bad_code!r}"


# ---------------------------------------------------------------------------
# 8. Schema-drift edge cases
# ---------------------------------------------------------------------------


class TestSourceRowSchemaDrift:
    """Short / malformed rows degrade to sensible defaults."""

    def test_empty_row_yields_safe_defaults(self) -> None:
        row = SourceRow(_raw=[])
        assert row.id == ""
        assert row.title is None
        assert row.metadata is None
        assert row.type_code is None
        assert row.url is None
        assert row.created_at_raw is None
        assert row.status == SourceStatus.READY

    def test_id_only_row(self) -> None:
        row = SourceRow(_raw=[["only_id"]])
        assert row.id == "only_id"
        assert row.title is None  # title is None (not ""), preserving legacy
        assert row.metadata is None

    def test_title_position_can_be_none(self) -> None:
        row = SourceRow(_raw=[["id"], None])
        assert row.title is None

    def test_title_non_string_coerced_to_str(self) -> None:
        """Non-``None`` non-string titles coerce via ``str()`` so the
        ``str | None`` annotation is honored at runtime — aligns with
        :attr:`ArtifactRow.title`'s coercion (claude review feedback on
        #1029). ``None`` stays as ``None`` (preserves "missing title"
        sentinel)."""
        row_int = SourceRow(_raw=[["id"], 12345])
        assert row_int.title == "12345"
        row_none = SourceRow(_raw=[["id"], None])
        assert row_none.title is None  # None is preserved

    def test_metadata_with_only_url_block(self) -> None:
        """Minimal metadata: only enough length to carry ``[7]``."""
        md: list = [None] * 8
        md[SourceRow._META_URL_POS] = ["https://only-url.example/"]
        row = SourceRow.from_entry(_entry(metadata=md))
        assert row.url == "https://only-url.example/"
        assert row.type_code is None
        assert row.created_at_raw is None


# ---------------------------------------------------------------------------
# 9. Strict-mode behaviour on deep drift
# ---------------------------------------------------------------------------


class TestSourceRowStrictMode:
    """``method_id`` plumbing and strict-mode behavior for deep descents.

    Unlike :class:`ArtifactRow.variant` (which walks a two-step
    ``[9][1][0]`` descent and CAN trigger strict-mode raises when
    ``[9][1]`` is missing), :attr:`SourceRow.created_at_raw` walks only
    a single ``metadata[2][0]`` step gated by an outer "non-empty list"
    guard. That guard short-circuits the two scenarios safe_index would
    raise on (empty / non-list timestamp block), so in practice the
    safe_index call here is a forward-compatible placeholder: it
    propagates ``method_id`` for diagnostics today and is the migration
    point if Google ever deepens the timestamp block to require
    ``[0][0]``-style descent.
    """

    def test_method_id_default_is_get_notebook(self) -> None:
        """The default ``method_id`` matches the most-common caller (GET_NOTEBOOK)."""
        row = SourceRow.from_entry(_entry())
        assert row.method_id == "rLM1Ne"  # RPCMethod.GET_NOTEBOOK

    def test_method_id_propagates_through_constructor(self) -> None:
        row = SourceRow.from_entry(_entry(), method_id="my_custom")
        assert row.method_id == "my_custom"

    def test_method_id_propagates_through_unknown_shape(self) -> None:
        row = SourceRow.from_unknown_shape(
            [[["id"], "T", _metadata()]],
            method_id="my_custom",
        )
        assert row.method_id == "my_custom"

    def test_non_numeric_timestamp_returns_none_in_strict_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even in strict mode, a non-numeric value at ``metadata[2][0]``
        degrades to ``None`` rather than raising — the type-guard at the
        property boundary is the legitimate filter, not drift."""
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "1")
        md = _metadata()
        md[SourceRow._META_TIMESTAMP_POS] = [{"unexpected": "shape"}]
        row = SourceRow.from_entry(_entry(metadata=md))
        assert row.created_at_raw is None


# ---------------------------------------------------------------------------
# 10. Immutability
# ---------------------------------------------------------------------------


class TestSourceRowImmutability:
    """The adapter is frozen so the wrapped row can't be swapped out."""

    def test_cannot_assign_to_raw(self) -> None:
        row = SourceRow(_raw=[])
        with pytest.raises(AttributeError):
            row._raw = [1, 2, 3]  # type: ignore[misc]

    def test_does_not_mutate_wrapped_row(self) -> None:
        raw = _entry()
        snapshot = list(raw)
        row = SourceRow.from_entry(raw)

        # Touch every property.
        _ = row.id
        _ = row.title
        _ = row.metadata
        _ = row.type_code
        _ = row.url
        _ = row.created_at_raw
        _ = row.created_at
        _ = row.status
        _ = row.has_id

        assert raw == snapshot
