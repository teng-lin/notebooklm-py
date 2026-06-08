"""Service for the ``label`` CLI group — the members→titles list join.

ADR-0008: this is a ``cli/services`` module, so it is boundary-clean (no Click
imports, no ``..rendering`` / ``..error_handler`` / ``..runtime`` imports, and
it never writes to stdout). It returns a :class:`~notebooklm.cli.services.listing.ListRender`
for the command layer to render.

The composite ``<id|name>`` resolver (:func:`resolve_label_id`) and the typed
:class:`LabelResolutionError` now live in the transport-neutral
:mod:`notebooklm._app.labels`; this module **re-exports** them so existing
``from .services.label_listing import resolve_label_id`` imports (the command
layer + ``tests/unit/cli/test_label_listing.py``) keep resolving. Only the
members→titles JOIN stays here because it depends on the CLI ``listing``
presentation pipeline (``ListRender`` / ``prepare_list``).

The join: one ``labels.list()`` plus one ``sources.list()`` build the
``{source_id: title}`` map, so each label's members carry resolved titles
without an N+1 fan-out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..._app.labels import LabelResolutionError, resolve_label_id
from ...types import Label, Source
from .listing import ListRender, ListSpec, prepare_list

if TYPE_CHECKING:
    from ...client import NotebookLMClient


@dataclass(frozen=True)
class LabelListPlan:
    """Prepared inputs for :func:`execute_label_list`."""

    notebook_id: str
    json_output: bool
    limit: int | None
    no_truncate: bool


def _label_serialize(label: Label, titles: dict[str, str | None]) -> dict[str, Any]:
    """Serialize a label with its members joined to resolved source titles."""
    return {
        "id": label.id,
        "name": label.name,
        "emoji": label.emoji,
        "source_ids": list(label.source_ids),
        # Include EVERY member id (title=None for any source missing from the
        # notebook list — a benign concurrent-delete race) so ``sources`` stays
        # 1:1 with ``source_ids`` and a consumer can rely on equal lengths.
        "sources": [{"id": sid, "title": titles.get(sid)} for sid in label.source_ids],
    }


async def execute_label_list(client: NotebookLMClient, plan: LabelListPlan) -> ListRender[Label]:
    """Fetch + assemble the ``label list`` render payload.

    One ``labels.list()`` + one ``sources.list()`` (the title join) — no N+1.
    """
    sources: list[Source] = await client.sources.list(plan.notebook_id)
    titles: dict[str, str | None] = {source.id: source.title for source in sources}

    async def fetch(_client: NotebookLMClient, notebook_id: str) -> list[Label]:
        return await _client.labels.list(notebook_id)

    spec = ListSpec[Label](
        title="Labels in {notebook_id}",
        items_key="labels",
        fetch=fetch,
        serialize=lambda label: _label_serialize(label, titles),
        columns=["ID", "Emoji", "Name", "Sources"],
        row=lambda label: [
            label.id,
            label.emoji or "-",
            label.name,
            str(len(label.source_ids)),
        ],
        include_index=False,
        empty_message="[yellow]No labels found[/yellow]",
    )
    return await prepare_list(
        spec,
        client,
        notebook_id=plan.notebook_id,
        limit=plan.limit,
        json_output=plan.json_output,
        no_truncate=plan.no_truncate,
    )


__all__ = [
    "LabelListPlan",
    "LabelResolutionError",
    "execute_label_list",
    "resolve_label_id",
]
