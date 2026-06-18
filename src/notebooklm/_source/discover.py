"""Private source-discovery service backing :meth:`SourcesAPI.discover`.

Wraps the synchronous ``DiscoverSources`` (``Es3dTe``) RPC: given a topic
query and notebook id, Google returns ~10 candidate web sources in a single
call *without adding them*. This is distinct from the async
DiscoverSourcesManifold/Async "research run" pipeline that ``client.research``
wraps.
"""

from __future__ import annotations

import logging
from typing import Any

from .._row_adapters.discover import DiscoveredSourceRow, DiscoverResultRow
from .._runtime.contracts import RpcCaller
from .._types.research import DiscoveredSource
from ..rpc import RPCMethod

logger = logging.getLogger("notebooklm").getChild("_sources")

# Corpus selector -> wire ``mode`` int. Web is the only live-verified value for
# v1; the map is the documented extension point for Drive/other corpus types.
# (The Web UI's bundle builder maps a selector b: 1->2, 2->3, 3->4, default->1,
# but a direct ``1`` is what live-returns web sources, so that is what we send.)
DISCOVER_MODE_WEB = 1
_SOURCE_MODES: dict[str, int] = {"web": DISCOVER_MODE_WEB}


def resolve_discover_mode(source: str) -> int:
    """Map a public ``source`` selector to the wire ``mode`` int.

    Only ``"web"`` is supported for v1; an unknown selector raises so a typo
    fails loudly instead of silently discovering web sources.
    """
    try:
        return _SOURCE_MODES[source.strip().lower()]
    except (KeyError, AttributeError):
        supported = ", ".join(sorted(_SOURCE_MODES))
        raise ValueError(f"Invalid source {source!r}. Supported: {supported}.") from None


def build_discover_sources_params(notebook_id: str, query: str, *, mode: int) -> list[Any]:
    """Build the ``DISCOVER_SOURCES`` (``Es3dTe``) request param array.

    Live-verified wire shape: ``[[query], None, mode, notebook_id]``.

    Args:
        notebook_id: The notebook ID (raw UUID string).
        query: The discovery topic/query string.
        mode: Corpus-type int (``1`` = web).
    """
    return [[query], None, mode, notebook_id]


def parse_discovered_sources(result: Any) -> list[DiscoveredSource]:
    """Parse a ``DISCOVER_SOURCES`` response into typed candidate sources.

    Live-verified envelope::

        [ [[url, title, why_relevant, type], ...], "<summary>", ["<run-id>"] ]

    Rows without a usable URL are skipped (a thin/over-trimmed row degrades
    gracefully). A structurally missing candidate slot RAISES via the row
    adapter's ``safe_index`` descent (genuine drift, not an empty result).

    Only a ``None`` envelope (``allow_null`` null result) short-circuits to
    ``[]``; an empty-but-shaped ``[]`` still descends through the row adapter so
    schema drift is detected instead of being silently swallowed as "no
    candidates".
    """
    if result is None:
        return []
    discovered: list[DiscoveredSource] = []
    for raw_row in DiscoverResultRow(result).candidate_rows:
        row = DiscoveredSourceRow(raw_row)
        if not row.is_well_formed:
            continue
        discovered.append(
            DiscoveredSource(
                url=row.url,
                title=row.title,
                why_relevant=row.why_relevant,
                source_type=row.source_type,
            )
        )
    return discovered


class SourceDiscoverer:
    """Discover candidate sources by topic via the synchronous ``Es3dTe`` RPC."""

    def __init__(self, rpc: RpcCaller) -> None:
        self._rpc = rpc

    async def discover(
        self,
        notebook_id: str,
        query: str,
        *,
        source: str = "web",
    ) -> list[DiscoveredSource]:
        """Return candidate sources for ``query`` without adding them."""
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string")
        mode = resolve_discover_mode(source)
        logger.debug(
            "Discovering sources in notebook %s: %s",
            notebook_id,
            query[:50],
        )
        result = await self._rpc.rpc_call(
            RPCMethod.DISCOVER_SOURCES,
            build_discover_sources_params(notebook_id, query, mode=mode),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return parse_discovered_sources(result)
