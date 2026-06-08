"""Shared JSON serializers for source CLI output."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..._app.serialize import source_summary

if TYPE_CHECKING:
    from ...types import Source, SourceFulltext, SourceType


def source_kind_value(kind: SourceType | None) -> str | None:
    """Return the public JSON value for a source kind."""
    return kind.value if kind is not None else None


def source_summary_payload(src: Source) -> dict[str, Any]:
    """Return the stable public JSON shape for source summaries.

    Thin re-export of the neutral :func:`notebooklm._app.serialize.source_summary`
    — the single source of truth for the ``{"id", "title", "type", "url"}``
    shape shared by the CLI and the ``_app`` add/add-drive envelopes (§11). Kept
    as a named wrapper so the historical
    ``source_serializers.source_summary_payload`` import/patch surface resolves.
    """
    return source_summary(src)


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
