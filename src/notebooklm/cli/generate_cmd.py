"""Generate content CLI commands — thin Click handlers (ADR-0008).

Request validation, retry/wait orchestration, and per-kind generation execution
live in the transport-neutral ``_app.generate`` core. This module constructs the
typed requests and owns rendering and exit policy. Tests patch ``console`` /
``json_error_response`` / ``json_output_response`` / ``get_language`` /
``_output_mind_map_result`` as module-level attributes here, so those names
remain imported at module scope and ``_output_mind_map_result`` +
``resolve_language`` remain defined inline rather than re-exported.
"""

import contextlib
import os
from typing import Any

import click
from click.core import ParameterSource

from .._app.generate import GenerationExecutionResult, execute_generation
from .._app.generate_retry import GenerationOutcome
from .._app.generation_requests import (
    UNSET,
    AudioGenerationRequest,
    DataTableGenerationRequest,
    FlashcardsGenerationRequest,
    GenerationRequest,
    GenerationRequestValidationError,
    GenerationValidationCode,
    InfographicGenerationRequest,
    MindMapGenerationRequest,
    QuizGenerationRequest,
    ReportGenerationRequest,
    ReviseSlideGenerationRequest,
    SlideDeckGenerationRequest,
    SourceSelection,
    build_generation_request,
)
from ..types import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    MindMap,
    MindMapKind,
    MindMapResult,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)
from ._generate_render import (
    format_generation_wait,
    generation_display_name,
    generation_exit_code,
)
from .auth_runtime import resolve_client_factory, with_client
from .error_handler import current_json_output, output_error
from .input import resolve_prompt
from .language_cmd import SUPPORTED_LANGUAGES, get_language
from .options import (
    _complete_artifacts,
    alias_command,
    json_option,
    language_option,
    multi_source_option,
    notebook_option,
    prompt_file_option,
    retry_option,
    wait_option,
    wait_polling_options,
)
from .polling_ui import status_with_elapsed
from .rendering import (
    console,
    json_error_response,
    json_output_response,
)
from .resolve import require_notebook, resolve_notebook_id, resolve_source_ids

DEFAULT_LANGUAGE = "en"

