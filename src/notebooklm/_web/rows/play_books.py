"""Positional decode for ``ListExpertIntelligenceContent`` rows (#2292).

The row-adapter layer owns the positional knowledge for the Play Books library
listing, mirroring :mod:`notebooklm._web.rows.sources`. One library row is
``[content_id, provider, title, description_html, cover_url, export_disabled,
reason, [authors], field_type, [updated_ts]]`` (live-captured on the web tier).
"""

from __future__ import annotations

from typing import Any

from ..._types.common import _datetime_from_timestamp
from ..._types.sources import (
    _PLAY_BOOK_EXPORT_REASON_MAP,
    PlayBook,
    PlayBookExportReason,
)
from ...exceptions import DecodingError
from ...rpc import RPCMethod

# Positions inside one ListExpertIntelligenceContent row.
_ROW_CONTENT_ID = 0
_ROW_TITLE = 2
_ROW_DESCRIPTION = 3
_ROW_COVER_URL = 4
_ROW_EXPORT_DISABLED = 5
_ROW_REASON = 6
_ROW_AUTHORS = 7
_ROW_FIELD_TYPE = 8
_ROW_UPDATED = 9


def _row_str(row: list[Any], pos: int) -> str | None:
    if len(row) <= pos:
        return None
    value = row[pos]
    return value if isinstance(value, str) and value else None


def _row_authors(row: list[Any]) -> tuple[str, ...]:
    if len(row) <= _ROW_AUTHORS:
        return ()
    value = row[_ROW_AUTHORS]
    if not isinstance(value, list):
        return ()
    return tuple(a for a in value if isinstance(a, str))


def _row_field_type(row: list[Any]) -> float | None:
    if len(row) <= _ROW_FIELD_TYPE:
        return None
    value = row[_ROW_FIELD_TYPE]
    return float(value) if isinstance(value, (int, float)) else None


def _row_updated_at(row: list[Any]) -> Any | None:
    if len(row) <= _ROW_UPDATED:
        return None
    value = row[_ROW_UPDATED]
    if isinstance(value, list) and value and isinstance(value[0], (int, float)):
        return _datetime_from_timestamp(value[0])
    return None


def _row_reason(row: list[Any]) -> PlayBookExportReason | None:
    if len(row) <= _ROW_REASON:
        return None
    value = row[_ROW_REASON]
    return _PLAY_BOOK_EXPORT_REASON_MAP.get(value) if isinstance(value, int) else None


def _row_export_disabled(row: list[Any]) -> bool:
    return bool(row[_ROW_EXPORT_DISABLED]) if len(row) > _ROW_EXPORT_DISABLED else False


def decode_play_book_row(row: list[Any]) -> PlayBook:
    """Decode one ``ListExpertIntelligenceContent`` row into a :class:`PlayBook`."""
    content_id = _row_str(row, _ROW_CONTENT_ID)
    if content_id is None:
        # The content id is the book's identity — the only field add_play_book
        # can act on. A row without one is a wire-shape break, not a usable
        # entry, so raise rather than emit a hollow PlayBook(content_id="").
        raise DecodingError(
            "ListExpertIntelligenceContent row is missing its content id",
            method_id=RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT.value,
        )
    return PlayBook(
        content_id=content_id,
        title=_row_str(row, _ROW_TITLE),
        authors=_row_authors(row),
        description_html=_row_str(row, _ROW_DESCRIPTION),
        cover_url=_row_str(row, _ROW_COVER_URL),
        export_disabled=_row_export_disabled(row),
        reason=_row_reason(row),
        field_type=_row_field_type(row),
        updated_at=_row_updated_at(row),
    )


def decode_play_books_response(payload: Any) -> list[PlayBook]:
    """Decode a ``ListExpertIntelligenceContent`` response into :class:`PlayBook`s.

    The response is ``[[row, …]]`` (the rows wrapped in a single-element outer
    list); ``None`` / empty means an account with no Play Books library. A shape
    that is neither raises :class:`DecodingError`.
    """
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise DecodingError(
            "Unexpected ListExpertIntelligenceContent response shape",
            method_id=RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT.value,
        )
    if not payload:
        return []
    rows = payload[0]
    if not isinstance(rows, list):
        raise DecodingError(
            "Unexpected ListExpertIntelligenceContent rows shape",
            method_id=RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT.value,
        )
    # A non-list row is a shape break, not a droppable value: raise so a wire
    # change surfaces loudly rather than silently shrinking the library.
    for row in rows:
        if not isinstance(row, list):
            raise DecodingError(
                "Malformed ListExpertIntelligenceContent row (expected a list, "
                f"got {type(row).__name__})",
                method_id=RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT.value,
            )
    return [decode_play_book_row(row) for row in rows]
