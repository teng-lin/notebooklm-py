"""Tests for ``notebooklm._row_adapters`` (``ArtifactRow`` + ``NoteRow``).

The adapters centralise position knowledge for the ``LIST_ARTIFACTS``
and ``GET_NOTES_AND_MIND_MAPS`` row shapes so consumers
(``Artifact.from_api_response``, ``ArtifactListingService.select_artifact``,
``NoteService.classify_row``, ``NotesAPI._parse_note``) read named
properties instead of open-coding ``data[2]`` / ``data[4]`` / ``data[15]``
or ``row[1][1]`` / ``row[1][4]``. See ``docs/improvement.md`` §6.2 for
the motivation and ``src/notebooklm/_row_adapters.py`` for the position
contracts.

These tests cover three layers per adapter:

1. **Position-contract pin** — the canary that fails loudly if anyone
   edits a position constant. When this fails, the diff is the
   wire-shape change signal Google has rotated something.
2. **Shape handling** — missing trailing positions return sensible
   defaults; deep descent goes through ``safe_index`` so strict-mode
   drift raises ``UnknownRPCMethodError``.
3. **Predicate / domain helpers** — ``matches_type`` for artifacts,
   ``is_deleted`` / ``is_mind_map_content`` for notes.
"""

from __future__ import annotations

import json

import pytest

from notebooklm._row_adapters import ArtifactRow, NoteRow
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
