"""Artifact generation RPC payload builders."""

from __future__ import annotations

from typing import Any

from ..rpc import (
    INTERACTIVE_MIND_MAP_VARIANT,
    ArtifactTypeCode,
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
    nest_source_ids,
)

_STATIC_REPORT_CONFIGS: dict[ReportFormat, dict[str, str]] = {
    ReportFormat.BRIEFING_DOC: {
        "title": "Briefing Doc",
        "description": "Key insights and important quotes",
        "prompt": (
            "Create a comprehensive briefing document that includes an "
            "Executive Summary, detailed analysis of key themes, important "
            "quotes with context, and actionable insights."
        ),
    },
    ReportFormat.STUDY_GUIDE: {
        "title": "Study Guide",
        "description": "Short-answer quiz, essay questions, glossary",
        "prompt": (
            "Create a comprehensive study guide that includes key concepts, "
            "short-answer practice questions, essay prompts for deeper "
            "exploration, and a glossary of important terms."
        ),
    },
    ReportFormat.BLOG_POST: {
        "title": "Blog Post",
        "description": "Insightful takeaways in readable article format",
        "prompt": (
            "Write an engaging blog post that presents the key insights "
            "in an accessible, reader-friendly format. Include an attention-"
            "grabbing introduction, well-organized sections, and a compelling "
            "conclusion with takeaways."
        ),
    },
}


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
    """Build ``CREATE_ARTIFACT`` params for audio overview generation."""
    source_ids_triple = nest_source_ids(source_ids, 2)
    source_ids_double = nest_source_ids(source_ids, 1)

    format_code = audio_format.value if audio_format is not None else AudioFormat.DEEP_DIVE.value
    length_code = audio_length.value if audio_length is not None else AudioLength.DEFAULT.value

    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.AUDIO.value,
            source_ids_triple,
            None,
            None,
            [
                None,
                [
                    instructions,
                    length_code,
                    None,
                    source_ids_double,
                    language,
                    None,
                    format_code,
                ],
            ],
        ],
    ]


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
    """Build ``CREATE_ARTIFACT`` params for video overview generation."""
    source_ids_triple = nest_source_ids(source_ids, 2)
    source_ids_double = nest_source_ids(source_ids, 1)

    format_code = video_format.value if video_format is not None else VideoFormat.EXPLAINER.value
    if video_style == VideoStyle.CUSTOM:
        # Live Web UI serializes the CUSTOM enum's proto-default value (0) as
        # an omitted/null field and carries the custom visual prompt in slot 6.
        style_code = None
    else:
        style_code = video_style.value if video_style is not None else VideoStyle.AUTO_SELECT.value

    video_config = [
        source_ids_double,
        language,
        instructions,
        None,
        format_code,
        style_code,
    ]
    if video_style == VideoStyle.CUSTOM and style_prompt:
        video_config.append(style_prompt)

    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.VIDEO.value,
            source_ids_triple,
            None,
            None,
            None,
            None,
            [
                None,
                None,
                video_config,
            ],
        ],
    ]


def build_cinematic_video_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
) -> list[Any]:
    """Build ``CREATE_ARTIFACT`` params for cinematic video generation."""
    source_ids_triple = nest_source_ids(source_ids, 2)
    source_ids_double = nest_source_ids(source_ids, 1)

    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.VIDEO.value,
            source_ids_triple,
            None,
            None,
            None,
            None,
            [
                None,
                None,
                [
                    source_ids_double,
                    language,
                    instructions,
                    None,
                    VideoFormat.CINEMATIC.value,
                ],
            ],
        ],
    ]


def build_report_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    report_format: ReportFormat,
    language: str,
    custom_prompt: str | None,
    extra_instructions: str | None,
) -> list[Any]:
    """Build ``CREATE_ARTIFACT`` params for report generation."""
    config = _report_config(report_format, custom_prompt)
    if extra_instructions and report_format != ReportFormat.CUSTOM:
        config = {**config, "prompt": f"{config['prompt']}\n\n{extra_instructions}"}

    source_ids_triple = nest_source_ids(source_ids, 2)
    source_ids_double = nest_source_ids(source_ids, 1)

    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.REPORT.value,
            source_ids_triple,
            None,
            None,
            None,
            [
                None,
                [
                    config["title"],
                    config["description"],
                    None,
                    source_ids_double,
                    language,
                    config["prompt"],
                    None,
                    True,
                ],
            ],
        ],
    ]


def build_quiz_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    instructions: str | None,
    quantity: QuizQuantity | None,
    difficulty: QuizDifficulty | None,
) -> list[Any]:
    """Build ``CREATE_ARTIFACT`` params for quiz generation."""
    source_ids_triple = nest_source_ids(source_ids, 2)
    quantity_code = quantity.value if quantity is not None else QuizQuantity.STANDARD.value
    difficulty_code = difficulty.value if difficulty is not None else QuizDifficulty.MEDIUM.value

    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.QUIZ_FLASHCARD.value,
            source_ids_triple,
            None,
            None,
            None,
            None,
            None,
            [
                None,
                [
                    2,
                    None,
                    instructions,
                    None,
                    None,
                    None,
                    None,
                    [quantity_code, difficulty_code],
                ],
            ],
        ],
    ]


