"""Tests for the notebook row adapters.

Covers the ``GeneratePromptSuggestions`` (``otmP3b`` / ``SUGGEST_PROMPTS``)
suggestion-list unwrap and per-row reads that back
``NotebooksAPI.suggest_prompts``:

1. **Position-contract pin** — the canary that fails loudly if a position
   constant is edited (the wire-shape change signal).
2. **Shape handling** — happy-path reads plus the permissive "absent / short /
   non-list → default" degrade (a suggestion list is best-effort UI sugar).
"""

from __future__ import annotations

import pytest

from notebooklm._row_adapters.notebooks import (
    PromptSuggestionRow,
    unwrap_prompt_suggestions,
)


class TestPromptSuggestionPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            PromptSuggestionRow._TITLE_POS,
            PromptSuggestionRow._PROMPT_POS,
            PromptSuggestionRow._MIN_LEN,
        ) == (0, 1, 2)


class TestPromptSuggestionRow:
    """Permissive position reads for one ``SUGGEST_PROMPTS`` suggestion row."""

    def test_well_formed_row(self) -> None:
        # The backend formats the prompt as a markdown list item; the leading
        # "\n- " marker is stripped so it is a clean ready-to-send string (#1909).
        row = PromptSuggestionRow(["Professional Briefing", "\n- Summarize."])
        assert row.is_well_formed
        assert row.title == "Professional Briefing"
        assert row.prompt == "Summarize."

    def test_leading_list_marker_stripped(self) -> None:
        # Bullet ("- "/"* "/"+ ") leading markers, with any surrounding
        # whitespace, are stripped from both title and prompt.
        assert PromptSuggestionRow(["t", "\n- Ask X"]).prompt == "Ask X"
        assert PromptSuggestionRow(["t", "* Ask X"]).prompt == "Ask X"
        assert PromptSuggestionRow(["t", "  + Ask X"]).prompt == "Ask X"
        assert PromptSuggestionRow(["\n- A title", "p"]).title == "A title"

    def test_marker_only_leaf_collapses_to_empty(self) -> None:
        # A leaf that is only a bullet + whitespace collapses to "" — not a bare
        # "-" (the lstrip-before-match structure handles this, #1912 review).
        assert PromptSuggestionRow(["t", "\n-   "]).prompt == ""

    def test_numeric_prefix_preserved(self) -> None:
        # Ordered-list counters are NOT stripped — a legit numeric prefix (a year,
        # a count) is meaningful content, not list framing (#1912 review).
        assert (
            PromptSuggestionRow(["t", "2026. Summarize the annual trends"]).prompt
            == "2026. Summarize the annual trends"
        )
        assert (
            PromptSuggestionRow(["t", "5) reasons to refactor"]).prompt == "5) reasons to refactor"
        )

    def test_clean_leaf_unchanged_apart_from_surrounding_whitespace(self) -> None:
        # A leaf with no leading marker keeps its content; only surrounding
        # whitespace is trimmed, and interior newlines are preserved.
        assert PromptSuggestionRow(["t", "Ask X"]).prompt == "Ask X"
        assert PromptSuggestionRow(["t", "Line one\n- Line two"]).prompt == "Line one\n- Line two"

    def test_short_row_degrades_without_raise(self) -> None:
        # Missing the prompt slot (< _MIN_LEN): not well-formed; reads degrade to "".
        row = PromptSuggestionRow(["Only a title"])
        assert not row.is_well_formed
        assert row.title == "Only a title"
        assert row.prompt == ""

    def test_non_string_and_non_list_shapes_degrade(self) -> None:
        # Non-string fields and non-list rows degrade to "" / not-well-formed,
        # never raising — a suggestion list is best-effort UI sugar.
        assert PromptSuggestionRow([None, 7]).title == ""
        assert PromptSuggestionRow([None, 7]).prompt == ""
        for raw in (None, "x", 7, {"k": 1}):
            row = PromptSuggestionRow(raw)
            assert not row.is_well_formed
            assert (row.title, row.prompt) == ("", "")


class TestUnwrapPromptSuggestions:
    """``result[0]`` envelope-unwrap for ``SUGGEST_PROMPTS`` replies."""

    def test_wrapped_envelope_returns_inner_rows(self) -> None:
        rows = [["A", "\n- a"], ["B", "\n- b"]]
        assert unwrap_prompt_suggestions([rows], source="t") == rows

    @pytest.mark.parametrize("payload", [None, [], "unexpected", 7, [None], [[]]])
    def test_degenerate_payloads_yield_empty(self, payload: object) -> None:
        assert unwrap_prompt_suggestions(payload, source="t") == []

    def test_never_raises(self) -> None:
        # Best-effort: every shape degrades, never UnknownRPCMethodError.
        for raw in (None, "x", 7, {"k": 1}, [7], [[1, 2]]):
            unwrap_prompt_suggestions(raw, source="t")
