"""Unit tests for ``AndroidSourcesAPI.search`` (``RetrieveRelevantChunks`` over gRPC, #2283).

Pins the exact request message (tags live-validated 2026-09-01: ``project_id``
#1, ``query`` #2, ``options{mode = 1}`` #4, ``source_filter{source_ids}`` #5),
the read-only ``replay_safe`` contract, and the ranked projection of the reply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm._android.source_search import RETRIEVE_RELEVANT_CHUNKS_METHOD
from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm.exceptions import ValidationError
from notebooklm.types import RelevantChunk

NB = "nb-1"
SRC_A, SRC_B = "src-a", "src-b"


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


class FakeTransport:
    def __init__(self, outcomes: dict[str, list[Any]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease()

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        outcome = self.outcomes[method].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _api(transport: FakeTransport) -> AndroidSourcesAPI:
    return AndroidSourcesAPI(cast(AndroidSession, transport), cast(AndroidUploadPipeline, object()))


def _chunk(text: str, rank: int, start: int, end: int) -> sources_pb2.RelevantChunk:
    return sources_pb2.RelevantChunk(
        content=sources_pb2.RelevantChunkContent(text=sources_pb2.RelevantChunkText(parts=[text])),
        rank=rank,
        spans=[sources_pb2.RelevantChunkSpan(start=start, end=end)],
    )


def _reply() -> sources_pb2.RetrieveRelevantChunksResponse:
    return sources_pb2.RetrieveRelevantChunksResponse(
        source_chunks=[
            sources_pb2.SourceRelevantChunks(
                source_id=SRC_A,
                chunks=[_chunk("doc order first", 2, 0, 10), _chunk("best", 1, 20, 30)],
            ),
            sources_pb2.SourceRelevantChunks(source_id=SRC_B, chunks=[_chunk("third", 3, 0, 5)]),
        ]
    )


@pytest.mark.asyncio
async def test_search_sends_the_pinned_request_and_ranks_the_reply() -> None:
    transport = FakeTransport({RETRIEVE_RELEVANT_CHUNKS_METHOD: [_reply()]})
    chunks = await _api(transport).search(NB, "  emperor ", source_ids=[SRC_B, SRC_A])
    assert chunks == [
        RelevantChunk(SRC_A, "best", 1, 20, 30),
        RelevantChunk(SRC_A, "doc order first", 2, 0, 10),
        RelevantChunk(SRC_B, "third", 3, 0, 5),
    ]
    ((method, request, kwargs),) = transport.calls
    assert method == RETRIEVE_RELEVANT_CHUNKS_METHOD
    assert request == sources_pb2.RetrieveRelevantChunksRequest(
        project_id=NB,
        query="emperor",
        options=sources_pb2.RetrieveRelevantChunksOptions(mode=1),
        source_filter=sources_pb2.SourceIdFilter(
            source_ids=[read_pb2.SourceId(id=SRC_B), read_pb2.SourceId(id=SRC_A)]
        ),
    )
    # A pure read: safe to replay on a transport loss.
    assert kwargs["replay_safe"] is True
    assert kwargs["response_type"] is sources_pb2.RetrieveRelevantChunksResponse
    assert kwargs["expected_epoch"] == 7
    assert transport.scopes == ["source.search"]


@pytest.mark.asyncio
async def test_search_without_filter_omits_the_filter_message_and_honours_limit() -> None:
    transport = FakeTransport({RETRIEVE_RELEVANT_CHUNKS_METHOD: [_reply()]})
    chunks = await _api(transport).search(NB, "emperor", limit=2)
    assert [c.text for c in chunks] == ["best", "doc order first"]
    request = transport.calls[0][1]
    assert not request.HasField("source_filter")
    assert request.options.mode == 1


@pytest.mark.asyncio
async def test_search_multi_part_text_is_joined_and_missing_span_is_none() -> None:
    reply = sources_pb2.RetrieveRelevantChunksResponse(
        source_chunks=[
            sources_pb2.SourceRelevantChunks(
                source_id=SRC_A,
                chunks=[
                    sources_pb2.RelevantChunk(
                        content=sources_pb2.RelevantChunkContent(
                            text=sources_pb2.RelevantChunkText(parts=["alpha ", "beta"])
                        ),
                        rank=1,
                    )
                ],
            )
        ]
    )
    transport = FakeTransport({RETRIEVE_RELEVANT_CHUNKS_METHOD: [reply]})
    chunks = await _api(transport).search(NB, "q")
    assert chunks == [RelevantChunk(SRC_A, "alpha beta", 1, None, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("start,end", [(-1, 10), (0, -1), (11, 10)])
async def test_search_omits_malformed_span(start: int, end: int) -> None:
    reply = sources_pb2.RetrieveRelevantChunksResponse(
        source_chunks=[
            sources_pb2.SourceRelevantChunks(
                source_id=SRC_A,
                chunks=[_chunk("kept", 1, start, end)],
            )
        ]
    )
    transport = FakeTransport({RETRIEVE_RELEVANT_CHUNKS_METHOD: [reply]})
    assert await _api(transport).search(NB, "q") == [RelevantChunk(SRC_A, "kept", 1, None, None)]


@pytest.mark.asyncio
async def test_search_skips_textless_chunks_and_idless_sources() -> None:
    reply = sources_pb2.RetrieveRelevantChunksResponse(
        source_chunks=[
            sources_pb2.SourceRelevantChunks(source_id="", chunks=[_chunk("orphan", 1, 0, 1)]),
            sources_pb2.SourceRelevantChunks(
                source_id=SRC_A,
                chunks=[sources_pb2.RelevantChunk(rank=1), _chunk("kept", 2, 0, 4)],
            ),
        ]
    )
    transport = FakeTransport({RETRIEVE_RELEVANT_CHUNKS_METHOD: [reply]})
    assert await _api(transport).search(NB, "q") == [RelevantChunk(SRC_A, "kept", 2, 0, 4)]


@pytest.mark.asyncio
async def test_search_validates_before_any_call() -> None:
    transport = FakeTransport()
    with pytest.raises(ValidationError):
        await _api(transport).search(NB, "   ")
    with pytest.raises(ValidationError):
        await _api(transport).search(NB, "q", source_ids=[""])
    with pytest.raises(ValidationError):
        await _api(transport).search(NB, "q", limit=0)
    assert transport.calls == []
