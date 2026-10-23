"""Shared helper for the destructive tools' confirmation previews.

The single-type delete tools (``notebook_delete`` / ``source_delete``) and the
confirm-gated sharing tools build a ``needs_confirmation`` preview that includes
the resolved resource's title. A name ref reuses the list its resolution read; a
full-UUID ref (resolved without a list) reads the list here. Either way the
id-to-title match is identical, so it lives here, and the read is best-effort
through :func:`preview_title`. (Cross-type ``studio_delete`` gets its preview
title straight off the merged-list resolution instead.)

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from ...client import NotebookLMClient

__all__ = ["notebook_title_or_none", "preview_title", "title_for_id"]

logger = logging.getLogger(__name__)


def _is_full_uuid(value: str) -> bool:
    """Whether ``value`` is a hyphenated 36-character UUID, in any case."""
    try:
        return str(UUID(value)) == value.casefold()
    except ValueError:
        return False


def title_for_id(items: Sequence[Any], target_id: str) -> str | None:
    """Return the title of the item whose id equals ``target_id``, else ``None``.

    Ids compare exactly; a full UUID ref also matches case-insensitively. The
    resolvers already lowercase a full UUID, and lists return lowercase ids, so
    the fallback only covers a full UUID that reaches here some other way.

    Best-effort: a miss (the resource vanished between resolution and this lookup)
    yields ``None`` rather than raising, so the delete preview still renders.
    """
    match = next((item for item in items if str(item.id) == target_id), None)
    if match is None and _is_full_uuid(target_id):
        key = target_id.casefold()
        match = next(
            (
                item
                for item in items
                if _is_full_uuid(str(item.id)) and str(item.id).casefold() == key
            ),
            None,
        )
    return None if match is None else match.title


async def preview_title(
    items: Sequence[Any] | None,
    fetch: Callable[[], Awaitable[Sequence[Any]]],
    target_id: str,
) -> str | None:
    """Return ``target_id``'s title for a confirmation preview, or ``None`` if unreadable.

    ``items`` is the list the resolution already read; ``None`` (a full-UUID ref
    lists nothing) calls ``fetch`` instead. A preview adds the title only so a
    name match is visible before anything changes. A failed list read must not
    turn a working preview into an error, so any ``Exception`` yields ``None``
    (logged at debug level). Cancellation is a ``BaseException`` and still
    propagates.
    """
    try:
        return title_for_id(await fetch() if items is None else items, target_id)
    except Exception:
        logger.debug("Title lookup for a confirmation preview failed", exc_info=True)
        return None


async def notebook_title_or_none(
    client: NotebookLMClient, notebook_id: str, *, items: Sequence[Any] | None = None
) -> str | None:
    """:func:`preview_title` over the notebook list."""
    return await preview_title(items, client.notebooks.list, notebook_id)
