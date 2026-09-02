"""Unit tests for ``sources.search`` on the web tier (``RetrieveRelevantChunks``, #2283).

Pins the request shape (``[notebook_id, query, None, [1], [[[id], …]]?]``), the
positional decode of the live reply (one ``[source_id, [chunk, …]]`` row per
source, each chunk ``[[[[text, …]]], rank, [[None, start, end]]]``), the
transport-neutral validation shared by both backends, and the ranked / limited
projection — against a fake ``RpcCaller``, no network.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._sources import finalize_search_results, validate_search
from notebooklm._web.params.sources import build_retrieve_relevant_chunks_params
from notebooklm._web.rows.chunks import (
    RelevantChunkRow,
    RelevantChunkSourceRow,
    decode_relevant_chunks,
    unwrap_relevant_chunk_sources,
)
from notebooklm._web.sources.search import SourceSearchService
from notebooklm.exceptions import DecodingError, RPCError, ValidationError
from notebooklm.rpc import RPCMethod
from notebooklm.types import RelevantChunk

NB = "7901cb08-5039-413a-9d6c-c41ecc623405"
SRC_A = "5777b434-46fd-4f4a-bf28-577b26ead71b"
SRC_B = "f6e549bc-7593-4146-ab8f-ce01094d1387"
TEXT_A = "Photosynthesis converts light energy into chemical energy.\n"
TEXT_B = "The Roman Republic was founded in 509 BC.\n"
_LOGGER = logging.getLogger("notebooklm._sources")


def _chunk(text: str | list[str], rank: int, start: int, end: int) -> list[Any]:
    parts = [text] if isinstance(text, str) else text
    return [[[parts]], rank, [[None, start, end]]]


def _payload() -> list[Any]:
    # The live reply for "who was the first emperor": the Rome source ranks first.
    return [
        [
            [SRC_B, [_chunk(TEXT_B, 1, 0, 828)]],
            [SRC_A, [_chunk(TEXT_A, 2, 0, 903)]],
        ]
    ]


def _rpc(return_value: Any = None, side_effect: Any = None) -> MagicMock:
    return MagicMock(rpc_call=AsyncMock(return_value=return_value, side_effect=side_effect))


class TestParams:
    def test_without_filter_omits_the_source_filter_slot(self) -> None:
        assert build_retrieve_relevant_chunks_params(NB, "emperor", None) == [
            NB,
            "emperor",
            None,
            [1],
        ]

    def test_with_filter_wraps_each_id_as_a_source_id(self) -> None:
        assert build_retrieve_relevant_chunks_params(NB, "emperor", [SRC_A, SRC_B]) == [
            NB,
            "emperor",
            None,
            [1],
            [[[SRC_A], [SRC_B]]],
        ]

    def test_empty_filter_is_the_same_as_no_filter(self) -> None:
        assert build_retrieve_relevant_chunks_params(NB, "q", []) == [NB, "q", None, [1]]


class TestRows:
    def test_unwrap_returns_the_source_rows(self) -> None:
        rows = unwrap_relevant_chunk_sources(_payload(), method_id="ASU5Oe")
        assert len(rows) == 2 and rows[0][0] == SRC_B

    @pytest.mark.parametrize("payload", [None, [], [None], [[]]])
    def test_unwrap_empty_reply_is_no_results(self, payload: Any) -> None:
        assert unwrap_relevant_chunk_sources(payload, method_id="ASU5Oe") == []

    @pytest.mark.parametrize("payload", ["nope", [1], [[1, 2]], [["not-a-row"]]])
    def test_unwrap_rejects_a_foreign_envelope(self, payload: Any) -> None:
        with pytest.raises(DecodingError):
            unwrap_relevant_chunk_sources(payload, method_id="ASU5Oe")

    def test_source_row_view(self) -> None:
        row = RelevantChunkSourceRow([SRC_A, [_chunk(TEXT_A, 2, 0, 903)]])
        assert row.is_well_formed
        assert row.source_id == SRC_A
        assert len(row.chunk_rows) == 1
        assert not RelevantChunkSourceRow([SRC_A]).is_well_formed
        assert not RelevantChunkSourceRow(["", []]).is_well_formed
        assert RelevantChunkSourceRow([SRC_A, None]).chunk_rows == []

    def test_chunk_row_view_reads_text_rank_and_span(self) -> None:
        row = RelevantChunkRow(_chunk(TEXT_A, 3, 14202, 14733))
        assert row.is_well_formed
        assert row.text == TEXT_A
        assert row.rank == 3
        assert row.span == (14202, 14733)

    def test_chunk_text_is_a_repeated_string_joined_in_order(self) -> None:
        # ``[[[[a, b]]]`` on the wire: the innermost list is ``repeated string``
        # (one element on every live observation; joined so a multi-part chunk
        # cannot silently drop text).
        row = RelevantChunkRow(_chunk(["alpha ", "beta"], 1, 0, 10))
        assert row.text == "alpha beta"

    def test_chunk_without_span_or_rank_degrades_to_none(self) -> None:
        row = RelevantChunkRow([[[[TEXT_A]]]])
        assert row.text == TEXT_A
        assert row.rank is None
        assert row.span is None
        assert row.is_well_formed

    @pytest.mark.parametrize("start,end", [(-1, 10), (0, -1), (11, 10)])
    def test_chunk_invalid_span_degrades_to_none(self, start: int, end: int) -> None:
        assert RelevantChunkRow(_chunk(TEXT_A, 1, start, end)).span is None

    @pytest.mark.parametrize("raw", [None, [], [None, 1], [[[[""]]], 1], "text", [[[[1]]], 1]])
    def test_chunk_row_without_text_is_malformed(self, raw: Any) -> None:
        assert not RelevantChunkRow(raw).is_well_formed

    def test_decode_projects_ranked_chunks_across_sources(self) -> None:
        chunks = decode_relevant_chunks(_payload(), method_id="ASU5Oe", logger=_LOGGER)
        assert chunks == [
            RelevantChunk(source_id=SRC_B, text=TEXT_B, rank=1, start=0, end=828),
            RelevantChunk(source_id=SRC_A, text=TEXT_A, rank=2, start=0, end=903),
        ]

    def test_decode_preserves_wire_order_for_service_finalization(self) -> None:
        payload = [[[SRC_A, [_chunk("first", 14, 0, 10), _chunk("best", 1, 20, 30)]]]]
        chunks = decode_relevant_chunks(payload, method_id="ASU5Oe", logger=_LOGGER)
        assert [c.text for c in chunks] == ["first", "best"]
        assert [c.rank for c in chunks] == [14, 1]

    def test_decode_unranked_chunks_remain_in_arrival_order(self) -> None:
        payload = [[[SRC_A, [[[[["z"]]]], _chunk("ranked", 2, 0, 1), [[[["y"]]]]]]]]
        chunks = decode_relevant_chunks(payload, method_id="ASU5Oe", logger=_LOGGER)
        assert [c.text for c in chunks] == ["z", "ranked", "y"]
        assert [c.rank for c in chunks] == [0, 2, 0]

    def test_decode_skips_malformed_rows_and_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        payload = [
            [
                ["", [_chunk("orphan", 1, 0, 1)]],  # no source id
                [SRC_A, [[None, 5], _chunk(TEXT_A, 2, 0, 903)]],  # one bad chunk
                "garbage",
            ]
        ]
        with caplog.at_level(logging.WARNING, logger=_LOGGER.name):
            chunks = decode_relevant_chunks(payload, method_id="ASU5Oe", logger=_LOGGER)
        assert chunks == [RelevantChunk(source_id=SRC_A, text=TEXT_A, rank=2, start=0, end=903)]
        assert sum("RetrieveRelevantChunks" in r.message for r in caplog.records) == 3


class TestValidation:
    def test_normalizes_and_returns_inputs(self) -> None:
        assert validate_search("  emperor ", None, None) == ("emperor", (), None)
        assert validate_search("q", [SRC_A, SRC_B], 5) == ("q", (SRC_A, SRC_B), 5)

    @pytest.mark.parametrize("query", ["", "   ", "\n"])
    def test_blank_query_is_rejected(self, query: str) -> None:
        with pytest.raises(ValidationError, match="query"):
            validate_search(query, None, None)

    def test_empty_source_id_entry_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="source_ids"):
            validate_search("q", [SRC_A, ""], None)

    def test_duplicate_source_ids_are_collapsed_in_order(self) -> None:
        assert validate_search("q", [SRC_B, SRC_A, SRC_B], None)[1] == (SRC_B, SRC_A)

    @pytest.mark.parametrize("limit", [0, -1])
    def test_non_positive_limit_is_rejected(self, limit: int) -> None:
        with pytest.raises(ValidationError, match="limit"):
            validate_search("q", None, limit)

    def test_finalize_sorts_and_truncates(self) -> None:
        c1 = RelevantChunk(SRC_A, "a", 3)
        c2 = RelevantChunk(SRC_B, "b", 1)
        c3 = RelevantChunk(SRC_A, "c", 2)
        assert finalize_search_results([c1, c2, c3], None) == [c2, c3, c1]
        assert finalize_search_results([c1, c2, c3], 2) == [c2, c3]


class TestRelevantChunkType:
    def test_rejects_empty_source_id_and_negative_rank(self) -> None:
        with pytest.raises(ValueError):
            RelevantChunk(source_id="", text="t", rank=1)
        with pytest.raises(ValueError):
            RelevantChunk(source_id=SRC_A, text="t", rank=-1)

    def test_is_frozen(self) -> None:
        chunk = RelevantChunk(source_id=SRC_A, text="t", rank=1)
        with pytest.raises(AttributeError):
            chunk.text = "u"  # type: ignore[misc]

    def test_rejects_inverted_span(self) -> None:
        with pytest.raises(ValueError, match="less than or equal"):
            RelevantChunk(source_id=SRC_A, text="t", rank=1, start=2, end=1)


class TestService:
    @pytest.mark.asyncio
    async def test_calls_the_rpc_with_the_read_shape_and_decodes(self) -> None:
        rpc = _rpc(_payload())
        chunks = await SourceSearchService(rpc, logger=_LOGGER).search(
            NB, "who was the first emperor", source_ids=(), limit=None
        )
        assert [c.source_id for c in chunks] == [SRC_B, SRC_A]
        method, params = rpc.rpc_call.await_args.args[:2]
        assert method is RPCMethod.RETRIEVE_RELEVANT_CHUNKS
        assert params == [NB, "who was the first emperor", None, [1]]
        kwargs = rpc.rpc_call.await_args.kwargs
        assert kwargs["source_path"] == f"/notebook/{NB}"
        # A read that can legitimately answer ``[]`` — but a status-tagged null
        # must still surface as the server's rejection, never as "no results".
        assert kwargs["allow_null"] is True
        assert kwargs["raise_on_null_status"] is True

    @pytest.mark.asyncio
    async def test_forwards_the_source_filter(self) -> None:
        rpc = _rpc([[[SRC_B, [_chunk(TEXT_B, 1, 0, 828)]]]])
        chunks = await SourceSearchService(rpc, logger=_LOGGER).search(
            NB, "emperor", source_ids=(SRC_B,), limit=None
        )
        assert [c.source_id for c in chunks] == [SRC_B]
        assert rpc.rpc_call.await_args.args[1] == [NB, "emperor", None, [1], [[[SRC_B]]]]

    @pytest.mark.asyncio
    async def test_limit_truncates_after_ranking(self) -> None:
        payload = [[[SRC_A, [_chunk("third", 3, 0, 1), _chunk("first", 1, 2, 3)]]]]
        chunks = await SourceSearchService(_rpc(payload), logger=_LOGGER).search(
            NB, "q", source_ids=(), limit=1
        )
        assert [c.text for c in chunks] == ["first"]

    @pytest.mark.asyncio
    async def test_empty_reply_is_no_results(self) -> None:
        for payload in (None, []):
            chunks = await SourceSearchService(_rpc(payload), logger=_LOGGER).search(
                NB, "q", source_ids=(), limit=None
            )
            assert chunks == []

    @pytest.mark.asyncio
    async def test_rpc_errors_propagate_unwrapped(self) -> None:
        rpc = _rpc(side_effect=RPCError("not found", rpc_code=5))
        with pytest.raises(RPCError):
            await SourceSearchService(rpc, logger=_LOGGER).search(
                NB, "q", source_ids=(), limit=None
            )
