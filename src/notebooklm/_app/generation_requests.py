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
GenerationValidationCode: TypeAlias = Literal[
    "cinematic_style_prompt",
    "short_video_style",
    "custom_style_prompt_required",
    "style_prompt_requires_custom",
]


class GenerationRequestValidationError(ValidationError):
    """Typed semantic validation failure for adapter-owned presentation."""

    def __init__(self, code: GenerationValidationCode, message: str) -> None:
        self.code = code
        super().__init__(message)


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
        normalized = prompt.strip() if isinstance(prompt, str) else prompt
        has_prompt = isinstance(normalized, str) and bool(normalized)
        if self.video_format is VideoFormat.CINEMATIC:
            if has_prompt:
                raise GenerationRequestValidationError(
                    "cinematic_style_prompt",
                    "style_prompt is not supported for cinematic video",
                )
            # Cinematic video has no style input.  Preserve the historical
            # contract by ignoring every video_style value, including CUSTOM,
            # instead of applying the explainer-video style/prompt coupling.
        elif self.video_format is VideoFormat.SHORT and (
            self.video_style is not VideoStyle.AUTO_SELECT or has_prompt
        ):
            raise GenerationRequestValidationError(
                "short_video_style",
                "video_style and style_prompt are not supported for short video "
                "because it has a fixed visual style",
            )
        elif self.video_style is VideoStyle.CUSTOM and not has_prompt:
            raise GenerationRequestValidationError(
                "custom_style_prompt_required",
                "video_style=custom requires a non-empty style_prompt",
            )
        elif has_prompt and self.video_style is not VideoStyle.CUSTOM:
            raise GenerationRequestValidationError(
                "style_prompt_requires_custom",
                "style_prompt requires video_style=custom",
            )
        if isinstance(normalized, str):
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
    if kind in ("video", "cinematic-video"):
        # Validate video semantics before normalizing cinematic video to its
        # distinct execution variant.  Cinematic rejects a style prompt but
        # deliberately ignores video_style, including CUSTOM.
        normalized_video = VideoGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            video_format=(VideoFormat.CINEMATIC if kind == "cinematic-video" else video_format),
            video_style=video_style,
            style_prompt=style_prompt,
            wait=wait,
            timeout=timeout,
            interval=interval,
            max_retries=max_retries,
        )
        if normalized_video.video_format is VideoFormat.CINEMATIC:
            return CinematicVideoGenerationRequest(
                notebook_id=normalized_video.notebook_id,
                source_ids=normalized_video.source_ids,
                language=normalized_video.language,
                instructions=normalized_video.instructions,
                wait=normalized_video.wait,
                timeout=normalized_video.timeout,
                interval=normalized_video.interval,
                max_retries=normalized_video.max_retries,
            )
        return normalized_video
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
    "GenerationRequestValidationError",
    "GenerationValidationCode",
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
