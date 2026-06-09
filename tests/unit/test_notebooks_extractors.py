"""Unit tests for named extraction helpers in ``_notebooks.py``.

These cover ``_extract_summary`` and ``_extract_suggested_topics`` — the
named wrappers that replaced raw ``outer[0][0]`` / ``outer[1][0]`` deep
index access in ``NotebooksAPI.get_description``. Each helper gets a
happy-path case plus two drift cases (missing index, wrong type).
"""

from __future__ import annotations

import logging

import pytest

from notebooklm._notebooks import _extract_suggested_topics, _extract_summary
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.types import SuggestedTopic


class TestExtractSummary:
    """Happy path + drift coverage for ``_extract_summary``."""

    def test_happy_path_returns_summary_string(self) -> None:
        # Real shape: result[0] = [["the summary"], [[...topics...]]]
        outer = [["the summary"], [[["Q", "P"]]]]

        assert _extract_summary(outer) == "the summary"

    def test_none_outer_returns_empty(self) -> None:
        # Regression for #1485: a brand-new, source-less notebook has no
        # summary, so result[0] (== outer) is None. Routine "no summary yet",
        # NOT schema drift.
        assert _extract_summary(None) == ""

    def test_empty_outer_list_returns_empty(self) -> None:
        # Regression for #1485: outer with no [0] slot at all is the same
        # routine "no summary yet" state.
        assert _extract_summary([]) == ""

    def test_none_at_outer_zero_returns_empty(self) -> None:
        # Regression for #1485: outer[0] is explicitly None (no summary slot)
        # — routine absence, handled by the plain guard, not safe_index drift.
        assert _extract_summary([None, [[["Q", "P"]]]]) == ""

    def test_nested_summary_string_returns_string(self) -> None:
        # outer[0][0] holds the summary string at the documented shape.
        assert _extract_summary([["the summary"]]) == "the summary"

    def test_drift_missing_inner_index_raises(self) -> None:
        # outer[0] is present but an empty list — the inner safe_index descent
        # into outer[0][0] drifts and raises under strict decoding (the only
        # mode). Present-but-malformed is real drift, distinct from absence.
        outer: list = [[], [[["Q", "P"]]]]

        with pytest.raises(UnknownRPCMethodError) as exc_info:
            _extract_summary(outer)

        assert exc_info.value.source == "_notebooks._extract_summary"

    def test_drift_wrong_type_at_outer_zero_raises(self) -> None:
        # outer[0] is present but an int — the inner safe_index descent into
        # outer[0][0] raises TypeError, surfaced as a typed drift error. A
        # present-but-malformed slot must NOT be over-suppressed as "absence".
        outer = [42, [[["Q", "P"]]]]

        with pytest.raises(UnknownRPCMethodError):
            _extract_summary(outer)

    def test_drift_scalar_outer_raises(self) -> None:
        # Regression for #1485 codex review: ``outer`` is itself a scalar (not
        # None, not a list) — e.g. the server returned ``result[0] == 123``.
        # This is present-but-malformed at the payload level, NOT the routine
        # "no summary yet" absence, so it must raise drift rather than silently
        # collapse to "". A `not isinstance(outer, list)` short-circuit would
        # wrongly suppress it; the descent must reach safe_index.
        with pytest.raises(UnknownRPCMethodError) as exc_info:
            _extract_summary(123)

        assert exc_info.value.source == "_notebooks._extract_summary"

    def test_drift_str_outer_raises(self) -> None:
        # Regression for #1485 codex review (second round): a *string* outer is
        # indexable, so a naive safe_index descent would char-index it
        # ("abc"[0] -> "a") and silently return "a" instead of raising. A string
        # at the outer-payload level is genuine drift, not a 1-char summary.
        with pytest.raises(UnknownRPCMethodError):
            _extract_summary("abc")

    def test_drift_str_at_outer_zero_raises(self) -> None:
        # And a string at outer[0] (present, non-None, but not the expected
        # [summary_string, ...] list) is drift too — must not collapse to the
        # first character of the string.
        with pytest.raises(UnknownRPCMethodError):
            _extract_summary(["summary text"])


class TestExtractSuggestedTopics:
    """Happy path + drift coverage for ``_extract_suggested_topics``."""

    def test_happy_path_returns_typed_topics(self) -> None:
        outer = [
            ["summary text"],
            [
                [
                    ["What is X?", "Explain X"],
                    ["How does Y work?", "Describe Y"],
                ]
            ],
        ]

        topics = _extract_suggested_topics(outer)

        assert topics == [
            SuggestedTopic(question="What is X?", prompt="Explain X"),
            SuggestedTopic(question="How does Y work?", prompt="Describe Y"),
        ]

    def test_drift_missing_outer_one_returns_empty_with_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # outer has only the summary slot — no outer[1].
        outer = [["summary only"]]

        with caplog.at_level(logging.DEBUG, logger="notebooklm"):
            topics = _extract_suggested_topics(outer)

        assert topics == []
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "Partial description" in r.message
        ]
        assert debug_records, "expected DEBUG diagnostic when outer[1] is absent"
        # And no WARNING should be emitted for this legitimate "no topics" case.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []

    def test_drift_wrong_type_at_outer_one_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # outer[1] is a string instead of a list — should not parse topics.
        outer = [["summary text"], "not-a-list"]

        with caplog.at_level(logging.DEBUG, logger="notebooklm"):
            topics = _extract_suggested_topics(outer)

        assert topics == []
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "Partial description" in r.message
        ]
        assert debug_records, "expected DEBUG diagnostic when outer[1] is wrong type"

    def test_inner_drift_at_topics_zero_logs_and_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # outer[1] is a non-empty list whose [0] is not a list — safe_index
        # descent succeeds (returns the value) but isinstance check rejects it.
        outer = [["summary text"], ["not-a-topic-list"]]

        with caplog.at_level(logging.DEBUG, logger="notebooklm"):
            topics = _extract_suggested_topics(outer)

        assert topics == []
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "expected list at outer[1][0]" in r.message
        ]
        assert debug_records, "expected DEBUG when outer[1][0] is wrong type"

    def test_malformed_topic_entries_are_skipped(self) -> None:
        # Mixed: one valid topic, one too-short, one wrong-type. Only the
        # valid entry should make it into the result; the rest are dropped
        # rather than aborting the whole list.
        outer = [
            ["summary"],
            [
                [
                    ["Valid question", "Valid prompt"],
                    ["Only question"],  # too short
                    "not a list",  # wrong type
                ]
            ],
        ]

        topics = _extract_suggested_topics(outer)

        assert topics == [SuggestedTopic(question="Valid question", prompt="Valid prompt")]
