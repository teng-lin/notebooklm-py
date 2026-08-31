"""Private source listing service."""

from __future__ import annotations

import builtins
import logging
from collections.abc import Awaitable, Callable, Collection
from typing import Any, TypeVar

from ..._types.enums import SourceStatus
from ...rpc import RPCError, RPCMethod, safe_index
from ...types import Source, SourceType
from ..contracts import RpcCaller
from ..params.sources import build_template_block
from ..rows.sources import SourceRow

# Keep source-list warnings on the historical logger so existing log filters
# continue to see the same channel after the service extraction.
logger = logging.getLogger("notebooklm").getChild("_sources")


SourceListHook = Callable[[str], Awaitable[builtins.list[Source]]]
_FilterValue = TypeVar("_FilterValue")


def _snapshot_enum_filter(
    values: Collection[_FilterValue] | None,
    *,
    enum_type: type[_FilterValue],
    parameter: str,
) -> frozenset[_FilterValue] | None:
    """Validate and snapshot one public source-list filter before I/O."""
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, Collection):
        raise TypeError(f"{parameter} must be a collection of {enum_type.__name__} values")

    snapshot = tuple(values)
    for value in snapshot:
        if not isinstance(value, enum_type):
            raise TypeError(f"{parameter} must contain only {enum_type.__name__} values")
    return frozenset(snapshot)


class SourceLister:
    """List and parse notebook sources from GET_NOTEBOOK responses."""

    def __init__(self, rpc: RpcCaller) -> None:
        self._rpc = rpc

    async def list(
        self,
        notebook_id: str,
        *,
        strict: bool = False,
        statuses: Collection[SourceStatus] | None = None,
        types: Collection[SourceType] | None = None,
    ) -> builtins.list[Source]:
        """List all sources in a notebook.

        A malformed or error-shaped ``GET_NOTEBOOK`` response raises
        :class:`RPCError`. This prevents a drifted response from being
        silently reported as "0 sources" — see issue #1159. The legacy
        ``NOTEBOOKLM_STRICT_DECODE=0`` opt-out into warn-and-return-``[]``
        was retired in v0.7.0; strict decoding is now the only mode.
        ``strict=True`` additionally rejects malformed source rows and
        conflicting duplicate IDs instead of skipping/deduplicating them.
        Filters are applied after normalization: members are ORed within one
        filter and the status/type filters are ANDed together.
        """
        status_filter = _snapshot_enum_filter(
            statuses,
            enum_type=SourceStatus,
            parameter="statuses",
        )
        type_filter = _snapshot_enum_filter(
            types,
            enum_type=SourceType,
            parameter="types",
        )

        # GET_NOTEBOOK read-path tail migrated to the nested template block
        # (#1549; live-verified forward-compatible). Mirrors
        # ``_web.params.notebooks.build_get_notebook_params`` — inlined here to
        # avoid an ownership cycle because that web builder itself depends on
        # ``_web.params.sources``.
        params = [notebook_id, None, build_template_block(), None, 0]
        notebook = await self._rpc.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        sources_list = self._extract_sources_list(notebook_id, notebook, strict=strict)
        if sources_list is None:
            return []

        # Dedup by resolved id, keeping the FIRST occurrence (#1919). The
        # backend can surface the same id-bearing source in ``nb_info[1]`` more
        # than once — research imports re-emit a URL, and ghost/probe rows can
        # echo an existing id — which would otherwise over-count both
        # ``source_list`` and ``metadata.sources``. A collision is a benign
        # backend artifact, so it logs at DEBUG rather than WARNING.
        seen_sources: dict[str, Source] = {}
        sources: builtins.list[Source] = []
        for index, src in enumerate(sources_list):
            source = self._parse_source(
                src,
                notebook_id=notebook_id,
                index=index,
                strict=strict,
            )
            if source is None:
                continue
            previous = seen_sources.get(source.id)
            if previous is not None:
                if strict and source != previous:
                    raise RPCError(
                        f"Could not list sources for {notebook_id}: "
                        f"conflicting duplicate source row at index {index}"
                    )
                logger.debug("SourcesAPI.list: Skipping duplicate source id %s", source.id)
                continue
            seen_sources[source.id] = source
            sources.append(source)

        return [
            source
            for source in sources
            if (status_filter is None or source.status in status_filter)
            and (type_filter is None or source.kind in type_filter)
        ]

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
            # genuinely-empty case (see tests/cassettes/web/notebook_zero_sources.yaml).
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
    def _parse_source(
        src: Any,
        *,
        notebook_id: str,
        index: int,
        strict: bool,
    ) -> Source | None:
        if not isinstance(src, builtins.list) or len(src) == 0:
            if strict:
                raise RPCError(
                    f"Could not list sources for {notebook_id}: "
                    f"malformed source row at index {index}"
                )
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
            if strict:
                raise RPCError(
                    f"Could not list sources for {notebook_id}: "
                    f"source row at index {index} has no usable id"
                )
            return None

        if strict and (shape_error := row.listing_shape_error()) is not None:
            raise RPCError(
                f"Could not list sources for {notebook_id}: "
                f"incomplete source row at index {index} ({shape_error})"
            )

        # Funnel through the single ``Source`` construction site shared
        # with ``Source.from_api_response`` so the list/get/poll path and
        # the ADD_SOURCE/rename path produce identical Sources.
        return Source.from_row(row)


__all__ = ["SourceLister"]
