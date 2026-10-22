"""Closed public-input requests constructed by the artifact feature facade.

``creation_policy`` resolves these into the separate per-family frozen union in
``creation_normalized`` before calling either backend's protocol hook.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from .._types.enums import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)


@dataclass(frozen=True)
class _CreationRequest:
    notebook_id: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class AudioCreationRequest(_CreationRequest):
    language: str
    instructions: str | None
    audio_format: AudioFormat | None
    audio_length: AudioLength | None
    later: bool = False


@dataclass(frozen=True)
class VideoCreationRequest(_CreationRequest):
    language: str
    instructions: str | None
    video_format: VideoFormat | None
    video_style: VideoStyle | None
    style_prompt: str | None


@dataclass(frozen=True)
class CinematicVideoCreationRequest(_CreationRequest):
    language: str
    instructions: str | None


@dataclass(frozen=True)
class ReportCreationRequest(_CreationRequest):
    report_format: ReportFormat
    language: str
    custom_prompt: str | None
    extra_instructions: str | None


@dataclass(frozen=True)
class QuizCreationRequest(_CreationRequest):
    instructions: str | None
    quantity: QuizQuantity | None
    difficulty: QuizDifficulty | None


@dataclass(frozen=True)
class FlashcardsCreationRequest(QuizCreationRequest):
    pass


@dataclass(frozen=True)
class InfographicCreationRequest(_CreationRequest):
    language: str
    instructions: str | None
    orientation: InfographicOrientation | None
    detail_level: InfographicDetail | None
    style: InfographicStyle | None


@dataclass(frozen=True)
class SlideDeckCreationRequest(_CreationRequest):
    language: str
    instructions: str | None
    slide_format: SlideDeckFormat | None
    slide_length: SlideDeckLength | None


@dataclass(frozen=True)
class DataTableCreationRequest(_CreationRequest):
    language: str
    instructions: str | None


@dataclass(frozen=True)
class InteractiveMindMapCreationRequest(_CreationRequest):
    language: str
    instructions: str | None


ArtifactCreationRequest: TypeAlias = (
    AudioCreationRequest
    | VideoCreationRequest
    | CinematicVideoCreationRequest
    | ReportCreationRequest
    | QuizCreationRequest
    | FlashcardsCreationRequest
    | InfographicCreationRequest
    | SlideDeckCreationRequest
    | DataTableCreationRequest
    | InteractiveMindMapCreationRequest
)
