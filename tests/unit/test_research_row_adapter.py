"""Tests for the ``POLL_RESEARCH`` row adapters (issue #1501).

These adapters centralise the positional knowledge ``_web/rows/research_task.py``
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

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from notebooklm._web.rows import research as research_adapters
from notebooklm._web.rows.research import (
    ImportedSourceRow,
    ResearchResultRow,
    ResearchStartRow,
    ResearchTaskInfoRow,
    ResearchTaskRow,
    unwrap_import_rows,
    unwrap_poll_tasks,
)
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc.types import DiscoveryMode

#: Live ``POLL_RESEARCH`` capture (#2122): one fast web run polled three times
#: — before any task row existed, while in flight, and once it settled. The
#: create/update slots are asserted against these rather than a hand-built
#: pair, because which slot is which is exactly what a hand-built fixture
#: cannot establish.
_CAPTURED_POLLS: list = json.loads(
    (Path(__file__).parent / "fixtures" / "research_poll_task_metadata.json").read_text(
        encoding="utf-8"
    )
)["polls"]
#: The in-flight and settled task rows from that capture.
_IN_FLIGHT_ROW: list = _CAPTURED_POLLS[1][0][0]
_SETTLED_ROW: list = _CAPTURED_POLLS[2][0][0]

#: A second live capture (#2122): one poll carrying BOTH a settled deep task and
#: a settled fast one, so the two discovery modes are pinned against real
#: ``task_info`` rows from the same payload rather than a hand-built list.
_DEEP_POLL: list = json.loads(
    (Path(__file__).parent / "fixtures" / "research_poll_deep_task.json").read_text(
        encoding="utf-8"
    )
)["poll"]
_DEEP_TASK_INFO: list = next(task[1] for task in _DEEP_POLL[0] if task[1][2] == 5)
_FAST_TASK_INFO: list = next(task[1] for task in _DEEP_POLL[0] if task[1][2] == 1)

# ---------------------------------------------------------------------------
# 1. Position-contract pins (the canaries)
# ---------------------------------------------------------------------------


class TestResearchTaskRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (ResearchTaskRow._ID_POS, ResearchTaskRow._INFO_POS) == (0, 1)

    def test_metadata_positions_pinned(self) -> None:
        assert (
            ResearchTaskRow._UPDATED_AT_POS,
            ResearchTaskRow._CREATED_AT_POS,
            ResearchTaskRow._ACCOUNT_ID_POS,
            ResearchTaskRow._TS_SECONDS_POS,
        ) == (2, 3, 4, 0)


class TestResearchTaskInfoRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            ResearchTaskInfoRow._QUERY_TEXT_POS,
            ResearchTaskInfoRow._QUERY_SOURCE_TYPE_POS,
            ResearchTaskInfoRow._QUERY_SOURCE_TYPE_MIN_LEN,
            ResearchTaskInfoRow._SOURCES_POS,
            ResearchTaskInfoRow._SUMMARY_POS,
            ResearchTaskInfoRow._SUMMARY_MIN_LEN,
        ) == (0, 1, 2, 0, 1, 2)

    def test_discovery_mode_position_pinned(self) -> None:
        assert ResearchTaskInfoRow._DISCOVERY_MODE_POS == 2


class TestResearchResultRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            ResearchResultRow._URL_POS,
            ResearchResultRow._TITLE_POS,
            ResearchResultRow._HINT_POS,
            ResearchResultRow._RESULT_TYPE_POS,
            ResearchResultRow._CONTENT_BLOCK_POS,
            ResearchResultRow._SOURCE_ORDINAL_POS,
            ResearchResultRow._MIN_LEN,
        ) == (0, 1, 2, 3, 6, 8, 2)

    def test_content_block_positions_pinned(self) -> None:
        assert (
            ResearchResultRow._CONTENT_TEXT_POS,
            ResearchResultRow._CONTENT_KIND_POS,
            ResearchResultRow._CONTENT_MIN_LEN,
            ResearchResultRow._REPORT_CONTENT_KIND,
        ) == (0, 1, 2, 3)

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

    def test_query_source_type_reads_the_tag(self) -> None:
        # Live-captured shapes (#1964): drive echoes 2, web echoes 1.
        assert ResearchTaskInfoRow.query_source_type(["notes", 2]) == 2
        assert ResearchTaskInfoRow.query_source_type(["python asyncio", 1]) == 1

    def test_query_source_type_absent_soft_degrades(self) -> None:
        # A text-only query block legitimately omits the tag — a soft read like
        # every other optional slot in this adapter, never an IndexError.
        assert ResearchTaskInfoRow.query_source_type(["query only"]) is None
        assert ResearchTaskInfoRow.query_source_type([]) is None
        assert ResearchTaskInfoRow.query_source_type(None) is None

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
        assert row.source_ordinal is None

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
        assert row.content_block == []
        assert row.report_markdown == ""

    def test_result_type_absent_short_circuits(self) -> None:
        row = ResearchResultRow([None, "title"])
        assert row.has_result_type is False
        assert row.result_type_slot is None

    def test_deep_research_sentinel_url(self) -> None:
        row = ResearchResultRow([None, ["Deep Report", "# Report"], None, 1])
        assert row.url_slot is None
        assert row.title_slot == ["Deep Report", "# Report"]

    def test_report_content_block_present(self) -> None:
        block = ["# Report", 3, None, None, None, ["document"]]
        row = ResearchResultRow([None, "Report", None, 5, None, None, block])
        assert row.content_block is block
        assert row.report_markdown == "# Report"

    def test_content_block_non_list_returns_empty(self) -> None:
        row = ResearchResultRow([None, "t", None, 5, None, None, "str"])
        assert row.content_block == []
        assert row.report_markdown == ""

    def test_content_block_absent_returns_empty(self) -> None:
        row = ResearchResultRow([None, "t", None, 5, None, None])
        assert row.content_block == []
        assert row.report_markdown == ""

    def test_source_ordinal_reads_captured_ordinal(self) -> None:
        row = ResearchResultRow(
            ["https://example.com", "t", None, 1, None, None, [None, 1], None, 17]
        )
        assert row.source_ordinal == 17

    @pytest.mark.parametrize("value", [None, True, 3.0, "3", []])
    def test_source_ordinal_absent_or_non_integer_returns_none(self, value) -> None:
        row = ResearchResultRow(
            ["https://example.com", "t", None, 1, None, None, [None, 1], None, value]
        )
        assert row.source_ordinal is None

    @pytest.mark.parametrize("block", [[], ["# Report"], [None, 3], [42, 3]])
    def test_malformed_report_content_block_returns_empty(self, block) -> None:
        row = ResearchResultRow([None, "t", None, 5, None, None, block])
        assert row.report_markdown == ""

    @pytest.mark.parametrize("kind", [1, 2, 3.0, True])
    def test_non_report_content_kind_is_not_report(self, kind) -> None:
        row = ResearchResultRow(["https://example.com", "t", None, 1, None, None, ["# Fake", kind]])
        assert row.report_markdown == ""


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


# ---------------------------------------------------------------------------
# 6. ResearchStartRow — START_*_RESEARCH kickoff result
# ---------------------------------------------------------------------------


class TestResearchStartRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (ResearchStartRow._TASK_ID_POS, ResearchStartRow._REPORT_ID_POS) == (0, 1)


class TestResearchStartRow:
    def test_task_id_and_report_id(self) -> None:
        row = ResearchStartRow(["task_abc", "report_xyz"])
        assert row.task_id_raw == "task_abc"
        assert row.report_id == "report_xyz"

    def test_report_id_absent_returns_none(self) -> None:
        # A fast-research start legitimately omits the report id — soft default.
        row = ResearchStartRow(["task_only"])
        assert row.task_id_raw == "task_only"
        assert row.report_id is None

    def test_task_id_returned_verbatim(self) -> None:
        # The adapter only reads the slot; the caller validates truthiness.
        assert ResearchStartRow([None]).task_id_raw is None
        assert ResearchStartRow([""]).task_id_raw == ""

    def test_missing_task_id_slot_raises(self) -> None:
        # The caller guards a non-empty list before wrapping; an absent id slot
        # on a wrapped row is genuine drift and RAISES (strict ``safe_index``).
        with pytest.raises(UnknownRPCMethodError):
            _ = ResearchStartRow([]).task_id_raw


# ---------------------------------------------------------------------------
# 7. IMPORT_RESEARCH adapters — envelope probe + per-row reads (soft)
# ---------------------------------------------------------------------------


class TestUnwrapImportRows:
    def test_empty_and_non_list_return_empty(self) -> None:
        assert unwrap_import_rows(None) == []
        assert unwrap_import_rows([]) == []
        assert unwrap_import_rows("not rows") == []

    def test_wrapped_envelope_is_unwrapped(self) -> None:
        rows = [[["id_1"], "Title"]]
        assert unwrap_import_rows([rows]) == rows

    def test_single_wrapped_row_is_unwrapped(self) -> None:
        # Matches ``research_import_sources_direct.yaml``: the recorded single
        # imported row still arrives under a one-element ``[[row]]`` envelope.
        wrapped = [[[["id"], "t"]]]
        assert unwrap_import_rows(wrapped) == [[["id"], "t"]]

    def test_flat_single_row_with_id_envelope_is_returned_unchanged(self) -> None:
        # Regression for #1558: flat ``[row]`` where row is ``[[id], title]``
        # must not mistake the row's id envelope for a wrapper.
        flat = [[["id"], "t"]]
        assert unwrap_import_rows(flat) == flat

    def test_flat_populated_row_with_metadata_is_returned_unchanged(self) -> None:
        # Real imported rows may carry metadata arrays after the title. The
        # envelope probe must look only at ``result[0][0]`` as a row candidate;
        # scanning the whole row could mistake metadata arrays for rows.
        flat = [[["id"], "Title", [None, 1, [2]], [None, 2]]]
        assert unwrap_import_rows(flat) == flat

    def test_wrapped_row_with_absent_id_envelope_is_unwrapped(self) -> None:
        rows = [[None, "Missing id"], [["id_2"], "Imported"]]
        assert unwrap_import_rows([rows]) == rows

    def test_flat_list_with_non_list_head_returned_unchanged(self) -> None:
        # ``result[0]`` is a list but its first element is NOT a list, so the
        # probe falls through and returns ``result`` unchanged — this is the
        # already-flat row list ``[row, row, ...]`` shape.
        flat = [["id_str_head", "t"]]
        assert unwrap_import_rows(flat) == flat


class TestImportedSourceRowPositionContract:
    def test_positions_pinned(self) -> None:
        assert (
            ImportedSourceRow._ID_ENVELOPE_POS,
            ImportedSourceRow._ID_POS,
            ImportedSourceRow._TITLE_POS,
            ImportedSourceRow._MIN_LEN,
        ) == (0, 0, 1, 2)


class TestImportedSourceRow:
    def test_happy_path(self) -> None:
        row = ImportedSourceRow([["src_id_1"], "My Title"])
        assert row.is_well_formed is True
        assert row.source_id == "src_id_1"
        assert row.title_slot == "My Title"

    def test_short_row_not_well_formed(self) -> None:
        row = ImportedSourceRow([["src_id_1"]])
        assert row.is_well_formed is False
        assert row.source_id is None
        assert row.title_slot is None

    def test_non_list_not_well_formed(self) -> None:
        row = ImportedSourceRow(None)
        assert row.is_well_formed is False
        assert row.source_id is None
        assert row.title_slot is None

    def test_absent_id_envelope_short_circuits(self) -> None:
        # A falsy / non-list id envelope legitimately means "skip this row".
        assert ImportedSourceRow([None, "Title"]).source_id is None
        assert ImportedSourceRow([[], "Title"]).source_id is None
        assert ImportedSourceRow(["not_a_list", "Title"]).source_id is None

    def test_id_returned_verbatim(self) -> None:
        # The adapter reads the slot; the caller checks truthiness.
        assert ImportedSourceRow([[None], "Title"]).source_id is None
        assert ImportedSourceRow([[42], "Title"]).source_id == 42


# ---------------------------------------------------------------------------
# 9. Task metadata: timestamps, account id, discovery mode, hint (#2122)
# ---------------------------------------------------------------------------


class TestResearchTaskRowTimestampsAgainstCapture:
    """Which slot is create and which is update, settled by the wire.

    #2122's issue text says ``createTime task[2]`` / ``updateTime task[3]``.
    The capture says the opposite, and says it in the only way that can
    distinguish them: the same run polled twice.
    """

    def test_update_slot_advances_while_create_slot_holds(self) -> None:
        in_flight = ResearchTaskRow(_IN_FLIGHT_ROW)
        settled = ResearchTaskRow(_SETTLED_ROW)

        assert in_flight.task_id_raw == settled.task_id_raw
        # Created is the same instant on both polls; updated moved on.
        assert settled.created_at == in_flight.created_at
        assert settled.updated_at > in_flight.updated_at

    def test_a_just_started_run_reports_equal_timestamps(self) -> None:
        row = ResearchTaskRow(_IN_FLIGHT_ROW)
        assert row.created_at == row.updated_at

    def test_decoded_instants_are_utc_aware_and_match_the_wire_seconds(self) -> None:
        row = ResearchTaskRow(_SETTLED_ROW)
        assert row.created_at == datetime.fromtimestamp(_SETTLED_ROW[3][0], tz=timezone.utc)
        assert row.updated_at == datetime.fromtimestamp(_SETTLED_ROW[2][0], tz=timezone.utc)
        assert row.updated_at.tzinfo is not None


class TestResearchTaskRowTimestamps:
    @pytest.mark.parametrize(
        "row",
        [
            ["id", ["info"]],  # short row: no timestamp slots at all
            ["id", ["info"], None, None],
            ["id", ["info"], [], []],  # empty pairs
            ["id", ["info"], "reshaped", "reshaped"],
            ["id", ["info"], [None], [None]],
            ["id", ["info"], [True], [False]],  # bool is an int subclass
            ["id", ["info"], [10**20], [10**20]],  # out of datetime range
        ],
    )
    def test_unusable_slots_degrade_to_none(self, row: list) -> None:
        assert ResearchTaskRow(row).created_at is None
        assert ResearchTaskRow(row).updated_at is None

    def test_non_list_row_degrades(self) -> None:
        assert ResearchTaskRow("reshaped").created_at is None

    def test_nanos_are_dropped_not_read_as_seconds(self) -> None:
        row = ResearchTaskRow(["id", ["info"], [1786619585, 195655000], [1786619578, 139902000]])
        assert row.updated_at == datetime.fromtimestamp(1786619585, tz=timezone.utc)


class TestResearchTaskRowAccountId:
    def test_capture_carries_the_account_id(self) -> None:
        assert ResearchTaskRow(_SETTLED_ROW).account_id == "400237754469"

    @pytest.mark.parametrize(
        "row",
        [
            ["id", ["info"], None, None],  # no slot
            ["id", ["info"], None, None, None],
            ["id", ["info"], None, None, ""],  # empty string is not an id
            ["id", ["info"], None, None, 400237754469],  # int, not the wire's str
        ],
    )
    def test_absent_or_unusable_account_id_is_none(self, row: list) -> None:
        assert ResearchTaskRow(row).account_id is None


class TestResearchTaskInfoRowDiscoveryMode:
    @pytest.fixture(autouse=True)
    def _reset_warn_once(self):
        research_adapters._warned_discovery_modes.clear()
        yield
        research_adapters._warned_discovery_modes.clear()

    def test_capture_reports_the_fast_mode(self) -> None:
        assert (
            ResearchTaskInfoRow.discovery_mode(_SETTLED_ROW[1]) is DiscoveryMode.DEFAULT_LLM_SEARCH
        )

    def test_deep_run_code_reports_deep_research(self) -> None:
        """Against a REAL deep ``task_info`` — 5 is what ``start(mode="deep")``
        sends and what the poll echoes back, the same enum on both sides."""
        assert ResearchTaskInfoRow.discovery_mode(_DEEP_TASK_INFO) is DiscoveryMode.DEEP_RESEARCH

    def test_one_payload_distinguishes_a_deep_task_from_a_fast_one(self) -> None:
        """Both modes arrive in the SAME poll, so this is the discrimination the
        field exists for: a caller can tell which run is which without having
        remembered what it started."""
        assert ResearchTaskInfoRow.discovery_mode(_DEEP_TASK_INFO) is DiscoveryMode.DEEP_RESEARCH
        assert (
            ResearchTaskInfoRow.discovery_mode(_FAST_TASK_INFO) is DiscoveryMode.DEFAULT_LLM_SEARCH
        )

    @pytest.mark.parametrize(
        "task_info",
        [
            [None, ["q", 1]],  # short block
            [None, ["q", 1], None],
            [None, ["q", 1], 0],  # explicit UNSPECIFIED == no claim
            "reshaped",
            None,
        ],
    )
    def test_no_claim_is_none(self, task_info: object) -> None:
        assert ResearchTaskInfoRow.discovery_mode(task_info) is None

    @pytest.mark.parametrize("code", [99, -1, True, "5", [5]])
    def test_populated_but_unmappable_is_unknown_not_none(self, code: object, caplog) -> None:
        """``None`` means "no mode claimed"; ``UNKNOWN`` means "a mode we cannot
        read" — including a literal -1, which is a client sentinel and must not
        round-trip off the wire."""
        with caplog.at_level(logging.WARNING, logger="notebooklm._row_adapters.research"):
            assert ResearchTaskInfoRow.discovery_mode([None, ["q", 1], code]) is (
                DiscoveryMode.UNKNOWN
            )
        assert len(caplog.records) == 1

    def test_unmapped_code_warns_once_per_value(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="notebooklm._row_adapters.research"):
            for _ in range(3):
                ResearchTaskInfoRow.discovery_mode([None, ["q", 1], 99])
            ResearchTaskInfoRow.discovery_mode([None, ["q", 1], 98])
        assert len(caplog.records) == 2


class TestResearchResultRowHint:
    def test_capture_carries_the_hint(self) -> None:
        sources = _SETTLED_ROW[1][3][0]
        assert ResearchResultRow(sources[0]).hint == (
            "Comprehensive introduction to the event loop's role as a conductor."
        )

    def test_every_captured_fast_row_carries_one(self) -> None:
        sources = _SETTLED_ROW[1][3][0]
        assert all(ResearchResultRow(src).hint for src in sources)

    @pytest.mark.parametrize(
        "row",
        [
            ["https://example.com", "Title"],  # short row
            ["https://example.com", "Title", None, 1],
            ["https://example.com", "Title", ["nested"], 1],
            [None, "Report title", None, 3],  # the deep report row shape
        ],
    )
    def test_absent_or_unusable_hint_is_empty_string(self, row: list) -> None:
        assert ResearchResultRow(row).hint == ""

    def test_a_real_deep_run_mixes_hinted_and_unhinted_rows(self) -> None:
        """Absence is routine on a deep run, not an edge case.

        The live deep capture carried 89 sources, 28 of them with no hint
        (including the report row). The trimmed fixture keeps rows of both
        kinds, so this pins that the decode does not invent a hint for a row
        the backend left bare.
        """
        rows = [ResearchResultRow(src) for src in _DEEP_TASK_INFO[3][0]]
        hints = [row.hint for row in rows]
        assert any(h for h in hints), "fixture should keep at least one hinted row"
        assert any(not h for h in hints), "fixture should keep at least one bare row"
