"""Exact-message builders for Android ``CreateArtifact`` generation families."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any

from .._artifact import validation as _artifact_validation
from .._idempotency import unresolved_commit_error
from .._types.enums import (
    ArtifactTypeCode,
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
from ..exceptions import NetworkError, RateLimitError, RPCError, ServerError, ValidationError
from .artifact_proto import ARTIFACT_WIRE_PROTO as _WIRE_PROTO
from .artifact_proto import ARTIFACTS_PROTO as _PROTO
from .artifact_proto import READ_PROTO as _READ_PROTO
from .session import AndroidSession

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
CREATE_ARTIFACT_METHOD = f"/{_SERVICE}/CreateArtifact"

_REPORT_CONFIGS: dict[ReportFormat, tuple[str, str, str]] = {
    ReportFormat.BRIEFING_DOC: (
        "Briefing Doc",
        "Key insights and important quotes",
        "Create a comprehensive briefing document that includes an Executive Summary, "
        "detailed analysis of key themes, important quotes with context, and actionable "
        "insights.",
    ),
    ReportFormat.STUDY_GUIDE: (
        "Study Guide",
        "Short-answer quiz, essay questions, glossary",
        "Create a comprehensive study guide that includes key concepts, short-answer "
        "practice questions, essay prompts for deeper exploration, and a glossary of "
        "important terms.",
    ),
    ReportFormat.BLOG_POST: (
        "Blog Post",
        "Insightful takeaways in readable article format",
        "Write an engaging blog post that presents the key insights in an accessible, "
        "reader-friendly format. Include an attention-grabbing introduction, well-organized "
        "sections, and a compelling conclusion with takeaways.",
    ),
    ReportFormat.CONCEPT_EXPLANATION: (
        "Concept Explanation",
        "Clear explanations of key concepts",
        "Explain the key concepts from the provided sources clearly and comprehensively. "
        "Define important terms, connect related ideas, use examples where helpful, and "
        "address common misconceptions.",
    ),
}


@dataclass(frozen=True)
class CreateArtifactPlan:
    """One exact request plus the response-family invariant it establishes."""

    request: Any
    expected_type: int
    expected_variant: int | None
    family_label: str


async def create_artifact_once(
    session: AndroidSession,
    request: Any,
    *,
    expected_epoch: int | None = None,
) -> Any:
    """Send ``CreateArtifact`` once and preserve an ambiguous commit outcome."""

    epoch_kwargs: dict[str, Any] = (
        {} if expected_epoch is None else {"expected_epoch": expected_epoch}
    )
    try:
        return await session.unary(
            CREATE_ARTIFACT_METHOD,
            request,
            replay_safe=False,
            response_type=_PROTO.CreateArtifactResponse,
            **epoch_kwargs,
        )
    except (NetworkError, RateLimitError, ServerError) as exc:
        rpc_code = exc.rpc_code if isinstance(exc, RPCError) else None
        raise unresolved_commit_error(
            CREATE_ARTIFACT_METHOD,
            "CreateArtifact",
            RPCError(
                "UNRESOLVED — CreateArtifact may have committed before its response was lost. "
                "Do not blindly retry; list artifacts and resolve the outcome manually first.",
                method_id=CREATE_ARTIFACT_METHOD,
                rpc_code=rpc_code,
            ),
            preserve_exception=True,
        ) from None


def _enum_code(value: Any, enum_type: type[Any], parameter: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, enum_type):
        raise ValidationError(f"{parameter} must be a {enum_type.__name__} value")
    return int(value.value)


def _language(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("language must be a non-empty string")
    return value


def _optional_string(value: Any, parameter: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{parameter} must be a string or None")
    return value


def normalize_creation_options(family: str, **options: Any) -> dict[str, Any]:
    """Validate and normalize a supported family's public options before collaborator I/O."""

    if family in {"video", "cinematic_video"}:
        language = _language(options.get("language"))
        instructions = _optional_string(options.get("instructions"), "instructions")
        video_format = (
            VideoFormat.CINEMATIC if family == "cinematic_video" else options.get("video_format")
        )
        format_code = _enum_code(
            video_format,
            VideoFormat,
            "video_format",
            VideoFormat.EXPLAINER.value,
        )
        video_style = options.get("video_style")
        style_code = _enum_code(
            video_style,
            VideoStyle,
            "video_style",
            (VideoStyle.AUTO_SELECT.value),
        )
        style_prompt = _optional_string(options.get("style_prompt"), "style_prompt")
        style_prompt = style_prompt.strip() if style_prompt is not None else None
        if format_code == VideoFormat.CINEMATIC.value:
            if style_prompt:
                raise ValidationError("style_prompt is not supported for cinematic videos")
            style_code = 0
        if format_code == VideoFormat.SHORT.value and (
            (video_style is not None and video_style != VideoStyle.AUTO_SELECT) or style_prompt
        ):
            raise ValidationError(
                "video_style and style_prompt are not supported for short videos "
                "(short has a fixed visual style)"
            )
        if video_style == VideoStyle.CUSTOM and not style_prompt:
            raise ValidationError("style_prompt is required when video_style is CUSTOM")
        if style_prompt and video_style != VideoStyle.CUSTOM:
            raise ValidationError("style_prompt requires video_style=VideoStyle.CUSTOM")
        return {
            "language": language,
            "instructions": instructions,
            "format_code": format_code,
            "style_code": style_code,
            "style_prompt": style_prompt,
        }

    if family == "report":
        report_format = _artifact_validation.coerce_report_format(options.get("report_format"))
        language = _language(options.get("language"))
        custom_prompt = _optional_string(options.get("custom_prompt"), "custom_prompt")
        extra = _optional_string(options.get("extra_instructions"), "extra_instructions")
        if report_format == ReportFormat.CUSTOM:
            title, description = "Custom Report", "Custom format"
            directive = custom_prompt or "Create a report based on the provided sources."
        else:
            title, description, directive = _REPORT_CONFIGS[report_format]
            if extra:
                directive = f"{directive}\n\n{extra}"
        return {
            "language": language,
            "report_format": report_format,
            "title": title,
            "description": description,
            "directive": directive,
        }

    if family == "flashcards":
        return {
            "instructions": _optional_string(options.get("instructions"), "instructions"),
            "quantity_code": _enum_code(
                options.get("quantity"),
                QuizQuantity,
                "quantity",
                QuizQuantity.STANDARD.value,
            ),
            "difficulty_code": _enum_code(
                options.get("difficulty"),
                QuizDifficulty,
                "difficulty",
                QuizDifficulty.MEDIUM.value,
            ),
        }

    if family == "quiz":
        return {
            "instructions": _optional_string(options.get("instructions"), "instructions"),
            "quantity_code": _enum_code(
                options.get("quantity"),
                QuizQuantity,
                "quantity",
                QuizQuantity.STANDARD.value,
            ),
            "difficulty_code": _enum_code(
                options.get("difficulty"),
                QuizDifficulty,
                "difficulty",
                QuizDifficulty.MEDIUM.value,
            ),
        }

    if family == "interactive_mind_map":
        return {
            "language": _language(options.get("language")),
            "instructions": _optional_string(options.get("instructions"), "instructions"),
        }

    if family == "infographic":
        detail_level = options.get("detail_level")
        if detail_level is not None and not isinstance(detail_level, InfographicDetail):
            raise ValidationError("detail_level must be an InfographicDetail value")
        return {
            "language": _language(options.get("language")),
            "instructions": _optional_string(options.get("instructions"), "instructions"),
            "orientation_code": _enum_code(
                options.get("orientation"),
                InfographicOrientation,
                "orientation",
                InfographicOrientation.LANDSCAPE.value,
            ),
            "style_code": _enum_code(
                options.get("style"),
                InfographicStyle,
                "style",
                InfographicStyle.AUTO_SELECT.value,
            ),
            "detail_code": (
                InfographicDetail.STANDARD.value
                if detail_level is None
                else int(detail_level.value)
            ),
        }

    if family == "data_table":
        return {
            "language": _language(options.get("language")),
            "instructions": _optional_string(options.get("instructions"), "instructions"),
        }

    if family == "slide_deck":
        return {
            "language": _language(options.get("language")),
            "instructions": _optional_string(options.get("instructions"), "instructions"),
            "format_code": _enum_code(
                options.get("slide_format"),
                SlideDeckFormat,
                "slide_format",
                SlideDeckFormat.DETAILED_DECK.value,
            ),
            "length_code": _enum_code(
                options.get("slide_length"),
                SlideDeckLength,
                "slide_length",
                SlideDeckLength.DEFAULT.value,
            ),
        }

    raise ValidationError(f"Unsupported Android artifact family: {family}")


