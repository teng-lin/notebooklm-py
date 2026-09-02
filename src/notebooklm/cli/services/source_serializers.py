"""Shared JSON serializers for source CLI output."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..._app.serialize import source_summary
from ...types import drive_source_status_to_str, source_status_to_str

if TYPE_CHECKING:
    from ...types import RelevantChunk, Source, SourceFulltext, SourceType


def source_kind_value(kind: SourceType | None) -> str | None:
    """Return the public JSON value for a source kind."""
    return kind.value if kind is not None else None


def relevant_chunk_payload(chunk: RelevantChunk) -> dict[str, Any]:
    """Return the stable JSON shape for one ranked source-search result."""
    return {
        "source_id": chunk.source_id,
        "text": chunk.text,
        "rank": chunk.rank,
        "start": chunk.start,
        "end": chunk.end,
    }


def source_summary_payload(src: Source) -> dict[str, Any]:
    """Return the stable public JSON shape for source summaries.

    Thin re-export of the neutral :func:`notebooklm._app.serialize.source_summary`
    — the single source of truth for the ``{"id", "title", "type", "url"}``
    shape shared by the CLI and the ``_app`` add/add-drive envelopes (§11). Kept
    as a named wrapper so the historical
    ``source_serializers.source_summary_payload`` import/patch surface resolves.
    """
    return source_summary(src)


def source_row_payload(src: Source) -> dict[str, Any]:
    """Return the CLI's full source row: summary + status axis + Drive axis.

    The single source of truth for the row shape emitted by BOTH
    ``source list --json`` and ``source get --json``. Those two paths built the
    same dict independently, which is exactly how the Drive fields from #2113 /
    #2111 could land on the MCP/REST surfaces and not here; sharing the builder
    means the two can no longer disagree.

    Composed, not widened: :func:`~notebooklm._app.serialize.source_summary`
    stays the narrow ``{id, title, type, url}`` shape the ``source add`` /
    ``source add-drive`` / ``source add-drive-file`` envelopes publish, and the
    Drive axis is added on top of it here — so those envelopes are unchanged.
    The Drive keys live in this CLI module, not beside ``source_summary`` in the
    neutral ``_app`` layer, because their **spelling is CLI-specific**: MCP and
    REST emit the same axis under different key names (see below), so there is
    no neutral shape to share.

    Key naming, and a trap it avoids. The Drive axis ships the label only —
    ``drive_status`` — with no raw-code sibling to ``status_id``:

    * The code would carry no information. ``drive_source_status_to_str`` is a
      total bijection over :class:`~notebooklm.types.DriveSourceStatus`, and the
      one case where a raw code would earn its keep — a code this client cannot
      map — is precisely the case where
      :attr:`notebooklm._web.rows.sources.SourceRow.drive_status` has
      already replaced the wire value with the ``UNKNOWN`` (-1) client sentinel
      and sent the original to a log warning. It never reaches this row.
    * The code spaces collide adversarially. ``status_id`` 2 is ``ready`` and 3
      is ``error``, while the Drive codes run the other way: 2 is ``syncing``
      (degraded) and 3 is ``active`` (healthy). A consumer reasoning by analogy
      from ``status_id`` would select exactly the wrong rows.

    That matches the rule ``mcp/tools/sources.py`` already applies to its
    compact roster — labels, not raw codes. ``status`` / ``status_id`` keep
    their historical pairing untouched.

    All Drive keys are present on every row (``None`` / ``False`` on a non-Drive
    source) rather than omitted, so a ``jq`` filter needs no ``// empty`` guard;
    this matches :func:`notebooklm._app.views.source_view`, which also emits
    them unconditionally. ``drive_status`` is ``None`` — not ``"unknown"`` —
    when the row made no Drive-health claim at all, so "not a Drive claim" stays
    distinguishable from "a Drive code we could not read" (#2111).

    ``is_drive_degraded`` carries the *verdict* over those codes. It is a
    property, so no dataclass-walking serializer would pick it up, and without
    it every consumer would re-derive the degraded set from the label string.
    ``False`` means "the backend reported no degradation" — true alike for a
    non-Drive source, for ``active``, and for an unreadable code — not "the
    Drive file is confirmed present".
    """
    drive_status = src.drive_status
    return {
        **source_summary(src),
        "status": source_status_to_str(src.status),
        "status_id": src.status,
        "created_at": src.created_at.isoformat() if src.created_at else None,
        # ``drive_document_id`` and ``drive_status`` are decoded from
        # structurally independent wire slots, so either one alone is enough to
        # call a row Drive-backed; neither is the authoritative "is this Drive?"
        # bit on its own. Documented as such in docs/cli-reference.md.
        "drive_document_id": src.drive_document_id,
        "drive_status": (
            drive_source_status_to_str(drive_status) if drive_status is not None else None
        ),
        "is_drive_degraded": src.is_drive_degraded,
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
