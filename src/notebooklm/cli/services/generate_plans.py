"""Plan-building for ``notebooklm generate`` commands (ADR-0008).

This module owns the Click-free plan-construction half of the ``generate``
service: the enum/format maps, the :class:`GenerationPlan` dataclass, the
:class:`GenerationPlanValidationError`, :func:`build_generation_plan`, and the
per-kind builders it dispatches to. It imports nothing from
``services/generate.py`` so the executor half can re-import the plan types
without forming a cycle.

:func:`build_generation_plan` does all Click-time validation, parameter
coercion (e.g. report smart-custom detection, cinematic-video alias
enforcement), enum mapping, and the cinematic-video timeout default. It returns
a frozen :class:`GenerationPlan` dataclass that ``services/generate.py``'s
``execute_generation`` consumes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

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

# Exhaustive infographic style map used by the generate handlers.
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

# Cinematic generation is frequently queue-bound. Standard video gets its
# 1800s default from the Click option so programmatic callers can pass their
# own raw timeout without being clobbered here.
_CINEMATIC_DEFAULT_TIMEOUT = 3600.0


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
        timeout: Wait timeout in seconds. The CLI supplies 1800.0 for video;
            cinematic-video is coerced to 3600.0 when the user did not pass
            ``--timeout`` explicitly.
        interval: Polling interval in seconds for the wait loop.
        max_retries: Number of retry-after-rate-limit attempts. ``0``
            means a single attempt with no retry.
        json_output: Whether to emit JSON instead of text. Suppresses
            spinner / progress messages.
        params: Kind-specific keyword arguments forwarded to the
            ``client.artifacts.<method>`` call. Already enum-mapped.
        warnings: Informational stderr warnings queued during plan
            construction (e.g. ``--append`` with ``--format custom``). Emitted
            in order before the API call, but **only in human (non-JSON) mode**
            so they never pollute machine-readable output.
        stderr_warnings: Behavioral warnings that must surface even under
            ``--json`` because they describe an input the CLI actually dropped
            (e.g. ``--instructions`` ignored for interactive mind maps).
            Always written to stderr; stdout stays pure JSON.
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
    stderr_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class GenerationPlanValidationError(Exception):
    """Service-level generation validation error for command-layer rendering."""

    message: str
    code: str = "VALIDATION_ERROR"

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", (self.message,))


def build_generation_plan(
    kind: str,
    raw_args: Mapping[str, Any],
    parameter_explicit: Callable[[str], bool] | None = None,
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
        parameter_explicit: Optional callable returning whether a parameter
            was supplied explicitly by the user. Used to detect "user did
            not pass --format / --timeout" cases for the cinematic-video
            alias. If ``None``, defaults to false for every parameter.
        language_resolver: Optional callable that resolves a raw
            ``--language`` value through the env/config/default chain.
            When ``None``, the raw value is passed through unchanged
            (None or the user's literal flag). The Click layer always
            supplies the real resolver; tests can pass ``lambda x: x``
            or a custom stub.

    Returns:
        A frozen :class:`GenerationPlan` ready for :func:`execute_generation`.

    Raises:
        GenerationPlanValidationError: For invalid parameter combinations
            (cinematic video + ``--style-prompt``, ``--style custom``
            without ``--style-prompt``, ``cinematic-video --format
            <non-cinematic>``). The command layer renders the error through
            the ADR-0015 JSON/text surface.
        ValueError: When ``kind`` is not recognized.
    """
    is_explicit: Callable[[str], bool] = parameter_explicit or (lambda _name: False)
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
    return builder(raw_args, is_explicit, resolve_language)


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
    _source: Callable[[str], bool],
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
    source: Callable[[str], bool],
    resolve_language: Callable[[str | None], str],
    *,
    alias: bool,
) -> GenerationPlan:
    """Shared builder for ``video`` and the ``cinematic-video`` alias.

    ``alias=True`` enforces the cinematic-video flag rules. Validation
    failures are raised as typed service errors for the command layer to
    render via text or the ADR-0015 JSON envelope.
    """
    common = _common(raw_args)
    video_format = raw_args.get("video_format", "explainer")
    style = raw_args.get("style", "auto")
    style_prompt_raw = raw_args.get("style_prompt")

    if alias:
        format_explicit = source("video_format")
        if format_explicit and video_format != "cinematic":
            raise GenerationPlanValidationError(
                "--format must be 'cinematic' for the cinematic-video subcommand "
                "(use 'generate video --format <other>' for other formats)"
            )
        video_format = "cinematic"

    is_cinematic = video_format == "cinematic"
    normalized_style_prompt = (
        style_prompt_raw.strip() if isinstance(style_prompt_raw, str) else None
    )
    if is_cinematic and normalized_style_prompt:
        raise GenerationPlanValidationError("--style-prompt cannot be used with cinematic video")
    if not is_cinematic and style == "custom" and not normalized_style_prompt:
        raise GenerationPlanValidationError("--style custom requires --style-prompt")
    if not is_cinematic and normalized_style_prompt and style != "custom":
        raise GenerationPlanValidationError("--style-prompt requires --style custom")

    timeout_value = common["timeout"]
    if is_cinematic and not source("timeout"):
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
    source: Callable[[str], bool],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    return _build_video_plan_for_kind(raw_args, source, resolve_language, alias=False)


def _build_cinematic_video_plan(
    raw_args: Mapping[str, Any],
    source: Callable[[str], bool],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    return _build_video_plan_for_kind(raw_args, source, resolve_language, alias=True)


def _build_slide_deck_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], bool],
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
    _source: Callable[[str], bool],
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
    _source: Callable[[str], bool],
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
    _source: Callable[[str], bool],
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
    _source: Callable[[str], bool],
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
    _source: Callable[[str], bool],
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
    _source: Callable[[str], bool],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    map_kind = raw_args.get("map_kind") or "interactive"
    interactive = map_kind == "interactive"
    instructions = raw_args.get("instructions")
    # The interactive (studio-artifact) generator takes only sources — it has no
    # custom-instruction slot in its CREATE_ARTIFACT payload. This warning is
    # *behavioral* (we actually drop the user's --instructions), so it goes to
    # stderr_warnings and surfaces even under --json — silently ignoring an
    # explicit input would be a nasty surprise for scripted callers.
    stderr_warnings: list[str] = []
    if interactive and instructions:
        stderr_warnings.append(
            "Warning: --instructions is ignored for interactive mind maps "
            "(the interactive generator does not accept custom instructions)."
        )
        instructions = None
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
        params={"instructions": instructions, "kind": map_kind},
        stderr_warnings=tuple(stderr_warnings),
    )


def _build_report_plan(
    raw_args: Mapping[str, Any],
    _source: Callable[[str], bool],
    resolve_language: Callable[[str | None], str],
) -> GenerationPlan:
    common = _common(raw_args)
    description = common["description"]
    report_format = raw_args.get("report_format", "briefing-doc")
    append_instructions = raw_args.get("append_instructions")

    # Smart detection: a bare description with the default --format briefing-doc
    # is treated as a custom report.
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
            Callable[[str], bool],
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
