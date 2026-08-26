"""Private source listing service."""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import TypeVar

from .._semantic.projectors import project_source
from .._semantic.records import SourceRecord
from ..rpc.types import SourceStatus
from ..types import Source, SourceType

SourceListHook = Callable[[str], Awaitable[builtins.list[Source]]]
FetchSourceSnapshot = Callable[[str, bool], Awaitable[Sequence[SourceRecord]]]
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
    """Compatibility helper over an injected source-snapshot fetch callback."""

    def __init__(self, fetch_snapshot: FetchSourceSnapshot) -> None:
        self._fetch_snapshot = fetch_snapshot

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

        snapshot = await self._fetch_snapshot(notebook_id, strict)
        sources = [project_source(record) for record in snapshot]
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
        sources = (
            await self.list(notebook_id)
            if list_sources is None
            else await list_sources(notebook_id)
        )
        for source in sources:
            if source.id == source_id:
                return source
        return None


__all__ = ["SourceLister"]
