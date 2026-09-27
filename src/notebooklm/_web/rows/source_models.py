"""Web-owned construction of public source models from positional rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..._types.sources import _disambiguate_type_code, _pdf_url_title_fallback
from .sources import SourceRow

if TYPE_CHECKING:
    from ..._types.sources import Source


def source_from_row(cls: type[Source], row: SourceRow) -> Source:
    """Construct a public source from one normalized Web source row."""
    # Correct the type_code==14 native-Sheet/Drive-PDF overload before it
    # reaches ``kind`` (#1832). Prefer the original-content MIME, then fall
    # back to the Drive-only MIME if the first value is not a known override.
    type_code = _disambiguate_type_code(row.type_code, row.content_mime)
    type_code = _disambiguate_type_code(type_code, row.mime)
    return cls(
        id=row.id,
        # #1850: direct-PDF URLs arrive with the URL in the title slot; unlike
        # HTML pages the server does not extract a title, so use the basename.
        title=_pdf_url_title_fallback(row.title, row.url, type_code),
        url=row.url,
        _type_code=type_code,
        created_at=row.created_at,
        status=row.status,
        experimental_failure_code=row.experimental_failure_code,
        drive_document_id=row.drive_document_id,
        drive_status=row.drive_status,
        download_url=row.download_url,
        viewer_url=row.viewer_url,
        content_mime=row.content_mime,
        word_count=row.word_count,
        revision_id=row.revision_id,
        revision_timestamp=row.revision_timestamp,
        last_modified_at=row.last_modified_at,
        expert_intelligence=row.expert_intelligence,
    )


def decode_source(
    cls: type[Source],
    data: list[Any],
    notebook_id: str | None = None,
    *,
    method_id: str | None = None,
) -> Source:
    """Construct a public source from any supported Web source response shape."""
    del notebook_id  # Retained for parity with the public compatibility shim.
    return source_from_row(cls, SourceRow.from_unknown_shape(data, method_id=method_id))


__all__ = ["decode_source", "source_from_row"]
