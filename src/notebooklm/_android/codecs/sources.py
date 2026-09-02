"""Projection of Android source protobuf messages into public types."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import timezone
from typing import Any, cast

from ...exceptions import DecodingError
from ...types import (
    DriveSourceStatus,
    ExpertIntelligenceSourceMetadata,
    Source,
    SourceStatus,
)


def _read_proto() -> Any:
    from ..proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

    return cast(Any, read_pb2)


def _settings_proto() -> Any:
    from ..proto.google.internal.labs.tailwind.v1 import source_settings_pb2

    return cast(Any, source_settings_pb2)


#: Mobile ``SourceContentType`` name -> the public ``Source._type_code``.
#:
#: Name-based rather than a straight integer copy so a renumbering on either
#: side is caught instead of silently mistranslated. The two enums do in fact
#: agree on every value both define, including 7 (GOOGLE_SHEET) and 14 (DRIVE);
#: the map is kept explicit rather than collapsed to identity so that stays a
#: checked fact.
_SOURCE_TYPE_CODE_BY_NAME: dict[str, int] = {
    "SOURCE_CONTENT_TYPE_UNKNOWN": 0,
    "SOURCE_CONTENT_TYPE_GOOGLE_DOC": 1,
    "SOURCE_CONTENT_TYPE_GOOGLE_SLIDES": 2,
    "SOURCE_CONTENT_TYPE_PDF": 3,
    "SOURCE_CONTENT_TYPE_TEXT": 4,
    "SOURCE_CONTENT_TYPE_URL": 5,
    "SOURCE_CONTENT_TYPE_POWERPOINT": 6,
    "SOURCE_CONTENT_TYPE_GOOGLE_SHEET": 7,
    "SOURCE_CONTENT_TYPE_MARKDOWN": 8,
    "SOURCE_CONTENT_TYPE_YOUTUBE_VIDEO": 9,
    "SOURCE_CONTENT_TYPE_AUDIO": 10,
    "SOURCE_CONTENT_TYPE_WORD": 11,
    "SOURCE_CONTENT_TYPE_EXCEL": 12,
    "SOURCE_CONTENT_TYPE_IMAGE": 13,
    # The backend's catch-all for a Drive file it gives no format-specific code.
    "SOURCE_CONTENT_TYPE_DRIVE": 14,
    "SOURCE_CONTENT_TYPE_GMAIL": 15,
    "SOURCE_CONTENT_TYPE_CSV": 16,
    "SOURCE_CONTENT_TYPE_EPUB": 17,
    "SOURCE_CONTENT_TYPE_GEMINI_CHAT": 18,
    "SOURCE_CONTENT_TYPE_AI_MODE_CHAT": 19,
    "SOURCE_CONTENT_TYPE_EXPERT_INTELLIGENCE": 20,
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


def _decode_expert_intelligence(meta: Any) -> ExpertIntelligenceSourceMetadata:
    """Decode an Android ``ExpertIntelligenceSourceMetadata`` proto (#2292).

    The web tier carries a ``provider`` (ContentProvider) the recovered mobile
    schema does not declare, so it is left ``None`` on Android reads; every
    other field maps one-to-one.
    """
    return ExpertIntelligenceSourceMetadata(
        content_id=meta.content_id or None,
        provider=None,
        title=meta.title or None,
        authors=tuple(meta.authors),
        thumbnail_image_url=meta.thumbnail_image_url or None,
        description=meta.description or None,
        field_type=meta.field_type or None,
    )


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
    expert_intelligence = None
    created_at = None
    if source.HasField("metadata"):
        metadata = source.metadata
        if metadata.HasField("source_added_timestamp"):
            created_at = metadata.source_added_timestamp.ToDatetime(tzinfo=timezone.utc)
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
        if metadata.HasField("expert_intelligence_source_metadata"):
            expert_intelligence = _decode_expert_intelligence(
                metadata.expert_intelligence_source_metadata
            )

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
        created_at=created_at,
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
        expert_intelligence=expert_intelligence,
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


def _document_guide_echo(guide: Any) -> str:
    """Return the source id a ``DocumentGuide`` labels itself with, or ``""``.

    The label is optional on the wire, so an empty return means *unlabelled*,
    never *mismatched*.
    """

    if not guide.HasField("source") or not guide.source.HasField("source_id"):
        return ""
    return str(guide.source.source_id.id)


def _guide_echo_diagnostic(echoes: Iterable[str]) -> list[str]:
    """Render observed guide echoes for ``RPCError.found_ids``."""

    return [echo or "<unlabelled>" for echo in echoes]


def _guide_failure(
    reason: str,
    *,
    source_id: str,
    method_id: str,
    response: Any,
    echoes: Sequence[str],
) -> DecodingError:
    """Build a rejected-guide error that can be diagnosed from a CI log alone.

    The original strict-echo branches reported neither the observed ids nor the
    guide count, which is why issue #2276 could not be closed from the nightly
    output. The counts and ids go in the *message*, which is what a pytest
    traceback prints in full.

    ``raw_response`` carries the per-guide **field tags** rather than any wire
    bytes. Tags answer the question these branches exist to ask -- whether the
    server labelled the guide at all, and whether it used a field we do not
    model -- and they carry no content. A byte preview cannot: a guide's
    payload begins with ``#2 snippet`` exactly when the label is missing, so
    even a short prefix would capture the start of a model-written summary of
    the user's source. Hex is reversible and merely hides that text from
    ``scrub_secrets``, and ``NOTEBOOKLM_DEBUG=1`` opts out of truncation
    entirely, so there is no safe prefix length.
    """

    observed = ", ".join(_guide_echo_diagnostic(echoes)) or "<none>"
    return DecodingError(
        f"{reason} (requested={source_id}, guides={len(response.guides)}, observed=[{observed}])",
        method_id=method_id,
        found_ids=_guide_echo_diagnostic(echoes),
        raw_response=_guide_field_tags(response),
    )


def _guide_field_tags(response: Any) -> str:
    """Render the field tags present on each guide, e.g. ``"[2,3,4 | 1,2,3]"``.

    Read off the wire rather than from ``ListFields()`` so tags we do not model
    are reported too -- an unmodelled tag is the shape that would indicate the
    label had moved rather than been dropped. Only tag numbers are kept; no
    payload is read.
    """

    return (
        "["
        + " | ".join(
            ",".join(str(tag) for tag in _top_level_tags(guide.SerializeToString()))
            for guide in response.guides
        )
        + "]"
    )


def _top_level_tags(payload: bytes) -> list[int]:
    """List the top-level protobuf field numbers in ``payload``, skipping values."""

    tags: list[int] = []
    offset = 0
    try:
        while offset < len(payload):
            key, offset = _read_varint(payload, offset)
            tags.append(key >> 3)
            offset = _skip_value(payload, offset, key & 7)
    except (IndexError, ValueError):
        tags.append(-1)  # truncated or unparsable past this point
    return tags


def _read_varint(payload: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = payload[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def _skip_value(payload: bytes, offset: int, wire_type: int) -> int:
    if wire_type == 0:
        _, offset = _read_varint(payload, offset)
        return offset
    if wire_type == 2:
        length, offset = _read_varint(payload, offset)
        if offset + length > len(payload):
            raise IndexError("length-delimited field runs past the payload")
        return offset + length
    if wire_type == 5:
        return offset + 4
    if wire_type == 1:
        return offset + 8
    raise ValueError(f"unsupported wire type {wire_type}")


def select_document_guide(response: Any, *, source_id: str, method_id: str) -> Any:
    """Pick the ``DocumentGuide`` describing ``source_id`` from a non-empty response.

    ``DocumentGuide.source #1`` is optional in practice. A live probe on
    2026-08-31 (issue #2276) found the backend labels a guide with the
    requested id on the **first** response for a source and omits field #1
    from the wire on every repeat call -- same summary bytes, no substitute
    identifier anywhere in the message. Source type does not predict it; the
    unlabelled form is simply the steady state, so requiring the echo rejected
    guides the server really had returned for all but each source's first
    read. The same probe found the endpoint rejects a two-source request with
    ``INVALID_ARGUMENT``, so a lone unlabelled guide can only describe the
    source that was asked about.

    The rule matches ``refresh``/``check_freshness``: only a *populated* and
    *different* echo is a decoding failure. Past one guide the response is
    ambiguous, so an exact match becomes mandatory again. See
    ``docs/android/proto-evidence-ledger.md#document-guide-source-echo``.
    """

    if not response.guides:
        raise ValueError("select_document_guide requires a non-empty guides list")
    echoes = [_document_guide_echo(guide) for guide in response.guides]
    matches = [
        guide for guide, echo in zip(response.guides, echoes, strict=True) if echo == source_id
    ]
    if len(matches) > 1:
        raise _guide_failure(
            "Android source guide response contained duplicate source ids",
            source_id=source_id,
            method_id=method_id,
            response=response,
            echoes=echoes,
        )
    if matches:
        return next(iter(matches))
    sole_unlabelled = len(response.guides) == 1 and not any(echoes)
    if not sole_unlabelled:
        raise _guide_failure(
            "Android source guide response did not match the requested source id",
            source_id=source_id,
            method_id=method_id,
            response=response,
            echoes=echoes,
        )
    # The sole unlabelled guide: the normal shape for a URL source, not an
    # anomaly, so this is deliberately not logged.
    return next(iter(response.guides))


__all__ = ["decode_source", "decode_sources", "select_document_guide"]
