"""Exact-message builders for Android ``CreateArtifact`` generation families."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Any

from typing_extensions import assert_never

from .._artifact import creation_normalized as n
from .._idempotency import (
    attach_journal_entry,
    bound_operation_journal_entry,
    unresolved_commit_error,
)
from .._types.enums import (
    ArtifactTypeCode,
    VideoFormat,
)
from ..exceptions import (
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    UnsupportedOperationError,
)
from .artifact_proto import ARTIFACT_WIRE_PROTO as _WIRE_PROTO
from .artifact_proto import ARTIFACTS_PROTO as _PROTO
from .artifact_proto import READ_PROTO as _READ_PROTO
from .session import AndroidSession

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
CREATE_ARTIFACT_METHOD = f"/{_SERVICE}/CreateArtifact"


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

    journal_entry = bound_operation_journal_entry()
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
        error = unresolved_commit_error(
            CREATE_ARTIFACT_METHOD,
            "CreateArtifact",
            RPCError(
                "UNRESOLVED — CreateArtifact may have committed before its response was lost. "
                "Do not blindly retry; list artifacts and resolve the outcome manually first.",
                method_id=CREATE_ARTIFACT_METHOD,
                rpc_code=rpc_code,
            ),
            preserve_exception=True,
        )
        if journal_entry is not None:
            attach_journal_entry(error, journal_entry)
        raise error from None


def _sources(source_ids: builtins.list[str]) -> builtins.list[Any]:
    return [
        _PROTO.ArtifactSource(source_id=_READ_PROTO.SourceId(id=source_id))
        for source_id in source_ids
    ]


def _source_ids(source_ids: builtins.list[str]) -> builtins.list[Any]:
    return [_READ_PROTO.SourceId(id=source_id) for source_id in source_ids]


def build_normalized_create_artifact_plan(
    normalized: n.NormalizedVideo
    | n.NormalizedCinematicVideo
    | n.NormalizedReport
    | n.NormalizedQuiz
    | n.NormalizedFlashcards
    | n.NormalizedInteractiveMindMap
    | n.NormalizedInfographic
    | n.NormalizedSlideDeck
    | n.NormalizedDataTable,
) -> CreateArtifactPlan:
    """Encode a closed normalized domain request into protobuf fields."""
    notebook_id = normalized.notebook_id
    source_ids = list(normalized.source_ids)
    artifact_sources = _sources(source_ids)

    if isinstance(normalized, (n.NormalizedVideo, n.NormalizedCinematicVideo)):
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO,
            sources=artifact_sources,
            explainer_video=_PROTO.ExplainerVideoArtifact(
                generation_options=_PROTO.ExplainerVideoGenerationOptions(
                    source_ids=_source_ids(source_ids),
                    language_code=normalized.language,
                    video_focus=normalized.instructions or "",
                    template_format=(
                        normalized.format_code
                        if isinstance(normalized, n.NormalizedVideo)
                        else VideoFormat.CINEMATIC.value
                    ),
                    video_overview_style=(
                        normalized.style_code if isinstance(normalized, n.NormalizedVideo) else 0
                    ),
                    style_prompt=(
                        normalized.style_prompt or ""
                        if isinstance(normalized, n.NormalizedVideo)
                        else ""
                    ),
                )
            ),
        )
        expected_type = ArtifactTypeCode.VIDEO.value
        expected_variant = None
        family_label = "video"
    elif isinstance(normalized, n.NormalizedReport):
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_TAILORED_REPORT,
            sources=artifact_sources,
            tailored_report=_PROTO.TailoredReportArtifact(
                generation_options=_PROTO.TailoredReportArtifactGenerationOptions(
                    type=normalized.title,
                    description=normalized.description,
                    source_ids=_source_ids(source_ids),
                    language_code=normalized.language,
                    document_directive=normalized.directive,
                )
            ),
        )
        expected_type = ArtifactTypeCode.REPORT.value
        expected_variant = None
        family_label = "report"
    elif isinstance(normalized, (n.NormalizedFlashcards, n.NormalizedQuiz)):
        app_type = (
            _PROTO.APP_TYPE_FLASHCARDS
            if isinstance(normalized, n.NormalizedFlashcards)
            else _PROTO.APP_TYPE_QUIZ
        )
        if isinstance(normalized, n.NormalizedQuiz) and normalized.avoid_question_type_codes:
            # Live-verified: the Android server ignores the proto's avoid-list and
            # always returns multiple-choice quizzes, so fail loud instead of
            # silently dropping the caller's selection.
            raise UnsupportedOperationError(
                "question_types is not supported on the Android backend"
            )
        generation_options = _PROTO.AppArtifactGenerationOptions(
            app_type=app_type,
            free_text_steering_prompt=normalized.instructions or "",
            language_code=normalized.language or "",
        )
        if isinstance(normalized, n.NormalizedFlashcards):
            generation_options.flashcards_generation_options.CopyFrom(
                _PROTO.FlashcardsGenerationOptions(
                    card_quantity=normalized.quantity_code,
                    flashcards_difficulty=normalized.difficulty_code,
                )
            )
        else:
            generation_options.quiz_generation_options.CopyFrom(
                _PROTO.QuizGenerationOptions(
                    question_quantity=normalized.quantity_code,
                    quiz_difficulty=normalized.difficulty_code,
                )
            )
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_APP,
            sources=artifact_sources,
            app=_PROTO.AppArtifact(generation_options=generation_options),
        )
        expected_type = ArtifactTypeCode.QUIZ.value
        expected_variant = app_type
        family_label = "flashcards" if isinstance(normalized, n.NormalizedFlashcards) else "quiz"
    elif isinstance(normalized, n.NormalizedInteractiveMindMap):
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_APP,
            sources=artifact_sources,
            app=_PROTO.AppArtifact(
                generation_options=_PROTO.AppArtifactGenerationOptions(
                    app_type=_PROTO.APP_TYPE_MINDMAP,
                    free_text_steering_prompt=normalized.instructions or "",
                    language_code=normalized.language,
                )
            ),
        )
        expected_type = ArtifactTypeCode.QUIZ.value
        expected_variant = _PROTO.APP_TYPE_MINDMAP
        family_label = "interactive mind map"
    elif isinstance(normalized, n.NormalizedInfographic):
        generation_options = _PROTO.InfographicGenerationOptions(
            user_steering_prompt=normalized.instructions or "",
            language_code=normalized.language,
            aspect_ratio=normalized.orientation_code,
            style=normalized.style_code,
        )
        generation_options.MergeFromString(
            _WIRE_PROTO.WireInfographicGenerationOptionsProjection(
                detail_level=normalized.detail_code
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
    elif isinstance(normalized, n.NormalizedSlideDeck):
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_SLIDES,
            sources=artifact_sources,
            slides=_PROTO.SlidesArtifact(
                generation_options=_PROTO.SlidesGenerationOptions(
                    user_steering_prompt=normalized.instructions or "",
                    language_code=normalized.language,
                    deck_type=normalized.format_code,
                    length=normalized.length_code,
                )
            ),
        )
        expected_type = ArtifactTypeCode.SLIDE_DECK.value
        expected_variant = None
        family_label = "slide deck"
    elif isinstance(normalized, n.NormalizedDataTable):
        artifact = _PROTO.Artifact(
            type=_PROTO.ARTIFACT_TYPE_TABLE,
            sources=artifact_sources,
        )
        artifact.MergeFromString(
            _WIRE_PROTO.WireArtifactTableProjection(
                table=_WIRE_PROTO.WireTableArtifact(
                    generation_options=_WIRE_PROTO.WireTableArtifactGenerationOptions(
                        user_steering_prompt=normalized.instructions or "",
                        language_code=normalized.language,
                    )
                )
            ).SerializeToString()
        )
        expected_type = ArtifactTypeCode.DATA_TABLE.value
        expected_variant = None
        family_label = "data table"

    else:
        assert_never(normalized)

    return CreateArtifactPlan(
        request=_PROTO.CreateArtifactRequest(project_id=notebook_id, artifact=artifact),
        expected_type=expected_type,
        expected_variant=expected_variant,
        family_label=family_label,
    )


__all__ = [
    "CREATE_ARTIFACT_METHOD",
    "CreateArtifactPlan",
    "build_normalized_create_artifact_plan",
    "create_artifact_once",
]
