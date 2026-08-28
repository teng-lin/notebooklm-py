"""Tests for the streamed-chat row adapters (issue #1491).

These adapters centralise the positional knowledge the stream parser used to
open-code as scattered single-level subscripts (``first[4]``, ``cite[1]``,
``cite_inner[5]``, ``passage_data[0]`` …). The tests cover three layers per
adapter:

1. **Position-contract pin** — the canary that fails loudly if a position
   constant is edited (the wire-shape change signal).
2. **Shape handling** — happy-path reads plus the permissive "absent / short /
   non-list → default" degrade that matches the historical wire parser.
3. **Drift** — the one descent that goes through ``safe_index``
   (``StreamFrameRow.tag``) RAISES ``UnknownRPCMethodError`` when the
   guaranteed slot is missing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from notebooklm import ConversationTurnKey
from notebooklm._web.rows.chat import (
    AnswerRow,
    ChatSettingsRow,
    CitationDetail,
    CitationRow,
    ConversationTurnRow,
    ErrorPayloadRow,
    NextStepSuggestionRow,
    SavedChatNoteRow,
    StreamEnvelopeRow,
    StreamFrameRow,
    unwrap_chat_settings,
    unwrap_conversation_turns,
    unwrap_last_conversation_id,
)
from notebooklm._web.wire.decoder import _MAX_STATUS_MESSAGE_CHARS
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod

#: Live ``GenerateFreeFormStreamedResponse`` capture (#2122) — the five chunks
#: of one real answer stream, decoded from the ``wrb.fr`` frames exactly as the
#: parser sees them. Used instead of a hand-built envelope so the
#: ``isFinalResponse`` / ``ConversationTurnKey`` reads below are pinned against
#: a shape the backend actually sent.
_CAPTURED_STREAM: list = json.loads(
    (Path(__file__).parent / "fixtures" / "chat_stream_final_response.json").read_text(
        encoding="utf-8"
    )
)["chunks"]

# ---------------------------------------------------------------------------
# 1. Position-contract pins (the canaries)
# ---------------------------------------------------------------------------


class TestAnswerRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            AnswerRow._TEXT_POS,
            AnswerRow._CONV_BLOCK_POS,
            AnswerRow._EMPTY_ANSWER_REASON_POS,
            AnswerRow._TYPE_BLOCK_POS,
            AnswerRow._ANSWER_MARKER_POS,
            AnswerRow._CITATIONS_POS,
            AnswerRow._ANSWER_MARKER_VALUE,
            AnswerRow._DOC_BODY_POS,
        ) == (0, 2, 3, 4, 4, 3, 1, 0)


class TestStreamEnvelopeRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            StreamEnvelopeRow._IS_FINAL_RESPONSE_POS,
            StreamEnvelopeRow._NEXT_STEPS_POS,
            StreamEnvelopeRow._NEXT_STEPS_ROWS_POS,
        ) == (4, 5, 0)


class TestAnswerRowTurnKeyPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            AnswerRow._TURN_KEY_SESSION_ID_POS,
            AnswerRow._TURN_KEY_TURN_ID_POS,
            AnswerRow._TURN_KEY_TURN_CODE_POS,
        ) == (0, 1, 2)


class TestCitationPositionContract:
    def test_citation_row_positions_pinned(self) -> None:
        assert (CitationRow._CHUNK_BLOCK_POS, CitationRow._DETAIL_POS) == (0, 1)

    def test_citation_detail_positions_pinned(self) -> None:
        assert (
            CitationDetail._SCORE_POS,
            CitationDetail._FRAGMENT_RANGE_POS,
            CitationDetail._FRAGMENT_POS,
            CitationDetail._SOURCE_ID_POS,
            CitationDetail._FRAGMENT_RANGE_START_POS,
            CitationDetail._FRAGMENT_RANGE_END_POS,
        ) == (2, 3, 4, 5, 1, 2)

    def test_fragment_elements_position_pinned(self) -> None:
        """The second level of the #2120 descent — the one that was missing."""
        assert CitationDetail._FRAGMENT_ELEMENTS_POS == 0


class TestFramePositionContract:
    def test_stream_frame_positions_pinned(self) -> None:
        assert (
            StreamFrameRow._TAG_POS,
            StreamFrameRow._INNER_JSON_POS,
            StreamFrameRow._ERROR_CODE_POS,
            StreamFrameRow._ERROR_PAYLOAD_POS,
        ) == (0, 2, 2, 5)

    def test_error_payload_positions_pinned(self) -> None:
        # ``google.rpc.Status``: code tag 1, message tag 2, details tag 3.
        assert (
            ErrorPayloadRow._STATUS_POS,
            ErrorPayloadRow._MESSAGE_POS,
            ErrorPayloadRow._ENTRIES_POS,
        ) == (0, 1, 2)


class TestConversationTurnPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            ConversationTurnRow._ROLE_POS,
            ConversationTurnRow._QUESTION_TEXT_POS,
            ConversationTurnRow._ANSWER_CONTENT_POS,
            ConversationTurnRow._MIN_LEN,
            ConversationTurnRow.ROLE_QUESTION,
            ConversationTurnRow.ROLE_ANSWER,
        ) == (2, 3, 4, 3, 1, 2)


# ---------------------------------------------------------------------------
# 2. AnswerRow
# ---------------------------------------------------------------------------


def _answer_record(
    *,
    text: str = "answer text",
    conv_id: str | None = "server-conv",
    marker: int | None = 1,
    citations: list | None = None,
) -> list:
    """Build a populated answer record matching the streamed-chat wire shape."""
    conv = [conv_id, 123] if conv_id is not None else None
    type_info = [[], None, None, citations or [], marker] if marker is not None else None
    return [text, None, conv, None, type_info]


class TestAnswerRow:
    def test_happy_path_reads_text_conv_id_marker_citations(self) -> None:
        row = AnswerRow(_answer_record(citations=[["c"]]))
        assert row.text == "answer text"
        assert row.server_conversation_id == "server-conv"
        assert row.is_answer is True
        assert row.citations == [["c"]]

    def test_empty_text_returns_none(self) -> None:
        assert AnswerRow(_answer_record(text="")).text is None

    def test_non_string_text_returns_none(self) -> None:
        rec = _answer_record()
        rec[0] = 123
        assert AnswerRow(rec).text is None

    def test_missing_conv_block_returns_none(self) -> None:
        assert AnswerRow(_answer_record(conv_id=None)).server_conversation_id is None

    def test_non_answer_marker_is_false(self) -> None:
        assert AnswerRow(_answer_record(marker=0)).is_answer is False

    def test_trailing_type_field_does_not_change_answer_marker(self) -> None:
        rec = _answer_record()
        rec[4].append(0)
        assert AnswerRow(rec).is_answer is True

    def test_absent_type_block_means_not_answer_and_no_citations(self) -> None:
        row = AnswerRow(_answer_record(marker=None))
        assert row.is_answer is False
        assert row.citations == []

    def test_short_row_degrades(self) -> None:
        row = AnswerRow(["only-text"])
        assert row.text == "only-text"
        assert row.server_conversation_id is None
        assert row.is_answer is False
        assert row.citations == []

    def test_citation_rows_wrap_each_entry(self) -> None:
        row = AnswerRow(_answer_record(citations=[[["chunk"], [None, None, 0.5]]]))
        rows = row.citation_rows()
        assert len(rows) == 1
        assert isinstance(rows[0], CitationRow)
        assert rows[0].chunk_id == "chunk"

    def test_none_citation_slot_is_absence(self) -> None:
        """``first[4][3] is None`` is the routine real-traffic "no citations" shape."""
        rec = _answer_record()
        rec[4][3] = None
        assert AnswerRow(rec).citations == []


class TestNextStepSuggestionRow:
    def test_positions_pinned(self) -> None:
        assert (
            NextStepSuggestionRow._QUESTION_POS,
            NextStepSuggestionRow._TYPE_CODE_POS,
            NextStepSuggestionRow._MIN_LEN,
        ) == (0, 1, 2)

    @pytest.mark.parametrize(
        "raw",
        [
            [],
            ["question"],
            ["", 9],
            [7, 9],
            ["question", True],
            ["question", "9"],
        ],
    )
    def test_malformed_row_is_not_well_formed(self, raw: object) -> None:
        assert NextStepSuggestionRow(raw).is_well_formed is False

    def test_truthy_non_list_citation_slot_raises(self) -> None:
        """A truthy non-list where the citation container belongs is wire drift.

        #1505 absence-vs-malformed policy: matches the container raise in
        ``unwrap_conversation_turns`` and the ``inner_data[0]`` non-list raise
        in ``_web/rows/chat_stream.py`` (was a silent ``[]`` degrade).
        """
        for drifted in ("reshaped", {"v2": []}, 7):
            rec = _answer_record()
            rec[4][3] = drifted
            with pytest.raises(UnknownRPCMethodError) as raised:
                _ = AnswerRow(rec).citations
            assert raised.value.path == (4, 3)
            assert raised.value.source == "ChatAnswerRow.citations"


# ---------------------------------------------------------------------------
# 3. CitationRow / CitationDetail
# ---------------------------------------------------------------------------


