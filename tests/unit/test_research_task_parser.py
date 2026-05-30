"""Tests for POLL_RESEARCH task parsing."""

import logging

import pytest

from notebooklm._research_task_parser import (
    ResearchSource,
    ResearchTask,
    _extract_query_text,
    _extract_sources_and_summary,
    _extract_status_code,
    _extract_task_id,
    _extract_task_info,
    extract_legacy_report_chunks,
    parse_research_task_models,
    parse_research_tasks,
    parse_result_type,
)
from notebooklm.exceptions import UnknownRPCMethodError


class TestParseResultType:
    """Tests for research result type normalization."""

    def test_int_passthrough(self):
        assert parse_result_type(5) == 5

    def test_known_string_alias(self):
        assert parse_result_type("web") == 1
        assert parse_result_type("drive") == 2
        assert parse_result_type("report") == 5

    def test_case_insensitive(self):
        assert parse_result_type("WEB") == 1
        assert parse_result_type("Drive") == 2

    def test_unknown_string_preserved(self):
        assert parse_result_type("video") == "video"

    def test_none_defaults_to_1(self):
        assert parse_result_type(None) == 1

    def test_float_defaults_to_1(self):
        assert parse_result_type(3.14) == 1

    def test_list_defaults_to_1(self):
        assert parse_result_type([]) == 1


class TestExtractLegacyReportChunks:
    """Tests for legacy deep-research report chunk extraction."""

    def test_missing_index_6(self):
        assert extract_legacy_report_chunks([None, "t", None, 5, None, None]) == ""

    def test_index_6_not_list(self):
        assert extract_legacy_report_chunks([None, "t", None, 5, None, None, "str"]) == ""

    def test_single_chunk(self):
        assert extract_legacy_report_chunks([None, "t", None, 5, None, None, ["chunk"]]) == (
            "chunk"
        )

    def test_multiple_chunks_joined(self):
        src = [None, "t", None, 5, None, None, ["a", "b", "c"]]
        assert extract_legacy_report_chunks(src) == "a\n\nb\n\nc"

    def test_filters_non_string_and_empty(self):
        src = [None, "t", None, 5, None, None, ["real", None, "", 42, "also_real"]]
        assert extract_legacy_report_chunks(src) == "real\n\nalso_real"

    def test_all_empty_returns_empty(self):
        assert extract_legacy_report_chunks([None, "t", None, 5, None, None, ["", None]]) == ""


