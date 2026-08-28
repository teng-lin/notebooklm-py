"""Tests for the pure IMPORT_RESEARCH helpers added for #2187.

``_import_research_read_timeout``, ``_is_import_research_failed_precondition``,
and ``_reconcile_import_probe`` live in ``_web/research_import.py`` (ADR-0008
module-size ratchet keeps free functions out of ``_web/research.py`` where
possible). Behavioral coverage of how ``ResearchAPI.import_sources_with_verification``
actually uses them (retry/raise/log decisions) lives in
``test_research_import_with_verification.py``; these tests cover the pure
functions in isolation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from notebooklm._runtime.config import (
    DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT,
)
from notebooklm._types.research import ResearchSource
from notebooklm._web.research_import import (
    _import_research_read_timeout,
    _is_import_research_failed_precondition,
    _reconcile_import_probe,
)
from notebooklm.exceptions import RPCError


class TestImportResearchReadTimeout:
    def test_zero_sources_returns_base_floor(self):
        assert _import_research_read_timeout(0) == DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT

    def test_scales_linearly_with_source_count(self):
        # 3 sources: base + 3 * per-source increment, well under the ceiling.
        expected = (
            DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + 3 * DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
        )
        assert _import_research_read_timeout(3) == expected

    def test_notebook_source_cap_stays_under_ceiling(self):
        # 50 is the NotebookLM per-notebook source cap (#1919/#1926) — the
        # realistic worst case for one IMPORT_RESEARCH batch.
        result = _import_research_read_timeout(50)
        assert (
            result
            == DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT
            + 50 * DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
        )
        assert result <= DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT

    def test_clamps_at_max_timeout_for_pathological_batch(self):
        assert _import_research_read_timeout(1000) == DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT


class TestIsImportResearchFailedPrecondition:
    def test_true_for_grpc_failed_precondition_code(self):
        exc = RPCError("The server rejected this request (failed precondition).", rpc_code=9)
        assert _is_import_research_failed_precondition(exc) is True

    def test_true_for_string_encoded_code(self):
        # ``rpc_code`` is typed ``str | int | None`` on the wire path.
        exc = RPCError("failed precondition", rpc_code="9")
        assert _is_import_research_failed_precondition(exc) is True

    @pytest.mark.parametrize("rpc_code", [16, 5, "USER_DISPLAYABLE_ERROR", None])
    def test_false_for_any_other_code(self, rpc_code):
        exc = RPCError("some other failure", rpc_code=rpc_code)
        assert _is_import_research_failed_precondition(exc) is False


def _src(id_: str, url: str | None, title: str = "T") -> MagicMock:
    return MagicMock(id=id_, url=url, title=title)


class TestReconcileImportProbe:
    def test_full_match_returns_fully_verified_entries(self):
        source = ResearchSource(url="https://example.com/a", title="A")
        outcome = _reconcile_import_probe(
            current=[_src("src_a", "https://example.com/a")],
            baseline_ids=set(),
            requested_urls_norm={"https://example.com/a"},
            requested_no_url_count=0,
            source_inputs=[{"url": "https://example.com/a", "title": "A"}],
            source_models=[source],
            already_verified_ids=set(),
            allow_duplicate=False,
        )
        assert outcome.fully_verified_entries == [{"id": "src_a", "title": "T"}]
        assert outcome.filtered is False

    def test_no_match_leaves_state_unchanged(self):
        source = ResearchSource(url="https://example.com/a", title="A")
        outcome = _reconcile_import_probe(
            current=[],
            baseline_ids=set(),
            requested_urls_norm={"https://example.com/a"},
            requested_no_url_count=0,
            source_inputs=[{"url": "https://example.com/a", "title": "A"}],
            source_models=[source],
            already_verified_ids=set(),
            allow_duplicate=False,
        )
        assert outcome.fully_verified_entries is None
        assert outcome.filtered is False
        assert outcome.source_models == [source]

    def test_partial_match_filters_and_reports_committed_subset(self):
        source_a = ResearchSource(url="https://example.com/a", title="A")
        source_b = ResearchSource(url="https://example.com/b", title="B")
        outcome = _reconcile_import_probe(
            current=[_src("src_a", "https://example.com/a")],
            baseline_ids=set(),
            requested_urls_norm={"https://example.com/a", "https://example.com/b"},
            requested_no_url_count=0,
            source_inputs=[
                {"url": "https://example.com/a", "title": "A"},
                {"url": "https://example.com/b", "title": "B"},
            ],
            source_models=[source_a, source_b],
            already_verified_ids=set(),
            allow_duplicate=False,
        )
        assert outcome.fully_verified_entries is None
        assert outcome.filtered is True
        assert outcome.removed_count == 1
        assert outcome.newly_verified == [{"id": "src_a", "title": "T"}]
        assert outcome.source_models == [source_b]

    def test_filtering_down_to_empty_returns_success_with_empty_list(self):
        source = ResearchSource(url="https://example.com/a", title="A")
        outcome = _reconcile_import_probe(
            current=[_src("src_a", "https://example.com/a")],
            baseline_ids=None,  # baseline snapshot failed; falls back to current_urls_norm
            requested_urls_norm={"https://example.com/a"},
            requested_no_url_count=0,
            source_inputs=[{"url": "https://example.com/a", "title": "A"}],
            source_models=[source],
            already_verified_ids=set(),
            allow_duplicate=False,
        )
        assert outcome.fully_verified_entries == []
        assert outcome.filtered is True

    def test_already_verified_ids_are_not_double_counted(self):
        source_a = ResearchSource(url="https://example.com/a", title="A")
        source_b = ResearchSource(url="https://example.com/b", title="B")
        outcome = _reconcile_import_probe(
            current=[_src("src_a", "https://example.com/a")],
            baseline_ids=set(),
            requested_urls_norm={"https://example.com/a", "https://example.com/b"},
            requested_no_url_count=0,
            source_inputs=[
                {"url": "https://example.com/a", "title": "A"},
                {"url": "https://example.com/b", "title": "B"},
            ],
            source_models=[source_a, source_b],
            already_verified_ids={"src_a"},  # already accounted for in a prior iteration
            allow_duplicate=False,
        )
        assert outcome.newly_verified == []
