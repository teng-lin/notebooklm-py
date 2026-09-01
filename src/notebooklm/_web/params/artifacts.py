"""Artifact generation RPC payload builders."""

from __future__ import annotations

from typing import Any, Final, TypeVar

from ..._types.enums import (
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
)
from ...exceptions import ValidationError
from ...rpc import nest_source_ids
from .sources import build_template_block as _build_template_block

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


#: The client-side defaults applied when a quiz/flashcards option is omitted.
#: These are *sent explicitly*; see :func:`_quiz_option_code` for why. The CLI
#: binds its ``--quantity`` / ``--difficulty`` flag defaults to these too, so
#: the flag default and the wire default cannot drift apart (#2197).
DEFAULT_QUIZ_QUANTITY: Final = QuizQuantity.STANDARD
DEFAULT_QUIZ_DIFFICULTY: Final = QuizDifficulty.MEDIUM

_QuizOption = TypeVar("_QuizOption", QuizQuantity, QuizDifficulty)


def _quiz_option_code(
    value: _QuizOption | None,
    default: _QuizOption,
    *,
    parameter: str,
) -> int:
    """Resolve one quiz/flashcards option to the integer code sent on the wire.

    ``None`` means **"use this client's default"**, not "let the server
    choose": the default is substituted here and transmitted explicitly, so
    every request carries a concrete quantity and difficulty. That is a
    deliberate choice, not an oversight (#2196), and the alternative was
    measured rather than assumed:

    * Omission *is* accepted. Live probe: a ``CREATE_ARTIFACT`` with the option
      message left ``null`` (and one with the proto3 default pair ``[0, 0]``,
      which the backend stores as an empty message) both generate normally and
      reach ``ARTIFACT_STATUS_READY``.
    * But what the server picked is then **unobservable**. The stored options
      echo back as ``null`` / ``[]``, and the generated content is not carried
      in ``LIST_ARTIFACTS``, so a caller could not tell what they got — nor
      could :attr:`~notebooklm._web.rows.artifacts.ArtifactRow.quiz_options`,
      the read-back added in #2195 precisely so this surface stops being
      fixture-only. Passing ``None`` through would trade a value we can name,
      echo and assert for one we cannot see at all.
    * The web UI always sends an explicit pair, and every sibling builder in
      this module (audio, video, infographic, slide deck) resolves ``None`` to
      an explicit client default the same way. Diverging here would make quiz
      and flashcards the lone exception.

    A caller who genuinely wants the backend's own default should pass the
    member they want; there is no ``UNSPECIFIED`` member on
    :class:`~notebooklm.rpc.QuizQuantity` / :class:`~notebooklm.rpc.QuizDifficulty`
    because a value that cannot be read back could not be tested.

    Raises:
        ValidationError: If ``value`` is neither ``None`` nor a member of the
            expected enum. Previously a bare ``int`` reached ``.value`` and
            produced ``AttributeError: 'int' object has no attribute 'value'``.
            The ``isinstance`` check also rejects the *other* option enum —
            ``quantity=QuizDifficulty.HARD`` used to encode silently as
            ``MORE``, since both are ``3``.
    """
    if value is None:
        return default.value
    expected = type(default)
    if not isinstance(value, expected):
        raise ValidationError(
            f"{parameter} must be a {expected.__name__} member or None, got "
            f"{value!r} ({type(value).__name__})"
        )
    return value.value


def _quiz_option_pair(
    quantity: QuizQuantity | None,
    difficulty: QuizDifficulty | None,
) -> list[int]:
    """Build the ``[quantity, difficulty]`` pair both option messages carry.

    The ONE place this client decides that quantity comes first. Both
    ``QuizGenerationOptions`` and ``FlashcardsGenerationOptions`` number
    ``quantity`` 1 and ``difficulty`` 2, so the two builders emit an identical
    pair into different slots (quiz ``[9][1][7]``, flashcards ``[9][1][6]``).

    Sharing it is the point rather than a tidy-up: #2116 was two hand-written
    literals drifting apart, with the flashcards one transposed for long enough
    that ``docs/rpc-reference.md`` documented the inversion as intended. One
    expression cannot disagree with itself. The decode side is symmetric —
    :class:`~notebooklm._web.rows.artifacts.QuizOptionPair` reads the pair
    back through named fields rather than positions.
    """
    return [
        _quiz_option_code(quantity, DEFAULT_QUIZ_QUANTITY, parameter="quantity"),
        _quiz_option_code(difficulty, DEFAULT_QUIZ_DIFFICULTY, parameter="difficulty"),
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
    option_pair = _quiz_option_pair(quantity, difficulty)

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
                    option_pair,
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
    option_pair = _quiz_option_pair(quantity, difficulty)

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
                    # Same expression as the quiz builder above — the ordering
                    # lives in ``_quiz_option_pair`` so the two cannot drift
                    # apart again (#2116).
                    option_pair,
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