class TestExtractTaskId:
    """Tests for ``_extract_task_id`` helper."""

    def test_happy_path(self):
        assert _extract_task_id(["task_abc", ["info"]]) == "task_abc"

    def test_empty_list_drift_raises(self):
        with pytest.raises(UnknownRPCMethodError):
            _extract_task_id([])

    def test_non_string_id_drift_returns_none(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert _extract_task_id([42, ["info"]]) is None
        assert "task_data[0] is not a string" in caplog.text

    def test_non_list_input_raises(self):
        with pytest.raises(UnknownRPCMethodError):
            _extract_task_id(None)


class TestExtractTaskInfo:
    """Tests for ``_extract_task_info`` helper."""

    def test_happy_path(self):
        info = [None, ["q"], None, [[]], 2]
        assert _extract_task_info(["task_id", info]) is info

    def test_missing_index_raises(self):
        with pytest.raises(UnknownRPCMethodError):
            _extract_task_info(["only_id"])

    def test_non_list_value_returns_none(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert _extract_task_info(["task_id", "not_a_list"]) is None
        assert "task_data[1] is not a list" in caplog.text


class TestExtractQueryText:
    """Tests for ``_extract_query_text`` helper."""

    def test_happy_path(self):
        task_info = [None, ["quantum computing", "extra"], None, [], 1]
        assert _extract_query_text(task_info) == "quantum computing"

    def test_string_query_container_returns_none(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert _extract_query_text([None, "query", None, [], 1]) is None
        assert "task_info[1] is not a list" in caplog.text

    def test_missing_query_info_raises(self):
        with pytest.raises(UnknownRPCMethodError):
            _extract_query_text([None])

    def test_non_string_query_returns_none(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert _extract_query_text([None, [123], None, [], 1]) is None
        assert "task_info[1][0] is not a string" in caplog.text


class TestExtractStatusCode:
    """Tests for ``_extract_status_code`` helper."""

    def test_happy_path_in_progress(self):
        assert _extract_status_code([None, ["q"], None, [], 1]) == 1

    def test_happy_path_completed(self):
        assert _extract_status_code([None, ["q"], None, [], 2]) == 2

    def test_happy_path_deep_completed(self):
        assert _extract_status_code([None, ["q"], None, [], 6]) == 6

    def test_missing_index_raises(self):
        with pytest.raises(UnknownRPCMethodError):
            _extract_status_code([None, ["q"], None, []])

    def test_non_int_returns_none(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert _extract_status_code([None, ["q"], None, [], "completed"]) is None
        assert "task_info[4] is not an int" in caplog.text

    def test_bool_rejected(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert _extract_status_code([None, ["q"], None, [], True]) is None
        assert "task_info[4] is bool" in caplog.text


class TestExtractSourcesAndSummary:
    """Tests for ``_extract_sources_and_summary`` helper."""

    def test_happy_path_with_summary(self):
        task_info = [
            None,
            ["q"],
            None,
            [[["https://example.com", "Example"]], "Summary text"],
            2,
        ]
        sources, summary = _extract_sources_and_summary(task_info)
        assert sources == [["https://example.com", "Example"]]
        assert summary == "Summary text"

    def test_happy_path_sources_only(self):
        task_info = [None, ["q"], None, [[["url", "title"]]], 2]
        sources, summary = _extract_sources_and_summary(task_info)
        assert sources == [["url", "title"]]
        assert summary is None

    def test_missing_bundle_raises(self):
        with pytest.raises(UnknownRPCMethodError):
            _extract_sources_and_summary([None, ["q"], None])

    def test_empty_bundle_returns_empty(self):
        sources, summary = _extract_sources_and_summary([None, ["q"], None, [], 2])
        assert sources == []
        assert summary is None

    def test_non_list_bundle_drift(self, caplog):
        with caplog.at_level(logging.WARNING):
            sources, summary = _extract_sources_and_summary([None, ["q"], None, "drift", 2])
        assert sources == []
        assert summary is None
        assert "task_info[3] is not a list" in caplog.text

    def test_non_list_sources_slot_drift(self, caplog):
        with caplog.at_level(logging.WARNING):
            sources, summary = _extract_sources_and_summary(
                [None, ["q"], None, ["not_a_list", "Summary"], 2]
            )
        assert sources == []
        assert summary == "Summary"
        assert "task_info[3][0] is not a list" in caplog.text


class TestParseResearchTasks:
    """Tests for full task-row parsing."""

    def test_empty_or_malformed_payload_returns_empty(self):
        assert parse_research_tasks(None) == []
        assert parse_research_tasks([]) == []
        assert parse_research_tasks("not rows") == []

    def test_fast_research_source(self):
        sources = [["https://example.com", "Example", "desc", "web"]]
        task_info = [None, ["query"], None, [sources, "Summary"], 2]

        tasks = parse_research_tasks([[["task_123", task_info]]])

        assert tasks == [
            {
                "task_id": "task_123",
                "status": "completed",
                "query": "query",
                "sources": [
                    {
                        "url": "https://example.com",
                        "title": "Example",
                        "result_type": 1,
                        "research_task_id": "task_123",
                    }
                ],
                "summary": "Summary",
                "report": "",
            }
        ]

    def test_parse_research_task_models_returns_typed_models(self):
        sources = [["https://example.com", "Example", "desc", "web"]]
        task_info = [None, ["query"], None, [sources, "Summary"], 2]

        tasks = parse_research_task_models([[["task_123", task_info]]])

        assert len(tasks) == 1
        assert isinstance(tasks[0], ResearchTask)
        assert isinstance(tasks[0].sources[0], ResearchSource)
        assert tasks[0].task_id == "task_123"
        assert tasks[0].status == "completed"
        assert tasks[0].sources[0].result_type == 1
        # ``parse_research_tasks`` produces the per-task dict shape (no sibling
        # ``tasks`` key); the model's ``_to_task_dict`` is the matching builder.
        assert tasks[0]._to_task_dict() == parse_research_tasks([[["task_123", task_info]]])[0]

    def test_research_source_public_dict_preserves_unknown_result_type(self):
        source = ResearchSource(
            url="https://example.com/video",
            title="Video",
            result_type="video",
            research_task_id="task_123",
        )

        assert source.to_public_dict() == {
            "url": "https://example.com/video",
            "title": "Video",
            "result_type": "video",
            "research_task_id": "task_123",
        }

    def test_current_deep_research_report_source(self):
        sources = [[None, ["Deep Report", "# Report"], None, 1]]
        task_info = [None, ["deep query"], None, [sources], 6]

        tasks = parse_research_tasks([[["task_deep", task_info]]])

        assert tasks[0]["status"] == "completed"
        assert tasks[0]["report"] == "# Report"
        assert tasks[0]["sources"] == [
            {
                "url": "",
                "title": "Deep Report",
                "result_type": 5,
                "research_task_id": "task_deep",
                "report_markdown": "# Report",
            }
        ]

    def test_legacy_deep_research_report_source(self):
        sources = [[None, "Legacy Report", None, "report", None, None, ["a", "b"]]]
        task_info = [None, ["deep query"], None, [sources], 6]

        tasks = parse_research_tasks([[["task_legacy", task_info]]])

        assert tasks[0]["report"] == "a\n\nb"
        assert tasks[0]["sources"][0]["report_markdown"] == "a\n\nb"

    def test_legacy_report_chunks_after_current_report_are_not_attached(self):
        sources = [
            [None, ["Current Report", "# Current"], None, 1],
            [None, "Legacy Later", None, "report", None, None, ["legacy"]],
        ]
        task_info = [None, ["deep query"], None, [sources], 6]

        tasks = parse_research_tasks([[["task_mixed", task_info]]])

        assert tasks[0]["report"] == "# Current"
        assert tasks[0]["sources"][0]["report_markdown"] == "# Current"
        assert "report_markdown" not in tasks[0]["sources"][1]
