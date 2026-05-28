"""Shared JSON serializers for source CLI output."""

from __future__ import annotations

from typing import Any

from ...types import Source, SourceFulltext


def source_summary_payload(src: Source) -> dict[str, Any]:
    """Return the stable public JSON shape for source summaries."""
    return {
        "id": src.id,
        "title": src.title,
        "type": src.kind.value,
        "url": src.url,
    }


def source_fulltext_payload(fulltext: SourceFulltext) -> dict[str, Any]:
    """Return the stable public JSON shape for source fulltext."""
    return {
        "source_id": fulltext.source_id,
        "title": fulltext.title,
        "kind": fulltext.kind.value,
        "content": fulltext.content,
        "url": fulltext.url,
        "char_count": fulltext.char_count,
    }
