"""Shared JSON serializers for source CLI output."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...types import Source, SourceFulltext, SourceType


def source_kind_value(kind: SourceType | None) -> str | None:
    """Return the public JSON value for a source kind."""
    return kind.value if kind is not None else None


def source_summary_payload(src: Source) -> dict[str, Any]:
    """Return the stable public JSON shape for source summaries."""
    return {
        "id": src.id,
        "title": src.title,
        "type": source_kind_value(src.kind),
        "url": src.url,
    }


def source_fulltext_payload(fulltext: SourceFulltext) -> dict[str, Any]:
    """Return the stable public JSON shape for source fulltext."""
    return {
        "source_id": fulltext.source_id,
        "title": fulltext.title,
        "kind": source_kind_value(fulltext.kind),
        "content": fulltext.content,
        "url": fulltext.url,
        "char_count": fulltext.char_count,
    }
