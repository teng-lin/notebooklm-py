"""Tests for POLL_RESEARCH task parsing."""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from notebooklm._web.rows.research_task import (
    ResearchSource,
    ResearchTask,
    _extract_query_text,
    _extract_source_type,
    _extract_sources_and_summary,
    _extract_status_code,
    _extract_task_id,
    _extract_task_info,
    _status_from_code,
    extract_report_markdown,
    parse_research_task_models,
    parse_research_tasks,
    parse_result_type,
    termination_reason_from_code,
)
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc.types import DiscoveryMode
from notebooklm.types import ResearchStatus


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


class TestExtractReportMarkdown:
    """Tests for typed deep-research content-block extraction."""

    def test_missing_index_6(self):
        assert extract_report_markdown([None, "t", None, 5, None, None]) == ""

    def test_index_6_not_list(self):
        assert extract_report_markdown([None, "t", None, 5, None, None, "str"]) == ""

    def test_short_content_block(self):
        assert extract_report_markdown([None, "t", None, 5, None, None, ["# Report"]]) == ""

    def test_current_captured_report_content_block(self):
        src = [None, "t", None, 5, None, None, ["# Report", 3, None, None, None, []]]
        assert extract_report_markdown(src) == "# Report"

    @pytest.mark.parametrize("kind", [1, 2, 3.0, True])
    def test_non_report_content_kinds_are_not_reports(self, kind):
        src = [None, "t", None, 5, None, None, ["# Fake", kind, "web snippet", None, 13]]
        assert extract_report_markdown(src) == ""

    @pytest.mark.parametrize("text", [None, 42, ""])
    def test_report_content_requires_non_empty_text(self, text):
        src = [None, "t", None, 5, None, None, [text, 3]]
        assert extract_report_markdown(src) == ""


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
                        # src[2] is DiscoveredSource.hint, which this fixture
                        # has always carried as "desc" and the parser dropped
                        # until #2122.
                        "hint": "desc",
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

    def test_parse_preserves_raw_status_code(self):
        """The raw ``task_info[4]`` code is kept on the model verbatim (#1922, F10),
        even when the coarse ``status`` enum flattens it (an unknown code → FAILED)."""
        # An unknown terminal code coarsens to FAILED but the raw int survives.
        task_info = [None, ["query"], None, [[], None], 99]
        tasks = parse_research_task_models([[["task_fail", task_info]]])
        assert tasks[0].status == "failed"
        assert tasks[0].status_code == 99

        # A completed run keeps its raw code too.
        task_info_ok = [None, ["query"], None, [[], None], 2]
        ok = parse_research_task_models([[["task_ok", task_info_ok]]])
        assert ok[0].status == "completed"
        assert ok[0].status_code == 2

    def test_parse_status_code_none_when_non_int(self):
        """A non-int ``task_info[4]`` yields ``status_code=None`` (→ in_progress)."""
        task_info = [None, ["query"], None, [[], None], "weird"]
        tasks = parse_research_task_models([[["task_x", task_info]]])
        assert tasks[0].status == "in_progress"
        assert tasks[0].status_code is None

    def test_research_source_public_dict_preserves_unknown_result_type(self):
        source = ResearchSource(
            url="https://example.com/video",
            title="Video",
            result_type="video",
            research_task_id="task_123",
            source_ordinal=9,
        )

        assert source.to_public_dict() == {
            "url": "https://example.com/video",
            "title": "Video",
            "result_type": "video",
            "research_task_id": "task_123",
            "source_ordinal": 9,
        }

        assert ResearchSource.from_public_dict(source.to_public_dict()) == source

    def test_source_ordinals_stay_attached_across_reordered_duplicate_urls(self):
        sources = [
            ["https://same.example", "Second", None, 1, None, None, [None, 1], None, 2],
            ["https://same.example", "First", None, 1, None, None, [None, 1], None, 1],
            [None, "Deep Report", None, 5, None, None, ["# Report", 3]],
        ]
        task_info = [None, ["deep query"], None, [sources], 2]

        task = parse_research_task_models([[["task_deep", task_info]]])[0]

        assert [(source.title, source.source_ordinal) for source in task.sources] == [
            ("Second", 2),
            ("First", 1),
            ("Deep Report", None),
        ]

    def test_current_deep_research_report_source(self):
        sources = [[None, ["Deep Report", "# Report"], None, 1]]
        task_info = [None, ["deep query"], None, [sources], 2]

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

    def test_captured_deep_research_report_source(self):
        sources = [
            [
                None,
                "Deep Report",
                None,
                5,
                None,
                None,
                ["# Report markdown", 3, None, None, None, ["structured doc"]],
            ]
        ]
        task_info = [None, ["deep query"], None, [sources], 2]

        tasks = parse_research_tasks([[["task_deep", task_info]]])

        assert tasks[0]["report"] == "# Report markdown"
        assert tasks[0]["sources"][0]["report_markdown"] == "# Report markdown"

    def test_web_rows_before_report_do_not_win(self):
        sources = [
            ["https://one.example", "One", "desc", 1, None, None, [None, 1, "snippet"]],
            ["https://two.example", "Two", "desc", 1, None, None, [None, 2, "snippet"]],
            ["https://float.example", "Float", "desc", 1, None, None, ["# Fake", 3.0]],
            ["https://bool.example", "Bool", "desc", 1, None, None, ["# Fake", True]],
            [None, "Deep Report", None, 5, None, None, ["# Actual report", 3]],
        ]
        task_info = [None, ["deep query"], None, [sources], 2]

        tasks = parse_research_tasks([[["task_reordered", task_info]]])

        assert tasks[0]["report"] == "# Actual report"
        assert all("report_markdown" not in source for source in tasks[0]["sources"][:-1])
        assert tasks[0]["sources"][-1]["report_markdown"] == "# Actual report"

    def test_web_rows_without_report_do_not_fabricate_one(self):
        sources = [
            ["https://one.example", "One", "desc", 1, None, None, [None, 1, "snippet"]],
            ["https://two.example", "Two", "desc", 1, None, None, [None, 2, "snippet"]],
        ]
        task_info = [None, ["deep query"], None, [sources], 2]

        task = parse_research_tasks([[["task_no_report", task_info]]])[0]

        assert task["report"] == ""
        assert all("report_markdown" not in source for source in task["sources"])


