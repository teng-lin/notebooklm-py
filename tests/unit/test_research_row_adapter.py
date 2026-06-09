"""Tests for the ``POLL_RESEARCH`` row adapters (issue #1501).

These adapters centralise the positional knowledge ``_research_task_parser.py``
used to open-code as scattered single-level subscripts (``result[0]``,
``src[1]``, ``bundle[0]``, ``task_info[1][0]`` …). The tests cover three layers:

1. **Position-contract pins** — the canaries that fail loudly if a position
   constant is edited (the wire-shape change signal).
2. **Shape handling** — happy-path reads plus the permissive "absent / short /
   non-list → default" degrade that matches the historical wire parser.
3. **Drift** — the two GUARANTEED descents (``ResearchTaskRow.task_id_raw`` /
   ``task_info_raw``) RAISE ``UnknownRPCMethodError`` when their slot is absent.
"""

from __future__ import annotations

import pytest

from notebooklm._row_adapters.research import (
    ResearchResultRow,
    ResearchTaskInfoRow,
    ResearchTaskRow,
    unwrap_poll_tasks,
)
from notebooklm.exceptions import UnknownRPCMethodError

# ---------------------------------------------------------------------------
# 1. Position-contract pins (the canaries)
# ---------------------------------------------------------------------------


class TestResearchTaskRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (ResearchTaskRow._ID_POS, ResearchTaskRow._INFO_POS) == (0, 1)


class TestResearchTaskInfoRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            ResearchTaskInfoRow._QUERY_TEXT_POS,
            ResearchTaskInfoRow._SOURCES_POS,
            ResearchTaskInfoRow._SUMMARY_POS,
            ResearchTaskInfoRow._SUMMARY_MIN_LEN,
        ) == (0, 0, 1, 2)


class TestResearchResultRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            ResearchResultRow._URL_POS,
            ResearchResultRow._TITLE_POS,
            ResearchResultRow._RESULT_TYPE_POS,
            ResearchResultRow._LEGACY_CHUNKS_POS,
            ResearchResultRow._MIN_LEN,
        ) == (0, 1, 3, 6, 2)

    def test_deep_payload_positions_pinned(self) -> None:
        assert (
            ResearchResultRow._PAYLOAD_TITLE_POS,
            ResearchResultRow._PAYLOAD_REPORT_POS,
            ResearchResultRow._PAYLOAD_MIN_LEN,
        ) == (0, 1, 2)


# ---------------------------------------------------------------------------
# 2. ResearchTaskRow — GUARANTEED descents
# ---------------------------------------------------------------------------


class TestResearchTaskRow:
    def test_happy_path(self) -> None:
        row = ResearchTaskRow(["task_abc", ["info"]])
        assert row.task_id_raw == "task_abc"
        assert row.task_info_raw == ["info"]

    def test_missing_id_slot_raises(self) -> None:
        with pytest.raises(UnknownRPCMethodError):
            _ = ResearchTaskRow([]).task_id_raw

    def test_non_list_input_id_raises(self) -> None:
        with pytest.raises(UnknownRPCMethodError):
            _ = ResearchTaskRow(None).task_id_raw

    def test_missing_info_slot_raises(self) -> None:
        with pytest.raises(UnknownRPCMethodError):
            _ = ResearchTaskRow(["only_id"]).task_info_raw

    def test_wrong_type_id_is_returned_verbatim(self) -> None:
        # The adapter only *reads* the slot; the parser validates the type.
        assert ResearchTaskRow([42, ["info"]]).task_id_raw == 42

    def test_wrong_type_info_is_returned_verbatim(self) -> None:
        assert ResearchTaskRow(["id", "not_a_list"]).task_info_raw == "not_a_list"


# ---------------------------------------------------------------------------
# 3. ResearchTaskInfoRow — routinely-optional inner reads (soft)
# ---------------------------------------------------------------------------


