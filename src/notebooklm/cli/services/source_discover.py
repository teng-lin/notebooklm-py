"""CLI adapter for ``source discover`` — fetch + prepare candidate sources.

The ``DiscoverSources`` (``Es3dTe``) RPC returns ~10 candidate web sources for
a topic *without adding them*. This adapter owns the presentation half: it
builds a :class:`~notebooklm.cli.services.listing.ListSpec` over the
:class:`~notebooklm.types.DiscoveredSource` results and drives the shared
:func:`~notebooklm.cli.services.listing.prepare_list` pipeline. Actual JSON /
Rich rendering stays in the command layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...types import DiscoveredSource
from .listing import ListRender, ListSpec, prepare_list

if TYPE_CHECKING:
    from ...client import NotebookLMClient


@dataclass(frozen=True)
class SourceDiscoverPlan:
    """Prepared inputs for ``execute_source_discover``."""

    notebook_id: str
    query: str
    search_source: str
    json_output: bool
    limit: int | None
    no_truncate: bool


def _truncate(value: str, width: int = 60) -> str:
    return value if len(value) <= width else value[: width - 1] + "…"


def _build_spec(query: str, search_source: str) -> ListSpec[DiscoveredSource]:
    async def envelope_extras(client: NotebookLMClient, notebook_id: str) -> dict[str, str | None]:
        return {"notebook_id": notebook_id, "query": query}

    async def fetch(client: NotebookLMClient, notebook_id: str) -> list[DiscoveredSource]:
        return await client.sources.discover(notebook_id, query, source=search_source)

    return ListSpec(
        title="Discovered sources for {notebook_id}",
        items_key="sources",
        fetch=fetch,
        serialize=lambda src: src.to_public_dict(),
        columns=["Title", "URL", "Why relevant"],
        row=lambda src: [
            _truncate(src.title or "-"),
            src.url,
            _truncate(src.why_relevant or "-"),
        ],
        envelope_extras=envelope_extras,
    )


async def execute_source_discover(
    client: NotebookLMClient, plan: SourceDiscoverPlan
) -> ListRender[DiscoveredSource]:
    """Fetch and prepare the discovered-sources render payload."""
    spec = _build_spec(plan.query, plan.search_source)
    return await prepare_list(
        spec,
        client,
        notebook_id=plan.notebook_id,
        limit=plan.limit,
        json_output=plan.json_output,
        no_truncate=plan.no_truncate,
    )


__all__ = ["SourceDiscoverPlan", "execute_source_discover"]