_AUDIO_FORMAT_MAP = {
    "deep-dive": AudioFormat.DEEP_DIVE,
    "brief": AudioFormat.BRIEF,
    "critique": AudioFormat.CRITIQUE,
    "debate": AudioFormat.DEBATE,
}
_AUDIO_LENGTH_MAP = {
    "short": AudioLength.SHORT,
    "default": AudioLength.DEFAULT,
    "long": AudioLength.LONG,
}
_VIDEO_FORMAT_MAP = {
    "explainer": VideoFormat.EXPLAINER,
    "brief": VideoFormat.BRIEF,
    "cinematic": VideoFormat.CINEMATIC,
    "short": VideoFormat.SHORT,
}
_VIDEO_STYLE_MAP = {
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
_VIDEO_VALIDATION_MESSAGES: dict[GenerationValidationCode, str] = {
    "cinematic_style_prompt": "--style-prompt cannot be used with cinematic video",
    "short_video_style": (
        "--style/--style-prompt cannot be used with --format short "
        "(short video has a fixed visual style)"
    ),
    "custom_style_prompt_required": "--style custom requires --style-prompt",
    "style_prompt_requires_custom": "--style-prompt requires --style custom",
}
_SLIDE_FORMAT_MAP = {
    "detailed": SlideDeckFormat.DETAILED_DECK,
    "presenter": SlideDeckFormat.PRESENTER_SLIDES,
}
_SLIDE_LENGTH_MAP = {"default": SlideDeckLength.DEFAULT, "short": SlideDeckLength.SHORT}
_QUIZ_QUANTITY_MAP = {
    "fewer": QuizQuantity.FEWER,
    "standard": QuizQuantity.STANDARD,
    "more": QuizQuantity.MORE,
}
_QUIZ_DIFFICULTY_MAP = {
    "easy": QuizDifficulty.EASY,
    "medium": QuizDifficulty.MEDIUM,
    "hard": QuizDifficulty.HARD,
}
_INFOGRAPHIC_ORIENTATION_MAP = {
    "landscape": InfographicOrientation.LANDSCAPE,
    "portrait": InfographicOrientation.PORTRAIT,
    "square": InfographicOrientation.SQUARE,
}
_INFOGRAPHIC_DETAIL_MAP = {
    "concise": InfographicDetail.CONCISE,
    "standard": InfographicDetail.STANDARD,
    "detailed": InfographicDetail.DETAILED,
}
_INFOGRAPHIC_STYLE_MAP = {
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
_REPORT_FORMAT_MAP = {
    "briefing-doc": ReportFormat.BRIEFING_DOC,
    "study-guide": ReportFormat.STUDY_GUIDE,
    "blog-post": ReportFormat.BLOG_POST,
    "custom": ReportFormat.CUSTOM,
}


def resolve_language(language: str | None) -> str:
    """Resolve language from CLI flag, NOTEBOOKLM_HL env, config, or default.

    Priority: ``--language`` flag > ``NOTEBOOKLM_HL`` env var > config file
    > "en" default. Uses explicit None checks to avoid treating empty
    string as falsy. Validates each candidate against the supported list.

    Invalid codes route through :func:`output_error` per ADR-0015: under
    ``--json`` the typed JSON envelope is emitted on stdout
    (``code: "VALIDATION_ERROR"``, exit 1); in text mode the same message
    is written to stderr (exit 1, no Click usage footer). The active
    ``--json`` flag is inferred via :func:`current_json_output` so this
    helper stays callable from both the Click handler and the service-layer
    plan builder without threading the flag through its signature.
    """
    if language is not None:
        if language not in SUPPORTED_LANGUAGES:
            output_error(
                f"Unknown language code: {language}\n"
                "Run 'notebooklm language list' to see supported codes.",
                "VALIDATION_ERROR",
                current_json_output(),
                1,
            )
        return language
    env_lang = os.environ.get("NOTEBOOKLM_HL", "").strip()
    if env_lang:
        if env_lang not in SUPPORTED_LANGUAGES:
            # Distinguish the env-var source so the user knows which input is
            # at fault (the ``in config`` branch below already disambiguates;
            # the CLI flag is the unqualified default since it's the most
            # common path).
            output_error(
                f"Unknown language code from NOTEBOOKLM_HL: {env_lang}\n"
                "Run 'notebooklm language list' to see supported codes.",
                "VALIDATION_ERROR",
                current_json_output(),
                1,
            )
        return env_lang
    config_lang = get_language()
    if config_lang is not None:
        if config_lang not in SUPPORTED_LANGUAGES:
            output_error(
                f"Unknown language code in config: {config_lang}\n"
                "Run 'notebooklm language list' to see supported codes.",
                "VALIDATION_ERROR",
                current_json_output(),
                1,
            )
        return config_lang
    return DEFAULT_LANGUAGE


def _output_mind_map_result(result: Any, json_output: bool) -> None:
    """Output mind map result in appropriate format.

    Kept in this module (rather than the service) because the existing
    test suite patches it as a module-level attribute alongside
    ``console`` / ``json_error_response`` / ``json_output_response``.
    """
    if not result:
        if json_output:
            json_error_response("GENERATION_FAILED", "Mind map generation failed")
        else:
            console.print("[yellow]No result[/yellow]")
        return

    # Converge both kinds onto one shape (issue #1256): note-backed returns a
    # ``MindMapResult`` ({mind_map, note_id}); interactive returns a ``MindMap``
    # ({id, kind, tree}). Normalize to (id, tree, kind) so the JSON stays a
    # backward-compatible superset of the historical {mind_map, note_id} payload
    # — only the additive ``kind`` key is new — and the text is kind-agnostic.
    if isinstance(result, MindMap):
        mind_map_id: Any = result.id
        mind_map = result.tree
        kind = result.kind.value
    elif isinstance(result, MindMapResult):
        mind_map_id = result.note_id
        mind_map = result.mind_map
        kind = "note_backed"
    elif isinstance(result, dict):
        # Legacy/test path: a plain dict still patched in by older callers.
        mind_map_id = result.get("note_id")
        mind_map = result.get("mind_map", {})
        kind = result.get("kind", "note_backed")
    else:
        mind_map_id = None
        mind_map = result
        kind = "note_backed"

    if json_output:
        json_output_response({"mind_map": mind_map, "note_id": mind_map_id, "kind": kind})
        return

    console.print("[green]Mind map generated:[/green]")
    console.print(f"  ID: {mind_map_id if mind_map_id is not None else '-'}")
    console.print(f"  Kind: {kind}")
    if isinstance(mind_map, dict):
        console.print(f"  Root: {mind_map.get('name', '-')}")
        console.print(f"  Children: {len(mind_map.get('children', []))} nodes")
    elif mind_map is not None and not isinstance(result, (MindMap, MindMapResult, dict)):
        console.print(mind_map)


def _output_generation_outcome(
    outcome: GenerationOutcome,
    json_output: bool,
    display_name: str | None = None,
) -> None:
    """Render a generation outcome and apply command-layer exit policy."""
    display_name = display_name or outcome.kind.replace("-", " ")
    if outcome.status in {"failed", "rate_limited"}:
        exit_code = generation_exit_code(outcome)
        if outcome.status == "rate_limited":
            output_error(
                f"{display_name.title()} generation rate limited by Google.",
                "RATE_LIMITED",
                json_output,
                exit_code,
                hint=(
                    "Daily quota may be exceeded. Try again in 1-24 hours, "
                    "or use --retry N to retry automatically."
                ),
            )
        else:
            message = outcome.error or f"{display_name.title()} generation failed"
            output_error(message, "GENERATION_FAILED", json_output, exit_code)
        raise AssertionError("unreachable")  # pragma: no cover

    if json_output:
        if outcome.status == "completed":
            json_output_response(
                {"task_id": outcome.task_id, "status": "completed", "url": outcome.url}
            )
        else:
            json_output_response({"task_id": outcome.task_id, "status": "pending"})
        return

    if outcome.status == "completed":
        if outcome.url:
            console.print(f"[green]{display_name.title()} ready:[/green] {outcome.url}")
        else:
            console.print(f"[green]{display_name.title()} ready[/green]")
    else:
        console.print(f"[yellow]Started:[/yellow] {outcome.task_id or outcome.raw_status}")


def _render_generation_result(
    result: GenerationExecutionResult,
    request: GenerationRequest,
    json_output: bool,
) -> None:
    if result.kind == "mind-map":
        _output_mind_map_result(result.mind_map, json_output)
        return
    if result.generation is None:
        display_name = generation_display_name(request)
        output_error(
            f"{display_name.title()} generation failed",
            "GENERATION_FAILED",
            json_output,
            1,
        )
        raise AssertionError("unreachable")  # pragma: no cover
    _output_generation_outcome(
        result.generation,
        json_output,
        display_name=generation_display_name(request),
    )


def _source_selection(ctx: click.Context, source_ids: tuple[str, ...]) -> SourceSelection:
    if ctx.get_parameter_source("source_ids") == ParameterSource.COMMANDLINE:
        return tuple(source_ids)
    return UNSET


def _run_generate(
    *,
    ctx: click.Context,
    client_auth: Any,
    request: GenerationRequest,
    json_output: bool,
    notices: tuple[str, ...] = (),
) -> Any:
    """Resolve adapter references, execute a typed request, and render it."""

    if not json_output:
        for line in notices:
            click.echo(line, err=True)

    async def _run() -> Any:
        async with resolve_client_factory(ctx)(client_auth) as client:
            display_name = generation_display_name(request)

            async def notebook_resolver(current_client: Any, reference: str) -> str:
                return await resolve_notebook_id(
                    current_client,
                    reference,
                    json_output=json_output,
                )

            async def source_resolver(
                current_client: Any,
                notebook_id: str,
                references: tuple[str, ...],
            ) -> list[str] | None:
                return await resolve_source_ids(
                    current_client,
                    notebook_id,
                    references,
                    json_output=json_output,
                )

            result = await execute_generation(
                request,
                client,
                notebook_resolver=notebook_resolver,
                source_resolver=source_resolver,
                retry_sink=(
                    None
                    if json_output
                    else lambda event: console.print(
                        f"[yellow]{display_name.title()} rate limited. "
                        f"Retrying in {int(event.delay)}s "
                        f"(attempt {event.next_attempt_number}/{event.total_attempts})...[/yellow]"
                    )
                ),
                wait_context=lambda event: status_with_elapsed(
                    format_generation_wait(event),
                    json_output=json_output,
                    resume_hint=f"notebooklm artifact poll {event.task_id}",
                ),
                wait_start_sink=(
                    None
                    if json_output
                    else lambda event: console.print(
                        f"[yellow]Generating {display_name}...[/yellow] Task: {event.task_id}"
                    )
                ),
                mind_map_context=(
                    contextlib.nullcontext
                    if json_output
                    else lambda: status_with_elapsed("Generating mind map...", json_output=False)
                ),
            )
            _render_generation_result(result, request, json_output)

    return _run()


@click.group()
def generate():
    """Generate content from notebook.

    \b
    LLM-friendly design: Describe what you want in natural language.

    \b
    Examples:
      notebooklm use nb123
      notebooklm generate video "a funny explainer for kids age 5"
      notebooklm generate audio "deep dive focusing on chapter 3"
      notebooklm generate quiz "focus on vocabulary terms"

    \b
    Types:
      audio        Audio overview (podcast)
      video        Video overview
      slide-deck   Slide deck
      quiz         Quiz
      flashcards   Flashcards
      infographic  Infographic
      data-table   Data table
      mind-map     Mind map
      report       Report (briefing-doc, study-guide, blog-post, custom)
    """
    pass


@generate.command("audio")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--format",
    "audio_format",
    type=click.Choice(["deep-dive", "brief", "critique", "debate"]),
    default="deep-dive",
    help="Conversation style (default: deep-dive)",
)
@click.option(
    "--length",
    "audio_length",
    type=click.Choice(["short", "default", "long"]),
    default="default",
    help="Audio length: short, default, or long",
)
@language_option
@multi_source_option
@wait_option
@wait_polling_options(default_timeout=1200, default_interval=2)
@retry_option
@json_option
@with_client
def generate_audio(
    ctx,
    description,
    prompt_file,
    notebook_id,
    audio_format,
    audio_length,
    language,
    source_ids,
    wait,
    timeout,
    interval,
    max_retries,
    json_output,
    client_auth,
):
    """Generate audio overview (podcast).

    \b
    Use --json for machine-readable output.

    \b
    Example:
      notebooklm generate audio "deep dive focusing on key themes"
      notebooklm generate audio "make it funny and casual" --format debate
      notebooklm generate audio -s src_001 -s src_002 "from specific sources"
    """
    description = resolve_prompt(description, prompt_file, "description")
    request = AudioGenerationRequest(
        notebook_id=require_notebook(notebook_id),
        source_ids=_source_selection(ctx, source_ids),
        language=resolve_language(language),
        instructions=description or None,
        audio_format=_AUDIO_FORMAT_MAP[audio_format],
        audio_length=_AUDIO_LENGTH_MAP[audio_length],
        wait=wait,
        timeout=timeout,
        interval=interval,
        max_retries=max_retries,
    )
    return _run_generate(ctx=ctx, client_auth=client_auth, request=request, json_output=json_output)


@generate.command("video")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--format",
    "video_format",
    type=click.Choice(["explainer", "brief", "cinematic", "short"]),
    default="explainer",
    help=(
        "Video format; 'cinematic' uses Veo 3 footage, 'short' is a vertical "
        "short-form video (default: explainer)"
    ),
)
@click.option(
    "--style",
    type=click.Choice(
        [
            "auto",
            "custom",
            "classic",
            "whiteboard",
            "kawaii",
            "anime",
            "watercolor",
            "retro-print",
            "heritage",
            "paper-craft",
        ]
    ),
    default="auto",
    help=(
        "Visual style (default: auto). Use 'custom' with --style-prompt. "
        "Not supported for --format cinematic or short (fixed style)."
    ),
)
@click.option("--style-prompt", default=None, help="Custom visual style prompt")
@language_option
@multi_source_option
@wait_option
@wait_polling_options(
    default_timeout=1800,
    default_interval=2,
    timeout_help="Maximum seconds to wait (default: 1800; cinematic format defaults to 3600)",
)
@retry_option
@json_option
@with_client
def generate_video(
    ctx,
    description,
    prompt_file,
    notebook_id,
    video_format,
    style,
    style_prompt,
    language,
    source_ids,
    wait,
    timeout,
    interval,
    max_retries,
    json_output,
    client_auth,
):
    """Generate video overview.

    Use --format cinematic for AI-generated documentary footage (Veo 3).
    Cinematic videos ignore --style and take ~30-40 min (requires AI Ultra).

    \b
    Use --json for machine-readable output.

    \b
    Example:
      notebooklm generate video "a funny explainer for kids age 5"
      notebooklm generate video "professional presentation" --style classic
      notebooklm generate video --style custom --style-prompt "hand-drawn diagrams"
      notebooklm generate video --format cinematic "documentary overview"
      notebooklm generate video -s src_001 "from specific source"
    """
    description = resolve_prompt(description, prompt_file, "description")
    source_selection = _source_selection(ctx, source_ids)
    alias_is_cinematic = ctx.info_name == "cinematic-video"
    is_cinematic = alias_is_cinematic or video_format == "cinematic"
    if alias_is_cinematic:
        if (
            ctx.get_parameter_source("video_format") == ParameterSource.COMMANDLINE
            and video_format != "cinematic"
        ):
            output_error(
                "--format must be 'cinematic' for the cinematic-video subcommand "
                "(use 'generate video --format <other>' for other formats)",
                "VALIDATION_ERROR",
                json_output,
                1,
            )
    try:
        request = build_generation_request(
            "cinematic-video" if alias_is_cinematic else "video",
            notebook_id=require_notebook(notebook_id),
            source_ids=source_selection,
            language=resolve_language(language),
            instructions=description or None,
            video_format=_VIDEO_FORMAT_MAP[video_format],
            video_style=_VIDEO_STYLE_MAP[style],
            style_prompt=style_prompt,
            wait=wait,
            timeout=(
                timeout
                if not is_cinematic
                or ctx.get_parameter_source("timeout") == ParameterSource.COMMANDLINE
                else 3600.0
            ),
            interval=interval,
            max_retries=max_retries,
        )
    except GenerationRequestValidationError as exc:
        output_error(
            _VIDEO_VALIDATION_MESSAGES[exc.code],
            "VALIDATION_ERROR",
            json_output,
            1,
        )
    return _run_generate(ctx=ctx, client_auth=client_auth, request=request, json_output=json_output)


