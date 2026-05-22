"""Service layer for ``notebooklm generate`` commands (P3.T1, ADR-008).

This module owns the Click-free orchestration for all 11 ``generate``
leaf commands:

* ``audio``, ``video``, ``cinematic-video``, ``slide-deck``,
  ``revise-slide``, ``quiz``, ``flashcards``, ``infographic``,
  ``data-table``, ``mind-map``, ``report``

The split mirrors the ``services/source_add.py`` / ``services/login.py``
shape established by earlier ADR-008 extractions:

* :func:`build_generation_plan` does all Click-time validation, parameter
  coercion (e.g. report smart-custom detection, cinematic-video alias
  enforcement), enum mapping, and the cinematic-video timeout default.
  It returns a frozen :class:`GenerationPlan` dataclass.
* :func:`execute_generation` is the async orchestration: open-client
  scope is the caller's; this function resolves notebook/source IDs,
  emits queued warnings, dispatches to the right ``client.artifacts.*``
  method, runs the retry-with-backoff loop via the existing
  ``services/artifact_generation.py`` core, and handles output / wait.

The Click handlers in ``cli/generate_cmd.py`` shrink to a thin shell:
build the raw_args dict from Click params, call
``build_generation_plan(kind, raw_args, parameter_source)``, then call
``execute_generation(plan, client)`` inside an ``async with
NotebookLMClient(...) as client:`` block.

This module does NOT introduce parallel abstractions to
``services/artifact_generation.py`` (that module's
``generate_with_retry`` + ``handle_generation_result`` is the retry-core
and is reused as-is; see phase-3.md → P3.T1 must_not_do).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import click
from click.core import ParameterSource

from ...types import (
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

if TYPE_CHECKING:
    from ...client import NotebookLMClient
    from ...types import GenerationStatus

GenerationKind = Literal[
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

# Display name used in user-facing strings ("Audio ready", "rate limited", etc.).
# Mind-map is intentionally absent — it goes through a custom output path.
_DISPLAY_NAME: Mapping[str, str] = {
    "audio": "audio",
    "video": "video",
    "cinematic-video": "video",  # shares the "video" display
    "slide-deck": "slide deck",
    "revise-slide": "slide revision",
    "quiz": "quiz",
    "flashcards": "flashcards",
    "infographic": "infographic",
    "data-table": "data table",
    "mind-map": "mind map",
    # ``report`` uses a per-format display name (see _REPORT_DISPLAY).
}

# Per-report-format display name used in retry / status messages.
_REPORT_DISPLAY: Mapping[str, str] = {
    "briefing-doc": "briefing document",
    "study-guide": "study guide",
    "blog-post": "blog post",
    "custom": "custom report",
}

# Pre-extraction generate.py had the infographic style map inlined; reuse the
# same exhaustive mapping here so handler-regression byte-for-byte parity is
# preserved. Sourced from cli/generate_cmd.py at f1be552.
_INFOGRAPHIC_STYLE_MAP: Mapping[str, InfographicStyle] = {
    "auto": InfographicStyle.AUTO_SELECT,
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
}

_AUDIO_FORMAT_MAP: Mapping[str, AudioFormat] = {
    "deep-dive": AudioFormat.DEEP_DIVE,
    "brief": AudioFormat.BRIEF,
    "critique": AudioFormat.CRITIQUE,
    "debate": AudioFormat.DEBATE,
}

_AUDIO_LENGTH_MAP: Mapping[str, AudioLength] = {
    "short": AudioLength.SHORT,
    "default": AudioLength.DEFAULT,
    "long": AudioLength.LONG,
}

_VIDEO_FORMAT_MAP: Mapping[str, VideoFormat] = {
    "explainer": VideoFormat.EXPLAINER,
    "brief": VideoFormat.BRIEF,
    "cinematic": VideoFormat.CINEMATIC,
}

_VIDEO_STYLE_MAP: Mapping[str, VideoStyle] = {
    "auto": VideoStyle.AUTO_SELECT,
    "custom": VideoStyle.CUSTOM,
    "classic": VideoStyle.CLASSIC,
    "whiteboard": VideoStyle.WHITEBOARD,
    "kawaii": VideoStyle.KAWAII,
    "anime": VideoStyle.ANIME,
    "watercolor": VideoStyle.WATERCOLOR,
    "retro-print": VideoStyle.RETRO_PRINT,
    "heritage": VideoStyle.HERITAGE,
    "paper-craft": VideoStyle.PAPER_CRAFT,
}

_SLIDE_FORMAT_MAP: Mapping[str, SlideDeckFormat] = {
    "detailed": SlideDeckFormat.DETAILED_DECK,
    "presenter": SlideDeckFormat.PRESENTER_SLIDES,
}

_SLIDE_LENGTH_MAP: Mapping[str, SlideDeckLength] = {
    "default": SlideDeckLength.DEFAULT,
    "short": SlideDeckLength.SHORT,
}

_QUIZ_QUANTITY_MAP: Mapping[str, QuizQuantity] = {
    "fewer": QuizQuantity.FEWER,
    "standard": QuizQuantity.STANDARD,
    "more": QuizQuantity.MORE,
}

_QUIZ_DIFFICULTY_MAP: Mapping[str, QuizDifficulty] = {
    "easy": QuizDifficulty.EASY,
    "medium": QuizDifficulty.MEDIUM,
    "hard": QuizDifficulty.HARD,
}

_INFOGRAPHIC_ORIENTATION_MAP: Mapping[str, InfographicOrientation] = {
    "landscape": InfographicOrientation.LANDSCAPE,
    "portrait": InfographicOrientation.PORTRAIT,
    "square": InfographicOrientation.SQUARE,
}

_INFOGRAPHIC_DETAIL_MAP: Mapping[str, InfographicDetail] = {
    "concise": InfographicDetail.CONCISE,
    "standard": InfographicDetail.STANDARD,
    "detailed": InfographicDetail.DETAILED,
}

_REPORT_FORMAT_MAP: Mapping[str, ReportFormat] = {
    "briefing-doc": ReportFormat.BRIEFING_DOC,
    "study-guide": ReportFormat.STUDY_GUIDE,
    "blog-post": ReportFormat.BLOG_POST,
    "custom": ReportFormat.CUSTOM,
}

# Cinematic-video defaults to a longer wait ceiling (Veo 3 takes ~30-40 min).
# Preserved from cli/generate_cmd.py pre-extraction.
_CINEMATIC_DEFAULT_TIMEOUT = 1800.0


@dataclass(frozen=True)
class GenerationPlan:
    """Prepared inputs for a single ``generate`` command.

    All Click-time validation (parameter combinations, alias enforcement,
    smart-format coercion) has already run by the time a plan exists. The
    executor only needs to dispatch the right ``client.artifacts.*`` call,
    handle retry / wait / output, and emit any queued warnings to stderr.

    Attributes:
        kind: Internal kind identifier (one of :data:`GenerationKind`).
            ``cinematic-video`` is a distinct kind from ``video`` because
            it dispatches to a different API method
            (``generate_cinematic_video``).
        display_name: User-facing name used in retry / status messages
            (e.g. ``"audio"``, ``"slide deck"``, ``"briefing document"``).
            For ``report`` this is per-format.
        notebook_id: Pre-resolution notebook ID (the executor resolves it
            via ``resolve_notebook_id``).
        description: Resolved prompt text (already merged with
            ``--prompt-file``). May be empty for kinds that accept it.
        source_ids: Tuple of source IDs to scope generation to. Pre-
            resolution; the executor calls ``resolve_source_ids``. May be
            empty (== unscoped).
        language: ``en``-style language code, already resolved via the
            language-resolution chain (--language > NOTEBOOKLM_HL > config
            > "en"). ``None`` for kinds that do not accept ``--language``
            (currently ``revise-slide``, ``quiz``, ``flashcards``).
        wait: Whether to wait for completion before returning. Mind-map
            ignores this field and renders synchronously.
        timeout: Wait timeout in seconds. For cinematic-video this is
            defaulted to 1800.0 when the user did not pass ``--timeout``
            explicitly.
        interval: Polling interval in seconds for the wait loop.
        max_retries: Number of retry-after-rate-limit attempts. ``0``
            means a single attempt with no retry.
        json_output: Whether to emit JSON instead of text. Suppresses
            spinner / progress messages.
        params: Kind-specific keyword arguments forwarded to the
            ``client.artifacts.<method>`` call. Already enum-mapped.
        warnings: Stderr warnings queued during plan construction (e.g.
            ``--append`` with ``--format custom``). Emitted in order
            before the API call.
    """

    kind: GenerationKind
    display_name: str
    notebook_id: str
    description: str
    source_ids: tuple[str, ...]
    language: str | None
    wait: bool
    timeout: float
    interval: float
    max_retries: int
    json_output: bool
    params: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def build_generation_plan(
    kind: str,
    raw_args: Mapping[str, Any],
    parameter_source: Callable[[str], ParameterSource] | None = None,
    *,
    language_resolver: Callable[[str | None], str] | None = None,
) -> GenerationPlan:
    """Validate Click-layer inputs and return a :class:`GenerationPlan`.

    Args:
        kind: One of the literal kind names in :data:`GenerationKind`. The
            caller is responsible for mapping ``ctx.info_name`` to the
            right kind (``"cinematic-video"`` for the alias, ``"video"``
            for the canonical command).
        raw_args: Per-command kwargs dict. Required keys vary by kind;
            this function picks the relevant subset and ignores extras.
            Common keys: ``notebook_id``, ``description``, ``source_ids``,
            ``language``, ``wait``, ``timeout``, ``interval``,
            ``max_retries``, ``json_output``. Kind-specific keys: see
            internal builders below.
        parameter_source: Optional callable returning Click's
            ``ParameterSource`` for a given param name. Used to detect
            "user did not pass --format / --timeout" cases for the
            cinematic-video alias. If ``None``, defaults to "every
            parameter is treated as DEFAULT", which is the right behavior
            for unit tests that supply args directly.
        language_resolver: Optional callable that resolves a raw
            ``--language`` value through the env/config/default chain.
            When ``None``, the raw value is passed through unchanged
            (None or the user's literal flag). The Click layer always
            supplies the real resolver; tests can pass ``lambda x: x``
            or a custom stub.

    Returns:
        A frozen :class:`GenerationPlan` ready for :func:`execute_generation`.

    Raises:
        click.UsageError: For invalid parameter combinations (cinematic
            video + ``--style-prompt``, ``--style custom`` without
            ``--style-prompt``, ``cinematic-video --format <non-cinematic>``).
        ValueError: When ``kind`` is not recognized.
    """
    source: Callable[[str], ParameterSource] = parameter_source or (
        lambda _name: ParameterSource.DEFAULT
    )
    # Default resolver mirrors the canonical ``"en"`` fallback used by
    # ``cli/generate_cmd.py:resolve_language`` so unit tests that omit a
    # resolver get a stable string back. Production calls (from the Click
    # layer) always supply the real resolver, which walks the
    # --language > env > config > "en" chain.
    resolve_language: Callable[[str | None], str] = language_resolver or (
        lambda lang: lang if isinstance(lang, str) else "en"
    )

    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"Unknown generation kind: {kind!r}")
    return builder(raw_args, source, resolve_language)


# ---------------------------------------------------------------------------
# Per-kind plan builders
# ---------------------------------------------------------------------------


def _common(raw_args: Mapping[str, Any]) -> dict[str, Any]:
    """Pull the common cross-kind keys out of ``raw_args`` with defaults."""
    return {
        "notebook_id": raw_args["notebook_id"],
        "description": raw_args.get("description", "") or "",
        "source_ids": tuple(raw_args.get("source_ids") or ()),
        "wait": bool(raw_args.get("wait", False)),
        "timeout": float(raw_args.get("timeout", 300.0)),
        "interval": float(raw_args.get("interval", 2.0)),
        "max_retries": int(raw_args.get("max_retries", 0)),
        "json_output": bool(raw_args.get("json_output", False)),
    }


def _build_audio_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], ParameterSource],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    language = resolve_language(raw_args.get("language"))
    return GenerationPlan(
        kind="audio",
        display_name=_DISPLAY_NAME["audio"],
        notebook_id=common["notebook_id"],
        description=common["description"],
        source_ids=common["source_ids"],
        language=language,
        wait=common["wait"],
        timeout=common["timeout"],
        interval=common["interval"],
        max_retries=common["max_retries"],
        json_output=common["json_output"],
        params={
            "audio_format": _AUDIO_FORMAT_MAP[raw_args["audio_format"]],
            "audio_length": _AUDIO_LENGTH_MAP[raw_args["audio_length"]],
        },
    )


def _build_video_plan_for_kind(
    raw_args: Mapping[str, Any],
    source: Callable[[str], ParameterSource],
    resolve_language: Callable[[str | None], str],
    *,
    alias: bool,
) -> GenerationPlan:
    """Shared builder for ``video`` and the ``cinematic-video`` alias.

    ``alias=True`` enforces the cinematic-video flag rules: an explicit
    ``--format <non-cinematic>`` raises UsageError, the format is coerced
    to cinematic when not passed, and the timeout defaults to 1800s when
    the user did not pass ``--timeout``.
    """
    common = _common(raw_args)
    video_format = raw_args.get("video_format", "explainer")
    style = raw_args.get("style", "auto")
    style_prompt_raw = raw_args.get("style_prompt")

    if alias:
        format_explicit = source("video_format") == ParameterSource.COMMANDLINE
        if format_explicit and video_format != "cinematic":
            raise click.UsageError(
                "--format must be 'cinematic' for the cinematic-video subcommand "
                "(use 'generate video --format <other>' for other formats)"
            )
        video_format = "cinematic"

    is_cinematic = video_format == "cinematic"
    normalized_style_prompt = (
        style_prompt_raw.strip() if isinstance(style_prompt_raw, str) else None
    )
    if is_cinematic and normalized_style_prompt:
        raise click.UsageError("--style-prompt cannot be used with cinematic video")
    if not is_cinematic and style == "custom" and not normalized_style_prompt:
        raise click.UsageError("--style custom requires --style-prompt")
    if not is_cinematic and normalized_style_prompt and style != "custom":
        raise click.UsageError("--style-prompt requires --style custom")

    timeout_value = common["timeout"]
    if is_cinematic and source("timeout") != ParameterSource.COMMANDLINE:
        timeout_value = _CINEMATIC_DEFAULT_TIMEOUT

    language = resolve_language(raw_args.get("language"))

    if is_cinematic:
        # cinematic-video dispatches to a different API method and accepts
        # only the description (no format / style / style_prompt).
        return GenerationPlan(
            kind="cinematic-video",
            display_name=_DISPLAY_NAME["cinematic-video"],
            notebook_id=common["notebook_id"],
            description=common["description"],
            source_ids=common["source_ids"],
            language=language,
            wait=common["wait"],
            timeout=timeout_value,
            interval=common["interval"],
            max_retries=common["max_retries"],
            json_output=common["json_output"],
            params={},
        )

    return GenerationPlan(
        kind="video",
        display_name=_DISPLAY_NAME["video"],
        notebook_id=common["notebook_id"],
        description=common["description"],
        source_ids=common["source_ids"],
        language=language,
        wait=common["wait"],
        timeout=timeout_value,
        interval=common["interval"],
        max_retries=common["max_retries"],
        json_output=common["json_output"],
        params={
            "video_format": _VIDEO_FORMAT_MAP[video_format],
            "video_style": _VIDEO_STYLE_MAP[style],
            "style_prompt": normalized_style_prompt,
        },
    )


def _build_video_plan(
    raw_args: Mapping[str, Any],
    source: Callable[[str], ParameterSource],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    return _build_video_plan_for_kind(raw_args, source, resolve_language, alias=False)


def _build_cinematic_video_plan(
    raw_args: Mapping[str, Any],
    source: Callable[[str], ParameterSource],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    return _build_video_plan_for_kind(raw_args, source, resolve_language, alias=True)


def _build_slide_deck_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], ParameterSource],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    return GenerationPlan(
        kind="slide-deck",
        display_name=_DISPLAY_NAME["slide-deck"],
        notebook_id=common["notebook_id"],
        description=common["description"],
        source_ids=common["source_ids"],
        language=resolve_language(raw_args.get("language")),
        wait=common["wait"],
        timeout=common["timeout"],
        interval=common["interval"],
        max_retries=common["max_retries"],
        json_output=common["json_output"],
        params={
            "slide_format": _SLIDE_FORMAT_MAP[raw_args["deck_format"]],
            "slide_length": _SLIDE_LENGTH_MAP[raw_args["deck_length"]],
        },
    )


def _build_revise_slide_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], ParameterSource],
    _resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    return GenerationPlan(
        kind="revise-slide",
        display_name=_DISPLAY_NAME["revise-slide"],
        notebook_id=common["notebook_id"],
        description=common["description"],
        source_ids=(),  # revise-slide never resolves source IDs
        language=None,
        wait=common["wait"],
        timeout=common["timeout"],
        interval=common["interval"],
        max_retries=common["max_retries"],
        json_output=common["json_output"],
        params={
            "artifact_id": raw_args["artifact_id"],
            "slide_index": int(raw_args["slide_index"]),
            "prompt": common["description"],
        },
    )


def _build_quiz_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], ParameterSource],
    _resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    return GenerationPlan(
        kind="quiz",
        display_name=_DISPLAY_NAME["quiz"],
        notebook_id=common["notebook_id"],
        description=common["description"],
        source_ids=common["source_ids"],
        language=None,
        wait=common["wait"],
        timeout=common["timeout"],
        interval=common["interval"],
        max_retries=common["max_retries"],
        json_output=common["json_output"],
        params={
            "quantity": _QUIZ_QUANTITY_MAP[raw_args["quantity"]],
            "difficulty": _QUIZ_DIFFICULTY_MAP[raw_args["difficulty"]],
        },
    )


def _build_flashcards_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], ParameterSource],
    _resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    return GenerationPlan(
        kind="flashcards",
        display_name=_DISPLAY_NAME["flashcards"],
        notebook_id=common["notebook_id"],
        description=common["description"],
        source_ids=common["source_ids"],
        language=None,
        wait=common["wait"],
        timeout=common["timeout"],
        interval=common["interval"],
        max_retries=common["max_retries"],
        json_output=common["json_output"],
        params={
            "quantity": _QUIZ_QUANTITY_MAP[raw_args["quantity"]],
            "difficulty": _QUIZ_DIFFICULTY_MAP[raw_args["difficulty"]],
        },
    )


def _build_infographic_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], ParameterSource],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    return GenerationPlan(
        kind="infographic",
        display_name=_DISPLAY_NAME["infographic"],
        notebook_id=common["notebook_id"],
        description=common["description"],
        source_ids=common["source_ids"],
        language=resolve_language(raw_args.get("language")),
        wait=common["wait"],
        timeout=common["timeout"],
        interval=common["interval"],
        max_retries=common["max_retries"],
        json_output=common["json_output"],
        params={
            "orientation": _INFOGRAPHIC_ORIENTATION_MAP[raw_args["orientation"]],
            "detail_level": _INFOGRAPHIC_DETAIL_MAP[raw_args["detail"]],
            "style": _INFOGRAPHIC_STYLE_MAP[raw_args["style"]],
        },
    )


def _build_data_table_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], ParameterSource],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    return GenerationPlan(
        kind="data-table",
        display_name=_DISPLAY_NAME["data-table"],
        notebook_id=common["notebook_id"],
        description=common["description"],
        source_ids=common["source_ids"],
        language=resolve_language(raw_args.get("language")),
        wait=common["wait"],
        timeout=common["timeout"],
        interval=common["interval"],
        max_retries=common["max_retries"],
        json_output=common["json_output"],
        params={},
    )


def _build_mind_map_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], ParameterSource],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    return GenerationPlan(
        kind="mind-map",
        display_name=_DISPLAY_NAME["mind-map"],
        notebook_id=common["notebook_id"],
        description="",
        source_ids=common["source_ids"],
        language=resolve_language(raw_args.get("language")),
        wait=False,  # mind-map renders synchronously; no wait loop
        timeout=common["timeout"],
        interval=common["interval"],
        max_retries=0,
        json_output=common["json_output"],
        params={"instructions": raw_args.get("instructions")},
    )


def _build_report_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], ParameterSource],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    description = common["description"]
    report_format = raw_args.get("report_format", "briefing-doc")
    append_instructions = raw_args.get("append_instructions")

    # Smart detection: a bare description with the default --format briefing-doc
    # is treated as a custom report (preserves pre-extraction behavior).
    actual_format = report_format
    custom_prompt: str | None = None
    if description:
        if report_format == "briefing-doc":
            actual_format = "custom"
            custom_prompt = description
        else:
            custom_prompt = description

    warnings: list[str] = []
    if append_instructions and actual_format == "custom":
        warnings.append(
            "Warning: --append has no effect with --format custom. "
            "Use the description argument instead."
        )
        append_instructions = None

    display_name = _REPORT_DISPLAY[actual_format]
    return GenerationPlan(
        kind="report",
        display_name=display_name,
        notebook_id=common["notebook_id"],
        description=description,
        source_ids=common["source_ids"],
        language=resolve_language(raw_args.get("language")),
        wait=common["wait"],
        timeout=common["timeout"],
        interval=common["interval"],
        max_retries=common["max_retries"],
        json_output=common["json_output"],
        params={
            "report_format": _REPORT_FORMAT_MAP[actual_format],
            "custom_prompt": custom_prompt,
            "extra_instructions": append_instructions,
        },
        warnings=tuple(warnings),
    )


_BUILDERS: Mapping[
    str,
    Callable[
        [
            Mapping[str, Any],
            Callable[[str], ParameterSource],
            Callable[[str | None], str],
        ],
        GenerationPlan,
    ],
] = {
    "audio": _build_audio_plan,
    "video": _build_video_plan,
    "cinematic-video": _build_cinematic_video_plan,
    "slide-deck": _build_slide_deck_plan,
    "revise-slide": _build_revise_slide_plan,
    "quiz": _build_quiz_plan,
    "flashcards": _build_flashcards_plan,
    "infographic": _build_infographic_plan,
    "data-table": _build_data_table_plan,
    "mind-map": _build_mind_map_plan,
    "report": _build_report_plan,
}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


_KIND_TO_METHOD: Mapping[str, str] = {
    "audio": "generate_audio",
    "video": "generate_video",
    "cinematic-video": "generate_cinematic_video",
    "slide-deck": "generate_slide_deck",
    "revise-slide": "revise_slide",
    "quiz": "generate_quiz",
    "flashcards": "generate_flashcards",
    "infographic": "generate_infographic",
    "data-table": "generate_data_table",
    "mind-map": "generate_mind_map",
    "report": "generate_report",
}


def _build_call_kwargs(plan: GenerationPlan, *, notebook_id: str, sources: Any) -> dict[str, Any]:
    """Build the kwargs dict passed to ``client.artifacts.<method>(notebook_id, **kwargs)``.

    Common cross-kind kwargs (``source_ids``, ``language``, ``instructions``)
    are merged with kind-specific ``plan.params``. ``revise-slide`` and
    ``mind-map`` have bespoke shapes handled here.
    """
    if plan.kind == "revise-slide":
        # revise_slide(notebook_id, *, artifact_id, slide_index, prompt)
        return {
            "artifact_id": plan.params["artifact_id"],
            "slide_index": plan.params["slide_index"],
            "prompt": plan.params["prompt"],
        }

    if plan.kind == "mind-map":
        return {
            "source_ids": sources,
            "language": plan.language,
            "instructions": plan.params.get("instructions"),
        }

    if plan.kind == "cinematic-video":
        # cinematic-video API: (notebook_id, *, source_ids, language, instructions)
        return {
            "source_ids": sources,
            "language": plan.language,
            "instructions": plan.description or None,
        }

    base: dict[str, Any] = {"source_ids": sources}

    # Language: only kinds that accept it (plan.language not None).
    if plan.language is not None:
        base["language"] = plan.language

    # data-table requires ``instructions``; pre-extraction code passed
    # ``description`` (not ``description or None``) since the Click layer
    # enforces ``required=True``. Preserve that contract.
    if plan.kind == "data-table":
        base["instructions"] = plan.description

    # report packs report_format, custom_prompt, extra_instructions into
    # plan.params; it does NOT carry ``instructions``.
    elif plan.kind == "report":
        base["report_format"] = plan.params["report_format"]
        base["custom_prompt"] = plan.params["custom_prompt"]
        base["extra_instructions"] = plan.params["extra_instructions"]

    else:
        # audio / video / slide-deck / quiz / flashcards / infographic all
        # take ``instructions = description or None``.
        base["instructions"] = plan.description or None

    # Merge kind-specific params LAST so they win on key conflicts (none in
    # practice, but defensive).
    base.update(
        {
            k: v
            for k, v in plan.params.items()
            if k not in ("report_format", "custom_prompt", "extra_instructions")
        }
    )
    return base


def _emit_warnings(plan: GenerationPlan) -> None:
    """Emit any warnings queued by ``build_generation_plan`` to stderr.

    Plan warnings (e.g. ``--append`` with ``--format custom``) match the
    pre-extraction ``click.echo(..., err=True)`` semantics. Suppressed in
    JSON mode so stdout stays parseable AND stderr stays JSON-machine-
    consumer friendly (the warning text is human prose, not structured).
    """
    if plan.json_output or not plan.warnings:
        return
    for line in plan.warnings:
        click.echo(line, err=True)


async def execute_generation(
    plan: GenerationPlan,
    client: NotebookLMClient,
) -> GenerationStatus | None:
    """Drive a single generation request end-to-end.

    Caller responsibility: open and close the ``NotebookLMClient`` scope.
    This function resolves notebook/source IDs, emits queued warnings,
    dispatches to the matching ``client.artifacts.<method>``, runs the
    retry-with-backoff loop, then either renders the mind-map result via
    the legacy text/JSON formatter or hands off to
    ``handle_generation_result`` (wait + status output + non-zero exit on
    failure).

    Returns the final :class:`GenerationStatus` (or ``None`` when the
    underlying call resolved without a status). For ``mind-map`` returns
    ``None`` — output is rendered inline.
    """
    # Local imports defer heavy CLI-layer wiring (rendering, error_handler)
    # until execution time and keep the import graph testable.
    from .. import generate_cmd  # late import: avoids module-load cycles
    from ..resolve import resolve_notebook_id, resolve_source_ids
    from .artifact_generation import generate_with_retry, handle_generation_result
    from .polling import status_with_elapsed

    _emit_warnings(plan)

    nb_id_resolved = await resolve_notebook_id(
        client, plan.notebook_id, json_output=plan.json_output
    )

    if plan.kind == "revise-slide":
        # revise-slide never resolves source IDs.
        sources: Any = None
    else:
        sources = await resolve_source_ids(
            client, nb_id_resolved, plan.source_ids, json_output=plan.json_output
        )

    method_name = _KIND_TO_METHOD[plan.kind]
    api_method = getattr(client.artifacts, method_name)
    call_kwargs = _build_call_kwargs(plan, notebook_id=nb_id_resolved, sources=sources)

    async def _generate() -> Any:
        return await api_method(nb_id_resolved, **call_kwargs)

    if plan.kind == "mind-map":
        # Mind-map has a custom output path: no retry loop, optional
        # spinner, custom formatter. Stay inside the generate_cmd module
        # for _output_mind_map_result because tests patch it as a
        # module-level attribute.
        if plan.json_output:
            result = await _generate()
        else:
            async with status_with_elapsed(
                "Generating mind map...",
                json_output=False,
            ):
                result = await _generate()
        generate_cmd._output_mind_map_result(result, plan.json_output)
        return None

    result = await generate_with_retry(
        _generate, plan.max_retries, plan.display_name, plan.json_output
    )
    return await handle_generation_result(
        client,
        nb_id_resolved,
        result,
        plan.display_name,
        plan.wait,
        plan.json_output,
        timeout=plan.timeout,
        interval=plan.interval,
    )


__all__ = [
    "GenerationKind",
    "GenerationPlan",
    "build_generation_plan",
    "execute_generation",
]
