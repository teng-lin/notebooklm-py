"""Private source type implementations."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from ..rpc.types import SourceStatus
from .common import (
    UnknownTypeWarning,
    _datetime_from_timestamp,
)


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
    """Extract a source URL from a ``src[2]`` metadata array."""
    if not isinstance(metadata, list):
        return None
    url: str | None = None
    if len(metadata) > 7:
        url_list = metadata[7]
        if isinstance(url_list, list) and len(url_list) > 0:
            url = url_list[0]
    if not url and len(metadata) > 5:
        yt_data = metadata[5]
        if isinstance(yt_data, list) and len(yt_data) > 0 and isinstance(yt_data[0], str):
            url = yt_data[0]
    if not url and allow_bare_http and len(metadata) > 0:
        candidate = metadata[0]
        if isinstance(candidate, str) and candidate.startswith("http"):
            url = candidate
    return url


def _extract_source_created_at(metadata: Any) -> datetime | None:
    """Extract a source creation timestamp from a ``src[2]`` metadata array."""
    if not isinstance(metadata, list) or len(metadata) <= 2:
        return None

    timestamp_list = metadata[2]
    if not isinstance(timestamp_list, list) or not timestamp_list:
        return None

    return _datetime_from_timestamp(timestamp_list[0])


@dataclass
class Source:
    """Represents a NotebookLM source."""

    id: str
    title: str | None = None
    url: str | None = None
    _type_code: int | None = field(default=None, repr=False)
    created_at: datetime | None = None
    status: int = SourceStatus.READY

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
    def from_api_response(cls, data: list[Any], notebook_id: str | None = None) -> Source:
        """Parse source data from various API response formats."""
        if not data or not isinstance(data, list):
            raise ValueError(f"Invalid source data: {data}")

        # Try deeply nested format: [[[[id], title, metadata, ...]]]
        if isinstance(data[0], list) and len(data[0]) > 0:
            if isinstance(data[0][0], list) and len(data[0][0]) > 0:
                # Check if deeply nested vs medium nested
                if isinstance(data[0][0][0], list):
                    # Deeply nested: [[[[id], title, ...]]]
                    entry = data[0][0]
                    source_id = entry[0][0] if isinstance(entry[0], list) else entry[0]
                    title = entry[1] if len(entry) > 1 else None
                    # Fall through to the shared metadata parser below.
                else:
                    # Medium nested: [[['id'], 'title', ...]]
                    entry = data[0]
                    source_id = entry[0][0] if isinstance(entry[0], list) else entry[0]
                    title = entry[1] if len(entry) > 1 else None

                    metadata = entry[2] if len(entry) > 2 and isinstance(entry[2], list) else None
                    url = _extract_source_url(metadata, allow_bare_http=False)
                    type_code = (
                        metadata[4]
                        if metadata is not None
                        and len(metadata) > 4
                        and isinstance(metadata[4], int)
                        else None
                    )
                    created_at = _extract_source_created_at(metadata)

                    return cls(
                        id=str(source_id),
                        title=title,
                        url=url,
                        _type_code=type_code,
                        created_at=created_at,
                    )

                metadata = entry[2] if len(entry) > 2 and isinstance(entry[2], list) else None
                url = _extract_source_url(metadata)
                type_code = (
                    metadata[4]
                    if metadata is not None and len(metadata) > 4 and isinstance(metadata[4], int)
                    else None
                )
                created_at = _extract_source_created_at(metadata)

                return cls(
                    id=str(source_id),
                    title=title,
                    url=url,
                    _type_code=type_code,
                    created_at=created_at,
                )

        # Simple flat format: [id, title] or [id, title, ...]
        source_id = data[0] if len(data) > 0 else ""
        title = data[1] if len(data) > 1 else None
        return cls(id=str(source_id), title=title, _type_code=None)


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