# Convenience alias: 'generate cinematic-video' delegates to 'generate video --format cinematic'.
# Reuses generate_video's callback/params so changes stay in sync automatically.
alias_command(
    generate,
    generate_video,
    name="cinematic-video",
    help=(
        "Generate cinematic video overview (AI-generated documentary footage).\n\n"
        "Alias for 'generate video --format cinematic'. Uses Veo 3 AI to create\n"
        "documentary-style videos. Requires Google AI Ultra.\n\n"
        "Note: --format is locked to 'cinematic' on this subcommand; passing any\n"
        "other value (e.g. --format explainer) raises an error. Use\n"
        "'generate video --format <other>' for non-cinematic formats.\n\n"
        "Example:\n"
        '  notebooklm generate cinematic-video "documentary about quantum physics"'
    ),
)


@generate.command("slide-deck")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--format",
    "deck_format",
    type=click.Choice(["detailed", "presenter"]),
    default="detailed",
    help="Slide deck format (default: detailed)",
)
@click.option(
    "--length",
    "deck_length",
    type=click.Choice(["default", "short"]),
    default="default",
    help="Slide deck length: default or short",
)
@language_option
@multi_source_option
@wait_option
@wait_polling_options(default_timeout=300, default_interval=2)
@retry_option
@json_option
@with_client
def generate_slide_deck(
    ctx,
    description,
    prompt_file,
    notebook_id,
    deck_format,
    deck_length,
    language,
    source_ids,
    wait,
    timeout,
    interval,
    max_retries,
    json_output,
    client_auth,
):
    """Generate slide deck.

    \b
    Use --json for machine-readable output.

    \b
    Example:
      notebooklm generate slide-deck "include speaker notes"
      notebooklm generate slide-deck "executive summary" --format presenter --length short
    """
    description = resolve_prompt(description, prompt_file, "description")
    request = SlideDeckGenerationRequest(
        notebook_id=require_notebook(notebook_id),
        source_ids=_source_selection(ctx, source_ids),
        language=resolve_language(language),
        instructions=description or None,
        slide_format=_SLIDE_FORMAT_MAP[deck_format],
        slide_length=_SLIDE_LENGTH_MAP[deck_length],
        wait=wait,
        timeout=timeout,
        interval=interval,
        max_retries=max_retries,
    )
    return _run_generate(ctx=ctx, client_auth=client_auth, request=request, json_output=json_output)