def build_flashcards_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    instructions: str | None,
    quantity: QuizQuantity | None,
    difficulty: QuizDifficulty | None,
) -> list[Any]:
    """Build ``CREATE_ARTIFACT`` params for flashcard generation."""
    source_ids_triple = nest_source_ids(source_ids, 2)
    quantity_code = quantity.value if quantity is not None else QuizQuantity.STANDARD.value
    difficulty_code = difficulty.value if difficulty is not None else QuizDifficulty.MEDIUM.value

    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.QUIZ_FLASHCARD.value,
            source_ids_triple,
            None,
            None,
            None,
            None,
            None,
            [
                None,
                [
                    1,
                    None,
                    instructions,
                    None,
                    None,
                    None,
                    [difficulty_code, quantity_code],
                ],
            ],
        ],
    ]


def build_interactive_mind_map_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    instructions: str | None = None,
) -> list[Any]:
    """Build ``CREATE_ARTIFACT`` params for the interactive mind map.

    The interactive mind map is a studio artifact in the type-4 (QUIZ) family
    with variant 4 (``[9][1][0] == INTERACTIVE_MIND_MAP_VARIANT``) — distinct
    from the note-backed mind map built by :func:`build_mind_map_params`
    (which uses ``GENERATE_MIND_MAP``). Shape verified live against the
    captured GUI ``CREATE_ARTIFACT`` request (issue #1256).

    The options block mirrors quiz/flashcards: the variant sits at
    ``[9][1][0]`` and the free-text generation prompt at ``[9][1][2]``. The
    server honours that prompt for variant 4 too (verified live — it steers the
    generated tree), so ``instructions`` is emitted into that slot when a
    non-empty prompt is given. When it is ``None`` (or empty / whitespace-only)
    the bare ``[variant]`` options list is kept, so the default no-prompt
    request stays byte-identical to the original shape.
    """
    source_ids_triple = nest_source_ids(source_ids, 2)
    options: list[Any] = [INTERACTIVE_MIND_MAP_VARIANT]
    if instructions and instructions.strip():
        # Match the quiz/flashcards layout: prompt at index 2 of the options
        # list. Empty / whitespace-only instructions are treated as no prompt so
        # the request stays byte-identical to the bare ``[variant]`` shape (and a
        # blank prompt is never sent to the server).
        options = [INTERACTIVE_MIND_MAP_VARIANT, None, instructions]
    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.QUIZ_FLASHCARD.value,
            source_ids_triple,
            None,
            None,
            None,
            None,
            None,
            [None, options],
        ],
    ]


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
    """Build ``CREATE_ARTIFACT`` params for infographic generation."""
    source_ids_triple = nest_source_ids(source_ids, 2)
    orientation_code = (
        orientation.value if orientation is not None else InfographicOrientation.LANDSCAPE.value
    )
    detail_code = (
        detail_level.value if detail_level is not None else InfographicDetail.STANDARD.value
    )
    style_code = style.value if style is not None else InfographicStyle.AUTO_SELECT.value

    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.INFOGRAPHIC.value,
            source_ids_triple,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [[instructions, language, None, orientation_code, detail_code, style_code]],
        ],
    ]


def build_slide_deck_artifact_params(
    notebook_id: str,
    source_ids: list[str],
    *,
    language: str,
    instructions: str | None,
    slide_format: SlideDeckFormat | None,
    slide_length: SlideDeckLength | None,
) -> list[Any]:
    """Build ``CREATE_ARTIFACT`` params for slide deck generation."""
    source_ids_triple = nest_source_ids(source_ids, 2)
    format_code = (
        slide_format.value if slide_format is not None else SlideDeckFormat.DETAILED_DECK.value
    )
    length_code = slide_length.value if slide_length is not None else SlideDeckLength.DEFAULT.value

    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.SLIDE_DECK.value,
            source_ids_triple,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [[instructions, language, format_code, length_code]],
        ],
    ]


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
    """Build ``CREATE_ARTIFACT`` params for data table generation."""
    source_ids_triple = nest_source_ids(source_ids, 2)

    return [
        _artifact_client_options(),
        notebook_id,
        [
            None,
            None,
            ArtifactTypeCode.DATA_TABLE.value,
            source_ids_triple,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [None, [instructions, language]],
        ],
    ]


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


def _report_config(
    report_format: ReportFormat,
    custom_prompt: str | None,
) -> dict[str, str]:
    if report_format == ReportFormat.CUSTOM:
        return {
            "title": "Custom Report",
            "description": "Custom format",
            "prompt": custom_prompt or "Create a report based on the provided sources.",
        }
    try:
        return _STATIC_REPORT_CONFIGS[report_format]
    except KeyError as exc:
        known_formats = ", ".join(format_.value for format_ in _STATIC_REPORT_CONFIGS)
        raise ValueError(
            f"Unsupported report format {report_format!r}; expected one of: "
            f"{known_formats}, {ReportFormat.CUSTOM.value}"
        ) from exc