class TestResearchTaskInfoRow:
    def test_query_text_happy_path(self) -> None:
        assert ResearchTaskInfoRow.query_text(["quantum computing", "extra"]) == "quantum computing"

    def test_query_text_empty_returns_none(self) -> None:
        assert ResearchTaskInfoRow.query_text([]) is None

    def test_bundle_sources_returns_first(self) -> None:
        assert ResearchTaskInfoRow.bundle_sources([["a", "b"], "summary"]) == ["a", "b"]

    def test_bundle_sources_empty_bundle_soft_degrades(self) -> None:
        # Regression (#1502 review): an empty task_info[3] must hit the
        # missing-slot default like every other soft read in this adapter —
        # not raise IndexError. The parser's caller coerces None to [].
        assert ResearchTaskInfoRow.bundle_sources([]) is None

    def test_bundle_summary_present(self) -> None:
        assert ResearchTaskInfoRow.bundle_summary([["a"], "Summary text"]) == "Summary text"

    def test_bundle_summary_absent_returns_none(self) -> None:
        assert ResearchTaskInfoRow.bundle_summary([["a"]]) is None


# ---------------------------------------------------------------------------
# 4. ResearchResultRow — routinely-optional source-row reads (soft)
# ---------------------------------------------------------------------------


class TestResearchResultRow:
    def test_fast_research_shape(self) -> None:
        row = ResearchResultRow(["https://example.com", "Example", "desc", "web"])
        assert row.is_well_formed is True
        assert row.length == 4
        assert row.url_slot == "https://example.com"
        assert row.title_slot == "Example"
        assert row.has_result_type is True
        assert row.result_type_slot == "web"

    def test_short_row_not_well_formed(self) -> None:
        row = ResearchResultRow(["only_one"])
        assert row.is_well_formed is False

    def test_non_list_not_well_formed(self) -> None:
        row = ResearchResultRow(None)
        assert row.is_well_formed is False
        assert row.length == 0
        assert row.url_slot is None
        assert row.title_slot is None
        assert row.result_type_slot is None
        assert row.has_result_type is False
        assert row.legacy_report_chunks == []

    def test_result_type_absent_short_circuits(self) -> None:
        row = ResearchResultRow([None, "title"])
        assert row.has_result_type is False
        assert row.result_type_slot is None

    def test_deep_research_sentinel_url(self) -> None:
        row = ResearchResultRow([None, ["Deep Report", "# Report"], None, 1])
        assert row.url_slot is None
        assert row.title_slot == ["Deep Report", "# Report"]

    def test_legacy_chunks_present(self) -> None:
        row = ResearchResultRow([None, "Legacy", None, "report", None, None, ["a", "b"]])
        assert row.legacy_report_chunks == ["a", "b"]

    def test_legacy_chunks_non_list_returns_empty(self) -> None:
        row = ResearchResultRow([None, "t", None, 5, None, None, "str"])
        assert row.legacy_report_chunks == []

    def test_legacy_chunks_absent_returns_empty(self) -> None:
        row = ResearchResultRow([None, "t", None, 5, None, None])
        assert row.legacy_report_chunks == []


class TestResearchResultRowDeepPayload:
    def test_two_strings_unpacked(self) -> None:
        assert ResearchResultRow.deep_payload(["Title", "# Report"]) == ("Title", "# Report")

    def test_bare_string_is_not_payload(self) -> None:
        assert ResearchResultRow.deep_payload("Title") is None

    def test_non_string_elements_rejected(self) -> None:
        assert ResearchResultRow.deep_payload([123, "# Report"]) is None
        assert ResearchResultRow.deep_payload(["Title", 456]) is None

    def test_too_short_rejected(self) -> None:
        assert ResearchResultRow.deep_payload(["Title"]) is None

    def test_non_list_rejected(self) -> None:
        assert ResearchResultRow.deep_payload(None) is None


# ---------------------------------------------------------------------------
# 5. unwrap_poll_tasks — envelope probe (soft)
# ---------------------------------------------------------------------------


class TestUnwrapPollTasks:
    def test_empty_and_non_list_return_empty(self) -> None:
        assert unwrap_poll_tasks(None) == []
        assert unwrap_poll_tasks([]) == []
        assert unwrap_poll_tasks("not rows") == []

    def test_wrapped_envelope_is_unwrapped(self) -> None:
        tasks = [["task_1", ["info"]]]
        assert unwrap_poll_tasks([tasks]) == tasks

    def test_flat_list_is_returned_unchanged(self) -> None:
        flat = [["task_1", ["info"]]]
        # A flat list whose first element's first element is itself a list is
        # treated as wrapped; a flat list of task rows (first element's first is
        # a str id) is returned unchanged.
        assert unwrap_poll_tasks(flat) == flat