@generate.command("revise-slide")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "-a",
    "--artifact",
    "artifact_id",
    required=True,
    help="Slide deck artifact ID to revise",
    shell_complete=_complete_artifacts,
)
@click.option(
    "--slide",
    "slide_index",
    type=int,
    required=True,
    help="Zero-based index of the slide to revise (0 = first slide)",
)
@wait_option
@wait_polling_options(default_timeout=300, default_interval=2)
@retry_option
@json_option
@with_client
def generate_revise_slide(
    ctx,
    description,
    prompt_file,
    notebook_id,
    artifact_id,
    slide_index,
    wait,
    timeout,
    interval,
    max_retries,
    json_output,
    client_auth,
):
    """Revise an individual slide in an existing slide deck.

    DESCRIPTION is the natural language prompt for the revision.
    The slide deck must already be generated before using this command.

    \b
    Example:
      notebooklm generate revise-slide "Move the title up" --artifact <id> --slide 0
      notebooklm generate revise-slide "Remove taxonomy" --artifact <id> --slide 3 --wait
    """
    description = resolve_prompt(description, prompt_file, "description", required=True)
    request = ReviseSlideGenerationRequest(
        notebook_id=require_notebook(notebook_id),
        artifact_id=artifact_id,
        slide_index=slide_index,
        prompt=description,
        wait=wait,
        timeout=timeout,
        interval=interval,
        max_retries=max_retries,
    )
    return _run_generate(ctx=ctx, client_auth=client_auth, request=request, json_output=json_output)


