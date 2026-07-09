"""Private source type implementations."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..rpc.types import SourceStatus
from .common import (
    UnknownTypeWarning,
)

if TYPE_CHECKING:
    from .._row_adapters.sources import SourceRow


class SourceType(str, Enum):
    """User-facing source types.

    This is a str enum, so comparisons work with both enum members and strings:
        source.kind == SourceType.WEB_PAGE  # True
        source.kind == "web_page"           # Also True
    """

    GOOGLE_DOCS = "google_docs"
    GOOGLE_SLIDES = "google_slides"
    GOOGLE_SPREADSHEET = "google_spreadsheet"
    PDF = "pdf"
    PASTED_TEXT = "pasted_text"
    WEB_PAGE = "web_page"
    GOOGLE_DRIVE_AUDIO = "google_drive_audio"
    GOOGLE_DRIVE_VIDEO = "google_drive_video"
    YOUTUBE = "youtube"
    MARKDOWN = "markdown"
    DOCX = "docx"
    CSV = "csv"
    EPUB = "epub"
    IMAGE = "image"
    MEDIA = "media"
    UNKNOWN = "unknown"


_warned_source_types: set[int] = set()


_SOURCE_TYPE_CODE_MAP: dict[int, SourceType] = {
    1: SourceType.GOOGLE_DOCS,
    2: SourceType.GOOGLE_SLIDES,  # Was GOOGLE_OTHER, now more specific
    3: SourceType.PDF,
    4: SourceType.PASTED_TEXT,
    5: SourceType.WEB_PAGE,
    8: SourceType.MARKDOWN,
    9: SourceType.YOUTUBE,
    10: SourceType.MEDIA,
    11: SourceType.DOCX,
    13: SourceType.IMAGE,
    14: SourceType.GOOGLE_SPREADSHEET,
    16: SourceType.CSV,
    17: SourceType.EPUB,
}


_SOURCE_TYPE_COMPAT_MAP: dict[SourceType, str] = {
    SourceType.GOOGLE_DOCS: "text",
    SourceType.GOOGLE_SLIDES: "text",
    SourceType.GOOGLE_SPREADSHEET: "text",
    SourceType.PDF: "text_file",
    SourceType.PASTED_TEXT: "text",
    SourceType.WEB_PAGE: "url",
    SourceType.YOUTUBE: "youtube",
    SourceType.MARKDOWN: "text_file",
    SourceType.DOCX: "text_file",
    SourceType.CSV: "text",
    SourceType.EPUB: "text_file",
    SourceType.IMAGE: "text",
    SourceType.MEDIA: "text",
    SourceType.UNKNOWN: "text",
}


# The type_code==14 overload (#1828/#1832): the backend returns 14 for BOTH a
# native Google Sheet AND a Drive-hosted binary file (e.g. a PDF). Live capture
# showed Drive sources carry no URL (metadata[0]/[5]/[7] are null), so the only
# disambiguation signal is the MIME at metadata[19] / metadata[9][2]. A native
# Sheet carries "application/vnd.google-apps.spreadsheet" (→ stay 14); a Drive
# PDF carries "application/pdf" (→ 3). Only MIMEs proven by live capture are
# mapped; anything else under 14 is left as GOOGLE_SPREADSHEET (conservative —
# never relabel a real Sheet, never introduce UNKNOWN). Extend as more
# Drive-hosted-binary-under-14 collisions are captured.
_TYPE_CODE_14_MIME_OVERRIDE: dict[str, int] = {
    "application/pdf": 3,  # Drive-hosted PDF → PDF
}


def _disambiguate_type_code(type_code: int | None, mime: str | None) -> int | None:
    """Correct the ambiguous ``type_code == 14`` using the row MIME (#1832).

    Returns the effective type code: a Drive-hosted binary whose MIME maps in
    :data:`_TYPE_CODE_14_MIME_OVERRIDE` is remapped (PDF → 3); every other case
    (native Sheet MIME, no MIME, or an unrecognized MIME) is returned unchanged
    so real Google Sheets keep decoding as ``GOOGLE_SPREADSHEET``.
    """
    if type_code == 14 and mime is not None:
        return _TYPE_CODE_14_MIME_OVERRIDE.get(mime, type_code)
    return type_code


def _safe_source_type(type_code: int | None) -> SourceType:
    """Convert internal type code to user-facing SourceType enum."""
    if type_code is None:
        return SourceType.UNKNOWN

    result = _SOURCE_TYPE_CODE_MAP.get(type_code)
    if result is None:
        if type_code not in _warned_source_types:
            _warned_source_types.add(type_code)
            warnings.warn(
                f"Unknown source type code {type_code}. "
                "Consider updating notebooklm-py to the latest version.",
                UnknownTypeWarning,
                stacklevel=3,
            )
        return SourceType.UNKNOWN
    return result


def _extract_source_url(metadata: Any, *, allow_bare_http: bool = True) -> str | None:
    """Extract a source URL from a ``src[2]`` metadata array.

    Thin compatibility shim over
    :meth:`notebooklm._row_adapters.sources.SourceRow.url_from_metadata`,
    which centralises the ``metadata[7]`` > ``metadata[5]`` > ``metadata[0]``
    positional precedence in the sanctioned row-adapter layer. The adapter
    method reproduces this helper's exact (soft, un-coerced) semantics, so this
    re-exported public helper is behavior-preserved while its position
    knowledge no longer lives here.
    """
    from .._row_adapters.sources import SourceRow

    return SourceRow.url_from_metadata(metadata, allow_bare_http=allow_bare_http)


def _extract_source_created_at(metadata: Any) -> datetime | None:
    """Extract a source creation timestamp from a ``src[2]`` metadata array.

    Thin compatibility shim over
    :meth:`notebooklm._row_adapters.sources.SourceRow.created_at_from_metadata`,
    which owns the ``metadata[2][0]`` timestamp position. Behavior-identical to
    the original inline walk (both funnel the inner value through
    :func:`_datetime_from_timestamp`).
    """
    from .._row_adapters.sources import SourceRow

    return SourceRow.created_at_from_metadata(metadata)


@dataclass
class Source:
    """Represents a NotebookLM source."""

    id: str
    title: str | None = None
    url: str | None = None
    _type_code: int | None = field(default=None, repr=False)
    created_at: datetime | None = None
    # ``status`` holds a :class:`~notebooklm.rpc.SourceStatus` member (an
    # ``int`` enum) decoded from the GET_NOTEBOOK source-list status block.
    # The annotation was previously ``int`` even though every construction
    # path (the listing service and :meth:`from_api_response`) populates it
    # with a ``SourceStatus``; ``SourceStatus`` is the accurate declared type
    # and remains ``int``-compatible at runtime and for equality.
    status: SourceStatus = SourceStatus.READY

    @property
    def kind(self) -> SourceType:
        """Get source type as SourceType enum."""
        return _safe_source_type(self._type_code)

    @property
    def is_ready(self) -> bool:
        """Check if source is ready for use (status=READY)."""
        return self.status == SourceStatus.READY

    @property
    def is_processing(self) -> bool:
        """Check if source is still being processed (status=PROCESSING)."""
        return self.status == SourceStatus.PROCESSING

    @property
    def is_error(self) -> bool:
        """Check if source processing failed (status=ERROR)."""
        return self.status == SourceStatus.ERROR

    @classmethod
    def from_row(cls, row: SourceRow) -> Source:
        """Build a :class:`Source` from a normalized :class:`SourceRow`.

        This is the **single** construction site for a :class:`Source`
        from a parsed source row. Both :meth:`from_api_response` (the
        public classmethod used by ``ADD_SOURCE`` / rename paths) and
        :meth:`notebooklm._source.listing.SourceLister._parse_source`
        (the ``GET_NOTEBOOK`` list/get/poll path) funnel through here so
        every code path produces identical :class:`Source` instances —
        including the decoded :attr:`status`.

        Minimal flat rows historically yield ``_type_code=None`` and skip
        metadata-derived fields. That invariant is now handled by SourceRow
        when no metadata list is present at ``_raw[2]``, so
        :attr:`SourceRow.metadata` returns ``None`` and
        :attr:`~SourceRow.type_code` / :attr:`~SourceRow.url` /
        :attr:`~SourceRow.created_at` all resolve to ``None`` while
        :attr:`~SourceRow.status` resolves to ``SourceStatus.READY``. The
        single field mapping below therefore covers all three wire shapes
        identically.
        """
        return cls(
            id=row.id,
            title=row.title,
            url=row.url,
            # Correct the type_code==14 native-Sheet/Drive-PDF overload by the
            # row MIME before it reaches ``kind`` (#1832). No-op for every other
            # type code and for real Sheets.
            _type_code=_disambiguate_type_code(row.type_code, row.mime),
            created_at=row.created_at,
            status=row.status,
        )

    @classmethod
    def from_api_response(
        cls,
        data: list[Any],
        notebook_id: str | None = None,
        *,
        method_id: str | None = None,
    ) -> Source:
        """Parse source data from various API response formats.

        Multi-shape dispatch (the three wire shapes — deeply nested,
        medium nested, flat) is centralised in
        :meth:`notebooklm._row_adapters.sources.SourceRow.from_unknown_shape`;
        position knowledge for the entry layout lives on
        :class:`SourceRow` itself. This method only normalizes the wire
        shape into a :class:`SourceRow` and defers to :meth:`from_row` —
        the single construction site shared with the
        ``GET_NOTEBOOK`` list/get/poll path
        (:meth:`notebooklm._source.listing.SourceLister._parse_source`) —
        so all paths produce identical :class:`Source` instances,
        including the decoded :attr:`status`. ``status`` earlier silently
        fell back to the ``SourceStatus.READY`` default here while the
        listing path read it from the row.

        Args:
            data: Raw decoded source payload (one of the three wire
                shapes handled by
                :meth:`~notebooklm._row_adapters.sources.SourceRow.from_unknown_shape`).
            notebook_id: Accepted for call-site symmetry and forward
                compatibility but currently unused — the parsed source
                wire shape carries no notebook reference, so this value
                does not influence the returned :class:`Source`. It is
                retained (rather than dropped) because
                ``Source.from_api_response`` is tracked public surface;
                removing the parameter would be a backward-incompatible
                signature change flagged by
                ``scripts/audit_public_api_compat.py``.
            method_id: Originating RPC method id (e.g.
                ``RPCMethod.ADD_SOURCE.value`` /
                ``RPCMethod.UPDATE_SOURCE.value``) used only to tag
                ``safe_index`` drift diagnostics with the real method.
                Defaults to ``None``, which lets
                :meth:`~notebooklm._row_adapters.sources.SourceRow.from_unknown_shape`
                fall back to its ``GET_NOTEBOOK`` default — preserving
                the historical behavior for callers that do not pass it.
        """
        # Keep the row-adapter dependency local so importing the source
        # dataclass package does not pull source-row parsing helpers into
        # the top-level public type facade.
        from .._row_adapters.sources import SourceRow

        return cls.from_row(SourceRow.from_unknown_shape(data, method_id=method_id))


@dataclass
class SourceFulltext:
    """Full text content of a source as indexed by NotebookLM."""

    source_id: str
    title: str
    content: str
    _type_code: int | None = field(default=None, repr=False)
    url: str | None = None
    char_count: int = 0

    @property
    def kind(self) -> SourceType:
        """Get source type as SourceType enum."""
        return _safe_source_type(self._type_code)

    def find_citation_context(
        self,
        cited_text: str,
        context_chars: int = 200,
    ) -> list[tuple[str, int]]:
        """Search for citation text and return matching contexts."""
        if not cited_text or not self.content:
            return []

        search_text = cited_text[: min(40, len(cited_text))]

        matches = []
        pos = 0
        while (idx := self.content.find(search_text, pos)) != -1:
            start = max(0, idx - context_chars)
            end = min(len(self.content), idx + len(search_text) + context_chars)
            matches.append((self.content[start:end], idx))
            pos = idx + len(search_text)

        return matches
