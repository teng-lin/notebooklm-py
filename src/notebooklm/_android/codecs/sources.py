"""Projection of Android source protobuf messages into public types."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
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


# Structural prefix only: enough to see which field leads the first guide
# without carrying the summary text that follows it.
_WIRE_PREFIX_BYTES = 24


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

    ``raw_response`` carries only a short structural prefix of the wire bytes --
    enough to read which field leads the first guide, which is the question
    these branches exist to answer. It is capped *here* rather than left to
    ``_truncate_response_preview``: hex-encoding makes the credential shapes
    that ``scrub_secrets`` looks for unmatchable, and ``NOTEBOOKLM_DEBUG=1``
    otherwise opts out of truncation entirely, which would splice a whole
    model-written summary of the user's source into the exception string.
    """

    observed = ", ".join(_guide_echo_diagnostic(echoes)) or "<none>"
    wire = response.SerializeToString()
    return DecodingError(
        f"{reason} (requested={source_id}, guides={len(response.guides)}, "
        f"observed=[{observed}], bytes={len(wire)})",
        method_id=method_id,
        found_ids=_guide_echo_diagnostic(echoes),
        raw_response=wire[:_WIRE_PREFIX_BYTES].hex(),
    )


def select_document_guide(response: Any, *, source_id: str, method_id: str) -> Any:
    """Pick the ``DocumentGuide`` describing ``source_id`` from a non-empty response.

    ``DocumentGuide.source #1`` is optional in practice: a live probe on
    2026-08-31 (issue #2276) found text sources echo the requested id while URL
    sources omit field #1 from the wire entirely, with no substitute identifier
    elsewhere in the message. The same probe found the endpoint rejects a
    two-source request with ``INVALID_ARGUMENT``, so a lone unlabelled guide can
    only describe the source that was asked about. Treating the missing echo as
    a mismatch therefore rejected guides the server really had returned.

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
