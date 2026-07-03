"""Opt-in, non-breaking pagination envelope for the REST list routes.

The list routes (`GET` notebooks / sources / notes / artifacts) default to the
FULL collection under their existing top-level key — the DEFAULT shape is
unchanged (no ``limit`` supplied ⇒ full, unbounded list, exactly as before). When
the caller supplies ``?limit=``, the already-projected collection is sliced via
the shared, transport-neutral :func:`notebooklm._app.pagination.paginate` (the
SAME slice + bound validation the MCP ``*_list`` tools use — Option B-lite: shared
slice, adapter-owned envelope) and a ``meta`` block
(``{total, has_more, limit, offset}``) is added alongside the existing list key.

Bounds are validated by FastAPI ``Query`` constraints at the route boundary
(``limit >= 1``, ``offset >= 0`` ⇒ 422 on a bad value), so a bad request never
reaches the slice; the shared helper's own validation remains the guard for the
MCP path.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from .._app.pagination import paginate
from ..exceptions import ValidationError

__all__ = ["MAX_LIMIT", "paginate_envelope"]

#: Upper bound on the list-route ``?limit=`` query param. The whole collection is
#: still fetched from the RPC layer; this only caps the *returned* page so a huge
#: ``limit`` cannot defeat the bounded-response contract. Enforced by the routes'
#: ``Query(ge=1, le=MAX_LIMIT)`` (a larger value ⇒ 422 before the slice).
MAX_LIMIT = 1000


def paginate_envelope(
    items: list[Any], *, key: str, limit: int | None, offset: int, **extra: Any
) -> dict[str, Any]:
    """Wrap an already-projected ``items`` list in the list-route response envelope.

    ``extra`` carries any sibling top-level fields (e.g. ``notebook_id=...``) and is
    emitted first so the key ordering matches the pre-pagination shape. When
    ``limit`` is ``None`` (the default) the full list is returned under ``key`` with
    NO ``meta`` block — byte-for-byte the historical shape. When ``limit`` is set,
    ``items`` is sliced and a ``meta`` block (``{total, has_more, limit, offset}``)
    is added alongside ``key``.

    A non-zero ``offset`` without a ``limit`` is a contract error (the paging
    window is ambiguous — ``offset`` only means something relative to a page size),
    rejected with a :class:`~notebooklm.exceptions.ValidationError` (⇒ 400) rather
    than silently ignored. ``offset=0`` with no ``limit`` is the unchanged
    full-list default.
    """
    if limit is None:
        if offset != 0:
            raise ValidationError("offset requires limit; supply ?limit= to page with ?offset=.")
        return {**extra, key: items}
    page, meta = paginate(items, limit, offset)
    return {
        **extra,
        key: page,
        "meta": {
            "total": meta["total"],
            "has_more": meta["has_more"],
            "limit": limit,
            "offset": offset,
        },
    }
