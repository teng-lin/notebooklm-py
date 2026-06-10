"""Shared helper for the destructive tools' confirmation previews.

The three delete tools (``notebook_delete`` / ``source_delete`` / ``note_delete``)
build a ``needs_confirmation`` preview that includes the resolved resource's
title. Each fetches its own domain list (notebooks / sources / notes), but the
id-to-title match over that list is identical, so it lives here.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = ["title_for_id"]


def title_for_id(items: Sequence[Any], target_id: str) -> str | None:
    """Return the title of the item whose id equals ``target_id``, else ``None``.

    Best-effort: a miss (the resource vanished between resolution and this lookup)
    yields ``None`` rather than raising, so the delete preview still renders.
    """
    return next((item.title for item in items if str(item.id) == target_id), None)
