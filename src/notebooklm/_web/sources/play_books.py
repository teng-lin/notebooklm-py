"""Web ``batchexecute`` Google Play Books ("Expert Intelligence") source ops (#2292).

Listing rides ``LIST_EXPERT_INTELLIGENCE_CONTENT`` (``mVtEUb``); adding a listed
title rides ``ADD_SOURCES_ASYNC`` (``X1snv``) with an ``ExpertIntelligenceContent``
spec built by :mod:`notebooklm._web.params.sources`. Both verified live end to
end on the web tier. The Android write path additionally requires a per-account
Phenotype experiment header the client cannot synthesize, so add is web-only.

Positional decode lives in :mod:`notebooklm._web.rows.play_books` (list rows) and
:func:`notebooklm._web.rows.sources.first_added_source_id` (the add response), so
this module owns only the RPC orchestration.
"""

from __future__ import annotations

import logging

from ..._types.sources import PlayBook
from ...exceptions import PlayBookNotExportableError, SourceNotFoundError
from ...rpc import RPCMethod
from ..contracts import RpcCaller
from ..params.sources import (
    build_add_sources_async_params,
    build_list_play_books_params,
    build_play_book_source_spec,
)
from ..rows.play_books import decode_play_books_response
from ..rows.sources import first_added_source_id

logger = logging.getLogger("notebooklm._sources")


class PlayBooksService:
    """Web operations backing ``sources.list_play_books`` / ``add_play_book``."""

    def __init__(self, rpc: RpcCaller) -> None:
        self._rpc = rpc

    async def list_play_books(self) -> list[PlayBook]:
        """List the account's exportable Play Books library."""
        result = await self._rpc.rpc_call(
            RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT,
            build_list_play_books_params(),
            source_path="/",
            allow_null=True,
        )
        return decode_play_books_response(result)

    async def add_play_book_spec(self, notebook_id: str, book: PlayBook) -> str:
        """Dispatch the add and return the new source id.

        Refuses an ``export_disabled`` title client-side. The caller has already
        resolved ``book`` from the library so title/description/cover/authors
        are echoed back verbatim into the spec.
        """
        if book.export_disabled:
            raise PlayBookNotExportableError(book.content_id, book.reason)
        spec = build_play_book_source_spec(
            book.content_id,
            book.title,
            book.description_html,
            book.cover_url,
            book.field_type,
            list(book.authors),
        )
        params = build_add_sources_async_params([spec], notebook_id)
        result = await self._rpc.rpc_call(
            RPCMethod.ADD_SOURCES_ASYNC,
            params,
            source_path=f"/notebook/{notebook_id}",
            disable_internal_retries=True,
            operation_variant="play_book",
        )
        source_id = first_added_source_id(result, method_id=RPCMethod.ADD_SOURCES_ASYNC.value)
        if source_id is None:
            raise SourceNotFoundError(
                book.content_id,
                method_id=RPCMethod.ADD_SOURCES_ASYNC.value,
            )
        return source_id
