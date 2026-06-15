"""Private source listing service."""

from __future__ import annotations

import builtins
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .._row_adapters.sources import SourceRow
from .._runtime.contracts import RpcCaller
from ..rpc import RPCError, RPCMethod, safe_index
from ..types import Source
from .upload_payloads import build_template_block

# Keep source-list warnings on the historical logger so existing log filters
# continue to see the same channel after the service extraction.
logger = logging.getLogger("notebooklm").getChild("_sources")


SourceListHook = Callable[[str], Awaitable[builtins.list[Source]]]


class SourceLister:
    """List and parse notebook sources from GET_NOTEBOOK responses."""

    def __init__(self, rpc: RpcCaller) -> None:
        self._rpc = rpc

    async def list(self, notebook_id: str, *, strict: bool = False) -> builtins.list[Source]:
        """List all sources in a notebook.

        A malformed or error-shaped ``GET_NOTEBOOK`` response raises
        :class:`RPCError`. This prevents a drifted response from being
        silently reported as "0 sources" — see issue #1159. The legacy
        ``NOTEBOOKLM_STRICT_DECODE=0`` opt-out into warn-and-return-``[]``
        was retired in v0.7.0; strict decoding is now the only mode.
        """
        # GET_NOTEBOOK read-path tail migrated to the nested template block
        # (#1549; live-verified forward-compatible). Mirrors
        # ``_notebooks.build_get_notebook_params`` — inlined here because
        # importing ``_notebooks`` from this module would cycle (``_notebooks``
        # imports ``_source.upload_payloads``, which runs ``_source/__init__``).
        params = [notebook_id, None, build_template_block(), None, 0]
        notebook = await self._rpc.rpc_call(
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

        # ``notebook`` is a non-empty list here (the guard above raises
        # otherwise), so this ``[0]`` descent is a no-op on the happy path;
        # routed through ``safe_index`` to keep the envelope position out of the
        # raw ``name[int]`` shape while still failing loud if the envelope ever
        # loses its leading slot.
        nb_info = safe_index(
            notebook,
            0,
            method_id=RPCMethod.GET_NOTEBOOK.value,
            source="SourceLister.list",
        )
        if not isinstance(nb_info, builtins.list) or len(nb_info) <= 1:
            return self._handle_malformed_list_response(
                notebook_id,
                "Unexpected notebook structure for %s: expected list with sources at index 1 "
                "(API structure may have changed)",
                strict=strict,
            )

        # ``nb_info`` has length > 1 here (guard above), so the ``[1]`` sources
        # slot is always present; ``safe_index`` keeps the read off the raw
        # ``name[int]`` shape.
        sources_list = safe_index(
            nb_info,
            1,
            method_id=RPCMethod.GET_NOTEBOOK.value,
            source="SourceLister.list",
        )
        if sources_list is None:
            # A genuinely empty notebook elides the sources slot (``None``
            # instead of an empty list). This is a valid empty state, NOT a
            # malformed response, so return ``[]`` without raising even under
            # strict-decode — issue #1159 reserves the empty list for the
            # genuinely-empty case (see tests/cassettes/notebook_zero_sources.yaml).
            return []
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
        # Always emit the drift WARNING first so log searches and monitoring
        # on the historical "SourcesAPI.list:" prefix keep firing regardless
        # of whether we go on to raise — preserving the diagnostic breadcrumb
        # in strict mode too.
        logger.warning("SourcesAPI.list: " + message, notebook_id, *log_args)
        # Strict decoding is the only mode (the ``NOTEBOOKLM_STRICT_DECODE=0``
        # soft-mode opt-out was retired in v0.7.0), so a drifted or
        # error-enveloped GET_NOTEBOOK response is always surfaced as an error
        # rather than silently reported as "0 sources" (issue #1159). The
        # explicit ``strict`` flag is retained for call-site clarity.
        raise RPCError(f"Could not list sources for {notebook_id}: {error_detail}")

    @staticmethod
    def _parse_source(src: Any) -> Source | None:
        if not isinstance(src, builtins.list) or len(src) == 0:
            return None

        # GET_NOTEBOOK source-list entries arrive in the "entry" layout
        # (``[[id], title, metadata, status_block, ...]`` after the
        # envelope walk above) so we hand them directly to
        # ``SourceRow.from_entry`` and let the adapter handle all
        # positional knowledge — id-envelope variants (plain, drive-
        # backed), metadata url precedence, status decoding, etc.
        row = SourceRow.from_entry(src, method_id=RPCMethod.GET_NOTEBOOK.value)
        if not row.has_id:
            logger.warning(
                "SourcesAPI.list: Skipping source with unexpected id shape: %s",
                repr(src)[:500],
            )
            return None

        # Funnel through the single ``Source`` construction site shared
        # with ``Source.from_api_response`` so the list/get/poll path and
        # the ADD_SOURCE/rename path produce identical Sources.
        return Source.from_row(row)


__all__ = ["SourceLister"]
