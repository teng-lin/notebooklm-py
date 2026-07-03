"""Transport-neutral bounded-pagination slice for list adapters.

The ``*_list`` surfaces return a whole collection; on a large account or notebook
that can be a big payload. :func:`paginate` is the shared, pure slice + bound
validation that both the MCP ``*_list`` tools and the REST list routes build their
own envelope on top of (Option B-lite: shared slice helper, each adapter keeps its
own envelope field names).

The underlying ``batchexecute`` RPCs don't paginate, so this is a client-side
slice over the already-fetched list — the whole collection is still fetched, only
the *returned* payload is bounded.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from typing import Any

from ..exceptions import ValidationError

__all__ = ["paginate"]


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
