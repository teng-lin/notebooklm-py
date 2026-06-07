"""Service for ``source list`` — fetch + prepare the source list payload.

Composes :class:`~notebooklm.cli.services.listing.ListSpec` so the Click
handler in ``cli/source_cmd.py`` collapses to a one-call wrapper. The
extracted executor stays a thin facade over the shared listing pipeline.
Envelope-extras and column-row data live here, while actual JSON / Rich
rendering stays in the command layer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...types import Source, SourceType, source_status_to_str
from .label_listing import resolve_label_id
from .listing import ListRender, ListSpec, prepare_list
from .source_serializers import source_summary_payload

if TYPE_CHECKING:
    from ...client import NotebookLMClient


@dataclass(frozen=True)
class SourceListPlan:
    """Prepared inputs for ``execute_source_list``."""

    notebook_id: str
    json_output: bool
    limit: int | None
    no_truncate: bool
    source_type_display: Callable[[SourceType], str]
    # When set, restrict the listing to the sources in this label (id or name).
    # The filter is applied INSIDE the fetch closure so ``prepare_list``'s
    # ``count``/rows match the filtered set (no post-filter desync).
    label_filter: str | None = None


def _build_spec(
    source_type_display: Callable[[SourceType], str],
    *,
    label_filter: str | None = None,
    json_output: bool = False,
) -> ListSpec[Source]:
    """Build the ``ListSpec`` for ``source list``.

    Factored out of ``execute_source_list`` so unit tests can introspect
    the column / serialize shape directly without running the full
    pipeline. When ``label_filter`` is set, the ``fetch`` closure resolves the
    label ``<id|name>`` and returns ``client.labels.sources()`` (the group's
    members) instead of the full notebook source list — so the filter is applied
    before ``prepare_list`` counts/slices.
    """

    async def envelope_extras(client: NotebookLMClient, notebook_id: str) -> dict[str, str | None]:
        nb = await client.notebooks.get(notebook_id)
        return {"notebook_id": notebook_id, "notebook_title": nb.title if nb else None}

    async def fetch(client: NotebookLMClient, notebook_id: str) -> list[Source]:
        if label_filter is not None:
            label_id = await resolve_label_id(
                client, notebook_id, label_filter, json_output=json_output
            )
            # ``labels.sources()`` returns the group's members (joined from a
            # single ``sources.list()``), so the filtered set is fetched once.
            return await client.labels.sources(notebook_id, label_id)
        return await client.sources.list(notebook_id)

    return ListSpec(
        title="Sources in {notebook_id}",
        items_key="sources",
        fetch=fetch,
        serialize=lambda src: {
            **source_summary_payload(src),
            "status": source_status_to_str(src.status),
            "status_id": src.status,
            "created_at": src.created_at.isoformat() if src.created_at else None,
        },
        columns=["ID", "Title", "Type", "Created", "Status"],
        row=lambda src: [
            src.id,
            src.title or "-",
            source_type_display(src.kind),
            src.created_at.strftime("%Y-%m-%d %H:%M") if src.created_at else "-",
            source_status_to_str(src.status),
        ],
        envelope_extras=envelope_extras,
    )


async def execute_source_list(client: NotebookLMClient, plan: SourceListPlan) -> ListRender[Source]:
    """Fetch and prepare the source list render payload."""
    spec = _build_spec(
        plan.source_type_display,
        label_filter=plan.label_filter,
        json_output=plan.json_output,
    )
    return await prepare_list(
        spec,
        client,
        notebook_id=plan.notebook_id,
        limit=plan.limit,
        json_output=plan.json_output,
        no_truncate=plan.no_truncate,
    )


__all__ = ["SourceListPlan", "execute_source_list"]
