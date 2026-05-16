"""Unit tests for module-level helpers in ``notebooklm._chat``.

Focuses on ``_extract_next_turn_content`` — the named extractor that
replaces the raw ``next_turn[4][0][0]`` deep-index chain in
:meth:`ChatAPI._parse_turns_to_qa_pairs`. These tests pin the soft-mode
contract (returns ``None`` on shape drift, never raises) and exercise the
happy path plus two canonical drift shapes.
"""

from __future__ import annotations

import logging

import pytest

from notebooklm._chat import ChatAPI, _extract_next_turn_content

# ---------------------------------------------------------------------------
# _extract_next_turn_content — happy path
# ---------------------------------------------------------------------------


def test_extract_next_turn_content_happy_path() -> None:
    """Well-formed khqZz answer turn: returns the inner answer string."""
    # An AI answer turn from ``khqZz`` (GET_CONVERSATION_TURNS): turn[2] == 2,
    # turn[4] == [[answer_text]]. The first four slots (id/?/type/?) match
    # the schema documented in _chat.get_conversation_turns.
    next_turn = [None, None, 2, None, [["AI answer text."]]]

    result = _extract_next_turn_content(next_turn)

    assert result == "AI answer text."


# ---------------------------------------------------------------------------
# _extract_next_turn_content — drift shapes (must NOT raise)
# ---------------------------------------------------------------------------


def test_extract_next_turn_content_missing_inner_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``turn[4]`` exists but is an empty list — descent stops at index 0."""
    next_turn = [None, None, 2, None, []]

    with caplog.at_level(logging.WARNING):
        result = _extract_next_turn_content(next_turn)

    assert result is None
    # safe_index emits the structured drift warning; we don't assert on the
    # exact wording to keep the test resilient to log-format tweaks.
    assert any("safe_index" in rec.message or "drift" in rec.message for rec in caplog.records)


def test_extract_next_turn_content_wrong_type_at_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``turn[4][0]`` is a string instead of a list — descent must fail soft."""
    # The inner wrapper is a scalar, so descending to index ``[0]`` of a
    # string would succeed (yielding 'n'), but descending again would not —
    # safe_index normalises this to ``None`` via its TypeError/IndexError
    # catch and the helper's ``isinstance(..., str)`` check rejects single
    # characters that are not the expected wrapper shape… here we use a
    # non-string non-list scalar to force the safe_index drift path.
    next_turn = [None, None, 2, None, [42]]

    with caplog.at_level(logging.WARNING):
        result = _extract_next_turn_content(next_turn)

    assert result is None


def test_extract_next_turn_content_non_string_leaf() -> None:
    """Leaf at ``[4][0][0]`` is a scalar instead of a string — returns ``None``.

    This guards the explicit ``isinstance(content, str)`` normalisation
    branch in ``_extract_next_turn_content`` — distinct from the soft-mode
    ``safe_index`` drift path, which only triggers when descent itself
    fails.
    """
    next_turn = [None, None, 2, None, [[12345]]]

    result = _extract_next_turn_content(next_turn)

    assert result is None


# ---------------------------------------------------------------------------
# Regression: _parse_turns_to_qa_pairs still degrades gracefully
# ---------------------------------------------------------------------------


def test_parse_turns_to_qa_pairs_drift_yields_empty_answer() -> None:
    """Answer turn with broken inner shape yields an empty answer string.

    Pins the contract that the named extractor preserves the previous
    empty-answer fallback semantics of :meth:`_parse_turns_to_qa_pairs`.
    """
    turns_data = [
        [
            [None, None, 1, "Question?"],
            [None, None, 2, None, []],  # empty inner — drift via safe_index
        ]
    ]

    result = ChatAPI._parse_turns_to_qa_pairs(turns_data)

    assert result == [("Question?", "")]
