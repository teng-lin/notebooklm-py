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
        row = PromptSuggestionRow(["Professional Briefing", "\n- Summarize."])
        assert row.is_well_formed
        assert row.title == "Professional Briefing"
        assert row.prompt == "\n- Summarize."

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