class TestCitationRow:
    def test_chunk_id_and_detail(self) -> None:
        row = CitationRow([["chunk-1"], [None, None, 0.9, None, [], ["src"]]])
        assert row.is_well_formed is True
        assert row.chunk_id == "chunk-1"
        assert isinstance(row.detail, CitationDetail)

    def test_absent_chunk_block_returns_none_chunk_id(self) -> None:
        assert CitationRow([[], [None]]).chunk_id is None

    @pytest.mark.parametrize("raw", [[], ["only-one"], "not-a-list", None])
    def test_malformed_entry_is_not_well_formed(self, raw: object) -> None:
        row = CitationRow(raw)
        assert row.is_well_formed is False
        assert row.detail is None
        assert row.chunk_id is None

    def test_non_list_detail_slot_returns_none_detail(self) -> None:
        assert CitationRow([["chunk"], "not-a-list"]).detail is None


class TestCitationDetail:
    def test_score_fragment_source_id(self) -> None:
        raw = [None, None, 0.75, None, [[["p"]]], ["src-data"]]
        detail = CitationDetail(raw)
        assert detail.raw_score == 0.75
        assert detail.fragment_elements == [["p"]]
        assert detail.source_id_data == ["src-data"]
        assert detail.raw_list == raw

    def test_fragment_range_reads_inner_triple(self) -> None:
        detail = CitationDetail([None, None, None, [[None, 5, 9]]])
        assert detail.fragment_range() == (5, 9)

    @pytest.mark.parametrize(
        "raw",
        [
            [None, None, None],  # too short for the range slot
            [None, None, None, []],  # empty outer
            [None, None, None, ["not-a-list"]],  # inner not a list
            [None, None, None, [[None, 5]]],  # inner too short
        ],
    )
    def test_fragment_range_degrades_to_none_none(self, raw: list) -> None:
        assert CitationDetail(raw).fragment_range() == (None, None)

    def test_short_detail_degrades(self) -> None:
        detail = CitationDetail([None, None])
        assert detail.raw_score is None
        assert detail.fragment_elements == []
        assert detail.source_id_data is None


# ---------------------------------------------------------------------------
# 4. CitationDetail.fragment_elements (the #2120 two-level descent)
# ---------------------------------------------------------------------------


class TestCitationDetailFragment:
    def test_descends_through_the_fragment_message_to_its_elements(self) -> None:
        """``cite_inner[4]`` is the message; ``[4][0]`` is the element list."""
        detail = CitationDetail([None, None, None, None, [[[10, 20, "para"]]]])
        assert detail.fragment_elements == [[10, 20, "para"]]

    @pytest.mark.parametrize(
        "fragment",
        [
            [],  # message present but empty
            "not-a-list",  # message slot is not a list
            [None],  # elements slot is null
            ["not-a-list"],  # elements slot is not a list
        ],
        ids=["empty-message", "non-list-message", "null-elements", "non-list-elements"],
    )
    def test_malformed_fragment_degrades_to_empty(self, fragment: object) -> None:
        assert CitationDetail([None, None, None, None, fragment]).fragment_elements == []

    def test_annotation_join_key_is_read_from_the_document_object(self) -> None:
        """``Citation.objectId`` at ``cite_inner[6]`` is deliberately not read.

        It repeats the id the enclosing ``DocumentObject`` already carries at
        ``cite[0][0]``, which is the one this client surfaces as ``chunk_id``
        and joins the answer annotation map on (#2120). Pinned so a future
        reader does not "restore" the redundant accessor.
        """
        cite = [["obj-1"], [None, None, None, None, None, None, ["obj-1"]]]
        assert CitationRow(cite).chunk_id == "obj-1"
        assert not hasattr(CitationDetail(cite[1]), "object_id")


# ---------------------------------------------------------------------------
# 5. StreamFrameRow / ErrorPayloadRow
# ---------------------------------------------------------------------------


class TestStreamFrameRow:
    def test_wrb_fr_frame_reads_tag_and_inner_json(self) -> None:
        frame = StreamFrameRow(["wrb.fr", None, '[["x"]]'])
        assert frame.tag == "wrb.fr"
        assert frame.inner_json == '[["x"]]'
        assert frame.error_payload is None

    def test_er_frame_reads_tag_and_error_code(self) -> None:
        frame = StreamFrameRow(["er", "rpc-id", 429])
        assert frame.tag == "er"
        assert frame.error_code == 429

    def test_missing_error_code_is_none(self) -> None:
        # A short "er" frame legitimately omits the code — NOT drift.
        assert StreamFrameRow(["er", "rpc-id"]).error_code is None

    def test_error_payload_read_when_list(self) -> None:
        frame = StreamFrameRow(["wrb.fr", None, None, None, None, [8, None, []]])
        assert frame.error_payload == [8, None, []]

    def test_non_list_error_payload_returns_none(self) -> None:
        frame = StreamFrameRow(["wrb.fr", None, None, None, None, "x"])
        assert frame.error_payload is None

    def test_tag_descent_raises_on_drift(self) -> None:
        # The tag slot is the one guaranteed position; its absence is genuine
        # drift that must RAISE rather than silently degrade. ``safe_index``
        # cannot index an empty list at [0].
        with pytest.raises(UnknownRPCMethodError):
            _ = StreamFrameRow([]).tag


