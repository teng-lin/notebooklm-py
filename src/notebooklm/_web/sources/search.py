"""Web ``RetrieveRelevantChunks`` source-search service."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ..._sources import finalize_search_results
from ...rpc import RPCMethod
from ...types import RelevantChunk
from ..contracts import RpcCaller
from ..params.sources import build_retrieve_relevant_chunks_params
from ..rows.chunks import decode_relevant_chunks


class SourceSearchService:
    """Dispatch and decode the ranked source-passage read."""

    def __init__(self, rpc: RpcCaller, *, logger: logging.Logger) -> None:
        self._rpc = rpc
        self._logger = logger

    async def search(
        self,
        notebook_id: str,
        query: str,
        *,
        source_ids: Sequence[str],
        limit: int | None,
    ) -> list[RelevantChunk]:
        result = await self._rpc.rpc_call(
            RPCMethod.RETRIEVE_RELEVANT_CHUNKS,
            build_retrieve_relevant_chunks_params(notebook_id, query, tuple(source_ids)),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            raise_on_null_status=True,
        )
        chunks = decode_relevant_chunks(
            result,
            method_id=RPCMethod.RETRIEVE_RELEVANT_CHUNKS.value,
            logger=self._logger,
        )
        return finalize_search_results(chunks, limit)
