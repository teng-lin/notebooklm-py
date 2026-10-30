"""Artifact generation RPC payload builders."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..._artifact import creation as raw
from ..._artifact.creation_policy import DEFAULT_QUIZ_DIFFICULTY as DEFAULT_QUIZ_DIFFICULTY
from ..._artifact.creation_policy import DEFAULT_QUIZ_QUANTITY as DEFAULT_QUIZ_QUANTITY
from ..._artifact.creation_policy import (
    WEB_BUILDER_POLICY,
    coerce_question_types,
    normalize_creation,
)
from ..._types.enums import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    QuizQuestionType,
    ReportFormat,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)
from ...exceptions import ValidationError
from ...rpc import nest_source_ids
from .sources import build_template_block as _build_template_block


def _encode_public_creation(request: raw.ArtifactCreationRequest) -> list[Any]:
    from .creation import encode_creation

    try:
        normalized = normalize_creation(request, WEB_BUILDER_POLICY)
    except ValidationError as exc:
        if isinstance(request, raw.ReportCreationRequest):
            raise ValueError(
                f"Unsupported report format {request.report_format!r}; expected one of: briefing_doc, study_guide, blog_post, custom"
            ) from exc
        raise
    return encode_creation(normalized)[0]


def _artifact_client_options() -> list[Any]:
    """Return the client-options block used by Studio artifact RPCs.

    Live UI captures on 2026-06-15 for Data Table and interactive Mind Map send
    this full capability envelope as ``CREATE_ARTIFACT`` param 0. Older captures
    used the shorter ``[2]`` form, but the fuller envelope now matches the web
    client and the in-place retry RPC.
    """
    return [
        2,
        None,
        None,
        [1, None, None, None, None, None, None, None, None, None, [1]],
        [[1, 4, 8, 2, 3, 6]],
    ]


def build_audio_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
    audio_format: AudioFormat | None,
    audio_length: AudioLength | None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.AudioCreationRequest(
            notebook_id, tuple(source_ids), language, instructions, audio_format, audio_length
        )
    )


def build_video_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
    video_format: VideoFormat | None,
    video_style: VideoStyle | None,
    style_prompt: str | None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.VideoCreationRequest(
            notebook_id,
            tuple(source_ids),
            language,
            instructions,
            video_format,
            video_style,
            style_prompt,
        )
    )


def build_cinematic_video_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.CinematicVideoCreationRequest(notebook_id, tuple(source_ids), language, instructions)
    )


def build_report_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    report_format: ReportFormat,
    language: str,
    custom_prompt: str | None,
    extra_instructions: str | None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.ReportCreationRequest(
            notebook_id,
            tuple(source_ids),
            report_format,
            language,
            custom_prompt,
            extra_instructions,
        )
    )


def build_quiz_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    instructions: str | None,
    quantity: QuizQuantity | None,
    difficulty: QuizDifficulty | None,
    language: str | None = None,
    question_types: Sequence[QuizQuestionType] | None = None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.QuizCreationRequest(
            notebook_id,
            tuple(source_ids),
            instructions,
            quantity,
            difficulty,
            language=language,
            question_types=coerce_question_types(question_types),
        )
    )


def build_flashcards_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    instructions: str | None,
    quantity: QuizQuantity | None,
    difficulty: QuizDifficulty | None,
    language: str | None = None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.FlashcardsCreationRequest(
            notebook_id,
            tuple(source_ids),
            instructions,
            quantity,
            difficulty,
            language=language,
        )
    )


def build_interactive_mind_map_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    instructions: str | None = None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.InteractiveMindMapCreationRequest(notebook_id, tuple(source_ids), "", instructions)
    )


def build_infographic_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
    orientation: InfographicOrientation | None,
    detail_level: InfographicDetail | None,
    style: InfographicStyle | None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.InfographicCreationRequest(
            notebook_id, tuple(source_ids), language, instructions, orientation, detail_level, style
        )
    )


def build_slide_deck_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
    slide_format: SlideDeckFormat | None,
    slide_length: SlideDeckLength | None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.SlideDeckCreationRequest(
            notebook_id, tuple(source_ids), language, instructions, slide_format, slide_length
        )
    )


def build_revise_slide_params(artifact_id: str, slide_index: int, prompt: str) -> list[Any]:
    """Build ``REVISE_SLIDE`` params for slide revision."""
    return [
        [2],
        artifact_id,
        [[[slide_index, prompt]]],
    ]


def build_retry_artifact_params(artifact_id: str) -> list[Any]:
    """Build ``RETRY_ARTIFACT`` params for an in-place failed-artifact retry."""
    return [_artifact_client_options(), artifact_id]


def build_data_table_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
) -> list[Any]:
    """Compatibility builder using the shared domain normalization boundary."""
    return _encode_public_creation(
        raw.DataTableCreationRequest(notebook_id, tuple(source_ids), language, instructions)
    )


def build_mind_map_params(
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
) -> list[Any]:
    """Build ``GENERATE_MIND_MAP`` params."""
    source_ids_nested = nest_source_ids(source_ids, 2)

    return [
        source_ids_nested,
        None,
        None,
        None,
        None,
        ["interactive_mindmap", [["[CONTEXT]", instructions or ""]], language],
        None,
        [2, None, [1]],
    ]


def build_suggest_reports_params(notebook_id: str) -> list[Any]:
    """Build ``GET_SUGGESTED_REPORTS`` params."""
    return [[2], notebook_id]


def build_copy_artifacts_params(artifact_ids: list[str], target_notebook_id: str) -> list[Any]:
    """Build ``COPY_ARTIFACTS`` (``mKDdke`` / ``CopyArtifactsAsync``) params.

    Mobile proto (live-pinned, #2283): ``{ RequestContext request_context = 1;
    repeated string artifact_ids = 2; string target_project_id = 3 }``. The
    artifact ids are bare strings (not ``SourceId``-style wrappers).
    """
    return [_build_template_block(), list(artifact_ids), target_notebook_id]


def build_customization_choices_params(notebook_id: str | None = None) -> list[Any]:
    """Build ``GET_CUSTOMIZATION_CHOICES`` (``sqTeoe``) params.

    Mobile proto (APK-exact): ``{ RequestContext request_context = 1; string
    project_id = 2; ArtifactType artifact_type = 3 }``. Live (both front doors,
    2026-09-01) the server ignores fields 2 and 3 entirely — an empty request,
    a bogus notebook id and every artifact type return the same account-level
    table — so only the context is required. ``notebook_id`` is appended when the
    caller has one purely to fill the request's ``project_id`` (#2) slot.
    """
    params: list[Any] = [_build_template_block()]
    if notebook_id is not None:
        params.append(notebook_id)
    return params
