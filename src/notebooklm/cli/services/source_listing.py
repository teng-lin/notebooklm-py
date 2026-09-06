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
from ...types import Source, SourceType, drive_source_status_to_str, source_status_to_str
from .label_listing import resolve_label_id
from .listing import ListRender, ListSpec, prepare_list
from .source_serializers import source_row_payload

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
    # When set, restrict to sources whose rendered status label matches. Applied
    # in the same closure, for the same count/rows reason. ``preparing`` is the
    # reconciliation query for rows a failed add left behind (#2138).
    status_filter: str | None = None


def _status_cell(src: Source) -> str:
    """Render the table's Status cell, flagging a degraded Drive source.

    The Status column reports NotebookLM's *ingestion* pipeline, which stays
    ``ready`` after the underlying Drive file is deleted or unshared — so for
    those rows the bare label is actively misleading (#2111). Append the Drive
    verdict there rather than adding a Drive column: a column would be blank on
    every non-Drive row (the overwhelming majority) and would widen the table on
    **every** listing, whereas this costs nothing until a row is actually
    degraded. The trigger is the degraded verdict alone — a ``processing`` or
    ``error`` row whose Drive file is gone is annotated too, since the ingestion
    status is not what the Drive axis reports on.

    Be precise about "costs nothing", because Rich sizes columns table-wide:
    the *cell text* of healthy and non-Drive rows is unchanged, but when any row
    is annotated the wider Status column re-flows the whole table, so other
    rows' Title/ID can wrap or truncate. That re-flow is accepted only in the
    degraded case — a rare, actionable state where knowing the data is stale
    beats a roomier Title column. ``_build_spec`` pairs this with
    ``overflow="fold"`` on Status so the longest label
    (``gen_ai_access_denied``) wraps intact instead of ellipsizing away the very
    information the annotation exists to carry.

    The trigger is :attr:`~notebooklm.types.Source.is_drive_degraded`, so a code
    this client cannot map (``unknown``) is deliberately NOT flagged here: that
    predicate's contract is that an unnameable state is not evidence of
    degradation, and the table should not cry wolf across every row on protocol
    drift. ``source get`` and ``--json`` still report it — this is the triage
    column, not the full axis.

    ``--json`` is unaffected: it carries the full Drive axis on every row via
    :func:`~notebooklm.cli.services.source_serializers.source_row_payload`.
    """
    status = source_status_to_str(src.status)
    drive_status = src.drive_status
    # ``is_drive_degraded`` is already False when there is no Drive claim, so
    # the None test narrows the type for the label call below rather than
    # adding a second, independent condition.
    if drive_status is None or not src.is_drive_degraded:
        return status
    # Parentheses, not brackets: Rich would parse "[...]" as console markup.
    return f"{status} (drive: {drive_source_status_to_str(drive_status)})"


def _build_spec(
    source_type_display: Callable[[SourceType], str],
    *,
    label_filter: str | None = None,
    json_output: bool = False,
    status_filter: str | None = None,
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
            status_filter=status_filter,
        )

    return ListSpec(
        title="Sources in {notebook_id}",
        items_key="sources",
        fetch=fetch,
        serialize=source_row_payload,
        columns=["ID", "Title", "Type", "Created", "Status"],
        row=lambda src: [
            src.id,
            src.title or "-",
            source_type_display(src.kind),
            src.created_at.strftime("%Y-%m-%d %H:%M") if src.created_at else "-",
            _status_cell(src),
        ],
        # Fold rather than ellipsize Status: a degraded cell can reach
        # ``ready (drive: gen_ai_access_denied)``, and the default ellipsis
        # would clip it to ``gen_ai_acces…`` — dropping exactly the word that
        # makes the annotation worth showing. No effect on the common case,
        # where every label is short enough that neither policy engages.
        column_options={"Status": {"overflow": "fold"}},
        envelope_extras=envelope_extras,
    )


async def execute_source_list(client: NotebookLMClient, plan: SourceListPlan) -> ListRender[Source]:
    """Fetch and prepare the source list render payload."""
    spec = _build_spec(
        plan.source_type_display,
        label_filter=plan.label_filter,
        json_output=plan.json_output,
        status_filter=plan.status_filter,
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
