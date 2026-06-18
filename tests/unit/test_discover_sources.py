"""Unit tests for the DISCOVER_SOURCES (``Es3dTe``) builder, parser, adapter.

Covers the synchronous "discover sources by topic" path:

1. **Payload builder** — the live-verified ``[[query], None, mode, nb]`` shape.
2. **Mode resolver** — ``"web"`` -> 1; unknown selector raises.
3. **Row adapters** — position pins, permissive degrade, and the GUARANTEED
   candidate-slot drift raise.
4. **Parser** — typed ``DiscoveredSource`` rows, URL-skip, empty handling.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._row_adapters.discover import DiscoveredSourceRow, DiscoverResultRow
from notebooklm._runtime.contracts import RpcCaller
from notebooklm._source.discover import (
    DISCOVER_MODE_WEB,
    SourceDiscoverer,
    build_discover_sources_params,
    parse_discovered_sources,
    resolve_discover_mode,
)
from notebooklm._types.research import DiscoveredSource
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod

# A real-shape slice of the live ``Es3dTe`` response (3 candidates trimmed from
# the recorded 10), preserving ``[url, title, why_relevant, type]`` rows plus
# the trailing summary string and run-id list.
LIVE_SHAPE_RESULT = [
    [
        [
            "https://www.ibm.com/roadmaps/quantum/2026/",
            "Quantum 2026 — IBM Technology Atlas",
            "Details the 2026 milestone for demonstrating practical quantum advantage.",
            1,
        ],
        [
            "https://arxiv.org/pdf/2602.20352",
            "Quantum Machine Learning for Complex Systems - arXiv",
            "Reviews variational algorithms and their application to complex systems.",
            1,
        ],
        [
            "https://azure.microsoft.com/en-us/blog/quantum/",
            "Microsoft Azure Quantum Blog",
            "Profiles the first quantum processor powered by topological qubits.",
            1,
        ],
    ],
    "These selections trace the transition to operational quantum hardware.",
    ["c3603bb3-eb77-42a1-8879-4df3e60ea0a5"],
]


# ---------------------------------------------------------------------------
# 1. Payload builder
# ---------------------------------------------------------------------------


class TestBuildDiscoverSourcesParams:
    def test_live_verified_shape(self) -> None:
        params = build_discover_sources_params("nb-123", "quantum computing", mode=1)
        assert params == [["quantum computing"], None, 1, "nb-123"]

    def test_query_is_wrapped_in_single_element_list(self) -> None:
        params = build_discover_sources_params("nb", "q", mode=DISCOVER_MODE_WEB)
        assert params[0] == ["q"]
        assert params[1] is None
        assert params[3] == "nb"

    def test_mode_passes_through(self) -> None:
        assert build_discover_sources_params("nb", "q", mode=2)[2] == 2


# ---------------------------------------------------------------------------
# 2. Mode resolver
# ---------------------------------------------------------------------------


class TestResolveDiscoverMode:
    def test_web_is_one(self) -> None:
        assert resolve_discover_mode("web") == DISCOVER_MODE_WEB == 1

    def test_case_insensitive(self) -> None:
        assert resolve_discover_mode("WEB") == 1

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid source"):
            resolve_discover_mode("drive")


# ---------------------------------------------------------------------------
# 3. Row adapters
# ---------------------------------------------------------------------------


class TestDiscoverResultRow:
    def test_candidate_rows_extracts_slot_zero(self) -> None:
        rows = DiscoverResultRow(LIVE_SHAPE_RESULT).candidate_rows
        assert len(rows) == 3
        assert rows[0][0] == "https://www.ibm.com/roadmaps/quantum/2026/"

    def test_non_list_candidates_degrade_to_empty(self) -> None:
        # A thin response (slot present but not a list) -> [].
        assert DiscoverResultRow(["not-a-list", "summary"]).candidate_rows == []

    def test_missing_candidate_slot_raises(self) -> None:
        # An empty envelope has no [0] slot at all -> genuine drift.
        with pytest.raises(UnknownRPCMethodError):
            _ = DiscoverResultRow([]).candidate_rows


class TestDiscoveredSourceRow:
    def test_reads_all_fields(self) -> None:
        row = DiscoveredSourceRow(["https://x.test/", "Title", "Why it matters", 1])
        assert row.is_well_formed
        assert row.url == "https://x.test/"
        assert row.title == "Title"
        assert row.why_relevant == "Why it matters"
        assert row.source_type == 1

    def test_short_row_degrades_per_field(self) -> None:
        row = DiscoveredSourceRow(["https://x.test/"])
        assert row.url == "https://x.test/"
        assert row.title == ""
        assert row.why_relevant == ""
        assert row.source_type == 1  # default tag

    def test_missing_url_is_not_well_formed(self) -> None:
        assert not DiscoveredSourceRow([None, "Title"]).is_well_formed
        assert not DiscoveredSourceRow([]).is_well_formed
        assert not DiscoveredSourceRow("not-a-list").is_well_formed

    def test_non_int_type_falls_back_to_default(self) -> None:
        row = DiscoveredSourceRow(["https://x.test/", "T", "W", "weird"])
        assert row.source_type == 1


# ---------------------------------------------------------------------------
# 4. Parser
# ---------------------------------------------------------------------------


class TestParseDiscoveredSources:
    def test_parses_live_shape(self) -> None:
        sources = parse_discovered_sources(LIVE_SHAPE_RESULT)
        assert len(sources) == 3
        assert all(isinstance(s, DiscoveredSource) for s in sources)
        first = sources[0]
        assert first.url == "https://www.ibm.com/roadmaps/quantum/2026/"
        assert first.title == "Quantum 2026 — IBM Technology Atlas"
        assert first.why_relevant.startswith("Details the 2026")
        assert first.source_type == 1

    def test_empty_result_returns_empty_list(self) -> None:
        assert parse_discovered_sources(None) == []
        assert parse_discovered_sources([]) == []

    def test_empty_candidate_list_returns_empty(self) -> None:
        assert parse_discovered_sources([[], "summary", ["id"]]) == []

    def test_skips_rows_without_url(self) -> None:
        result = [
            [
                ["https://ok.test/", "Good", "why", 1],
                [None, "No URL", "why", 1],
                ["", "Empty URL", "why", 1],
            ],
            "summary",
        ]
        sources = parse_discovered_sources(result)
        assert len(sources) == 1
        assert sources[0].url == "https://ok.test/"

    def test_to_public_dict_round_trip(self) -> None:
        src = parse_discovered_sources(LIVE_SHAPE_RESULT)[0]
        assert src.to_public_dict() == {
            "url": "https://www.ibm.com/roadmaps/quantum/2026/",
            "title": "Quantum 2026 — IBM Technology Atlas",
            "why_relevant": "Details the 2026 milestone for demonstrating "
            "practical quantum advantage.",
            "source_type": 1,
        }


# ---------------------------------------------------------------------------
# 5. Service (SourceDiscoverer) — RPC wiring
# ---------------------------------------------------------------------------


def _fake_rpc(return_value: object | None = None) -> MagicMock:
    """A narrow ``RpcCaller`` fake with ``rpc_call`` injected at construction.

    Built via the constructor kwarg (not post-construction attribute
    assignment) to satisfy ADR-0007's no-forbidden-monkeypatch gate.
    """
    return MagicMock(spec=RpcCaller, rpc_call=AsyncMock(return_value=return_value))


class TestSourceDiscoverer:
    @pytest.mark.asyncio
    async def test_discover_calls_rpc_with_correct_envelope(self) -> None:
        rpc = _fake_rpc(LIVE_SHAPE_RESULT)
        discoverer = SourceDiscoverer(rpc)

        sources = await discoverer.discover("nb-7", "quantum computing")

        assert len(sources) == 3
        assert sources[0].url == "https://www.ibm.com/roadmaps/quantum/2026/"
        rpc.rpc_call.assert_awaited_once()
        call = rpc.rpc_call.await_args
        assert call.args[0] is RPCMethod.DISCOVER_SOURCES
        assert call.args[1] == [["quantum computing"], None, 1, "nb-7"]
        assert call.kwargs["source_path"] == "/notebook/nb-7"
        assert call.kwargs["allow_null"] is True

    @pytest.mark.asyncio
    async def test_empty_query_raises_before_rpc(self) -> None:
        rpc = _fake_rpc()
        discoverer = SourceDiscoverer(rpc)
        with pytest.raises(ValueError, match="non-empty"):
            await discoverer.discover("nb", "   ")
        rpc.rpc_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_source_raises_before_rpc(self) -> None:
        rpc = _fake_rpc()
        discoverer = SourceDiscoverer(rpc)
        with pytest.raises(ValueError, match="Invalid source"):
            await discoverer.discover("nb", "q", source="drive")
        rpc.rpc_call.assert_not_awaited()