@generate.command("quiz")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--quantity",
    type=click.Choice(list(_QUIZ_QUANTITY_MAP)),
    default="standard",
    help="Number of questions (default: standard)",
)
@click.option(
    "--difficulty",
    type=click.Choice(list(_QUIZ_DIFFICULTY_MAP)),
    default="medium",
    help="Question difficulty (default: medium)",
)
@multi_source_option
@wait_option
@wait_polling_options(default_timeout=300, default_interval=2)
@retry_option
@json_option
@with_client
def generate_quiz(
    ctx,
    description,
    prompt_file,
    notebook_id,
    quantity,
    difficulty,
    source_ids,
    wait,
    timeout,
    interval,
    max_retries,
    json_output,
    client_auth,
):
    """Generate quiz.

    \b
    Use --json for machine-readable output.

    \b
    Example:
      notebooklm generate quiz "focus on vocabulary terms"
      notebooklm generate quiz "test key concepts" --difficulty hard --quantity more
    """
    description = resolve_prompt(description, prompt_file, "description")
    request = QuizGenerationRequest(
        notebook_id=require_notebook(notebook_id),
        source_ids=_source_selection(ctx, source_ids),
        instructions=description or None,
        quantity=_QUIZ_QUANTITY_MAP[quantity],
        difficulty=_QUIZ_DIFFICULTY_MAP[difficulty],
        wait=wait,
        timeout=timeout,
        interval=interval,
        max_retries=max_retries,
    )
    return _run_generate(ctx=ctx, client_auth=client_auth, request=request, json_output=json_output)


