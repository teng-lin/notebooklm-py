"""Raw ``[cite: N]`` markers the backend never resolved (#2563)."""

import pytest

from notebooklm._types.chat import count_unresolved_citation_markers
from notebooklm.types import AskResult


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("Resolved claim [1] and another [2, 3].", 0),
        ("One [cite: 3].", 1),
        ("Two [cite: 4, 5] and [cite:12] and [cite: 7 ].", 3),
        ("Mixed [1] with raw [cite: 2].", 1),
        # Not a citation marker: no number, or prose that merely says "cite".
        ("See [cite: N] in the docs; also [cite here].", 0),
    ],
)
def test_count_unresolved_citation_markers(text: str, expected: int) -> None:
    assert count_unresolved_citation_markers(text) == expected


def test_ask_result_exposes_unresolved_citation_count() -> None:
    result = AskResult(
        answer="Claim [cite: 1] and claim [cite: 2, 3].",
        conversation_id="conv-1",
        turn_number=3,
        is_follow_up=True,
    )
    assert result.unresolved_citation_count == 2
