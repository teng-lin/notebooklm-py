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
from notebooklm.types import SuggestedTopic


class TestExtractSummary:
    """Happy path + drift coverage for ``_extract_summary``."""

    def test_happy_path_returns_summary_string(self) -> None:
        # Real shape: result[0] = [["the summary"], [[...topics...]]]
        outer = [["the summary"], [[["Q", "P"]]]]

        assert _extract_summary(outer) == "the summary"

    def test_drift_missing_inner_index_returns_empty_string(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Post-PR 13.9a default is strict; this test pins the soft-mode
        # warn-and-return-"" contract by opting back in explicitly.
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "0")
        # outer[0] is an empty list — outer[0][0] would IndexError.
        outer: list = [[], [[["Q", "P"]]]]

        with (
            caplog.at_level(logging.WARNING, logger="notebooklm"),
            pytest.warns(DeprecationWarning, match="safe_index soft-mode"),
        ):
            result = _extract_summary(outer)

        assert result == ""
        # safe_index logs at WARNING in soft-decode mode when descent fails.
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "safe_index drift" in r.message
        ]
        assert warning_records, "expected safe_index drift WARNING on missing index"
        assert any(
            "_extract_summary" in str(r.args) or "_extract_summary" in r.message
            for r in warning_records
        )

    def test_drift_wrong_type_at_outer_zero_returns_empty_string(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NOTEBOOKLM_STRICT_DECODE", "0")
        # outer[0] is an int — outer[0][0] raises TypeError.
        outer = [42, [[["Q", "P"]]]]

        with (
            caplog.at_level(logging.WARNING, logger="notebooklm"),
            pytest.warns(DeprecationWarning, match="safe_index soft-mode"),
        ):
            result = _extract_summary(outer)

        assert result == ""
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "safe_index drift" in r.message
        ]
        assert warning_records, "expected safe_index drift WARNING on wrong type"


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
