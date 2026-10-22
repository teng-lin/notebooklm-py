"""Shared creation normalization with explicit historical backend policies.

Web permits empty sources and historically does not validate language/instruction
strings or most enum memberships. Android rejects these inputs. Preserve those
accepted sets here; capability metadata is not permission to tighten them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, cast

from typing_extensions import assert_never

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
from ..exceptions import ValidationError
from . import creation as raw
from . import creation_normalized as normalized
from .creation_reports import _REPORT_CONFIGS
from .validation import coerce_report_format


@dataclass(frozen=True)
class CreationPolicy:
    strict_inputs: bool
    allow_empty_sources: bool
    concept_reports: bool
    interactive_language: bool
    validate_video_constraints: bool = True


WEB_CREATION_POLICY = CreationPolicy(False, True, False, False)
DEFAULT_QUIZ_QUANTITY: Final = QuizQuantity.STANDARD
DEFAULT_QUIZ_DIFFICULTY: Final = QuizDifficulty.MEDIUM
# Direct private payload builders historically permit preset-style prompts, which the
# public feature boundary rejects. Their compatibility policy does not change the hook.
WEB_BUILDER_POLICY = CreationPolicy(False, True, False, False, False)
ANDROID_CREATION_POLICY = CreationPolicy(True, False, True, True)


def _code(value: Enum | None, default: Enum, parameter: str, *, strict: bool) -> int:
    if value is None:
        value = default
    if strict and not isinstance(value, type(default)):
        raise ValidationError(f"{parameter} must be a {type(default).__name__} member or None")
    return cast(int, value.value)


def _language(value: str, policy: CreationPolicy) -> str:
    if policy.strict_inputs and (not isinstance(value, str) or not value.strip()):
        raise ValidationError("language must be a non-empty string")
    return value


def _text(value: str | None, parameter: str, policy: CreationPolicy) -> str | None:
    if policy.strict_inputs and value is not None and not isinstance(value, str):
        raise ValidationError(f"{parameter} must be a string or None")
    return value


def normalize_video_prompt(
    video_format: VideoFormat | None,
    video_style: VideoStyle | None,
    style_prompt: str | None,
) -> str | None:
    """Shared video constraints, also usable before source-resolution I/O."""
    if style_prompt is not None and not isinstance(style_prompt, str):
        raise ValidationError("style_prompt must be a string or None")
    prompt = style_prompt.strip() if style_prompt is not None else None
    if video_format == VideoFormat.CINEMATIC and prompt:
        raise ValidationError("style_prompt is not supported for cinematic videos")
    if video_format == VideoFormat.SHORT and (
        (video_style is not None and video_style != VideoStyle.AUTO_SELECT) or prompt
    ):
        raise ValidationError(
            "video_style and style_prompt are not supported for short videos (short has a fixed visual style)"
        )
    if video_style == VideoStyle.CUSTOM and not prompt:
        raise ValidationError("style_prompt is required when video_style is CUSTOM")
    if prompt and video_style != VideoStyle.CUSTOM:
        raise ValidationError("style_prompt requires video_style=VideoStyle.CUSTOM")
    return prompt


def normalize_creation(
    request: raw.ArtifactCreationRequest,
    policy: CreationPolicy,
) -> normalized.NormalizedArtifactCreationRequest:
    """Resolve every domain default before either protocol hook is entered."""
    if not policy.allow_empty_sources and not request.source_ids:
        label = type(request).__name__.removesuffix("CreationRequest")
        # Preserve the established human-readable error labels.
        import re

        label = re.sub(r"(?<!^)(?=[A-Z])", " ", label)
        raise ValidationError(f"{label} generation requires at least one source id")
    nb, sources = request.notebook_id, request.source_ids
    strict = policy.strict_inputs
    if isinstance(request, raw.AudioCreationRequest):
        return normalized.NormalizedAudio(
            nb,
            sources,
            _language(request.language, policy),
            _text(request.instructions, "instructions", policy),
            _code(request.audio_format, AudioFormat.DEEP_DIVE, "audio_format", strict=strict),
            _code(request.audio_length, AudioLength.DEFAULT, "audio_length", strict=strict),
            request.later,
        )
    if isinstance(request, raw.VideoCreationRequest):
        prompt = (
            normalize_video_prompt(request.video_format, request.video_style, request.style_prompt)
            if policy.validate_video_constraints
            else request.style_prompt
        )
        format_code = _code(
            request.video_format, VideoFormat.EXPLAINER, "video_format", strict=strict
        )
        style_code = _code(
            request.video_style, VideoStyle.AUTO_SELECT, "video_style", strict=strict
        )
        if strict and format_code == VideoFormat.CINEMATIC.value:
            style_code = VideoStyle.CUSTOM.value
        return normalized.NormalizedVideo(
            nb,
            sources,
            _language(request.language, policy),
            _text(request.instructions, "instructions", policy),
            format_code,
            style_code,
            prompt,
        )
    if isinstance(request, raw.CinematicVideoCreationRequest):
        return normalized.NormalizedCinematicVideo(
            nb,
            sources,
            _language(request.language, policy),
            _text(request.instructions, "instructions", policy),
        )
    if isinstance(request, raw.ReportCreationRequest):
        fmt = coerce_report_format(request.report_format)
        language = _language(request.language, policy)
        custom = _text(request.custom_prompt, "custom_prompt", policy)
        extra = _text(request.extra_instructions, "extra_instructions", policy)
        if fmt == ReportFormat.CUSTOM:
            title, description, directive = (
                "Custom Report",
                "Custom format",
                custom or "Create a report based on the provided sources.",
            )
        else:
            if fmt == ReportFormat.CONCEPT_EXPLANATION and not policy.concept_reports:
                raise ValueError(
                    f"Unsupported report format {fmt!r}; expected one of: briefing_doc, study_guide, blog_post, custom"
                )
            title, description, directive = _REPORT_CONFIGS[fmt]
            if extra:
                directive = f"{directive}\n\n{extra}"
        return normalized.NormalizedReport(nb, sources, language, title, description, directive)
    if isinstance(request, raw.QuizCreationRequest):
        cls = (
            normalized.NormalizedFlashcards
            if isinstance(request, raw.FlashcardsCreationRequest)
            else normalized.NormalizedQuiz
        )
        return cls(
            nb,
            sources,
            _text(request.instructions, "instructions", policy),
            _code(request.quantity, DEFAULT_QUIZ_QUANTITY, "quantity", strict=True),
            _code(request.difficulty, DEFAULT_QUIZ_DIFFICULTY, "difficulty", strict=True),
        )
    if isinstance(request, raw.InfographicCreationRequest):
        return normalized.NormalizedInfographic(
            nb,
            sources,
            _language(request.language, policy),
            _text(request.instructions, "instructions", policy),
            _code(
                request.orientation, InfographicOrientation.LANDSCAPE, "orientation", strict=strict
            ),
            _code(request.detail_level, InfographicDetail.STANDARD, "detail_level", strict=strict),
            _code(request.style, InfographicStyle.AUTO_SELECT, "style", strict=strict),
        )
    if isinstance(request, raw.SlideDeckCreationRequest):
        return normalized.NormalizedSlideDeck(
            nb,
            sources,
            _language(request.language, policy),
            _text(request.instructions, "instructions", policy),
            _code(
                request.slide_format, SlideDeckFormat.DETAILED_DECK, "slide_format", strict=strict
            ),
            _code(request.slide_length, SlideDeckLength.DEFAULT, "slide_length", strict=strict),
        )
    if isinstance(request, raw.DataTableCreationRequest):
        return normalized.NormalizedDataTable(
            nb,
            sources,
            _language(request.language, policy),
            _text(request.instructions, "instructions", policy),
        )
    if isinstance(request, raw.InteractiveMindMapCreationRequest):
        instructions = _text(request.instructions, "instructions", policy)
        if not policy.interactive_language and instructions and not instructions.strip():
            instructions = None
        return normalized.NormalizedInteractiveMindMap(
            nb,
            sources,
            _language(request.language, policy)
            if policy.interactive_language
            else request.language,
            instructions,
        )
    assert_never(request)
