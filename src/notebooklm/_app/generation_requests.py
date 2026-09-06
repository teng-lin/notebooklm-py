"""Frozen, transport-neutral generation request variants."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, TypeAlias

from ..exceptions import ValidationError
from ..types import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    MindMapKind,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)

GenerationKind: TypeAlias = Literal[
    "audio",
    "video",
    "cinematic-video",
    "slide-deck",
    "revise-slide",
    "quiz",
    "flashcards",
    "infographic",
    "data-table",
    "mind-map",
    "report",
]


class UnsetType(Enum):
    """Sentinel preserving omission separately from explicit ``None``/empty."""

    UNSET = "unset"


UNSET = UnsetType.UNSET
OptionalText: TypeAlias = str | None | UnsetType
SourceSelection: TypeAlias = tuple[str, ...] | UnsetType


@dataclass(frozen=True, kw_only=True)
class _GenerationRequestBase:
    notebook_id: str
    wait: bool = False
    timeout: float = 300.0
    interval: float = 2.0
    max_retries: int = 0

    def __post_init__(self) -> None:
        if not self.notebook_id.strip():
            raise ValidationError("notebook_id must not be empty")
        if self.timeout <= 0:
            raise ValidationError("timeout must be positive")
        if self.interval <= 0:
            raise ValidationError("interval must be positive")
        if self.max_retries < 0:
            raise ValidationError("max_retries must be non-negative")


@dataclass(frozen=True, kw_only=True)
class _SourcedGenerationRequest(_GenerationRequestBase):
    source_ids: SourceSelection = UNSET

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.source_ids is not UNSET and any(not item.strip() for item in self.source_ids):
            raise ValidationError("source_ids must not contain empty values")


@dataclass(frozen=True, kw_only=True)
class AudioGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["audio"] = "audio"
    language: OptionalText = UNSET
    instructions: OptionalText = UNSET
    audio_format: AudioFormat = AudioFormat.DEEP_DIVE
    audio_length: AudioLength = AudioLength.DEFAULT


@dataclass(frozen=True, kw_only=True)
class VideoGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["video"] = "video"
    language: OptionalText = UNSET
    instructions: OptionalText = UNSET
    video_format: VideoFormat = VideoFormat.EXPLAINER
    video_style: VideoStyle = VideoStyle.AUTO_SELECT
    style_prompt: OptionalText = UNSET

    def __post_init__(self) -> None:
        super().__post_init__()
        prompt = self.style_prompt
        normalized = prompt.strip() if isinstance(prompt, str) else None
        if self.video_format is VideoFormat.CINEMATIC and normalized:
            raise ValidationError("--style-prompt cannot be used with cinematic video")
        if self.video_format is VideoFormat.SHORT and (
            self.video_style is not VideoStyle.AUTO_SELECT or normalized
        ):
            raise ValidationError(
                "--style/--style-prompt cannot be used with --format short "
                "(short video has a fixed visual style)"
            )
        if self.video_style is VideoStyle.CUSTOM and not normalized:
            raise ValidationError("--style custom requires --style-prompt")
        if normalized and self.video_style is not VideoStyle.CUSTOM:
            raise ValidationError("--style-prompt requires --style custom")
        if normalized is not None:
            object.__setattr__(self, "style_prompt", normalized)


@dataclass(frozen=True, kw_only=True)
class CinematicVideoGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["cinematic-video"] = "cinematic-video"
    language: OptionalText = UNSET
    instructions: OptionalText = UNSET


@dataclass(frozen=True, kw_only=True)
class SlideDeckGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["slide-deck"] = "slide-deck"
    language: OptionalText = UNSET
    instructions: OptionalText = UNSET
    slide_format: SlideDeckFormat = SlideDeckFormat.DETAILED_DECK
    slide_length: SlideDeckLength = SlideDeckLength.DEFAULT


@dataclass(frozen=True, kw_only=True)
class ReviseSlideGenerationRequest(_GenerationRequestBase):
    kind: Literal["revise-slide"] = "revise-slide"
    artifact_id: str
    slide_index: int
    prompt: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.artifact_id.strip():
            raise ValidationError("artifact_id must not be empty")
        if self.slide_index < 0:
            raise ValidationError("slide_index must be non-negative")
        if not self.prompt.strip():
            raise ValidationError("prompt must not be empty")


@dataclass(frozen=True, kw_only=True)
class QuizGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["quiz"] = "quiz"
    instructions: OptionalText = UNSET
    quantity: QuizQuantity = QuizQuantity.STANDARD
    difficulty: QuizDifficulty = QuizDifficulty.MEDIUM


@dataclass(frozen=True, kw_only=True)
class FlashcardsGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["flashcards"] = "flashcards"
    instructions: OptionalText = UNSET
    quantity: QuizQuantity = QuizQuantity.STANDARD
    difficulty: QuizDifficulty = QuizDifficulty.MEDIUM


@dataclass(frozen=True, kw_only=True)
class InfographicGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["infographic"] = "infographic"
    language: OptionalText = UNSET
    instructions: OptionalText = UNSET
    orientation: InfographicOrientation = InfographicOrientation.LANDSCAPE
    detail_level: InfographicDetail = InfographicDetail.STANDARD
    style: InfographicStyle = InfographicStyle.AUTO_SELECT


@dataclass(frozen=True, kw_only=True)
class DataTableGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["data-table"] = "data-table"
    language: OptionalText = UNSET
    instructions: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.instructions.strip():
            raise ValidationError("data-table instructions must not be empty")


@dataclass(frozen=True, kw_only=True)
class MindMapGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["mind-map"] = "mind-map"
    language: OptionalText = UNSET
    instructions: OptionalText = UNSET
    map_kind: MindMapKind = MindMapKind.INTERACTIVE
    wait: bool = False
    max_retries: int = 0


@dataclass(frozen=True, kw_only=True)
class ReportGenerationRequest(_SourcedGenerationRequest):
    kind: Literal["report"] = "report"
    language: OptionalText = UNSET
    report_format: ReportFormat = ReportFormat.BRIEFING_DOC
    custom_prompt: OptionalText = UNSET
    extra_instructions: OptionalText = UNSET


GenerationRequest: TypeAlias = (
    AudioGenerationRequest
    | VideoGenerationRequest
    | CinematicVideoGenerationRequest
    | SlideDeckGenerationRequest
    | ReviseSlideGenerationRequest
    | QuizGenerationRequest
    | FlashcardsGenerationRequest
    | InfographicGenerationRequest
    | DataTableGenerationRequest
    | MindMapGenerationRequest
    | ReportGenerationRequest
)


def build_generation_request(
    kind: GenerationKind,
    *,
    notebook_id: str,
    source_ids: SourceSelection = UNSET,
    language: OptionalText = UNSET,
    instructions: OptionalText = UNSET,
    wait: bool = False,
    timeout: float = 300.0,
    interval: float = 2.0,
    max_retries: int = 0,
    audio_format: AudioFormat = AudioFormat.DEEP_DIVE,
    audio_length: AudioLength = AudioLength.DEFAULT,
    video_format: VideoFormat = VideoFormat.EXPLAINER,
    video_style: VideoStyle = VideoStyle.AUTO_SELECT,
    style_prompt: OptionalText = UNSET,
    slide_format: SlideDeckFormat = SlideDeckFormat.DETAILED_DECK,
    slide_length: SlideDeckLength = SlideDeckLength.DEFAULT,
    artifact_id: str = "",
    slide_index: int = 0,
    quantity: QuizQuantity = QuizQuantity.STANDARD,
    difficulty: QuizDifficulty = QuizDifficulty.MEDIUM,
    orientation: InfographicOrientation = InfographicOrientation.LANDSCAPE,
    detail_level: InfographicDetail = InfographicDetail.STANDARD,
    infographic_style: InfographicStyle = InfographicStyle.AUTO_SELECT,
    map_kind: MindMapKind = MindMapKind.INTERACTIVE,
    report_format: ReportFormat = ReportFormat.BRIEFING_DOC,
    extra_instructions: OptionalText = UNSET,
) -> GenerationRequest:
    """Build one frozen variant from adapter-normalized named values."""

    if kind == "audio":
        return AudioGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            audio_format=audio_format,
            audio_length=audio_length,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "video":
        if video_format is VideoFormat.CINEMATIC:
            return CinematicVideoGenerationRequest(
                notebook_id=notebook_id,
                source_ids=source_ids,
                language=language,
                instructions=instructions,
                wait=wait,
                timeout=timeout,
                interval=interval,
                max_retries=max_retries,
            )
        return VideoGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            video_format=video_format,
            video_style=video_style,
            style_prompt=style_prompt,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "cinematic-video":
        return CinematicVideoGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "slide-deck":
        return SlideDeckGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            slide_format=slide_format,
            slide_length=slide_length,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "revise-slide":
        prompt = instructions if isinstance(instructions, str) else ""
        return ReviseSlideGenerationRequest(
            notebook_id=notebook_id,
            artifact_id=artifact_id,
            slide_index=slide_index,
            prompt=prompt,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "quiz":
        return QuizGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            instructions=instructions,
            quantity=quantity,
            difficulty=difficulty,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "flashcards":
        return FlashcardsGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            instructions=instructions,
            quantity=quantity,
            difficulty=difficulty,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "infographic":
        return InfographicGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            orientation=orientation,
            detail_level=detail_level,
            style=infographic_style,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "data-table":
        if not isinstance(instructions, str):
            raise ValidationError("data-table instructions must be supplied")
        return DataTableGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "mind-map":
        return MindMapGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            map_kind=map_kind,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    if kind == "report":
        return ReportGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            report_format=report_format,
            custom_prompt=instructions,
            extra_instructions=extra_instructions,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
    raise AssertionError(f"unhandled generation kind: {kind}")


__all__ = [
    "AudioGenerationRequest",
    "CinematicVideoGenerationRequest",
    "DataTableGenerationRequest",
    "FlashcardsGenerationRequest",
    "GenerationKind",
    "GenerationRequest",
    "InfographicGenerationRequest",
    "MindMapGenerationRequest",
    "QuizGenerationRequest",
    "ReportGenerationRequest",
    "ReviseSlideGenerationRequest",
    "SlideDeckGenerationRequest",
    "UNSET",
    "UnsetType",
    "VideoGenerationRequest",
    "build_generation_request",
]
