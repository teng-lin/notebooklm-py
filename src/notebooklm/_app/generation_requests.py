"""Frozen, transport-neutral generation request variants."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Literal, TypeAlias, cast

from typing_extensions import NotRequired, TypedDict

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
    "option_not_valid_for_kind",
    "invalid_option_value",
    "cinematic_style_prompt",
    "short_video_style",
    "custom_style_prompt_required",
    "style_prompt_requires_custom",
]

GenerationOptionName: TypeAlias = Literal[
    "report_format",
    "audio_format",
    "audio_length",
    "quantity",
    "difficulty",
    "video_format",
    "style",
    "style_prompt",
    "deck_format",
    "deck_length",
    "orientation",
    "detail",
    "map_kind",
]


class GenerationValidationParams(TypedDict):
    """Machine-readable context for a generation validation failure."""

    kind: NotRequired[GenerationKind]
    option: NotRequired[GenerationOptionName]
    value: NotRequired[object]
    allowed_options: NotRequired[tuple[GenerationOptionName, ...]]
    choices: NotRequired[tuple[str, ...]]
    video_format: NotRequired[VideoFormat]
    video_style: NotRequired[VideoStyle]


class GenerationRequestValidationError(ValidationError):
    """Typed semantic validation failure for adapter-owned presentation."""

    def __init__(
        self,
        code: GenerationValidationCode,
        message: str,
        *,
        params: GenerationValidationParams | None = None,
    ) -> None:
        self.code = code
        self.params: Mapping[str, object] = MappingProxyType(dict(params or {}))
        super().__init__(message)


@dataclass(frozen=True)
class _GenerationOptionSpec:
    """One adapter spelling mapped to one typed request field."""

    request_field: str
    values: Mapping[str, object] | None
    default: object = UNSET


def _enum_values(**values: object) -> Mapping[str, object]:
    return MappingProxyType(values)


# This is the single semantic authority for the per-kind option surface.  MCP
# and REST retain their own schema declarations, but neither owns a second
# kind/choice table or validation loop.
_GENERATION_OPTION_SPECS: Mapping[
    GenerationKind, Mapping[GenerationOptionName, _GenerationOptionSpec]
] = MappingProxyType(
    {
        "audio": MappingProxyType(
            {
                "audio_format": _GenerationOptionSpec(
                    "audio_format",
                    _enum_values(
                        **{
                            "deep-dive": AudioFormat.DEEP_DIVE,
                            "brief": AudioFormat.BRIEF,
                            "critique": AudioFormat.CRITIQUE,
                            "debate": AudioFormat.DEBATE,
                        }
                    ),
                    AudioFormat.DEEP_DIVE,
                ),
                "audio_length": _GenerationOptionSpec(
                    "audio_length",
                    _enum_values(
                        short=AudioLength.SHORT,
                        default=AudioLength.DEFAULT,
                        long=AudioLength.LONG,
                    ),
                    AudioLength.DEFAULT,
                ),
            }
        ),
        "video": MappingProxyType(
            {
                "video_format": _GenerationOptionSpec(
                    "video_format",
                    _enum_values(
                        explainer=VideoFormat.EXPLAINER,
                        brief=VideoFormat.BRIEF,
                        cinematic=VideoFormat.CINEMATIC,
                        short=VideoFormat.SHORT,
                    ),
                    VideoFormat.EXPLAINER,
                ),
                "style": _GenerationOptionSpec(
                    "video_style",
                    _enum_values(
                        auto=VideoStyle.AUTO_SELECT,
                        custom=VideoStyle.CUSTOM,
                        classic=VideoStyle.CLASSIC,
                        whiteboard=VideoStyle.WHITEBOARD,
                        kawaii=VideoStyle.KAWAII,
                        anime=VideoStyle.ANIME,
                        watercolor=VideoStyle.WATERCOLOR,
                        **{
                            "retro-print": VideoStyle.RETRO_PRINT,
                            "heritage": VideoStyle.HERITAGE,
                            "paper-craft": VideoStyle.PAPER_CRAFT,
                        },
                    ),
                    VideoStyle.AUTO_SELECT,
                ),
                "style_prompt": _GenerationOptionSpec("style_prompt", None),
            }
        ),
        "cinematic-video": MappingProxyType({}),
        "slide-deck": MappingProxyType(
            {
                "deck_format": _GenerationOptionSpec(
                    "slide_format",
                    _enum_values(
                        detailed=SlideDeckFormat.DETAILED_DECK,
                        presenter=SlideDeckFormat.PRESENTER_SLIDES,
                    ),
                    SlideDeckFormat.DETAILED_DECK,
                ),
                "deck_length": _GenerationOptionSpec(
                    "slide_length",
                    _enum_values(default=SlideDeckLength.DEFAULT, short=SlideDeckLength.SHORT),
                    SlideDeckLength.DEFAULT,
                ),
            }
        ),
        "revise-slide": MappingProxyType({}),
        "quiz": MappingProxyType(
            {
                "quantity": _GenerationOptionSpec(
                    "quantity",
                    _enum_values(
                        fewer=QuizQuantity.FEWER,
                        standard=QuizQuantity.STANDARD,
                        more=QuizQuantity.MORE,
                    ),
                    QuizQuantity.STANDARD,
                ),
                "difficulty": _GenerationOptionSpec(
                    "difficulty",
                    _enum_values(
                        easy=QuizDifficulty.EASY,
                        medium=QuizDifficulty.MEDIUM,
                        hard=QuizDifficulty.HARD,
                    ),
                    QuizDifficulty.MEDIUM,
                ),
            }
        ),
        "flashcards": MappingProxyType(
            {
                "quantity": _GenerationOptionSpec(
                    "quantity",
                    _enum_values(
                        fewer=QuizQuantity.FEWER,
                        standard=QuizQuantity.STANDARD,
                        more=QuizQuantity.MORE,
                    ),
                    QuizQuantity.STANDARD,
                ),
                "difficulty": _GenerationOptionSpec(
                    "difficulty",
                    _enum_values(
                        easy=QuizDifficulty.EASY,
                        medium=QuizDifficulty.MEDIUM,
                        hard=QuizDifficulty.HARD,
                    ),
                    QuizDifficulty.MEDIUM,
                ),
            }
        ),
        "infographic": MappingProxyType(
            {
                "orientation": _GenerationOptionSpec(
                    "orientation",
                    _enum_values(
                        landscape=InfographicOrientation.LANDSCAPE,
                        portrait=InfographicOrientation.PORTRAIT,
                        square=InfographicOrientation.SQUARE,
                    ),
                    InfographicOrientation.LANDSCAPE,
                ),
                "detail": _GenerationOptionSpec(
                    "detail_level",
                    _enum_values(
                        concise=InfographicDetail.CONCISE,
                        standard=InfographicDetail.STANDARD,
                        detailed=InfographicDetail.DETAILED,
                    ),
                    InfographicDetail.STANDARD,
                ),
                "style": _GenerationOptionSpec(
                    "infographic_style",
                    _enum_values(
                        auto=InfographicStyle.AUTO_SELECT,
                        **{
                            "sketch-note": InfographicStyle.SKETCH_NOTE,
                            "professional": InfographicStyle.PROFESSIONAL,
                            "bento-grid": InfographicStyle.BENTO_GRID,
                            "editorial": InfographicStyle.EDITORIAL,
                            "instructional": InfographicStyle.INSTRUCTIONAL,
                            "bricks": InfographicStyle.BRICKS,
                            "clay": InfographicStyle.CLAY,
                            "anime": InfographicStyle.ANIME,
                            "kawaii": InfographicStyle.KAWAII,
                            "scientific": InfographicStyle.SCIENTIFIC,
                        },
                    ),
                    InfographicStyle.AUTO_SELECT,
                ),
            }
        ),
        "data-table": MappingProxyType({}),
        "mind-map": MappingProxyType(
            {
                "map_kind": _GenerationOptionSpec(
                    "map_kind",
                    _enum_values(
                        interactive=MindMapKind.INTERACTIVE,
                        **{"note-backed": MindMapKind.NOTE_BACKED},
                    ),
                    MindMapKind.INTERACTIVE,
                )
            }
        ),
        "report": MappingProxyType(
            {
                "report_format": _GenerationOptionSpec(
                    "report_format",
                    _enum_values(
                        **{
                            "briefing-doc": ReportFormat.BRIEFING_DOC,
                            "study-guide": ReportFormat.STUDY_GUIDE,
                            "blog-post": ReportFormat.BLOG_POST,
                            "custom": ReportFormat.CUSTOM,
                        }
                    ),
                    ReportFormat.BRIEFING_DOC,
                )
            }
        ),
    }
)


def generation_option_choices(
    kind: GenerationKind,
) -> Mapping[GenerationOptionName, tuple[str, ...] | None]:
    """Return an immutable schema projection of the neutral option contract."""

    return MappingProxyType(
        {
            name: None if spec.values is None else tuple(spec.values)
            for name, spec in _GENERATION_OPTION_SPECS[kind].items()
        }
    )


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
                    params={
                        "option": "style_prompt",
                        "value": normalized,
                        "video_format": self.video_format,
                        "video_style": self.video_style,
                    },
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
                params={
                    "video_format": self.video_format,
                    "video_style": self.video_style,
                    "value": normalized,
                },
            )
        elif self.video_style is VideoStyle.CUSTOM and not has_prompt:
            raise GenerationRequestValidationError(
                "custom_style_prompt_required",
                "video_style=custom requires a non-empty style_prompt",
                params={
                    "option": "style_prompt",
                    "video_format": self.video_format,
                    "video_style": self.video_style,
                    "value": normalized,
                },
            )
        elif has_prompt and self.video_style is not VideoStyle.CUSTOM:
            raise GenerationRequestValidationError(
                "style_prompt_requires_custom",
                "style_prompt requires video_style=custom",
                params={
                    "option": "style_prompt",
                    "video_format": self.video_format,
                    "video_style": self.video_style,
                    "value": normalized,
                },
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


def _option_error(
    kind: GenerationKind,
    option: GenerationOptionName,
    value: object,
) -> GenerationRequestValidationError:
    allowed = tuple(_GENERATION_OPTION_SPECS[kind])
    accepts = (
        f"this kind accepts {sorted(allowed)}"
        if allowed
        else "this kind accepts no per-kind options"
    )
    return GenerationRequestValidationError(
        "option_not_valid_for_kind",
        f"option {option!r} is not valid for generation kind {kind!r}; {accepts}",
        params={
            "kind": kind,
            "option": option,
            "value": value,
            "allowed_options": allowed,
        },
    )


def _normalize_option_value(
    kind: GenerationKind,
    option: GenerationOptionName,
    spec: _GenerationOptionSpec,
    value: object,
) -> object:
    if value is None:
        # Adapter schemas historically use ``None`` for an omitted enum option.
        # Free-text style_prompt is the one field where explicit None reaches the
        # typed request and remains distinct from a direct caller's UNSET.
        return None if spec.values is None else spec.default

    if spec.values is None:
        if isinstance(value, str):
            return value
        choices: tuple[str, ...] = ()
        expected = "text or None"
    else:
        choices = tuple(spec.values)
        expected_type = type(spec.default)
        if isinstance(value, expected_type) and any(
            value is candidate for candidate in spec.values.values()
        ):
            return value
        if isinstance(value, str) and value in spec.values:
            return spec.values[value]
        expected = f"one of {list(choices)}"

    raise GenerationRequestValidationError(
        "invalid_option_value",
        f"Invalid {option} {value!r}; expected {expected}",
        params={
            "kind": kind,
            "option": option,
            "value": value,
            "choices": choices,
        },
    )


def _normalize_generation_options(
    kind: GenerationKind,
    option_values: Mapping[GenerationOptionName, object],
    direct_values: tuple[tuple[GenerationOptionName, str, object], ...],
) -> dict[str, object]:
    """Validate and normalize schema and typed-call options through one table."""

    specs = _GENERATION_OPTION_SPECS[kind]
    normalized = {spec.request_field: spec.default for spec in specs.values()}
    cinematic_compat: dict[str, object] = {}

    for option, request_field, value in direct_values:
        if value is UNSET:
            continue
        spec = specs.get(option)
        if spec is None or spec.request_field != request_field:
            # The typed builder predates the dedicated cinematic variant. Keep
            # its compatibility contract: video format/style are accepted and
            # ignored for that variant, while style_prompt still participates in
            # the shared video semantic validation below.
            if kind == "cinematic-video" and request_field in {
                "video_format",
                "video_style",
                "style_prompt",
            }:
                cinematic_compat[request_field] = value
                continue
            # Existing callers use None to mean "no override". Do not turn a
            # neutral placeholder into a wrong-kind failure.
            if value is None:
                continue
            raise _option_error(kind, option, value)
        normalized[request_field] = _normalize_option_value(kind, option, spec, value)

    for option, value in option_values.items():
        if value is UNSET:
            continue
        spec = specs.get(option)
        if spec is None:
            if value is None:
                continue
            raise _option_error(kind, option, value)
        normalized[spec.request_field] = _normalize_option_value(kind, option, spec, value)

    normalized.update(cinematic_compat)
    return normalized


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
    option_values: Mapping[GenerationOptionName, object] | None = None,
    audio_format: AudioFormat | None | UnsetType = UNSET,
    audio_length: AudioLength | None | UnsetType = UNSET,
    video_format: VideoFormat | None | UnsetType = UNSET,
    video_style: VideoStyle | None | UnsetType = UNSET,
    style_prompt: OptionalText = UNSET,
    slide_format: SlideDeckFormat | None | UnsetType = UNSET,
    slide_length: SlideDeckLength | None | UnsetType = UNSET,
    artifact_id: str = "",
    slide_index: int = 0,
    quantity: QuizQuantity | None | UnsetType = UNSET,
    difficulty: QuizDifficulty | None | UnsetType = UNSET,
    orientation: InfographicOrientation | None | UnsetType = UNSET,
    detail_level: InfographicDetail | None | UnsetType = UNSET,
    infographic_style: InfographicStyle | None | UnsetType = UNSET,
    map_kind: MindMapKind | None | UnsetType = UNSET,
    report_format: ReportFormat | None | UnsetType = UNSET,
    extra_instructions: OptionalText = UNSET,
) -> GenerationRequest:
    """Build one frozen variant using the canonical per-kind option contract."""

    normalized = _normalize_generation_options(
        kind,
        option_values or {},
        (
            ("report_format", "report_format", report_format),
            ("audio_format", "audio_format", audio_format),
            ("audio_length", "audio_length", audio_length),
            ("quantity", "quantity", quantity),
            ("difficulty", "difficulty", difficulty),
            ("video_format", "video_format", video_format),
            ("style", "video_style", video_style),
            ("style_prompt", "style_prompt", style_prompt),
            ("deck_format", "slide_format", slide_format),
            ("deck_length", "slide_length", slide_length),
            ("orientation", "orientation", orientation),
            ("detail", "detail_level", detail_level),
            ("style", "infographic_style", infographic_style),
            ("map_kind", "map_kind", map_kind),
        ),
    )

    if kind == "audio":
        return AudioGenerationRequest(
            notebook_id=notebook_id,
            source_ids=source_ids,
            language=language,
            instructions=instructions,
            audio_format=normalized["audio_format"],  # type: ignore[arg-type]
            audio_length=normalized["audio_length"],  # type: ignore[arg-type]
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
            video_format=cast(
                VideoFormat,
                VideoFormat.CINEMATIC if kind == "cinematic-video" else normalized["video_format"],
            ),
            video_style=normalized.get("video_style", VideoStyle.AUTO_SELECT),  # type: ignore[arg-type]
            style_prompt=normalized.get("style_prompt", UNSET),  # type: ignore[arg-type]
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
            slide_format=normalized["slide_format"],  # type: ignore[arg-type]
            slide_length=normalized["slide_length"],  # type: ignore[arg-type]
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
            quantity=normalized["quantity"],  # type: ignore[arg-type]
            difficulty=normalized["difficulty"],  # type: ignore[arg-type]
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
            quantity=normalized["quantity"],  # type: ignore[arg-type]
            difficulty=normalized["difficulty"],  # type: ignore[arg-type]
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
            orientation=normalized["orientation"],  # type: ignore[arg-type]
            detail_level=normalized["detail_level"],  # type: ignore[arg-type]
            style=normalized["infographic_style"],  # type: ignore[arg-type]
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
            map_kind=normalized["map_kind"],  # type: ignore[arg-type]
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
            report_format=cast(
                ReportFormat,
                ReportFormat.CUSTOM
                if isinstance(instructions, str)
                and instructions
                and normalized["report_format"] is ReportFormat.BRIEFING_DOC
                else normalized["report_format"],
            ),
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
    "GenerationOptionName",
    "GenerationRequest",
    "GenerationRequestValidationError",
    "GenerationValidationCode",
    "GenerationValidationParams",
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
    "generation_option_choices",
]
