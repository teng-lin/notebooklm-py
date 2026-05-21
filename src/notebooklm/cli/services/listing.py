"""Shared list-command pipeline for CLI resources.

``ListSpec.envelope_extras`` is the per-command hook for JSON envelope fields
that do not belong to the entity array itself. For example, source and artifact
lists return extras like ``{"notebook_id": "...", "notebook_title": "..."}``.
``run_list`` merges the returned dict at the top level WITHOUT validation,
before adding the entity list and ``count`` keys. Future list commands should
treat this docstring as the canonical contract for command-specific envelope
fields.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from rich.table import Table

from ...client import NotebookLMClient
from ..rendering import console, json_output_response

T = TypeVar("T")

EnvelopeExtras = Callable[[NotebookLMClient, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ListSpec(Generic[T]):
    """Command-specific configuration for :func:`run_list`."""

    title: str
    items_key: str
    fetch: Callable[[NotebookLMClient, str], Awaitable[list[T]]]
    serialize: Callable[[T], dict[str, Any]]
    columns: list[str]
    row: Callable[[T], list[str]]
    envelope_extras: EnvelopeExtras | None = None
    column_options: dict[str, dict[str, Any]] | None = None
    include_index: bool = True
    empty_message: str | None = None


@dataclass(frozen=True)
class ListResult(Generic[T]):
    """Rendered list result returned for focused tests and future composition."""

    items: list[T]
    envelope: dict[str, Any] | None = None


def _table_title(title: str, notebook_id: str) -> str:
    """Format a table title with the resolved notebook id."""
    return title.format(notebook_id=notebook_id)


def _column_options(header: str, *, no_truncate: bool) -> dict[str, Any]:
    """Return the default Rich column options for common list-table headers."""
    title_overflow: Literal["fold", "ellipsis"] = "fold" if no_truncate else "ellipsis"
    if header == "ID":
        return {"style": "cyan"}
    if header == "Title":
        return {"style": "green", "overflow": title_overflow}
    if header == "Created":
        return {"style": "dim"}
    if header == "Status":
        return {"style": "yellow"}
    if header == "Preview":
        return {"style": "dim", "max_width": 50}
    return {}


def _serialize_items(spec: ListSpec[T], items: list[T]) -> list[dict[str, Any]]:
    """Serialize fetched items and inject 1-based indexes when configured."""
    serialized: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        payload = spec.serialize(item)
        if spec.include_index:
            payload = {"index": index, **payload}
        serialized.append(payload)
    return serialized


async def run_list(
    spec: ListSpec[T],
    client: NotebookLMClient,
    *,
    notebook_id: str,
    limit: int | None,
    json_output: bool,
    no_truncate: bool = False,
) -> ListResult[T]:
    """Fetch and render a list command in text or JSON mode."""
    items = await spec.fetch(client, notebook_id)
    if limit is not None and limit >= 0:
        items = items[:limit]

    if json_output:
        extras: dict[str, Any] = {}
        if spec.envelope_extras is not None:
            extras = await spec.envelope_extras(client, notebook_id)
        serialized = _serialize_items(spec, items)
        envelope = {**extras, spec.items_key: serialized, "count": len(serialized)}
        json_output_response(envelope)
        return ListResult(items=items, envelope=envelope)

    if not items and spec.empty_message is not None:
        console.print(spec.empty_message)
        return ListResult(items=items)

    table = Table(title=_table_title(spec.title, notebook_id))
    for header in spec.columns:
        options = _column_options(header, no_truncate=no_truncate)
        if spec.column_options and header in spec.column_options:
            options.update(spec.column_options[header])
        table.add_column(header, **options)
    for item in items:
        table.add_row(*spec.row(item))
    console.print(table)
    return ListResult(items=items)


__all__ = ["ListResult", "ListSpec", "run_list"]
