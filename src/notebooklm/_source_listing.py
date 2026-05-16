"""Private source listing service."""

from __future__ import annotations

import builtins
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .rpc import RPCError, RPCMethod
from .rpc.types import SourceStatus
from .types import Source, _extract_source_created_at, _extract_source_url

# Keep source-list warnings on the historical logger so existing log filters
# continue to see the same channel after the service extraction.
logger = logging.getLogger("notebooklm").getChild("_sources")


class RpcCall(Protocol):
    async def __call__(
        self,
        method: RPCMethod,
        params: builtins.list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
    ) -> Any:
        """Call a NotebookLM RPC method."""


SourceListHook = Callable[[str], Awaitable[builtins.list[Source]]]


class SourceLister:
    """List and parse notebook sources from GET_NOTEBOOK responses."""

    def __init__(self, rpc_call: RpcCall) -> None:
        self._rpc_call = rpc_call

    async def list(self, notebook_id: str, *, strict: bool = False) -> builtins.list[Source]:
        """List all sources in a notebook."""
        params = [notebook_id, None, [2], None, 0]
        notebook = await self._rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        sources_list = self._extract_sources_list(notebook_id, notebook, strict=strict)
        if sources_list is None:
            return []

        return [source for src in sources_list if (source := self._parse_source(src)) is not None]

    async def get(
        self,
        notebook_id: str,
        source_id: str,
        *,
        list_sources: SourceListHook | None = None,
    ) -> Source | None:
        """Get source details by filtering the GET_NOTEBOOK source list."""
        if list_sources is None:
            sources = await self.list(notebook_id)
        else:
            sources = await list_sources(notebook_id)
        for source in sources:
            if source.id == source_id:
                return source
        return None

    def _extract_sources_list(
        self,
        notebook_id: str,
        notebook: Any,
        *,
        strict: bool,
    ) -> builtins.list[Any] | None:
        if not notebook or not isinstance(notebook, builtins.list):
            return self._handle_malformed_list_response(
                notebook_id,
                "Empty or invalid notebook response when listing sources for %s "
                "(API response structure may have changed)",
                strict=strict,
            )

        nb_info = notebook[0]
        if not isinstance(nb_info, builtins.list) or len(nb_info) <= 1:
            return self._handle_malformed_list_response(
                notebook_id,
                "Unexpected notebook structure for %s: expected list with sources at index 1 "
                "(API structure may have changed)",
                strict=strict,
            )

        sources_list = nb_info[1]
        if not isinstance(sources_list, builtins.list):
            return self._handle_malformed_list_response(
                notebook_id,
                "Sources data for %s is not a list (type=%s), returning empty list "
                "(API structure may have changed)",
                type(sources_list).__name__,
                strict=strict,
                error_detail=f"sources data is {type(sources_list).__name__}, not list",
            )

        return sources_list

    @staticmethod
    def _handle_malformed_list_response(
        notebook_id: str,
        message: str,
        *log_args: object,
        strict: bool,
        error_detail: str = "API response structure changed",
    ) -> None:
        # Preserve the historical message prefix so log searches on
        # "SourcesAPI.list:" continue to match after the service extraction.
        logger.warning("SourcesAPI.list: " + message, notebook_id, *log_args)
        if strict:
            raise RPCError(f"Could not list sources for {notebook_id}: {error_detail}")

    @staticmethod
    def _parse_source(src: Any) -> Source | None:
        if not isinstance(src, builtins.list) or len(src) == 0:
            return None

        src_id = SourceLister._extract_source_id(src)
        if src_id is None:
            logger.warning(
                "SourcesAPI.list: Skipping source with unexpected id shape: %s",
                repr(src)[:500],
            )
            return None

        title = src[1] if len(src) > 1 else None
        metadata = src[2] if len(src) > 2 else None

        # GET_NOTEBOOK source entries use the same medium-nested metadata
        # shape as Source.from_api_response. In this shape metadata[0] can
        # pack unrelated data, so only the shared [7] > [5] precedence applies.
        url = _extract_source_url(metadata, allow_bare_http=False)
        created_at = _extract_source_created_at(metadata)
        status = SourceLister._extract_status(src)
        type_code = SourceLister._extract_type_code(metadata)

        return Source(
            id=str(src_id),
            title=title,
            url=url,
            _type_code=type_code,
            created_at=created_at,
            status=status,
        )

    @staticmethod
    def _extract_source_id(src: builtins.list[Any]) -> object | None:
        raw_id = src[0]
        if not isinstance(raw_id, builtins.list):
            return raw_id
        if raw_id and raw_id[0] is not None:
            return raw_id[0]
        # Drive-backed entries can nest the source id inside the id envelope:
        # [None, true, [source_id]]. Keep this local to source-list parsing so
        # the public Source model remains a shape-preserving value object.
        if len(raw_id) > 2 and isinstance(raw_id[2], builtins.list) and raw_id[2]:
            return raw_id[2][0]
        return None

    @staticmethod
    def _extract_status(src: builtins.list[Any]) -> SourceStatus:
        if len(src) <= 3 or not isinstance(src[3], builtins.list) or len(src[3]) <= 1:
            return SourceStatus.READY

        status_code = src[3][1]
        if status_code in (
            SourceStatus.PROCESSING,
            SourceStatus.READY,
            SourceStatus.ERROR,
            SourceStatus.PREPARING,
        ):
            return status_code
        return SourceStatus.READY

    @staticmethod
    def _extract_type_code(metadata: Any) -> int | None:
        if (
            isinstance(metadata, builtins.list)
            and len(metadata) > 4
            and isinstance(metadata[4], int)
        ):
            return metadata[4]
        return None


__all__ = ["SourceLister"]
