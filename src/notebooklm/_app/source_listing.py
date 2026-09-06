"""Transport-neutral ``source list`` fetch business logic.

This is the Click-free core of ``cli/services/source_listing.py``: it owns the
one piece of genuine business logic in the source-list path — **which sources
to fetch** given an optional label filter. Everything else in the list path
(the JSON-envelope assembly, the Rich-table column/row shape, the
``source_summary_payload`` serializer) is presentation that stays in the CLI
adapter's ``ListSpec`` + ``prepare_list`` pipeline.

The label ``<id|name>`` resolver is **injected** as a callable
(``label_resolver``) so this module never imports the Click-coupled
``cli.services.label_listing.resolve_label_id`` (which reaches into
``cli.resolve``); the CLI adapter supplies the live resolver and keeps its
resolution behaviour + error contract.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..types import Source, source_status_to_str

if TYPE_CHECKING:
    from ..client import NotebookLMClient

#: Resolves a label ``<id|name>`` token to a full label id. The CLI adapter
#: supplies its ``cli.services.label_listing.resolve_label_id``-backed
#: implementation (which raises its own typed ``LabelResolutionError``).
LabelResolver = Callable[..., Awaitable[str]]


async def fetch_sources(
    client: NotebookLMClient,
    notebook_id: str,
    *,
    label_filter: str | None,
    label_resolver: LabelResolver,
    status_filter: str | None = None,
) -> list[Source]:
    """Fetch the notebook's sources, optionally restricted by label and/or status.

    When ``label_filter`` is set, the injected ``label_resolver`` resolves the
    ``<id|name>`` token and the group's members are fetched via
    ``client.labels.sources()`` (a single ``sources.list()`` join) — so the
    filtered set is fetched once and the caller's ``count``/rows match the
    filtered set with no post-filter desync. Otherwise the full notebook source
    list is returned.

    ``status_filter`` is a ``source_status_to_str`` label (``ready``,
    ``processing``, ``error``, ``preparing``, ``unknown``) and is applied here,
    inside the fetch, for the same count/rows reason. It composes with
    ``label_filter``: both narrow the same set.

    ``preparing`` is the one worth naming (#2138). A file add that fails after
    its source row is registered deliberately leaves that row in place rather
    than deleting it — the row is the evidence — but it sits at ``PREPARING``,
    not ``ERROR``, so a caller looking for the wreckage under ``error`` finds
    nothing and the row stays invisible while still counting against the
    notebook's source quota. Filtering on ``preparing`` is the reconciliation
    query: "what did my failed adds leave behind?"

    """
    if label_filter is not None:
        label_id = await label_resolver(client, notebook_id, label_filter)
        # ``labels.sources()`` returns the group's members (joined from a single
        # ``sources.list()``), so the filtered set is fetched once.
        sources = await client.labels.sources(notebook_id, label_id)
    else:
        sources = await client.sources.list(notebook_id)
    if status_filter is not None:
        # Compare on the same rendered label the rows themselves show, so what a
        # caller reads in the Status column is exactly what they can filter on.
        sources = [src for src in sources if source_status_to_str(src.status) == status_filter]
    return sources


__all__ = ["LabelResolver", "fetch_sources"]