@generate.command("flashcards")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--quantity",
    type=click.Choice(list(_QUIZ_QUANTITY_MAP)),
    default="standard",
    help="Number of flashcards (default: standard)",
)
@click.option(
    "--difficulty",
    type=click.Choice(list(_QUIZ_DIFFICULTY_MAP)),
    default="medium",
    help="Flashcard difficulty (default: medium)",
)
@multi_source_option
@wait_option
@wait_polling_options(default_timeout=300, default_interval=2)
@retry_option
@json_option
@with_client
def generate_flashcards(
    ctx,
    description,
    prompt_file,
    notebook_id,
    quantity,
    difficulty,
    source_ids,
    wait,
    timeout,
    interval,
    max_retries,
    json_output,
    client_auth,
):
    """Generate flashcards.

    \b
    Use --json for machine-readable output.

    \b
    Example:
      notebooklm generate flashcards "vocabulary terms only"
      notebooklm generate flashcards --quantity more --difficulty easy
    """
    description = resolve_prompt(description, prompt_file, "description")
    request = FlashcardsGenerationRequest(
        notebook_id=require_notebook(notebook_id),
        source_ids=_source_selection(ctx, source_ids),
        instructions=description or None,
        quantity=_QUIZ_QUANTITY_MAP[quantity],
        difficulty=_QUIZ_DIFFICULTY_MAP[difficulty],
        wait=wait,
        timeout=timeout,
        interval=interval,
        max_retries=max_retries,
    )
    return _run_generate(ctx=ctx, client_auth=client_auth, request=request, json_output=json_output)


