"""Projection of Android source protobuf messages into public types."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, cast

from ...exceptions import DecodingError
from ...types import DriveSourceStatus, Source, SourceStatus


def _read_proto() -> Any:
    from ..proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

    return cast(Any, read_pb2)


def _settings_proto() -> Any:
    from ..proto.google.internal.labs.tailwind.v1 import source_settings_pb2

    return cast(Any, source_settings_pb2)


_SOURCE_TYPE_CODE_BY_NAME: dict[str, int] = {
    "SOURCE_CONTENT_TYPE_UNKNOWN": 0,
    "SOURCE_CONTENT_TYPE_GOOGLE_DOC": 1,
    "SOURCE_CONTENT_TYPE_GOOGLE_SLIDES": 2,
    "SOURCE_CONTENT_TYPE_PDF": 3,
    "SOURCE_CONTENT_TYPE_TEXT": 4,
    "SOURCE_CONTENT_TYPE_URL": 5,
    "SOURCE_CONTENT_TYPE_POWERPOINT": 6,
    # Android uses 7 for Sheets; the established public/web code is 14.
    "SOURCE_CONTENT_TYPE_GOOGLE_SHEET": 14,
    "SOURCE_CONTENT_TYPE_MARKDOWN": 8,
    "SOURCE_CONTENT_TYPE_YOUTUBE_VIDEO": 9,
    "SOURCE_CONTENT_TYPE_AUDIO": 10,
    "SOURCE_CONTENT_TYPE_WORD": 11,
    "SOURCE_CONTENT_TYPE_IMAGE": 13,
    "SOURCE_CONTENT_TYPE_CSV": 16,
    "SOURCE_CONTENT_TYPE_EPUB": 17,
}

_SOURCE_STATUS_BY_NAME: dict[str, SourceStatus] = {
    "SOURCE_STATUS_UNSPECIFIED": SourceStatus.UNKNOWN,
    "SOURCE_STATUS_PENDING": SourceStatus.PROCESSING,
    "SOURCE_STATUS_COMPLETE": SourceStatus.READY,
    "SOURCE_STATUS_ERROR": SourceStatus.ERROR,
    "SOURCE_STATUS_PENDING_DELETION": SourceStatus.UNKNOWN,
    "SOURCE_STATUS_TENTATIVE": SourceStatus.PREPARING,
}

_DRIVE_STATUS_BY_NAME: dict[str, DriveSourceStatus] = {
    "DRIVE_SOURCE_STATUS_INACCESSIBLE": DriveSourceStatus.INACCESSIBLE,
    "DRIVE_SOURCE_STATUS_SYNCING": DriveSourceStatus.SYNCING,
    "DRIVE_SOURCE_STATUS_ACTIVE": DriveSourceStatus.ACTIVE,
    "DRIVE_SOURCE_STATUS_DELETED": DriveSourceStatus.DELETED,
    "DRIVE_SOURCE_STATUS_GEN_AI_ACCESS_DENIED": DriveSourceStatus.GEN_AI_ACCESS_DENIED,
}


class _MissingSourceIdError(Exception):
    """Internal discriminator for the one malformed row read may skip."""

    def __init__(self, message: str, *, method_id: str) -> None:
        super().__init__(message)
        self.method_id = method_id


def _enum_name(enum: Any, value: int) -> str | None:
    """Return an enum symbol without trusting backend-specific integer parity."""
    try:
        return str(enum.Name(value))
    except ValueError:
        return None


def _decode_source(
    source: Any,
    *,
    method_id: str,
    index: int | None = None,
) -> Source:
    """Decode one Android source, rejecting a missing required source id."""
    source_id = source.source_id.id if source.HasField("source_id") else ""
    if not source_id:
        location = f" at index {index}" if index is not None else ""
        raise _MissingSourceIdError(
            f"Android source response did not contain a source id{location}",
            method_id=method_id,
        )

    url = None
    drive_document_id = None
    content_mime = None
    type_code = 0
    if source.HasField("metadata"):
        metadata = source.metadata
        type_name = _enum_name(
            _read_proto().OriginalSourceContentType,
            metadata.original_source_content_type,
        )
        # Unrepresentable and ambiguous kinds (including generic DRIVE and
        # EXCEL) intentionally remain UNKNOWN rather than becoming a false kind.
        type_code = _SOURCE_TYPE_CODE_BY_NAME.get(type_name or "", 0)
        if metadata.HasField("google_docs_metadata"):
            drive_document_id = metadata.google_docs_metadata.document_id or None
        if metadata.HasField("webpage_metadata"):
            url = metadata.webpage_metadata.url or None
        if metadata.HasField("google_drive_source_metadata"):
            drive_metadata = metadata.google_drive_source_metadata
            if drive_document_id is None:
                drive_document_id = drive_metadata.document_id or None
            content_mime = drive_metadata.mime_type or None

    status = SourceStatus.UNKNOWN
    drive_status = None
    if source.HasField("settings"):
        settings = source.settings
        settings_proto = _settings_proto()
        status_name = _enum_name(settings_proto.SourceStatus, settings.status)
        status = _SOURCE_STATUS_BY_NAME.get(status_name or "", SourceStatus.UNKNOWN)

        if settings.user_drive_source_status != 0:
            drive_name = _enum_name(
                settings_proto.UserDriveSourceStatus,
                settings.user_drive_source_status,
            )
            drive_status = _DRIVE_STATUS_BY_NAME.get(
                drive_name or "",
                DriveSourceStatus.UNKNOWN,
            )

    return Source(
        id=source_id,
        title=source.title,
        url=url,
        _type_code=type_code,
        created_at=None,
        status=status,
        drive_document_id=drive_document_id,
        drive_status=drive_status,
        download_url=None,
        viewer_url=None,
        content_mime=content_mime,
        word_count=None,
        revision_id=None,
        revision_timestamp=None,
        last_modified_at=None,
    )


def decode_source(
    source: Any,
    *,
    method_id: str,
    index: int | None = None,
) -> Source:
    """Decode one source and normalize projection failures to bounded drift."""
    try:
        return _decode_source(source, method_id=method_id, index=index)
    except _MissingSourceIdError as exc:
        raise DecodingError(str(exc), method_id=exc.method_id) from None
    except DecodingError:
        raise
    except Exception:
        location = f" at index {index}" if index is not None else ""
        raise DecodingError(
            f"Could not decode Android source response{location}",
            method_id=method_id,
        ) from None


def decode_sources(
    sources: Iterable[Any],
    *,
    method_id: str,
    strict: bool,
    logger: logging.Logger,
) -> list[Source]:
    """Decode an ordered source sequence with first-row duplicate semantics."""
    seen: dict[str, Source] = {}
    decoded: list[Source] = []
    for index, raw_source in enumerate(sources):
        try:
            source = _decode_source(raw_source, method_id=method_id, index=index)
        except _MissingSourceIdError as exc:
            if strict:
                raise DecodingError(str(exc), method_id=exc.method_id) from None
            logger.warning("Skipping Android source without an id at index %d", index)
            continue
        except DecodingError:
            raise
        except Exception:
            raise DecodingError(
                f"Could not decode Android source response at index {index}",
                method_id=method_id,
            ) from None

        previous = seen.get(source.id)
        if previous is not None:
            if strict and source != previous:
                raise DecodingError(
                    f"Conflicting duplicate Android source at index {index}",
                    method_id=method_id,
                )
            logger.debug("Skipping duplicate Android source id %s", source.id)
            continue

        seen[source.id] = source
        decoded.append(source)

    return decoded


__all__ = ["decode_source", "decode_sources"]
