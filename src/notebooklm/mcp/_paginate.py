"""Bounded pagination for MCP list tools.

The ``*_list`` tools return a whole collection; on a large account or notebook
that can be a big payload that burns agent context. :func:`paginate` slices to a
``limit`` and reports ``total`` / ``has_more`` so the agent sees a bounded page
and knows whether to ask for more.

The underlying ``batchexecute`` RPCs don't paginate, so this is a client-side
slice over the already-fetched list — the whole collection is still fetched, only
the *returned* payload is bounded. (ponytail: client-side slice; push paging into
the RPC layer only if list sizes ever make the fetch itself the bottleneck.)

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from ..exceptions import ValidationError

__all__ = ["paginate", "DEFAULT_LIMIT"]

#: Default page size for the ``*_list`` tools when the caller omits ``limit``.
DEFAULT_LIMIT = 50


def paginate(items: list[Any], limit: int, offset: int = 0) -> tuple[list[Any], dict[str, Any]]:
    """Return ``(page, meta)`` — the ``items[offset : offset+limit]`` slice + meta.

    ``meta`` is ``{"total": <full count>, "offset": <offset>, "has_more": <bool>}``.
    ``limit`` must be >= 1 (a bounded page is the point) and ``offset`` >= 0; page
    forward by re-calling with ``offset += limit`` until ``has_more`` is false.
    """
    if limit < 1:
        raise ValidationError("limit must be >= 1.")
    if offset < 0:
        raise ValidationError("offset must be >= 0.")
    page = items[offset : offset + limit]
    return page, {
        "total": len(items),
        "offset": offset,
        "has_more": offset + len(page) < len(items),
    }