@generate.command("infographic")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--orientation",
    type=click.Choice(["landscape", "portrait", "square"]),
    default="landscape",
    help="Infographic orientation (default: landscape)",
)
@click.option(
    "--detail",
    type=click.Choice(["concise", "standard", "detailed"]),
    default="standard",
    help="Level of detail (default: standard)",
)
@click.option(
    "--style",
    type=click.Choice(list(_INFOGRAPHIC_STYLE_MAP)),
    default="auto",
    help="Visual style (default: auto)",
)
@language_option
@multi_source_option
@wait_option
@wait_polling_options(default_timeout=300, default_interval=2)
@retry_option
@json_option
@with_client
def generate_infographic(
    ctx,
    description,
    prompt_file,
    notebook_id,
    orientation,
    detail,
    style,
    language,
    source_ids,
    wait,
    timeout,
    interval,
    max_retries,
    json_output,
    client_auth,
):
    """Generate infographic.

    \b
    Use --json for machine-readable output.

    \b
    Example:
      notebooklm generate infographic "include statistics and key findings"
      notebooklm generate infographic --orientation portrait --detail detailed
    """
    description = resolve_prompt(description, prompt_file, "description")
    request = InfographicGenerationRequest(
        notebook_id=require_notebook(notebook_id),
        source_ids=_source_selection(ctx, source_ids),
        language=resolve_language(language),
        instructions=description or None,
        orientation=_INFOGRAPHIC_ORIENTATION_MAP[orientation],
        detail_level=_INFOGRAPHIC_DETAIL_MAP[detail],
        style=_INFOGRAPHIC_STYLE_MAP[style],
        wait=wait,
        timeout=timeout,
        interval=interval,
        max_retries=max_retries,
    )
    return _run_generate(ctx=ctx, client_auth=client_auth, request=request, json_output=json_output)


@generate.command("data-table")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@language_option
@multi_source_option
@wait_option
@wait_polling_options(default_timeout=300, default_interval=2)
@retry_option
@json_option
@with_client
def generate_data_table(
    ctx,
    description,
    prompt_file,
    notebook_id,
    language,
    source_ids,
    wait,
    timeout,
    interval,
    max_retries,
    json_output,
    client_auth,
):
    """Generate data table.

    \b
    Use --json for machine-readable output.

    \b
    Example:
      notebooklm generate data-table "comparison of key concepts"
      notebooklm generate data-table -s src_001 "timeline of events"
    """
    description = resolve_prompt(description, prompt_file, "description", required=True)
    request = DataTableGenerationRequest(
        notebook_id=require_notebook(notebook_id),
        source_ids=_source_selection(ctx, source_ids),
        language=resolve_language(language),
        instructions=description,
        wait=wait,
        timeout=timeout,
        interval=interval,
        max_retries=max_retries,
    )
    return _run_generate(ctx=ctx, client_auth=client_auth, request=request, json_output=json_output)


