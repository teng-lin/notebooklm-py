"""Unit tests for chat history parsing and formatting functions.

Tests cover:
- ChatAPI._parse_turns_to_qa_pairs: Raw API turn data → (question, answer) pairs
- CLI helpers: _format_single_qa, _format_all_qa, _determine_conversation_id
- CLI helper: _get_latest_conversation_from_server
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._chat import ChatAPI
from notebooklm.exceptions import UnknownRPCMethodError


class TestParseTurnsToQaPairs:
    """Tests for ChatAPI._parse_turns_to_qa_pairs static method.

    The API returns turns in chronological order: [Q, A, Q, A, ...]
    where turn[2]==1 is a user question (text at turn[3])
    and turn[2]==2 is an AI answer (text at turn[4][0][0]).
    """

    def test_single_qa_pair(self):
        turns_data = [
            [
                [None, None, 1, "What is AI?"],
                [None, None, 2, None, [["AI is artificial intelligence."]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        assert result == [("What is AI?", "AI is artificial intelligence.")]

    def test_multiple_qa_pairs(self):
        turns_data = [
            [
                [None, None, 1, "First question?"],
                [None, None, 2, None, [["First answer."]]],
                [None, None, 1, "Second question?"],
                [None, None, 2, None, [["Second answer."]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        assert len(result) == 2
        assert result[0] == ("First question?", "First answer.")
        assert result[1] == ("Second question?", "Second answer.")

    def test_three_qa_pairs(self):
        turns_data = [
            [
                [None, None, 1, "Q1"],
                [None, None, 2, None, [["A1"]]],
                [None, None, 1, "Q2"],
                [None, None, 2, None, [["A2"]]],
                [None, None, 1, "Q3"],
                [None, None, 2, None, [["A3"]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        assert len(result) == 3
        assert result[0] == ("Q1", "A1")
        assert result[1] == ("Q2", "A2")
        assert result[2] == ("Q3", "A3")

    def test_question_without_answer(self):
        """A question at the end with no following answer gets empty string."""
        turns_data = [
            [
                [None, None, 1, "Unanswered question?"],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        assert result == [("Unanswered question?", "")]

    def test_question_followed_by_another_question(self):
        """When two questions are adjacent, the first gets an empty answer."""
        turns_data = [
            [
                [None, None, 1, "First?"],
                [None, None, 1, "Second?"],
                [None, None, 2, None, [["Answer to second."]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        assert len(result) == 2
        assert result[0] == ("First?", "")
        assert result[1] == ("Second?", "Answer to second.")

    def test_empty_turns_data(self):
        """Falsy payloads are absence — soft ``[]``, no drift."""
        assert ChatAPI._parse_turns_to_qa_pairs(None) == []
        assert ChatAPI._parse_turns_to_qa_pairs([]) == []
        assert ChatAPI._parse_turns_to_qa_pairs("") == []

    def test_truthy_non_list_payload_raises(self):
        """A truthy non-list TOP-LEVEL payload is wire drift, not absence.

        This historically parsed to a silent ``[]`` (the fabricated-empty-
        history class this hardening removes); per the #1485
        absence-vs-malformed policy it now raises ``UnknownRPCMethodError``
        from ``unwrap_conversation_turns`` — same as the inner-slot case.
        """
        with pytest.raises(UnknownRPCMethodError):
            ChatAPI._parse_turns_to_qa_pairs("not a list")
        with pytest.raises(UnknownRPCMethodError):
            ChatAPI._parse_turns_to_qa_pairs({"unexpected": "dict"})

    def test_empty_inner_list(self):
        assert ChatAPI._parse_turns_to_qa_pairs([[]]) == []

    def test_none_inner_slot_is_soft_empty(self):
        """A ``None`` where the turn list belongs is absence, not drift."""
        assert ChatAPI._parse_turns_to_qa_pairs([None]) == []

    def test_inner_not_list_raises(self):
        """A *truthy non-list* where the turn list belongs is wire drift.

        Historically this silently parsed to ``[]`` — fabricating an empty
        chat history on a Google reshape. Per the #1485 absence-vs-malformed
        policy it now raises ``UnknownRPCMethodError`` (via
        ``unwrap_conversation_turns``), consistent with the strict
        ``safe_index`` leaf behavior in ``_extract_next_turn_content``.
        """
        with pytest.raises(UnknownRPCMethodError):
            ChatAPI._parse_turns_to_qa_pairs(["string"])
        with pytest.raises(UnknownRPCMethodError):
            ChatAPI._parse_turns_to_qa_pairs([42])

    def test_malformed_turn_too_short(self):
        """Turns with fewer than 3 elements are skipped."""
        turns_data = [
            [
                [None, None],  # too short, skipped
                [None, None, 1, "Valid question?"],
                [None, None, 2, None, [["Valid answer."]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        assert result == [("Valid question?", "Valid answer.")]

    def test_malformed_turn_skip_logs_debug_diagnostic(self, caplog):
        """A skipped malformed turn leaves a DEBUG record (never fully silent)."""
        import logging

        turns_data = [
            [
                "not a turn",  # malformed row: skipped WITH a diagnostic
                [None, None, 1, "Valid question?"],
                [None, None, 2, None, [["Valid answer."]]],
            ]
        ]
        with caplog.at_level(logging.DEBUG, logger="notebooklm"):
            result = ChatAPI._parse_turns_to_qa_pairs(turns_data)

        assert result == [("Valid question?", "Valid answer.")]
        assert any(
            r.levelno == logging.DEBUG and "skipping malformed turn" in r.message
            for r in caplog.records
        )

    def test_unrecognized_role_code_logs_debug_and_skips(self, caplog):
        """A well-formed turn with a role outside {1, 2} is role-slot drift.

        Historically such rows vanished silently — real history could parse
        to ``[]`` with zero diagnostics. The walk now leaves a DEBUG record
        before skipping, and surrounding valid pairs still parse (#1485).
        """
        import logging

        turns_data = [
            [
                [None, None, "user", "Drifted question?"],  # unknown role code
                [None, None, 1, "Valid question?"],
                [None, None, 2, None, [["Valid answer."]]],
            ]
        ]
        with caplog.at_level(logging.DEBUG, logger="notebooklm"):
            result = ChatAPI._parse_turns_to_qa_pairs(turns_data)

        assert result == [("Valid question?", "Valid answer.")]
        assert any(
            r.levelno == logging.DEBUG and "unrecognized role code" in r.message
            for r in caplog.records
        )

    def test_unpaired_answer_rows_do_not_log_role_diagnostic(self, caplog):
        """Ordinary unpaired ROLE_ANSWER rows are NOT role drift — no log.

        Answers are legitimately consumed via pairing; only role values
        outside {ROLE_QUESTION, ROLE_ANSWER} get the diagnostic.
        """
        import logging

        turns_data = [
            [
                [None, None, 2, None, [["Orphan answer."]]],
                [None, None, 1, "Question?"],
                [None, None, 2, None, [["Paired answer."]]],
            ]
        ]
        with caplog.at_level(logging.DEBUG, logger="notebooklm"):
            result = ChatAPI._parse_turns_to_qa_pairs(turns_data)

        assert result == [("Question?", "Paired answer.")]
        assert not any("unrecognized role code" in r.message for r in caplog.records)

    def test_non_list_turn_skipped(self):
        """Non-list items in the turns array are skipped but break Q-A adjacency."""
        turns_data = [
            [
                "not a turn",
                [None, None, 1, "Question?"],
                42,  # non-list between Q and A breaks the pair
                [None, None, 2, None, [["Answer."]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        # The 42 at i+1 is not a valid answer turn, so Q gets empty answer
        assert result == [("Question?", "")]

    def test_answer_with_index_error(self):
        """Answer turn with broken structure raises under strict decoding.

        Strict decoding is the only mode (the ``NOTEBOOKLM_STRICT_DECODE=0``
        soft-mode opt-out was retired in v0.7.0), so a non-descendable inner
        shape raises ``UnknownRPCMethodError`` rather than degrading to an
        empty answer.
        """
        turns_data = [
            [
                [None, None, 1, "Question?"],
                [None, None, 2, None, []],  # empty nested list
            ]
        ]
        with pytest.raises(UnknownRPCMethodError):
            ChatAPI._parse_turns_to_qa_pairs(turns_data)

    def test_answer_with_none_text(self):
        """Answer turn where text is None yields 'None' (str conversion)."""
        turns_data = [
            [
                [None, None, 1, "Question?"],
                [None, None, 2, None, [[None]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        # str(None) = "None", but (None or "") = "" so it should be empty
        assert result == [("Question?", "")]

    def test_question_with_none_text(self):
        """Question turn with None text yields empty string."""
        turns_data = [
            [
                [None, None, 1, None],
                [None, None, 2, None, [["Answer."]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        assert result == [("", "Answer.")]

    def test_only_answer_turns_returns_empty(self):
        """If there are only answer turns (type 2) and no questions, returns empty."""
        turns_data = [
            [
                [None, None, 2, None, [["An answer with no question."]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        assert result == []

    def test_preserves_long_text(self):
        """Full text is preserved (truncation happens at display layer, not parsing)."""
        long_q = "Q" * 500
        long_a = "A" * 500
        turns_data = [
            [
                [None, None, 1, long_q],
                [None, None, 2, None, [[long_a]]],
            ]
        ]
        result = ChatAPI._parse_turns_to_qa_pairs(turns_data)
        assert result == [(long_q, long_a)]


class TestFormatHelpers:
    """Tests for CLI formatting helpers."""

    def test_format_single_qa_both_present(self):
        from notebooklm.cli.chat_cmd import _format_single_qa

        result = _format_single_qa("What is AI?", "AI is artificial intelligence.")
        assert "**Q:** What is AI?" in result
        assert "**A:** AI is artificial intelligence." in result

    def test_format_single_qa_question_only(self):
        from notebooklm.cli.chat_cmd import _format_single_qa

        result = _format_single_qa("What is AI?", "")
        assert "**Q:** What is AI?" in result
        assert "**A:**" not in result

    def test_format_single_qa_answer_only(self):
        from notebooklm.cli.chat_cmd import _format_single_qa

        result = _format_single_qa("", "The answer.")
        assert "**Q:**" not in result
        assert "**A:** The answer." in result

    def test_format_single_qa_both_empty(self):
        from notebooklm.cli.chat_cmd import _format_single_qa

        result = _format_single_qa("", "")
        assert result == ""


class TestDetermineConversationId:
    """Tests for _determine_conversation_id CLI helper."""

    def test_explicit_conversation_id_used(self):
        from notebooklm.cli.chat_cmd import _determine_conversation_id

        result = _determine_conversation_id(
            explicit_conversation_id="conv_explicit",
            explicit_notebook_id=None,
            resolved_notebook_id="nb_123",
            json_output=True,
        )
        assert result == "conv_explicit"

    def test_different_notebook_starts_new(self):
        from notebooklm.cli.chat_cmd import _determine_conversation_id

        with patch("notebooklm.cli.chat_cmd.get_current_notebook", return_value="nb_old"):
            result = _determine_conversation_id(
                explicit_conversation_id=None,
                explicit_notebook_id="nb_new",
                resolved_notebook_id="nb_new",
                json_output=True,
            )
        assert result is None

    def test_same_notebook_continues_cached(self):
        from notebooklm.cli.chat_cmd import _determine_conversation_id

        with (
            patch("notebooklm.cli.chat_cmd.get_current_notebook", return_value="nb_123"),
            patch("notebooklm.cli.chat_cmd.get_current_conversation", return_value="conv_cached"),
        ):
            result = _determine_conversation_id(
                explicit_conversation_id=None,
                explicit_notebook_id="nb_123",
                resolved_notebook_id="nb_123",
                json_output=True,
            )
        assert result == "conv_cached"

    def test_no_explicit_notebook_uses_cached(self):
        from notebooklm.cli.chat_cmd import _determine_conversation_id

        with patch("notebooklm.cli.chat_cmd.get_current_conversation", return_value="conv_cached"):
            result = _determine_conversation_id(
                explicit_conversation_id=None,
                explicit_notebook_id=None,
                resolved_notebook_id="nb_123",
                json_output=True,
            )
        assert result == "conv_cached"


class TestGetLatestConversationFromServer:
    """Tests for _get_latest_conversation_from_server CLI helper."""

    @pytest.mark.asyncio
    async def test_returns_conversation_id(self):
        from notebooklm.cli.chat_cmd import _get_latest_conversation_from_server

        client = MagicMock()
        client.chat.get_conversation_id = AsyncMock(return_value="conv_from_server")

        result = await _get_latest_conversation_from_server(client, "nb_123", json_output=True)
        assert result == "conv_from_server"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_conversations(self):
        from notebooklm.cli.chat_cmd import _get_latest_conversation_from_server

        client = MagicMock()
        client.chat.get_conversation_id = AsyncMock(return_value=None)

        result = await _get_latest_conversation_from_server(client, "nb_123", json_output=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        from notebooklm.cli.chat_cmd import _get_latest_conversation_from_server

        client = MagicMock()
        client.chat.get_conversation_id = AsyncMock(side_effect=RuntimeError("Network error"))

        result = await _get_latest_conversation_from_server(client, "nb_123", json_output=True)
        assert result is None
