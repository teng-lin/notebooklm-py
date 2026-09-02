"""Native Android ``RetrieveRelevantChunks`` source search."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from .._sources import finalize_search_results
from ..types import RelevantChunk
from .session import AndroidSession

logger = logging.getLogger("notebooklm._sources")

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
RETRIEVE_RELEVANT_CHUNKS_METHOD = f"/{_SERVICE}/RetrieveRelevantChunks"


def _proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import sources_pb2

    return cast(Any, sources_pb2)


def _read_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

    return cast(Any, read_pb2)


def _decode_response(response: Any, limit: int | None) -> list[RelevantChunk]:
    decoded: list[RelevantChunk] = []
    for source in response.source_chunks:
        source_id = source.source_id
        if not source_id:
            logger.warning("RetrieveRelevantChunks: skipping Android row without a source id")
            continue
        for chunk in source.chunks:
            text = "".join(chunk.content.text.parts)
            rank = int(chunk.rank)
            if not text or rank < 0:
                logger.warning(
                    "RetrieveRelevantChunks: skipping malformed Android chunk for source %s",
                    source_id,
                )
                continue
            start: int | None
            end: int | None
            if chunk.spans:
                start = int(chunk.spans[0].start)
                end = int(chunk.spans[0].end)
                if start < 0 or end < 0 or start > end:
                    logger.warning(
                        "RetrieveRelevantChunks: omitting malformed Android span for source %s",
                        source_id,
                    )
                    start = end = None
            else:
                start = end = None
            decoded.append(
                RelevantChunk(
                    source_id=source_id,
                    text=text,
                    rank=rank,
                    start=start,
                    end=end,
                )
            )
    return finalize_search_results(decoded, limit)


class AndroidSourceSearchService:
    """Dispatch the replay-safe native search and project its typed reply."""

    def __init__(self, transport: AndroidSession) -> None:
        self._transport = transport

    async def search(
        self,
        notebook_id: str,
        query: str,
        *,
        source_ids: Sequence[str],
        limit: int | None,
    ) -> list[RelevantChunk]:
        proto = _proto()
        request = proto.RetrieveRelevantChunksRequest(
            project_id=notebook_id,
            query=query,
            options=proto.RetrieveRelevantChunksOptions(mode=1),
        )
        if source_ids:
            request.source_filter.source_ids.extend(
                _read_proto().SourceId(id=source_id) for source_id in source_ids
            )
        async with self._transport.operation_scope("source.search") as lease:
            response = await self._transport.unary(
                RETRIEVE_RELEVANT_CHUNKS_METHOD,
                request,
                replay_safe=True,
                response_type=proto.RetrieveRelevantChunksResponse,
                expected_epoch=lease.epoch,
            )
        return _decode_response(response, limit)
