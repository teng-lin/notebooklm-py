"""Shared helper for the destructive tools' confirmation previews.

The single-type delete tools (``notebook_delete`` / ``source_delete``) and the
confirm-gated sharing tools build a ``needs_confirmation`` preview that includes
the resolved resource's title. Each fetches its own domain list (notebooks /
sources), but the id-to-title match over that list is identical, so it lives
here. (Cross-type ``studio_delete`` gets its preview title straight off the
merged-list resolution instead.) The sharing previews read the notebook title
best-effort through :func:`notebook_title_or_none`.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from ...client import NotebookLMClient

__all__ = ["notebook_title_or_none", "title_for_id"]

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
    resolver passes a full UUID through verbatim in any case, while lists return
    canonical lowercase ids.

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


async def notebook_title_or_none(client: NotebookLMClient, notebook_id: str) -> str | None:
    """Return the notebook's title for a sharing preview, or ``None`` if unreadable.

    The sharing previews add the title only so a name match is visible before
    anything is shared or removed. A failed ``notebooks.list`` read must not turn
    a working preview into an error, so any ``Exception`` yields ``None`` (logged
    at debug level). Cancellation is a ``BaseException`` and still propagates.
    """
    try:
        return title_for_id(await client.notebooks.list(), notebook_id)
    except Exception:
        logger.debug("Notebook title lookup for a sharing preview failed", exc_info=True)
        return None
