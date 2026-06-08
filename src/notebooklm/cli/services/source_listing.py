"""CLI adapter for ``source list`` — fetch + prepare the source list payload.

The one piece of genuine business logic in this path — **which sources to
fetch** under an optional label filter — now lives in the transport-neutral
:func:`notebooklm._app.source_listing.fetch_sources`. This module is the
CLI-side adapter that owns the presentation half:

* builds the :class:`~notebooklm.cli.services.listing.ListSpec` (the JSON
  ``serialize`` shape, the Rich-table ``columns`` / ``row`` shape, and the
  ``envelope_extras``), and
* drives the shared :func:`~notebooklm.cli.services.listing.prepare_list`
  pipeline.

The neutral fetch core takes the label ``<id|name>`` resolver as an injected
callable; this adapter passes its own :func:`resolve_label_id` (read off this
module at call time, so a ``monkeypatch.setattr`` against it still lands).
Actual JSON / Rich rendering stays in the command layer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..._app.source_listing import fetch_sources
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
    pipeline. The ``fetch`` closure delegates to the neutral
    :func:`notebooklm._app.source_listing.fetch_sources`, injecting this
    module's :func:`resolve_label_id` as the label resolver — so the
    ``label_filter`` set is fetched (and counted/sliced) before
    ``prepare_list`` runs.
    """

    async def envelope_extras(client: NotebookLMClient, notebook_id: str) -> dict[str, str | None]:
        nb = await client.notebooks.get(notebook_id)
        return {"notebook_id": notebook_id, "notebook_title": nb.title if nb else None}

    async def fetch(client: NotebookLMClient, notebook_id: str) -> list[Source]:
        return await fetch_sources(
            client,
            notebook_id,
            label_filter=label_filter,
            label_resolver=resolve_label_id,
            json_output=json_output,
        )

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