class TestErrorPayloadRow:
    def test_entries_and_entry_type(self) -> None:
        payload = [8, None, [["type.googleapis.com/x.UserDisplayableError", "msg"]]]
        row = ErrorPayloadRow(payload)
        assert row.entries == [["type.googleapis.com/x.UserDisplayableError", "msg"]]
        assert (
            ErrorPayloadRow.entry_type(row.entries[0])
            == "type.googleapis.com/x.UserDisplayableError"
        )

    @pytest.mark.parametrize("payload", [[8, None], [8, None, "x"], []])
    def test_absent_or_non_list_entries_degrade(self, payload: list) -> None:
        assert ErrorPayloadRow(payload).entries == []

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [([3], 3), ([8, None, []], 8), ([], None), ([None], None)],
    )
    def test_status_code_reads_leading_slot(self, payload: list, expected: object) -> None:
        # ``[None]`` (leading slot present but null) yields ``None`` — the
        # rejection message then omits the status detail rather than printing
        # ``(status None)``.
        assert ErrorPayloadRow(payload).status_code == expected

    @pytest.mark.parametrize("entry", [[], [123], "x", None])
    def test_non_string_entry_type_is_none(self, entry: object) -> None:
        assert ErrorPayloadRow.entry_type(entry) is None

    @pytest.mark.parametrize(
        "payload",
        [
            # Every captured rejection: the bare status has no message slot,
            # and the recorded rate-limit shape holds ``None`` there (#2188).
            [3],
            [8, None, []],
            [],
            [8, 42],
            [8, ["nested"]],
            [8, "   "],
        ],
    )
    def test_message_is_none_for_every_captured_shape(self, payload: list) -> None:
        assert ErrorPayloadRow(payload).message is None

    def test_message_returns_server_text_when_present(self) -> None:
        assert ErrorPayloadRow([8, "  Slow  down\tplease ", []]).message == "Slow down please"

    def test_message_is_truncated(self) -> None:
        """Shares the decoder's cap, so the two layers cannot diverge."""
        message = ErrorPayloadRow([8, "x" * 5000]).message
        assert message is not None
        assert len(message) <= _MAX_STATUS_MESSAGE_CHARS + 1
        assert message.endswith("…")

    def test_non_string_message_slot_warns(self, caplog) -> None:
        """An unexpected type there is drift, and must not be inaudible."""
        with caplog.at_level(logging.WARNING, logger="notebooklm.rpc.decoder"):
            assert ErrorPayloadRow([8, ["nested"], []]).message is None

        assert any("not a string" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# ConversationTurnRow + unwrap_conversation_turns (GET_CONVERSATION_TURNS)
# ---------------------------------------------------------------------------


class TestConversationTurnRow:
    def test_question_turn_reads_role_and_text(self) -> None:
        row = ConversationTurnRow([None, None, 1, "What is AI?"])
        assert row.is_well_formed is True
        assert row.role == 1
        assert row.is_question is True
        assert row.is_answer is False
        assert row.question_text == "What is AI?"

    def test_answer_turn_classified_with_content_slot(self) -> None:
        row = ConversationTurnRow([None, None, 2, None, [["The answer."]]])
        assert row.is_answer is True
        assert row.is_question is False
        assert row.question_text == ""

    def test_role_one_without_text_slot_is_not_a_question(self) -> None:
        """Mirrors the historical ``turn[2] == 1 and len(turn) > 3`` guard."""
        row = ConversationTurnRow([None, None, 1])
        assert row.is_well_formed is True
        assert row.is_question is False
        assert row.question_text == ""

    def test_role_two_without_content_slot_is_not_an_answer(self) -> None:
        """Mirrors the historical ``len(next_turn) > 4`` guard."""
        row = ConversationTurnRow([None, None, 2, None])
        assert row.is_answer is False

    def test_unrecognized_role_is_flagged(self) -> None:
        """A well-formed row with a role outside {1, 2} is role-slot drift."""
        assert ConversationTurnRow([None, None, "user", "Q?"]).has_unrecognized_role is True
        assert ConversationTurnRow([None, None, 3]).has_unrecognized_role is True
        assert ConversationTurnRow([None, None, None]).has_unrecognized_role is True

    def test_known_roles_and_malformed_rows_are_not_flagged(self) -> None:
        """Known roles (incl. unpaired answers) and malformed rows never flag."""
        assert ConversationTurnRow([None, None, 1, "Q?"]).has_unrecognized_role is False
        assert ConversationTurnRow([None, None, 2, None, [["A."]]]).has_unrecognized_role is False
        # Short role-2 row: still a KNOWN role — skipped as unusable, not drift.
        assert ConversationTurnRow([None, None, 2]).has_unrecognized_role is False
        assert ConversationTurnRow([None]).has_unrecognized_role is False
        assert ConversationTurnRow("not a turn").has_unrecognized_role is False

    def test_none_question_text_coerces_to_empty(self) -> None:
        """Preserves the historical ``str(turn[3] or "")`` coercion."""
        row = ConversationTurnRow([None, None, 1, None])
        assert row.is_question is True
        assert row.question_text == ""

    @pytest.mark.parametrize("raw", [[], [None], [None, None], "not a turn", 42, None])
    def test_short_or_non_list_rows_are_malformed(self, raw: object) -> None:
        row = ConversationTurnRow(raw)
        assert row.is_well_formed is False
        assert row.role is None
        assert row.is_question is False
        assert row.is_answer is False
        assert row.question_text == ""

    def test_raw_returns_wrapped_row(self) -> None:
        turn = [None, None, 2, None, [["x"]]]
        assert ConversationTurnRow(turn).raw is turn


class TestUnwrapConversationTurns:
    """Absence-vs-malformed split for the turns container (#1485 policy)."""

    def test_unwraps_single_element_envelope(self) -> None:
        turns = [[None, None, 1, "Q?"], [None, None, 2, None, [["A."]]]]
        assert unwrap_conversation_turns([turns], source="test") is turns

    @pytest.mark.parametrize("payload", [None, [], "", 0, {}])
    def test_falsy_payload_is_soft_empty(self, payload: object) -> None:
        """A falsy payload is a legitimately-absent history, not drift."""
        assert unwrap_conversation_turns(payload, source="test") == []

    @pytest.mark.parametrize("payload", ["not a list", 42, {"unexpected": "dict"}])
    def test_truthy_non_list_payload_raises(self, payload: object) -> None:
        """A truthy non-list TOP-LEVEL payload is wire drift (#1485).

        Historically this degraded to a silent ``[]`` — the fabricated-
        empty-history class. ``path=()`` marks top-level drift.
        """
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            unwrap_conversation_turns(payload, source="test-source")
        assert exc_info.value.method_id == RPCMethod.GET_CONVERSATION_TURNS.value
        assert exc_info.value.source == "test-source"
        assert exc_info.value.path == ()

    @pytest.mark.parametrize("payload", [[[]], [None], [0], [""]])
    def test_falsy_container_slot_is_soft_empty(self, payload: list) -> None:
        """An empty/null turn list is a legitimately-empty history, not drift."""
        assert unwrap_conversation_turns(payload, source="test") == []

    @pytest.mark.parametrize("payload", [["string"], [42], [{"k": "v"}]])
    def test_truthy_non_list_container_raises(self, payload: list) -> None:
        """A truthy non-list where the turn list belongs is wire drift."""
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            unwrap_conversation_turns(payload, source="test-source")
        assert exc_info.value.method_id == RPCMethod.GET_CONVERSATION_TURNS.value
        assert exc_info.value.source == "test-source"


class TestUnwrapLastConversationId:
    """``GET_LAST_CONVERSATION_ID`` (``hPTbtc``) ``[[[conv_id]]]`` SOFT walk.

    Pins the historical ``ChatAPI.get_conversation_id`` contract: the first
    innermost ``[str]`` row wins, and ANY shape that yields no such row returns
    ``None`` (never raises — the caller keeps its own WARNING diagnostics).
    """

    def test_extracts_first_innermost_id(self) -> None:
        assert unwrap_last_conversation_id([[["conv-abc"]]]) == "conv-abc"

    def test_returns_first_match_when_multiple_rows(self) -> None:
        assert unwrap_last_conversation_id([[["first"], ["second"]]]) == "first"

    def test_skips_non_list_groups_and_rows(self) -> None:
        # A non-list group is skipped; a non-list/empty/non-str-leading row is
        # skipped; the first usable ``[str]`` row wins.
        raw = ["noise", [None, [], [42], ["winner"]]]
        assert unwrap_last_conversation_id(raw) == "winner"

    @pytest.mark.parametrize("raw", [None, [], "str", 42, {}, [[]], [[[]]], [[[42]]]])
    def test_no_id_returns_none_softly(self, raw: object) -> None:
        """Any payload yielding no innermost ``[str]`` row degrades to None."""
        assert unwrap_last_conversation_id(raw) is None

    def test_does_not_raise_on_truthy_non_list(self) -> None:
        # Unlike unwrap_conversation_turns, this stays soft for a truthy
        # non-list payload (the caller owns the WARNING-then-None contract).
        assert unwrap_last_conversation_id({"unexpected": "dict"}) is None
        assert unwrap_last_conversation_id("conv-abc") is None


class TestSavedChatNoteRowPositionContract:
    """Canary for the ``CREATE_NOTE`` saved-from-chat envelope positions.

    If these change, saved-chat note decoding has silently moved — update
    this pin in the same commit.
    """

    def test_positions_pinned(self) -> None:
        assert SavedChatNoteRow._OUTER_NOTE_POS == 0
        assert SavedChatNoteRow._ID_POS == 0
        assert SavedChatNoteRow._SERVER_TITLE_POS == 4


class TestSavedChatNoteRow:
    """SOFT unwrap of the ``CREATE_NOTE`` saved-from-chat response envelope."""

    def test_outer_wrapped_shape(self) -> None:
        inner = ["note-1", "content", None, None, "Server Title", "rich"]
        row = SavedChatNoteRow([inner])
        assert row.note_data is inner
        assert row.note_id == "note-1"
        assert row.server_title == "Server Title"

    def test_flat_shape(self) -> None:
        flat = ["note-2", "content", None, None, "Flat Title"]
        row = SavedChatNoteRow(flat)
        assert row.note_data is flat
        assert row.note_id == "note-2"
        assert row.server_title == "Flat Title"

    def test_present_id_without_server_title_slot(self) -> None:
        # Short note (no slot 4): id present, server_title falls back to None
        # (the caller keeps the requested title).
        row = SavedChatNoteRow([["note-3", "content"]])
        assert row.note_id == "note-3"
        assert row.server_title is None

    def test_non_string_server_title_is_none(self) -> None:
        row = SavedChatNoteRow([["note-4", "c", None, None, 99]])
        assert row.note_id == "note-4"
        assert row.server_title is None

    @pytest.mark.parametrize("raw", [None, [], "str", 42, {}, [None], [42]])
    def test_unrecognized_shape_degrades_to_none(self, raw: object) -> None:
        """Empty / wrong-typed leading slot → no note_data / id / title."""
        row = SavedChatNoteRow(raw)
        assert row.note_data is None
        assert row.note_id is None
        assert row.server_title is None

    def test_empty_inner_list_has_no_id(self) -> None:
        # Outer-wrapped but the inner note is empty — usable note_data, no id.
        row = SavedChatNoteRow([[]])
        assert row.note_data == []
        assert row.note_id is None
        assert row.server_title is None

    def test_does_not_raise_on_any_shape(self) -> None:
        # Saved-chat create reads are best-effort: never UnknownRPCMethodError.
        for raw in (None, "x", 7, {"k": 1}, [[7]], [["id", 1, 2]]):
            # Reading every property must not raise on any malformed shape.
            assert SavedChatNoteRow(raw).note_id in (None, "id")


def _nb_info(block: object) -> list[object]:
    """A GET_NOTEBOOK ``nb_info`` row with ``block`` at the chat-settings slot [7]."""
    return [0, 1, 2, 3, 4, 5, 6, block]


class TestUnwrapChatSettings:
    """``unwrap_chat_settings`` decodes the GET_NOTEBOOK chat-settings block (#1751).

    Live-verified shapes (scratch-notebook probe): ``nb_info[7]`` is
    ``[[goal, custom_prompt?], [length]]`` or ``null`` when never configured.
    """

    def test_null_block_is_never_configured_defaults(self) -> None:
        row = unwrap_chat_settings(_nb_info(None), source="t")
        assert isinstance(row, ChatSettingsRow)
        # 1 == ChatGoal.DEFAULT == ChatResponseLength.DEFAULT; no persona.
        assert (row.goal_code, row.response_length_code, row.custom_prompt) == (1, 1, None)

    def test_default_default(self) -> None:
        row = unwrap_chat_settings(_nb_info([[1], [1]]), source="t")
        assert (row.goal_code, row.response_length_code, row.custom_prompt) == (1, 1, None)

    def test_learning_guide_longer(self) -> None:
        row = unwrap_chat_settings(_nb_info([[3], [4]]), source="t")
        assert (row.goal_code, row.response_length_code, row.custom_prompt) == (3, 4, None)

    def test_default_shorter(self) -> None:
        row = unwrap_chat_settings(_nb_info([[1], [5]]), source="t")
        assert (row.goal_code, row.response_length_code, row.custom_prompt) == (1, 5, None)

    def test_custom_default_carries_persona(self) -> None:
        row = unwrap_chat_settings(_nb_info([[2, "persona text"], [1]]), source="t")
        assert (row.goal_code, row.response_length_code, row.custom_prompt) == (
            2,
            1,
            "persona text",
        )

    def test_custom_longer_carries_persona(self) -> None:
        row = unwrap_chat_settings(_nb_info([[2, "p"], [4]]), source="t")
        assert (row.goal_code, row.response_length_code, row.custom_prompt) == (2, 4, "p")

    def test_default_with_retained_draft_prompt_is_not_surfaced(self) -> None:
        """A prompt retained under goal code 1 (DEFAULT) is an inactive draft → custom_prompt None.

        Live-verified: the server keeps the prompt string when a preset is applied
        over a persona (``[[1, "…"], …]``), but it is inactive under DEFAULT and
        ``configure()`` cannot round-trip it, so ``get_settings`` reports None.
        """
        row = unwrap_chat_settings(
            _nb_info([[1, "You are a helpful science tutor"], [1]]), source="t"
        )
        assert (row.goal_code, row.response_length_code, row.custom_prompt) == (1, 1, None)

    @pytest.mark.parametrize(
        "nb_info",
        [
            [0, 1],  # too short — no slot at [7]
            "not-a-list",  # not a list
            None,  # not a list
        ],
    )
    def test_missing_slot_raises(self, nb_info: object) -> None:
        with pytest.raises(UnknownRPCMethodError):
            unwrap_chat_settings(nb_info, source="t")

    @pytest.mark.parametrize(
        "block",
        [
            99,  # truthy non-list block
            "settings",  # truthy non-list block
            [[], [1]],  # empty goal array
            [[1], []],  # empty length array
            [[1], "x"],  # non-list length array
            ["x", [1]],  # non-list goal array
            [["notint"], [1]],  # non-int goal code
            [[1], ["notint"]],  # non-int length code
            [[True], [1]],  # bool goal code (bool is an int subclass) — rejected
            [[1], [False]],  # bool length code — rejected
            [[2, 123], [1]],  # CUSTOM with a non-str prompt
            [[2], [1]],  # CUSTOM goal missing its persona prompt
            [[2, ""], [1]],  # CUSTOM goal with an empty persona prompt
        ],
    )
    def test_malformed_block_raises(self, block: object) -> None:
        with pytest.raises(UnknownRPCMethodError):
            unwrap_chat_settings(_nb_info(block), source="t")

    def test_drift_diagnostic_carries_get_notebook_method_id(self) -> None:
        with pytest.raises(UnknownRPCMethodError) as excinfo:
            unwrap_chat_settings(_nb_info(99), source="ChatAPI.get_settings")
        assert excinfo.value.method_id == RPCMethod.GET_NOTEBOOK.value


# ---------------------------------------------------------------------------
# 12. isFinalResponse + ConversationTurnKey (#2122)
# ---------------------------------------------------------------------------


class TestStreamEnvelopeRowAgainstCapture:
    """``isFinalResponse`` against the live five-chunk stream capture."""

    def test_capture_marks_exactly_the_last_chunk_final(self) -> None:
        flags = [StreamEnvelopeRow(chunk).is_final_response for chunk in _CAPTURED_STREAM]
        assert flags == [False, False, False, False, True]

    def test_capture_cannot_by_itself_discriminate_the_selection_policy(self) -> None:
        """States a LIMIT of this fixture, so no one mistakes the capture-based
        tests for coverage of the selection change.

        Chunks 3, 4 and 5 carry the identical answer string, so longest-wins
        and final-wins return the same text on this stream — a real property of
        cumulative streaming, not a defect in the capture. The policy is
        discriminated by the synthetic
        ``test_final_marker_beats_a_longer_earlier_chunk`` in
        ``test_streaming_chat_wire.py``; if this assertion ever fails, the
        capture has gained that power and that test's docstring should say so.
        """
        texts = [AnswerRow(chunk[0]).text for chunk in _CAPTURED_STREAM]
        final_text = texts[-1]
        assert final_text is not None
        longest = max((t for t in texts if t), key=len)
        assert len(final_text) == len(longest)


class TestStreamEnvelopeRow:
    def test_heartbeat_envelope_is_not_final(self) -> None:
        """Real heartbeats decode to ``[]`` and must not claim to end the stream."""
        assert StreamEnvelopeRow([]).is_final_response is False

    def test_short_envelope_is_not_final(self) -> None:
        assert StreamEnvelopeRow([["text"], None, None, None]).is_final_response is False

    def test_null_slot_is_not_final(self) -> None:
        assert StreamEnvelopeRow([["t"], None, None, None, None]).is_final_response is False

    @pytest.mark.parametrize("truthy", [1, "true", [1], {"final": True}])
    def test_truthy_non_bool_is_not_final(self, truthy: object) -> None:
        """Only a literal wire ``true`` selects the answer — anything else must
        leave the longest-wins fallback in charge rather than promote a chunk."""
        assert StreamEnvelopeRow([["t"], None, None, None, truthy]).is_final_response is False

    def test_non_list_envelope_is_not_final(self) -> None:
        assert StreamEnvelopeRow("reshaped").is_final_response is False

    def test_next_step_suggestions(self) -> None:
        row = StreamEnvelopeRow(
            [
                _answer_record(),
                None,
                None,
                None,
                True,
                [[["What happens next?", 9], ["Make a report", 12]]],
            ]
        )
        suggestions = row.next_step_rows
        assert [(item.question, item.type_code) for item in suggestions] == [
            ("What happens next?", 9),
            ("Make a report", 12),
        ]
        assert all(isinstance(item, NextStepSuggestionRow) for item in suggestions)

    @pytest.mark.parametrize("suggestions", [None, "bad", 7, {"rows": []}])
    def test_malformed_next_step_container_is_optional(self, suggestions: object) -> None:
        assert (
            StreamEnvelopeRow(
                [_answer_record(), None, None, None, True, suggestions]
            ).next_step_rows
            == []
        )


class TestAnswerRowTurnKeyAgainstCapture:
    def test_capture_decodes_the_whole_key(self) -> None:
        key = AnswerRow(_CAPTURED_STREAM[0][0]).turn_key
        assert key == ConversationTurnKey(
            session_id="3afea005-7d13-41d0-9257-6a9e28597818",
            turn_id="b38d4003-5be1-487d-a121-5c5958709021",
            turn_code=2187103311,
        )

    def test_key_is_identical_on_every_chunk_of_one_turn(self) -> None:
        keys = {AnswerRow(chunk[0]).turn_key for chunk in _CAPTURED_STREAM}
        assert len(keys) == 1

    def test_server_conversation_id_is_the_keys_session_id(self) -> None:
        """The pre-existing read and the new key must not disagree about slot 0.

        They are the SAME wire slot, which is why ``session_id`` carries no
        claim to be a conversation id — #659 established this slot is a
        per-stream identifier.
        """
        row = AnswerRow(_CAPTURED_STREAM[0][0])
        assert row.server_conversation_id == row.turn_key.session_id


class TestAnswerRowTurnKey:
    def test_absent_block_yields_no_key(self) -> None:
        assert AnswerRow(_answer_record(conv_id=None)).turn_key is None

    def test_short_row_yields_no_key(self) -> None:
        assert AnswerRow(["only-text"]).turn_key is None

    @pytest.mark.parametrize("block", [[], "reshaped", 7])
    def test_unusable_block_yields_no_key(self, block: object) -> None:
        rec = _answer_record()
        rec[2] = block
        assert AnswerRow(rec).turn_key is None

    @pytest.mark.parametrize("bad_id", [None, 123, ""])
    def test_key_without_a_usable_session_id_is_absent(self, bad_id: object) -> None:
        """A key is addressed BY slot 0; without it the key identifies nothing,
        so it is reported absent rather than half-populated."""
        rec = _answer_record()
        rec[2] = [bad_id, "turn-uuid", 7]
        assert AnswerRow(rec).turn_key is None

    def test_trailing_slots_are_optional(self) -> None:
        rec = _answer_record()
        rec[2] = ["conv-uuid"]
        assert AnswerRow(rec).turn_key == ConversationTurnKey("conv-uuid", None, None)

    def test_one_drifted_trailing_slot_does_not_discard_the_rest(self) -> None:
        rec = _answer_record()
        rec[2] = ["conv-uuid", ["nested"], 7]
        assert AnswerRow(rec).turn_key == ConversationTurnKey("conv-uuid", None, 7)

    def test_bool_turn_code_is_rejected(self) -> None:
        """``bool`` is an ``int`` subclass; a wire ``true`` must not read as 1."""
        rec = _answer_record()
        rec[2] = ["conv-uuid", "turn-uuid", True]
        assert AnswerRow(rec).turn_key == ConversationTurnKey("conv-uuid", "turn-uuid", None)


class TestConversationTurnKeyConstructor:
    def test_empty_session_id_is_refused(self) -> None:
        """The class exists because the parts travel together; a key with no
        session id addresses nothing, so it cannot be built."""
        with pytest.raises(ValueError, match="non-empty session_id"):
            ConversationTurnKey("")

    def test_trailing_parts_stay_optional(self) -> None:
        """A short wire block is decode-time absence, not a broken key."""
        assert ConversationTurnKey("s").turn_id is None
        assert ConversationTurnKey("s").turn_code is None