class TestExtractSourceType:
    """Tests for ``_extract_source_type`` (issue #1964).

    The tag lives at ``task_info[1][1]`` and is purely advisory (it selects the
    remediation hint), so every malformed shape degrades to ``None`` rather
    than failing the parse.
    """

    def test_web_tag(self):
        assert _extract_source_type([None, ["query", 1], None, [], 2]) == 1

    def test_drive_tag(self):
        assert _extract_source_type([None, ["query", 2], None, [], 2]) == 2

    def test_none_when_query_block_has_no_tag(self):
        assert _extract_source_type([None, ["query"], None, [], 2]) is None

    def test_none_when_query_block_empty(self):
        assert _extract_source_type([None, [], None, [], 2]) is None

    def test_none_when_query_block_not_a_list(self):
        assert _extract_source_type([None, "query", None, [], 2]) is None

    def test_none_when_tag_is_bool(self, caplog):
        """``bool`` is an ``int`` subclass — reject it so a drifted flag slot
        cannot masquerade as the web (1) tag."""
        with caplog.at_level(logging.WARNING):
            assert _extract_source_type([None, ["query", True], None, [], 2]) is None
        assert "task_info[1][1] is not an int source tag" in caplog.text

    def test_none_when_tag_is_non_int(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert _extract_source_type([None, ["query", "drive"], None, [], 2]) is None
        assert "task_info[1][1] is not an int source tag" in caplog.text

    def test_absent_tag_is_not_warned(self, caplog):
        """A query block that simply omits the tag is normal, not drift — it
        must degrade silently rather than logging a warning on every poll."""
        with caplog.at_level(logging.WARNING):
            assert _extract_source_type([None, ["query"], None, [], 2]) is None
        assert caplog.text == ""

    def test_absent_query_block_raises_like_query_text(self):
        """``task_info[1]`` is a guaranteed descent, so its absence is drift and
        raises — the same contract ``_extract_query_text`` has (only the TAG
        inside the block is advisory)."""
        with pytest.raises(UnknownRPCMethodError):
            _extract_source_type([None])


class TestTerminationReason:
    """End-to-end reason/message/hint derivation from live-captured wire rows.

    Every row below is the shape actually observed against the serving backend
    while investigating issue #1964 (see docs/rpc-reference.md).
    """

    # Verbatim from the live capture: an empty Drive search carries code 3 and a
    # ``[None, None, None, None, 1]`` sources bundle.
    DRIVE_NO_MATCH = [None, ["no-such-file-9f3a2b7c", 2], 1, [None, None, None, None, 1], 3]

    def test_drive_no_match_is_no_results_not_generic_failure(self):
        tasks = parse_research_task_models([[["task_a", self.DRIVE_NO_MATCH]]])
        task = tasks[0]
        # The coarse status is unchanged (no behavior break for existing callers)...
        assert task.status == "failed"
        assert task.sources == ()
        # ...but the reason now differentiates it.
        assert task.status_code == 3
        assert task.termination_reason == "no_results"
        assert task.is_drive_search is True

    def test_drive_no_match_carries_message_and_drive_specific_hint(self):
        task = parse_research_task_models([[["task_a", self.DRIVE_NO_MATCH]]])[0]
        # Asserted in FULL, not by substring: a substring match still passes on
        # a message that has become ungrammatical, double-quotes the query, or
        # drops the corpus name.
        assert task.reason_message == (
            "The search of Google Drive found no matches for 'no-such-file-9f3a2b7c'."
        )
        assert task.hint == (
            "Try the exact Drive filename, the document URL, or add the "
            "document directly by its document id."
        )

    def test_web_no_results_gets_query_advice_not_drive_advice(self):
        """The hint is source-specific: 'add it by document id' is meaningless
        for a web search."""
        task_info = [None, ["some query", 1], 1, [None], 3]
        task = parse_research_task_models([[["task_w", task_info]]])[0]
        assert task.termination_reason == "no_results"
        assert task.is_drive_search is False
        assert task.hint == "Try a broader or differently-worded query."
        assert "the web" in task.reason_message

    def test_cancelled_run_is_distinguished_from_no_results(self):
        """A cancelled deep run reports code 4, a distinct code from the 3 seen
        on an empty Drive search — the observation that separates the two."""
        task_info = [None, ["battery electrolytes", 1], 1, None, 4]
        task = parse_research_task_models([[["task_c", task_info]]])[0]
        assert task.status == "failed"
        assert task.termination_reason == "cancelled"
        assert "cancelled" in task.reason_message
        assert task.hint == "Start a new research run if you still need these sources."

    def test_completed_run_has_no_message_or_hint(self):
        task_info = [None, ["notes", 2], 1, [[["u", "t", "d", 2]], "summary"], 2]
        task = parse_research_task_models([[["task_ok", task_info]]])[0]
        assert task.termination_reason == "completed"
        assert task.reason_message is None
        assert task.hint is None

    def test_in_progress_run_has_no_message_or_hint(self):
        task_info = [None, ["notes", 2], 1, None, 1]
        task = parse_research_task_models([[["task_p", task_info]]])[0]
        assert task.termination_reason == "in_progress"
        assert task.reason_message is None
        assert task.hint is None

    def test_unrecognised_terminal_code_is_unknown_not_guessed(self):
        """An uncaptured code must never be silently folded into a named reason
        — these are undocumented, volatile Google internals."""
        task_info = [None, ["query", 2], 1, None, 99]
        task = parse_research_task_models([[["task_u", task_info]]])[0]
        assert task.status == "failed"
        assert task.termination_reason == "unknown"
        assert "99" in task.reason_message
        # Must not assert the run ENDED — an unrecognised code could be a
        # future non-terminal state; report only what was observed.
        assert "reported an unrecognised" in task.reason_message
        assert task.hint is not None

    def test_missing_status_code_yields_no_reason(self):
        """No code at all (empty/not-found placeholder) is not a termination
        reason of ``unknown`` — there is simply nothing to report."""
        task_info = [None, ["query", 2], 1, None, "weird"]
        task = parse_research_task_models([[["task_n", task_info]]])[0]
        assert task.status_code is None
        assert task.termination_reason is None
        assert task.reason_message is None
        assert task.hint is None

    def test_missing_source_tag_falls_back_to_source_agnostic_wording(self):
        """A drifted/absent source tag must not suppress the explanation, and
        must NOT be narrated as a web search — we simply do not know which
        corpus was searched."""
        task_info = [None, ["query"], 1, None, 3]
        task = parse_research_task_models([[["task_d", task_info]]])[0]
        assert task.source_type is None
        assert task.is_drive_search is False
        assert task.is_web_search is False
        assert task.termination_reason == "no_results"
        assert task.reason_message == "The search found no matches for 'query'."
        assert "the web" not in task.reason_message
        assert "Google Drive" not in task.reason_message
        # With the source unknown, BOTH remediations are offered — emitting
        # only the web advice would strand a Drive run whose tag drifted.
        assert task.hint == (
            "Try a broader or differently-worded query; if this searched Drive, "
            "try the exact filename, the document URL, or the document id."
        )

    @pytest.mark.parametrize("tag", [0, 5, 99, -1])
    def test_unrecognised_source_tag_is_not_narrated_as_web(self, tag):
        """A DRIFTED tag (not merely an absent one) must fail both predicates.

        Before the tri-state fix, ``source_type=5`` parsed as 5, left
        ``is_drive_search`` False, and was narrated as "the web" — the same
        wrong output as the absent-tag path, reached by a different input.
        """
        task_info = [None, ["query", tag], 1, None, 3]
        task = parse_research_task_models([[["task_e", task_info]]])[0]
        assert task.source_type == tag
        assert task.is_drive_search is False
        assert task.is_web_search is False
        assert "the web" not in task.reason_message
        assert "Google Drive" not in task.reason_message
        # And the remediation must not be the web-only one.
        assert "if this searched Drive" in task.hint

    def test_no_results_code_with_sources_degrades_to_unknown(self):
        """Defensive: three live captures are evidence, not proof. If code 3
        ever arrives WITH sources, reporting "found no matches" beside those
        matches would be self-contradictory — fall back to ``unknown``."""
        sources = [["https://example.com", "A result", "desc", 2]]
        task_info = [None, ["query", 2], 1, [sources, "summary"], 3]
        task = parse_research_task_models([[["task_f", task_info]]])[0]
        assert len(task.sources) == 1
        assert task.termination_reason == "unknown"
        assert "no matches" not in task.reason_message

    def test_placeholder_tasks_have_no_reason(self):
        assert ResearchTask.empty().termination_reason is None
        assert ResearchTask.not_found("abc").termination_reason is None
        assert ResearchTask.not_found("abc").reason_message is None

    def test_public_dict_shape_is_unchanged(self):
        """The new fields are attribute-only — the CLI ``--json`` payload must
        stay byte-stable (same guarantee ``status_code`` was given in #1922)."""
        task = parse_research_task_models([[["task_a", self.DRIVE_NO_MATCH]]])[0]
        public = task.to_public_dict()
        assert set(public) == {
            "task_id",
            "status",
            "query",
            "sources",
            "summary",
            "report",
            "tasks",
        }


class TestStatusCodeTableIsSingleSourced:
    """The lifecycle status and the termination reason must never disagree.

    They were two independent hand-written tables (#1964 review): adding a
    future code to only one could emit ``status="completed"`` alongside
    ``termination_reason="unknown"`` and a "retry the run" hint on a run that
    actually succeeded. ``_status_from_code`` now derives from the reason
    table; these tests pin both the derivation and each individual code, so a
    dropped table entry cannot slip through (the code-6 entry previously had
    NO test at all and could be deleted with the whole suite still green).
    """

    @pytest.mark.parametrize(
        ("code", "expected_status", "expected_reason"),
        [
            (1, "in_progress", "in_progress"),
            (2, "completed", "completed"),
            (3, "failed", "no_results"),
            (4, "failed", "cancelled"),
            # Forward-compat only: ``2`` is the observed completion code (every
            # completed run across the POLL cassettes, deep included), and ``6``
            # appears in no capture. Pinned explicitly so the entry cannot be
            # deleted as dead — were it to start appearing, dropping the mapping
            # would report "unrecognised backend status code (6)" on a run that
            # still says ``completed``.
            (6, "completed", "completed"),
            (0, "failed", "unknown"),
            (5, "failed", "unknown"),
            (7, "failed", "unknown"),
            (99, "failed", "unknown"),
        ],
    )
    def test_each_code_maps_to_both_tables(self, code, expected_status, expected_reason):
        task_info = [None, ["query", 1], 1, None, code]
        task = parse_research_task_models([[["task_x", task_info]]])[0]
        assert task.status == expected_status
        assert task.termination_reason == expected_reason

    def test_absent_code_is_in_progress_with_no_reason(self):
        task_info = [None, ["query", 1], 1, None, "not-an-int"]
        task = parse_research_task_models([[["task_x", task_info]]])[0]
        assert task.status == "in_progress"
        assert task.termination_reason is None

    @pytest.mark.parametrize("code", [None, 0, 1, 2, 3, 4, 5, 6, 7, 42, -1, 99])
    def test_status_never_contradicts_reason(self, code):
        """A ``completed`` status must never carry a non-success reason, and a
        successful reason must never surface as ``failed``."""
        reason = termination_reason_from_code(code)
        status = _status_from_code(code)
        if status == "completed":
            assert reason == "completed", f"code {code}: completed status, reason {reason}"
        if reason == "completed":
            assert status == "completed", f"code {code}: completed reason, status {status}"
        # A run we cannot name must never be reported as a success.
        if reason == "unknown":
            assert status == "failed"

    def test_unobserved_completion_code_6_produces_no_misleading_explanation(self):
        """Code ``6`` coarsens to completed without acquiring a retry hint.

        ``6`` has never appeared in a capture (0 of 9 task rows across the POLL
        cassettes; every completed run, deep included, reports ``2``), so this
        is a forward-compat mapping, NOT deep research's completion code — see
        the status-code table in ``docs/rpc-reference.md``. The mapping is worth
        keeping precisely because the failure mode it prevents is a successful
        run being told to retry itself.
        """
        sources = [[None, ["Deep Report", "# Report"], None, 1]]
        task_info = [None, ["deep query", 1], 1, [sources], 6]
        task = parse_research_task_models([[["task_deep", task_info]]])[0]
        assert task.status == "completed"
        assert task.termination_reason == "completed"
        assert task.reason_message is None
        assert task.hint is None


#: The same live ``POLL_RESEARCH`` capture the row-adapter tests use (#2122):
#: one fast web run polled while in flight and again once it settled.
_CAPTURED_POLLS = json.loads(
    (Path(__file__).parent / "fixtures" / "research_poll_task_metadata.json").read_text(
        encoding="utf-8"
    )
)["polls"]


class TestTaskMetadataFromCapture:
    """The #2122 metadata, end to end from a real poll payload."""

    def _task(self, poll_index: int) -> ResearchTask:
        tasks = parse_research_task_models(_CAPTURED_POLLS[poll_index])
        assert len(tasks) == 1
        return tasks[0]

    def test_settled_task_carries_every_new_field(self) -> None:
        task = self._task(2)
        assert task.discovery_mode is DiscoveryMode.DEFAULT_LLM_SEARCH
        assert task.account_id == "400237754469"
        assert task.created_at == datetime.fromtimestamp(1786619578, tz=timezone.utc)
        assert task.updated_at == datetime.fromtimestamp(1786619585, tz=timezone.utc)
        assert task.status == "completed"

    def test_duration_is_the_backend_measured_run_time(self) -> None:
        assert self._task(2).duration == timedelta(seconds=7)

    def test_in_flight_task_has_not_advanced_yet(self) -> None:
        task = self._task(1)
        assert task.created_at == task.updated_at
        assert task.duration == timedelta(0)
        assert task.status == "in_progress"

    def test_created_at_is_stable_across_polls_and_updated_at_is_not(self) -> None:
        in_flight, settled = self._task(1), self._task(2)
        assert in_flight.created_at == settled.created_at
        assert in_flight.updated_at < settled.updated_at

    def test_every_captured_source_carries_its_hint(self) -> None:
        sources = self._task(2).sources
        assert len(sources) == 10
        assert all(src.hint for src in sources)
        assert sources[0].hint == (
            "Comprehensive introduction to the event loop's role as a conductor."
        )

    def test_hint_reaches_the_public_source_dict(self) -> None:
        """``to_public_dict`` is what the CLI ``--json``, the MCP tool and the
        REST route all emit for a source, so the hint has to arrive there."""
        public = self._task(2).sources[0].to_public_dict()
        assert public["hint"] == (
            "Comprehensive introduction to the event loop's role as a conductor."
        )

    def test_empty_poll_yields_no_tasks(self) -> None:
        assert parse_research_task_models(_CAPTURED_POLLS[0]) == []


class TestTaskMetadataAbsence:
    """Most of these slots are optional in the type; absence must stay soft."""

    def test_a_row_without_metadata_slots_parses_with_none(self) -> None:
        task_info = [None, ["query", 1], None, [[], "Summary"], 2]
        task = parse_research_task_models([[["task_bare", task_info]]])[0]
        assert task.discovery_mode is None
        assert task.created_at is None
        assert task.updated_at is None
        assert task.account_id is None
        assert task.duration is None

    def test_one_missing_timestamp_leaves_duration_unreportable(self) -> None:
        task_info = [None, ["query", 1], 1, [[], "Summary"], 2]
        row = ["task_half", task_info, [1786619585, 0]]
        task = parse_research_task_models([[row]])[0]
        assert task.updated_at is not None
        assert task.created_at is None
        assert task.duration is None

    def test_public_dict_shape_is_unchanged_by_the_task_metadata(self) -> None:
        """The task-level fields stay OFF the byte-stable CLI ``--json``
        payload, matching ``status_code`` / ``source_type``."""
        task = parse_research_task_models(_CAPTURED_POLLS[2])[0]
        assert set(task.to_public_dict()) == {
            "task_id",
            "status",
            "query",
            "sources",
            "summary",
            "report",
            "tasks",
        }


class TestDurationRefusesAnInvertedInterval:
    """A negative duration means the two timestamp slots were read the wrong
    way round — this decode's most fragile claim. It must not reach a caller.
    """

    def _task(self, created, updated) -> ResearchTask:
        return ResearchTask(
            task_id="task_inverted",
            status=ResearchStatus.COMPLETED,
            created_at=created,
            updated_at=updated,
        )

    def test_inverted_interval_reports_none_and_warns(self, caplog) -> None:
        task = self._task(
            datetime.fromtimestamp(1786619585, tz=timezone.utc),
            datetime.fromtimestamp(1786619578, tz=timezone.utc),
        )
        with caplog.at_level(logging.WARNING, logger="notebooklm._types.research"):
            assert task.duration is None
        assert any("BEFORE created_at" in r.message for r in caplog.records)

    def test_zero_interval_is_a_real_duration_not_an_inversion(self) -> None:
        """A just-started run reports both slots equal — that is 0s, not drift."""
        instant = datetime.fromtimestamp(1786619578, tz=timezone.utc)
        assert self._task(instant, instant).duration == timedelta(0)

    def test_the_app_projection_refuses_it_too(self, caplog) -> None:
        """``ResearchStatusResult.duration_seconds`` re-derives from the ISO
        strings, so the guard has to hold there independently — AND warn.

        On the MCP/REST path ``ResearchTask.duration`` is never called (this
        projection is built from the timestamps directly), so if this did not
        warn, the one surface an agent consumes the value from would be the
        surface with no diagnostic.
        """
        from notebooklm._app.research import ResearchStatusResult

        inverted = ResearchStatusResult(
            kind="completed",
            status="completed",
            query="q",
            sources=[],
            summary="",
            report="",
            public_dict={},
            created_at="2026-08-13T12:11:16+00:00",
            updated_at="2026-08-13T12:11:10+00:00",
        )
        with caplog.at_level(logging.WARNING, logger="notebooklm._app.research"):
            assert inverted.duration_seconds is None
        assert any("BEFORE created_at" in r.message for r in caplog.records)

        forward = ResearchStatusResult(
            kind="completed",
            status="completed",
            query="q",
            sources=[],
            summary="",
            report="",
            public_dict={},
            created_at="2026-08-13T12:11:10+00:00",
            updated_at="2026-08-13T12:11:16+00:00",
        )
        assert forward.duration_seconds == 6.0
