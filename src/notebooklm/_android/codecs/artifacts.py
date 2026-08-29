"""Strict Android artifact protobuf projection."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timezone
from typing import Any

from ..._types.artifact_content import (
    ArtifactInfographic,
    ArtifactMedia,
    ArtifactMediaType,
    ArtifactSlide,
)
from ..._types.artifacts import Artifact, ReportSuggestion
from ...exceptions import DecodingError
from ..errors import sanitize_escaping_exception

_MEDIA_KINDS = {
    1: ArtifactMediaType.PROGRESSIVE,
    2: ArtifactMediaType.HLS,
    3: ArtifactMediaType.DASH,
    4: ArtifactMediaType.DOWNLOAD,
}


def _duration_seconds(value: Any) -> float | None:
    seconds = int(value.seconds)
    nanos = int(value.nanos)
    if seconds == 0 and nanos == 0:
        return None
    return seconds + nanos / 1_000_000_000


def _media(values: Iterable[Any]) -> tuple[ArtifactMedia, ...]:
    return tuple(
        ArtifactMedia(
            url=item.url,
            kind=_MEDIA_KINDS.get(int(item.type), ArtifactMediaType.UNKNOWN),
            type_code=int(item.type),
        )
        for item in values
        if item.url
    )


def _preferred_media(values: tuple[ArtifactMedia, ...]) -> str | None:
    for item in values:
        if item.kind is ArtifactMediaType.DOWNLOAD:
            return item.url
    if not values:
        return None
    first, *_remaining = values
    return first.url


def _prompt(message: Any, type_code: int) -> str | None:
    if type_code == 1 and message.HasField("audio_overview"):
        value = message.audio_overview.generation_options.episode_focus
    elif type_code == 2 and message.HasField("tailored_report"):
        value = message.tailored_report.generation_options.document_directive
    elif type_code == 3 and message.HasField("explainer_video"):
        value = message.explainer_video.generation_options.video_focus
    elif type_code == 4 and message.HasField("app"):
        value = message.app.generation_options.free_text_steering_prompt
    elif type_code == 7 and message.HasField("infographic"):
        value = message.infographic.generation_options.user_steering_prompt
    elif type_code == 8 and message.HasField("slides"):
        value = message.slides.generation_options.user_steering_prompt
    else:
        return None
    return value or None


def _decode_artifact(message: Any, *, method_id: str) -> Artifact:
    """Decode only the B4-ledgered artifact fields."""

    artifact_id = message.artifact_id
    if not artifact_id:
        raise DecodingError(
            "Android artifact response omitted its required artifact id.",
            method_id=method_id,
        )

    type_code = int(message.type)
    media_urls: tuple[ArtifactMedia, ...] = ()
    duration_seconds: float | None = None
    slides: tuple[ArtifactSlide, ...] = ()
    infographics: tuple[ArtifactInfographic, ...] = ()
    report_kind: str | None = None
    variant: int | None = None
    url: str | None = None

    if message.HasField("audio_overview"):
        media_urls = _media(message.audio_overview.media_urls)
        duration_seconds = _duration_seconds(message.audio_overview.duration)
        if type_code == 1:
            url = _preferred_media(media_urls)
    if message.HasField("explainer_video"):
        video_media = _media(message.explainer_video.media_urls)
        if type_code == 3:
            media_urls = video_media
            duration_seconds = _duration_seconds(message.explainer_video.duration)
            url = _preferred_media(video_media)
    if message.HasField("tailored_report"):
        report_kind = message.tailored_report.generation_options.type or None
    if message.HasField("app"):
        variant = int(message.app.generation_options.app_type) or None
    if message.HasField("infographic"):
        infographics = tuple(
            ArtifactInfographic(
                title=item.title or None,
                image_url=item.image.url or None,
                width=None,
                height=None,
                alt_text=None,
                text=None,
            )
            for item in message.infographic.infographics
        )
        if type_code == 7:
            url = next((item.image_url for item in infographics if item.image_url), None)
    if message.HasField("slides"):
        slides = tuple(
            ArtifactSlide(
                image_url=item.image.url or None,
                width=None,
                height=None,
                alt_text=None,
                text=None,
            )
            for item in message.slides.slides
        )
        if type_code == 8:
            url = message.slides.pdf_download_url or None
    if message.HasField("file") and type_code == 10:
        url = message.file.file_download_url or message.file.file_preview_url or None

    last_modified_at = None
    if message.HasField("last_modified_timestamp"):
        last_modified_at = message.last_modified_timestamp.ToDatetime(tzinfo=timezone.utc)

    source_ids = tuple(source.source_id.id for source in message.sources if source.source_id.id)
    if not source_ids and type_code == 1 and message.HasField("audio_overview"):
        source_ids = tuple(
            source.id
            for source in message.audio_overview.generation_options.source_ids
            if source.id
        )

    return Artifact(
        id=artifact_id,
        title=message.title,
        _artifact_type=type_code,
        status=int(message.status),
        created_at=None,
        url=url,
        _variant=variant,
        generation_prompt=_prompt(message, type_code),
        media_urls=media_urls,
        duration_seconds=duration_seconds,
        slides=slides,
        infographics=infographics,
        report_kind=report_kind,
        source_ids=source_ids,
        last_modified_at=last_modified_at,
        etag=message.etag or None,
    )


def decode_artifact(message: Any, *, method_id: str) -> Artifact:
    """Decode one artifact without retaining its capability URLs on failure."""

    result: Artifact | None = None
    failure: BaseException | None = None
    try:
        result = _decode_artifact(message, method_id=method_id)
    except DecodingError as error:
        failure = sanitize_escaping_exception(error)
    except (KeyboardInterrupt, SystemExit) as error:
        failure = sanitize_escaping_exception(error)
    except BaseException:
        failure = DecodingError(
            "Could not decode Android artifact response.",
            method_id=method_id,
        )
    finally:
        del message
    if failure is not None:
        failure.__cause__ = None
        failure.__context__ = None
        raise failure from None
    assert result is not None
    return result


def decode_artifacts(messages: Iterable[Any], *, method_id: str) -> list[Artifact]:
    """Decode an ordered artifact sequence without masking malformed rows."""

    decoded: list[Artifact] = []
    failure: BaseException | None = None
    raw_message: Any | None = None
    try:
        for raw_message in messages:
            decoded.append(decode_artifact(raw_message, method_id=method_id))
    except BaseException as error:
        failure = sanitize_escaping_exception(error)
    finally:
        del messages, raw_message
        if failure is not None:
            decoded.clear()
    if failure is not None:
        failure.__cause__ = None
        failure.__context__ = None
        raise failure from None
    return decoded


def decode_report_suggestions(messages: Iterable[Any]) -> list[ReportSuggestion]:
    """Project well-formed live-added report suggestion rows."""

    return [
        ReportSuggestion(
            title=item.title,
            description=item.description,
            prompt=item.prompt,
            audience_level=int(item.audience_level) or 2,
        )
        for item in messages
    ]


__all__ = ["decode_artifact", "decode_artifacts", "decode_report_suggestions"]
