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


def paginate(items: list[Any], limit: int) -> tuple[list[Any], dict[str, Any]]:
    """Return ``(page, meta)`` — the first ``limit`` items plus pagination meta.

    ``meta`` is ``{"total": <full count>, "has_more": <bool>}``. ``limit`` must be
    >= 1 (a bounded page is the point); pass a generously large number for "all".
    """
    if limit < 1:
        raise ValidationError("limit must be >= 1.")
    page = items[:limit]
    return page, {"total": len(items), "has_more": len(items) > len(page)}
