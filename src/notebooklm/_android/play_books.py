"""Android Play Books wire codecs and commit metadata helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import timezone
from typing import Any, cast

from .._types.sources import _PLAY_BOOK_EXPORT_REASON_MAP
from ..exceptions import DecodingError
from ..types import PlayBook


def _write_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import sources_pb2

    return cast(Any, sources_pb2)


def _settings_proto() -> Any:
    from .proto.google.internal.labs.tailwind.v1 import source_settings_pb2

    return cast(Any, source_settings_pb2)


def decode_play_book_item(item: Any, *, method_id: str) -> PlayBook:
    """Decode one ``ListExpertIntelligenceContent`` item."""
    content_id = item.content_id
    if not content_id:
        raise DecodingError(
            "ListExpertIntelligenceContent item is missing its content id",
            method_id=method_id,
        )
    reason = _PLAY_BOOK_EXPORT_REASON_MAP.get(item.export_reason) if item.export_reason else None
    updated_at = None
    if item.HasField("updated_timestamp"):
        try:
            updated_at = item.updated_timestamp.ToDatetime(tzinfo=timezone.utc)
        except Exception:
            raise DecodingError(
                "ListExpertIntelligenceContent item has an invalid update timestamp",
                method_id=method_id,
            ) from None
    return PlayBook(
        content_id=content_id,
        title=item.title or None,
        authors=tuple(item.authors),
        description_html=item.description or None,
        cover_url=item.thumbnail_image_url or None,
        export_disabled=bool(item.export_disabled),
        reason=reason,
        field_type=float(item.field_type),
        updated_at=updated_at,
    )


def build_expert_intelligence_content(book: PlayBook) -> Any:
    """Build the captured Android Play Books add payload."""
    return _write_proto().ExpertIntelligenceContent(
        provider=1,
        content_id=book.content_id,
        title=book.title or "",
        description=book.description_html or "",
        thumbnail_image_url=book.cover_url or "",
        field_type=book.field_type or 0.0,
        authors=list(book.authors),
    )


def tentative_source_ids(rows: Sequence[Any], candidate_ids: Collection[str]) -> set[str]:
    """Return exact candidate IDs affirmatively still in tentative state."""
    candidates = frozenset(candidate_ids)
    tentative: set[str] = set()
    for row in rows:
        try:
            source_id = row.source_id.id if row.HasField("source_id") else ""
            raw_status = row.settings.status if row.HasField("settings") else 0
        except Exception:
            continue
        if source_id in candidates and raw_status == _settings_proto().SOURCE_STATUS_TENTATIVE:
            tentative.add(source_id)
    return tentative


def static_metadata_augmentor(
    metadata: tuple[tuple[str, str | bytes], ...],
) -> Callable[[str], Awaitable[Sequence[tuple[str, str | bytes]]]]:
    """Return an augmentor that reuses metadata fetched before registration."""

    async def _augment(bearer: str) -> Sequence[tuple[str, str | bytes]]:
        del bearer
        return metadata

    return _augment


__all__ = [
    "build_expert_intelligence_content",
    "decode_play_book_item",
    "static_metadata_augmentor",
    "tentative_source_ids",
]
