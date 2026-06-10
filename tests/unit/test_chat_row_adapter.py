"""Tests for the streamed-chat row adapters (issue #1491).

These adapters centralise the positional knowledge ``_chat/wire.py`` used to
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

import pytest

from notebooklm._row_adapters.chat import (
    AnswerRow,
    CitationDetail,
    CitationRow,
    ConversationTurnRow,
    ErrorPayloadRow,
    PassageRow,
    StreamFrameRow,
    TextLeafRow,
    unwrap_conversation_turns,
)
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod

# ---------------------------------------------------------------------------
# 1. Position-contract pins (the canaries)
# ---------------------------------------------------------------------------


class TestAnswerRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            AnswerRow._TEXT_POS,
            AnswerRow._CONV_BLOCK_POS,
            AnswerRow._TYPE_BLOCK_POS,
            AnswerRow._ANSWER_MARKER_POS,
            AnswerRow._CITATIONS_POS,
            AnswerRow._ANSWER_MARKER_VALUE,
        ) == (0, 2, 4, -1, 3, 1)


class TestCitationPositionContract:
    def test_citation_row_positions_pinned(self) -> None:
        assert (CitationRow._CHUNK_BLOCK_POS, CitationRow._DETAIL_POS) == (0, 1)

    def test_citation_detail_positions_pinned(self) -> None:
        assert (
            CitationDetail._SCORE_POS,
            CitationDetail._ANSWER_RANGE_POS,
            CitationDetail._PASSAGES_POS,
            CitationDetail._SOURCE_ID_POS,
            CitationDetail._ANSWER_RANGE_START_POS,
            CitationDetail._ANSWER_RANGE_END_POS,
        ) == (2, 3, 4, 5, 1, 2)

    def test_passage_row_positions_pinned(self) -> None:
        assert (
            PassageRow._PASSAGE_DATA_POS,
            PassageRow._START_POS,
            PassageRow._END_POS,
            PassageRow._TEXT_PAYLOAD_POS,
        ) == (0, 0, 1, 2)


class TestFramePositionContract:
    def test_stream_frame_positions_pinned(self) -> None:
        assert (
            StreamFrameRow._TAG_POS,
            StreamFrameRow._INNER_JSON_POS,
            StreamFrameRow._ERROR_CODE_POS,
            StreamFrameRow._ERROR_PAYLOAD_POS,
        ) == (0, 2, 2, 5)

    def test_error_payload_positions_pinned(self) -> None:
        assert ErrorPayloadRow._ENTRIES_POS == 2

    def test_text_leaf_positions_pinned(self) -> None:
        assert TextLeafRow._TEXT_POS == 2


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
    def test_score_passages_source_id(self) -> None:
        detail = CitationDetail([None, None, 0.75, None, [["p"]], ["src-data"]])
        assert detail.raw_score == 0.75
        assert detail.passages == [["p"]]
        assert detail.source_id_data == ["src-data"]
        assert detail.raw_list == [None, None, 0.75, None, [["p"]], ["src-data"]]

    def test_answer_range_reads_inner_triple(self) -> None:
        detail = CitationDetail([None, None, None, [[None, 5, 9]]])
        assert detail.answer_range() == (5, 9)

    @pytest.mark.parametrize(
        "raw",
        [
            [None, None, None],  # too short for answer-range slot
            [None, None, None, []],  # empty outer
            [None, None, None, ["not-a-list"]],  # inner not a list
            [None, None, None, [[None, 5]]],  # inner too short
        ],
    )
    def test_answer_range_degrades_to_none_none(self, raw: list) -> None:
        assert CitationDetail(raw).answer_range() == (None, None)

    def test_short_detail_degrades(self) -> None:
        detail = CitationDetail([None, None])
        assert detail.raw_score is None
        assert detail.passages == []
        assert detail.source_id_data is None


# ---------------------------------------------------------------------------
# 4. PassageRow
# ---------------------------------------------------------------------------


class TestPassageRow:
    def test_unwraps_wrapper_and_reads_start_end_text(self) -> None:
        passage = PassageRow([[10, 20, [["text"]]]])
        assert passage.is_well_formed is True
        assert passage.start_char == 10
        assert passage.end_char == 20
        assert passage.text_payload == [["text"]]

    @pytest.mark.parametrize(
        "raw",
        [
            [],  # empty wrapper
            ["not-a-list"],  # inner not a list
            [[10, 20]],  # inner too short (< 3)
            "not-a-list",
            None,
        ],
    )
    def test_malformed_wrapper_degrades(self, raw: object) -> None:
        passage = PassageRow(raw)
        assert passage.is_well_formed is False
        assert passage.start_char is None
        assert passage.end_char is None
        assert passage.text_payload is None


# ---------------------------------------------------------------------------
# 5. StreamFrameRow / ErrorPayloadRow / TextLeafRow
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

    @pytest.mark.parametrize("entry", [[], [123], "x", None])
    def test_non_string_entry_type_is_none(self, entry: object) -> None:
        assert ErrorPayloadRow.entry_type(entry) is None


class TestTextLeafRow:
    def test_reads_text_value(self) -> None:
        leaf = TextLeafRow([0, 1, "hello"])
        assert leaf.is_well_formed is True
        assert leaf.text_value == "hello"

    @pytest.mark.parametrize("raw", [[], [0, 1], "x", None])
    def test_short_or_non_list_degrades(self, raw: object) -> None:
        leaf = TextLeafRow(raw)
        assert leaf.is_well_formed is False
        assert leaf.text_value is None


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