@generate.command("mind-map")
@notebook_option
@multi_source_option
@language_option
@click.option(
    "--instructions",
    default=None,
    help="Custom prompt to steer the mind map. Applied reliably for the "
    "interactive kind; sent for note-backed too, but the server may ignore it.",
)
@click.option(
    "--kind",
    "map_kind",
    type=click.Choice(["interactive", "note-backed"]),
    default="interactive",
    show_default=True,
    help=(
        "Which mind map to generate: 'interactive' (studio artifact, polled to "
        "completion) or 'note-backed' (JSON tree, synchronous)."
    ),
)
@json_option
@with_client
def generate_mind_map(
    ctx, notebook_id, source_ids, language, instructions, map_kind, json_output, client_auth
):
    """Generate mind map.

    \b
    Two kinds (issue #1256):
      --kind interactive   interactive studio artifact, polled to completion (default)
      --kind note-backed   JSON tree, synchronous
    Both export the same JSON node tree via 'download mind-map'.
    --instructions is a free-text prompt that steers generation; the
    interactive kind applies it reliably (the note-backed kind passes it
    through, but the server may not always honor it).

    \b
    Use --json for machine-readable output.
    """
    request = MindMapGenerationRequest(
        notebook_id=require_notebook(notebook_id),
        source_ids=_source_selection(ctx, source_ids),
        language=resolve_language(language),
        instructions=instructions,
        map_kind=(
            MindMapKind.INTERACTIVE if map_kind == "interactive" else MindMapKind.NOTE_BACKED
        ),
    )
    return _run_generate(ctx=ctx, client_auth=client_auth, request=request, json_output=json_output)


@generate.command("report")
@click.argument("description", default="", required=False)
@prompt_file_option
@click.option(
    "--format",
    "report_format",
    type=click.Choice(["briefing-doc", "study-guide", "blog-post", "custom"]),
    default="briefing-doc",
    help="Report format (default: briefing-doc)",
)
@notebook_option
@multi_source_option
@language_option
@click.option(
    "--append",
    "append_instructions",
    default=None,
    help="Append extra instructions to the built-in prompt for non-custom formats. Has no effect with --format custom.",
)
@wait_option
@wait_polling_options(default_timeout=300, default_interval=2)
@retry_option
@json_option
@with_client
def generate_report_cmd(
    ctx,
    description,
    prompt_file,
    report_format,
    notebook_id,
    source_ids,
    language,
    append_instructions,
    wait,
    timeout,
    interval,
    max_retries,
    json_output,
    client_auth,
):
    """Generate a report (briefing doc, study guide, blog post, or custom).

    \b
    Use --json for machine-readable output.

    \b
    Examples:
      notebooklm generate report                              # briefing-doc (default)
      notebooklm generate report --format study-guide         # study guide
      notebooklm generate report -s src_001 -s src_002        # from specific sources
      notebooklm generate report "Create a white paper..."    # custom report
      notebooklm generate report --format briefing-doc --append "Focus on AI trends"
      notebooklm generate report --format study-guide --append "Target audience: beginners"
    """
    description = resolve_prompt(description, prompt_file, "description")
    actual_format = report_format
    custom_prompt = description or None
    if description and report_format == "briefing-doc":
        actual_format = "custom"
    notices: tuple[str, ...] = ()
    if append_instructions and actual_format == "custom":
        notices = (
            "Warning: --append has no effect with --format custom. "
            "Use the description argument instead.",
        )
        append_instructions = None
    request = ReportGenerationRequest(
        notebook_id=require_notebook(notebook_id),
        source_ids=_source_selection(ctx, source_ids),
        language=resolve_language(language),
        report_format=_REPORT_FORMAT_MAP[actual_format],
        custom_prompt=custom_prompt,
        extra_instructions=append_instructions,
        wait=wait,
        timeout=timeout,
        interval=interval,
        max_retries=max_retries,
    )
    return _run_generate(
        ctx=ctx,
        client_auth=client_auth,
        request=request,
        json_output=json_output,
        notices=notices,
    )
