"""Unit tests for module-level helpers in ``notebooklm._chat``.

Focuses on ``_extract_next_turn_content`` — the named extractor that
replaces the raw ``next_turn[4][0][0]`` deep-index chain in
:meth:`ChatAPI._parse_turns_to_qa_pairs`. Strict decoding is the only mode
(the ``NOTEBOOKLM_STRICT_DECODE=0`` soft-mode opt-out was retired in v0.7.0):
shape drift raises ``UnknownRPCMethodError`` while a non-string leaf at a
valid path normalises to ``None``.
"""

from __future__ import annotations

import pytest

from notebooklm._chat import ChatAPI, _extract_next_turn_content
from notebooklm.exceptions import UnknownRPCMethodError

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
# _extract_next_turn_content — drift shapes (raise under strict decoding)
# ---------------------------------------------------------------------------


def test_extract_next_turn_content_missing_inner_list() -> None:
    """``turn[4]`` exists but is an empty list — descent drift raises."""
    next_turn = [None, None, 2, None, []]

    with pytest.raises(UnknownRPCMethodError):
        _extract_next_turn_content(next_turn)


def test_extract_next_turn_content_wrong_type_at_level() -> None:
    """``turn[4][0]`` is a non-list scalar — descent drift raises."""
    # The inner wrapper is a scalar, so descending past index ``[0]`` fails,
    # which safe_index surfaces as ``UnknownRPCMethodError`` under strict
    # decoding.
    next_turn = [None, None, 2, None, [42]]

    with pytest.raises(UnknownRPCMethodError):
        _extract_next_turn_content(next_turn)


def test_extract_next_turn_content_non_string_leaf() -> None:
    """Leaf at ``[4][0][0]`` is a scalar instead of a string — returns ``None``.

    This guards the explicit ``isinstance(content, str)`` normalisation
    branch in ``_extract_next_turn_content`` — distinct from the
    ``safe_index`` drift path, which raises only when descent itself fails.
    """
    next_turn = [None, None, 2, None, [[12345]]]

    result = _extract_next_turn_content(next_turn)

    assert result is None


# ---------------------------------------------------------------------------
# _parse_turns_to_qa_pairs — shape drift raises under strict decoding
# ---------------------------------------------------------------------------


def test_parse_turns_to_qa_pairs_drift_raises() -> None:
    """An answer turn with a broken inner shape raises under strict decoding.

    Strict decoding is the only mode (the soft-mode opt-out was retired in
    v0.7.0), so a genuine ``safe_index`` drift propagates rather than being
    silently coerced into an empty-answer pair.
    """
    turns_data = [
        [
            [None, None, 1, "Question?"],
            [None, None, 2, None, []],  # empty inner — drift via safe_index
        ]
    ]

    with pytest.raises(UnknownRPCMethodError):
        ChatAPI._parse_turns_to_qa_pairs(turns_data)