def _sources(source_ids: builtins.list[str]) -> builtins.list[Any]:
    return [
        _PROTO.ArtifactSource(source_id=_READ_PROTO.SourceId(id=source_id))
        for source_id in source_ids
    ]


def _source_ids(source_ids: builtins.list[str]) -> builtins.list[Any]:
    return [_READ_PROTO.SourceId(id=source_id) for source_id in source_ids]


def build_create_artifact_plan(
    notebook_id: str,
    family: str,
    source_ids: builtins.list[str],
    **options: Any,
) -> CreateArtifactPlan:
    """Build an exact mobile request for a supported ``CreateArtifact`` family."""

    if not source_ids:
        label = family.replace("_", " ").title()
        raise ValidationError(f"{label} generation requires at least one source id")
    normalized = normalize_creation_options(family, **options)
    artifact_sources = _sources(source_ids)

    if family in {"video", "cinematic_video"}:
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO,
            sources=artifact_sources,
            explainer_video=_PROTO.ExplainerVideoArtifact(
                generation_options=_PROTO.ExplainerVideoGenerationOptions(
                    source_ids=_source_ids(source_ids),
                    language_code=normalized["language"],
                    video_focus=normalized["instructions"] or "",
                    template_format=normalized["format_code"],
                    video_overview_style=normalized["style_code"],
                    style_prompt=normalized["style_prompt"] or "",
                )
            ),
        )
        expected_type = ArtifactTypeCode.VIDEO.value
        expected_variant = None
        family_label = "video"
    elif family == "report":
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_TAILORED_REPORT,
            sources=artifact_sources,
            tailored_report=_PROTO.TailoredReportArtifact(
                generation_options=_PROTO.TailoredReportArtifactGenerationOptions(
                    type=normalized["title"],
                    description=normalized["description"],
                    source_ids=_source_ids(source_ids),
                    language_code=normalized["language"],
                    document_directive=normalized["directive"],
                )
            ),
        )
        expected_type = ArtifactTypeCode.REPORT.value
        expected_variant = None
        family_label = "report"
    elif family in {"flashcards", "quiz"}:
        app_type = _PROTO.APP_TYPE_FLASHCARDS if family == "flashcards" else _PROTO.APP_TYPE_QUIZ
        generation_options = _PROTO.AppArtifactGenerationOptions(
            app_type=app_type,
            free_text_steering_prompt=normalized["instructions"] or "",
        )
        if family == "flashcards":
            generation_options.flashcards_generation_options.CopyFrom(
                _PROTO.FlashcardsGenerationOptions(
                    card_quantity=normalized["quantity_code"],
                    flashcards_difficulty=normalized["difficulty_code"],
                )
            )
        else:
            generation_options.quiz_generation_options.CopyFrom(
                _PROTO.QuizGenerationOptions(
                    question_quantity=normalized["quantity_code"],
                    quiz_difficulty=normalized["difficulty_code"],
                )
            )
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_APP,
            sources=artifact_sources,
            app=_PROTO.AppArtifact(generation_options=generation_options),
        )
        expected_type = ArtifactTypeCode.QUIZ.value
        expected_variant = app_type
        family_label = family
    elif family == "interactive_mind_map":
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_APP,
            sources=artifact_sources,
            app=_PROTO.AppArtifact(
                generation_options=_PROTO.AppArtifactGenerationOptions(
                    app_type=_PROTO.APP_TYPE_MINDMAP,
                    free_text_steering_prompt=normalized["instructions"] or "",
                    language_code=normalized["language"],
                )
            ),
        )
        expected_type = ArtifactTypeCode.QUIZ.value
        expected_variant = _PROTO.APP_TYPE_MINDMAP
        family_label = "interactive mind map"
    elif family == "infographic":
        generation_options = _PROTO.InfographicGenerationOptions(
            user_steering_prompt=normalized["instructions"] or "",
            language_code=normalized["language"],
            aspect_ratio=normalized["orientation_code"],
            style=normalized["style_code"],
        )
        generation_options.MergeFromString(
            _WIRE_PROTO.WireInfographicGenerationOptionsProjection(
                detail_level=normalized["detail_code"]
            ).SerializeToString()
        )
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_INFOGRAPHIC,
            sources=artifact_sources,
            infographic=_PROTO.InfographicArtifact(generation_options=generation_options),
        )
        expected_type = ArtifactTypeCode.INFOGRAPHIC.value
        expected_variant = None
        family_label = "infographic"
    elif family == "slide_deck":
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_SLIDES,
            sources=artifact_sources,
            slides=_PROTO.SlidesArtifact(
                generation_options=_PROTO.SlidesGenerationOptions(
                    user_steering_prompt=normalized["instructions"] or "",
                    language_code=normalized["language"],
                    deck_type=normalized["format_code"],
                    length=normalized["length_code"],
                )
            ),
        )
        expected_type = ArtifactTypeCode.SLIDE_DECK.value
        expected_variant = None
        family_label = "slide deck"
    else:
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_TABLE,
            sources=artifact_sources,
        )
        artifact.MergeFromString(
            _WIRE_PROTO.WireArtifactTableProjection(
                table=_WIRE_PROTO.WireTableArtifact(
                    generation_options=_WIRE_PROTO.WireTableArtifactGenerationOptions(
                        user_steering_prompt=normalized["instructions"] or "",
                        language_code=normalized["language"],
                    )
                )
            ).SerializeToString()
        )
        expected_type = ArtifactTypeCode.DATA_TABLE.value
        expected_variant = None
        family_label = "data table"

    return CreateArtifactPlan(
        request=_PROTO.CreateArtifactRequest(project_id=notebook_id, artifact=artifact),
        expected_type=expected_type,
        expected_variant=expected_variant,
        family_label=family_label,
    )


__all__ = [
    "CREATE_ARTIFACT_METHOD",
    "CreateArtifactPlan",
    "build_create_artifact_plan",
    "create_artifact_once",
    "normalize_creation_options",
]
