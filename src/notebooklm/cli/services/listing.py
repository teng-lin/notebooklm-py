"""Shared list-command pipeline for CLI resources.

``ListSpec.envelope_extras`` is the per-command hook for JSON envelope fields
that do not belong to the entity array itself. For example, source and artifact
lists return extras like ``{"notebook_id": "...", "notebook_title": "..."}``.
The pure-data preparer :func:`prepare_list` merges the returned dict at the top
level WITHOUT validation, before adding the entity list and ``count`` keys.
Future list commands should treat this docstring as the canonical contract for
command-specific envelope fields.

This module is part of the ``cli/services`` layer per ADR-008 and is therefore
boundary-clean: no Click imports, no ``..rendering`` / ``..error_handler`` /
``..runtime`` imports, and the service does not write to stdout. It returns a
:class:`ListRender` (and the smaller :class:`ListResult`) describing what to
render. The actual rendering lives in :func:`notebooklm.cli.rendering.render_list`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from ...client import NotebookLMClient

T = TypeVar("T")

EnvelopeExtras = Callable[[NotebookLMClient, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ListSpec(Generic[T]):
    """Command-specific configuration for :func:`prepare_list`."""

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
    """Rendered list result returned for focused tests and future composition.

    Retained for backward compatibility with callers that only care about the
    items fetched and the JSON envelope (when one was built). New code should
    prefer :class:`ListRender`, which carries the full render payload.
    """

    items: list[T]
    envelope: dict[str, Any] | None = None


@dataclass(frozen=True)
class ListRender(Generic[T]):
    """Pure-data render payload returned by :func:`prepare_list`.

    Carries everything the presentation layer needs to render either a JSON
    envelope or a Rich table — but no Rich/Click imports leak into this module.

    - ``json_envelope`` is populated only in JSON mode (None otherwise).
    - ``empty_message`` is populated only in text mode when items is empty AND
      the spec supplied an ``empty_message`` (None otherwise).
    - ``columns`` / ``rows`` / ``column_options`` are populated in standard
      table mode (text mode with at least one item, or no ``empty_message``
      to substitute).
    - In empty-state text mode (``empty_message`` set on an empty result),
      the table payload fields remain at their dataclass defaults (empty
      ``columns`` / ``rows`` / ``column_options``) and the renderer prints
      only the placeholder message.
    - ``no_truncate`` mirrors the caller's preference so the renderer can pick
      the Rich overflow style.
    """

    items: list[T]
    title: str
    json_envelope: dict[str, Any] | None = None
    empty_message: str | None = None
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    column_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    no_truncate: bool = False

    def to_list_result(self) -> ListResult[T]:
        """Backward-compatibility view of the render payload."""
        return ListResult(items=self.items, envelope=self.json_envelope)


def _table_title(title: str, notebook_id: str) -> str:
    """Format a table title with the resolved notebook id."""
    return title.format(notebook_id=notebook_id)


def _serialize_items(spec: ListSpec[T], items: list[T]) -> list[dict[str, Any]]:
    """Serialize fetched items and inject 1-based indexes when configured."""
    serialized: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        payload = spec.serialize(item)
        if spec.include_index:
            payload = {"index": index, **payload}
        serialized.append(payload)
    return serialized


async def prepare_list(
    spec: ListSpec[T],
    client: NotebookLMClient,
    *,
    notebook_id: str,
    limit: int | None,
    json_output: bool,
    no_truncate: bool = False,
) -> ListRender[T]:
    """Fetch + assemble the render payload for a list command.

    Pure data: returns a :class:`ListRender` describing what to render. The
    actual rendering is performed by ``notebooklm.cli.rendering.render_list``.
    """
    items = await spec.fetch(client, notebook_id)
    if limit is not None and limit >= 0:
        items = items[:limit]

    title = _table_title(spec.title, notebook_id)

    if json_output:
        extras: dict[str, Any] = {}
        if spec.envelope_extras is not None:
            extras = await spec.envelope_extras(client, notebook_id)
        serialized = _serialize_items(spec, items)
        envelope = {**extras, spec.items_key: serialized, "count": len(serialized)}
        return ListRender(items=items, title=title, json_envelope=envelope)

    if not items and spec.empty_message is not None:
        return ListRender(
            items=items,
            title=title,
            empty_message=spec.empty_message,
            no_truncate=no_truncate,
        )

    rows = [spec.row(item) for item in items]
    column_options = dict(spec.column_options or {})
    return ListRender(
        items=items,
        title=title,
        columns=list(spec.columns),
        rows=rows,
        column_options=column_options,
        no_truncate=no_truncate,
    )


__all__ = ["ListRender", "ListResult", "ListSpec", "prepare_list"]
