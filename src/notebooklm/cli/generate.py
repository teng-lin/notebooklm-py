"""Generate content CLI commands.

Commands:
    audio            Generate audio overview (podcast)
    video            Generate video overview
    cinematic-video  Generate cinematic video overview (AI documentary footage)
    slide-deck       Generate slide deck
    quiz         Generate quiz
    flashcards   Generate flashcards
    infographic  Generate infographic
    data-table   Generate data table
    mind-map     Generate mind map
    report       Generate report
"""

import os
from typing import Any

import click
from click.core import ParameterSource

from ..client import NotebookLMClient
from ..types import (
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
from .auth_runtime import with_client
from .input import resolve_prompt
from .language import SUPPORTED_LANGUAGES, get_language
from .options import (
    _complete_artifacts,
    _complete_sources,
    json_option,
    notebook_option,
    prompt_file_option,
    retry_option,
    wait_polling_options,
)
from .rendering import (
    console,
    json_error_response,
    json_output_response,
)
from .resolve import (
    require_notebook,
    resolve_notebook_id,
    resolve_source_ids,
)
from .services.artifact_generation import generate_with_retry, handle_generation_result

DEFAULT_LANGUAGE = "en"

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


def resolve_language(language: str | None) -> str:
    """Resolve language from CLI flag, NOTEBOOKLM_HL env, config, or default.

    Priority: ``--language`` flag > ``NOTEBOOKLM_HL`` env var > config file
    > "en" default. Uses explicit None checks to avoid treating empty
    string as falsy. Validates each candidate against the supported list.
    """
    if language is not None:
        if language not in SUPPORTED_LANGUAGES:
            raise click.BadParameter(
                f"Unknown language code: {language}\n"
                "Run 'notebooklm language list' to see supported codes.",
                param_hint="'--language'",
            )
        return language
    env_lang = os.environ.get("NOTEBOOKLM_HL", "").strip()
    if env_lang:
        if env_lang not in SUPPORTED_LANGUAGES:
            raise click.BadParameter(
                f"Unknown language code: {env_lang}\n"
                "Run 'notebooklm language list' to see supported codes.",
                param_hint="'NOTEBOOKLM_HL'",
            )
        return env_lang
    config_lang = get_language()
    if config_lang is not None:
        if config_lang not in SUPPORTED_LANGUAGES:
            raise click.BadParameter(
                f"Unknown language code in config: {config_lang}\n"
                "Run 'notebooklm language list' to see supported codes.",
                param_hint="config",
            )
        return config_lang
    return DEFAULT_LANGUAGE


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
)
@click.option(
    "--length",
    "audio_length",
    type=click.Choice(["short", "default", "long"]),
    default="default",
)
@click.option(
    "--language",
    default=None,
    help="Output language (default: --language > NOTEBOOKLM_HL env > config > 'en')",
)
@click.option(
    "--source",
    "-s",
    "source_ids",
    multiple=True,
    help="Limit to specific source IDs",
    shell_complete=_complete_sources,
)
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@wait_polling_options(default_timeout=300, default_interval=2)
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
    nb_id = require_notebook(notebook_id)
    format_map = {
        "deep-dive": AudioFormat.DEEP_DIVE,
        "brief": AudioFormat.BRIEF,
        "critique": AudioFormat.CRITIQUE,
        "debate": AudioFormat.DEBATE,
    }
    length_map = {
        "short": AudioLength.SHORT,
        "default": AudioLength.DEFAULT,
        "long": AudioLength.LONG,
    }

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )

            async def _generate():
                return await client.artifacts.generate_audio(
                    nb_id_resolved,
                    source_ids=sources,
                    language=resolve_language(language),
                    instructions=description or None,
                    audio_format=format_map[audio_format],
                    audio_length=length_map[audio_length],
                )

            result = await generate_with_retry(_generate, max_retries, "audio", json_output)
            await handle_generation_result(
                client,
                nb_id_resolved,
                result,
                "audio",
                wait,
                json_output,
                timeout=float(timeout),
                interval=float(interval),
            )

    return _run()


@generate.command("video")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--format",
    "video_format",
    type=click.Choice(["explainer", "brief", "cinematic"]),
    default="explainer",
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
)
@click.option("--style-prompt", default=None, help="Custom visual style prompt")
@click.option(
    "--language",
    default=None,
    help="Output language (default: --language > NOTEBOOKLM_HL env > config > 'en')",
)
@click.option(
    "--source",
    "-s",
    "source_ids",
    multiple=True,
    help="Limit to specific source IDs",
    shell_complete=_complete_sources,
)
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@wait_polling_options(default_timeout=600, default_interval=2)
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
    # The 'generate cinematic-video' alias hard-pins --format to 'cinematic'.
    # If the user explicitly passed --format with any non-cinematic value, raise
    # rather than silently overriding (mirrors the --style-prompt rejection
    # pattern below). When --format was not passed at all, fall through to the
    # implicit 'cinematic' coercion.
    if ctx.info_name == "cinematic-video":
        format_source = ctx.get_parameter_source("video_format")
        format_explicit = format_source == ParameterSource.COMMANDLINE
        if format_explicit and video_format != "cinematic":
            raise click.UsageError(
                "--format must be 'cinematic' for the cinematic-video subcommand "
                "(use 'generate video --format <other>' for other formats)"
            )
        video_format = "cinematic"

    nb_id = require_notebook(notebook_id)
    format_map = {
        "explainer": VideoFormat.EXPLAINER,
        "brief": VideoFormat.BRIEF,
        "cinematic": VideoFormat.CINEMATIC,
    }
    style_map = {
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
    is_cinematic = video_format == "cinematic"
    normalized_style_prompt = style_prompt.strip() if style_prompt is not None else None
    if is_cinematic and normalized_style_prompt:
        raise click.UsageError("--style-prompt cannot be used with cinematic video")
    if not is_cinematic and style == "custom" and not normalized_style_prompt:
        raise click.UsageError("--style custom requires --style-prompt")
    if not is_cinematic and normalized_style_prompt and style != "custom":
        raise click.UsageError("--style-prompt requires --style custom")

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )

            async def _generate():
                if is_cinematic:
                    return await client.artifacts.generate_cinematic_video(
                        nb_id_resolved,
                        source_ids=sources,
                        language=resolve_language(language),
                        instructions=description or None,
                    )
                return await client.artifacts.generate_video(
                    nb_id_resolved,
                    source_ids=sources,
                    language=resolve_language(language),
                    instructions=description or None,
                    video_format=format_map[video_format],
                    video_style=style_map[style],
                    style_prompt=normalized_style_prompt,
                )

            # Cinematic videos default to a longer 1800s ceiling (Veo 3 takes
            # ~30-40 min). Preserve that default *only* when the user did not
            # pass --timeout explicitly; an explicit value always wins so
            # `generate cinematic-video --timeout 60` honors the override.
            timeout_value = float(timeout)
            if is_cinematic and ctx.get_parameter_source("timeout") != ParameterSource.COMMANDLINE:
                timeout_value = 1800.0
            result = await generate_with_retry(_generate, max_retries, "video", json_output)
            await handle_generation_result(
                client,
                nb_id_resolved,
                result,
                "video",
                wait,
                json_output,
                timeout=timeout_value,
                interval=float(interval),
            )

    return _run()


# Convenience alias: 'generate cinematic-video' delegates to 'generate video --format cinematic'.
# Reuses generate_video's callback/params so changes stay in sync automatically.
_cinematic_video_gen_cmd = click.Command(
    name="cinematic-video",
    callback=generate_video.callback,
    params=list(generate_video.params),
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
generate.add_command(_cinematic_video_gen_cmd)


@generate.command("slide-deck")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--format",
    "deck_format",
    type=click.Choice(["detailed", "presenter"]),
    default="detailed",
)
@click.option(
    "--length",
    "deck_length",
    type=click.Choice(["default", "short"]),
    default="default",
)
@click.option(
    "--language",
    default=None,
    help="Output language (default: --language > NOTEBOOKLM_HL env > config > 'en')",
)
@click.option(
    "--source",
    "-s",
    "source_ids",
    multiple=True,
    help="Limit to specific source IDs",
    shell_complete=_complete_sources,
)
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
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
    nb_id = require_notebook(notebook_id)
    format_map = {
        "detailed": SlideDeckFormat.DETAILED_DECK,
        "presenter": SlideDeckFormat.PRESENTER_SLIDES,
    }
    length_map = {
        "default": SlideDeckLength.DEFAULT,
        "short": SlideDeckLength.SHORT,
    }

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )

            async def _generate():
                return await client.artifacts.generate_slide_deck(
                    nb_id_resolved,
                    source_ids=sources,
                    language=resolve_language(language),
                    instructions=description or None,
                    slide_format=format_map[deck_format],
                    slide_length=length_map[deck_length],
                )

            result = await generate_with_retry(_generate, max_retries, "slide deck", json_output)
            await handle_generation_result(
                client,
                nb_id_resolved,
                result,
                "slide deck",
                wait,
                json_output,
                timeout=float(timeout),
                interval=float(interval),
            )

    return _run()


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
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
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
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)

            async def _generate():
                return await client.artifacts.revise_slide(
                    nb_id_resolved,
                    artifact_id=artifact_id,
                    slide_index=slide_index,
                    prompt=description,
                )

            result = await generate_with_retry(
                _generate, max_retries, "slide revision", json_output
            )
            await handle_generation_result(
                client,
                nb_id_resolved,
                result,
                "slide revision",
                wait,
                json_output,
                timeout=float(timeout),
                interval=float(interval),
            )

    return _run()


@generate.command("quiz")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option("--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
@click.option(
    "--source",
    "-s",
    "source_ids",
    multiple=True,
    help="Limit to specific source IDs",
    shell_complete=_complete_sources,
)
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
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
    nb_id = require_notebook(notebook_id)
    quantity_map = {
        "fewer": QuizQuantity.FEWER,
        "standard": QuizQuantity.STANDARD,
        "more": QuizQuantity.MORE,
    }
    difficulty_map = {
        "easy": QuizDifficulty.EASY,
        "medium": QuizDifficulty.MEDIUM,
        "hard": QuizDifficulty.HARD,
    }

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )

            async def _generate():
                return await client.artifacts.generate_quiz(
                    nb_id_resolved,
                    source_ids=sources,
                    instructions=description or None,
                    quantity=quantity_map[quantity],
                    difficulty=difficulty_map[difficulty],
                )

            result = await generate_with_retry(_generate, max_retries, "quiz", json_output)
            await handle_generation_result(
                client,
                nb_id_resolved,
                result,
                "quiz",
                wait,
                json_output,
                timeout=float(timeout),
                interval=float(interval),
            )

    return _run()


@generate.command("flashcards")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option("--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
@click.option(
    "--source",
    "-s",
    "source_ids",
    multiple=True,
    help="Limit to specific source IDs",
    shell_complete=_complete_sources,
)
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
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
    nb_id = require_notebook(notebook_id)
    quantity_map = {
        "fewer": QuizQuantity.FEWER,
        "standard": QuizQuantity.STANDARD,
        "more": QuizQuantity.MORE,
    }
    difficulty_map = {
        "easy": QuizDifficulty.EASY,
        "medium": QuizDifficulty.MEDIUM,
        "hard": QuizDifficulty.HARD,
    }

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )

            async def _generate():
                return await client.artifacts.generate_flashcards(
                    nb_id_resolved,
                    source_ids=sources,
                    instructions=description or None,
                    quantity=quantity_map[quantity],
                    difficulty=difficulty_map[difficulty],
                )

            result = await generate_with_retry(_generate, max_retries, "flashcards", json_output)
            await handle_generation_result(
                client,
                nb_id_resolved,
                result,
                "flashcards",
                wait,
                json_output,
                timeout=float(timeout),
                interval=float(interval),
            )

    return _run()


@generate.command("infographic")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--orientation",
    type=click.Choice(["landscape", "portrait", "square"]),
    default="landscape",
)
@click.option(
    "--detail",
    type=click.Choice(["concise", "standard", "detailed"]),
    default="standard",
)
@click.option(
    "--style",
    type=click.Choice(list(_INFOGRAPHIC_STYLE_MAP)),
    default="auto",
)
@click.option(
    "--language",
    default=None,
    help="Output language (default: --language > NOTEBOOKLM_HL env > config > 'en')",
)
@click.option(
    "--source",
    "-s",
    "source_ids",
    multiple=True,
    help="Limit to specific source IDs",
    shell_complete=_complete_sources,
)
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
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
    nb_id = require_notebook(notebook_id)
    orientation_map = {
        "landscape": InfographicOrientation.LANDSCAPE,
        "portrait": InfographicOrientation.PORTRAIT,
        "square": InfographicOrientation.SQUARE,
    }
    detail_map = {
        "concise": InfographicDetail.CONCISE,
        "standard": InfographicDetail.STANDARD,
        "detailed": InfographicDetail.DETAILED,
    }

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )

            async def _generate():
                return await client.artifacts.generate_infographic(
                    nb_id_resolved,
                    source_ids=sources,
                    language=resolve_language(language),
                    instructions=description or None,
                    orientation=orientation_map[orientation],
                    detail_level=detail_map[detail],
                    style=_INFOGRAPHIC_STYLE_MAP[style],
                )

            result = await generate_with_retry(_generate, max_retries, "infographic", json_output)
            await handle_generation_result(
                client,
                nb_id_resolved,
                result,
                "infographic",
                wait,
                json_output,
                timeout=float(timeout),
                interval=float(interval),
            )

    return _run()


@generate.command("data-table")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--language",
    default=None,
    help="Output language (default: --language > NOTEBOOKLM_HL env > config > 'en')",
)
@click.option(
    "--source",
    "-s",
    "source_ids",
    multiple=True,
    help="Limit to specific source IDs",
    shell_complete=_complete_sources,
)
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
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
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )

            async def _generate():
                return await client.artifacts.generate_data_table(
                    nb_id_resolved,
                    source_ids=sources,
                    language=resolve_language(language),
                    instructions=description,
                )

            result = await generate_with_retry(_generate, max_retries, "data table", json_output)
            await handle_generation_result(
                client,
                nb_id_resolved,
                result,
                "data table",
                wait,
                json_output,
                timeout=float(timeout),
                interval=float(interval),
            )

    return _run()


@generate.command("mind-map")
@notebook_option
@click.option(
    "--source",
    "-s",
    "source_ids",
    multiple=True,
    help="Limit to specific source IDs",
    shell_complete=_complete_sources,
)
@click.option(
    "--language",
    default=None,
    help="Output language (default: --language > NOTEBOOKLM_HL env > config > 'en')",
)
@click.option("--instructions", default=None, help="Custom instructions for the mind map")
@json_option
@with_client
def generate_mind_map(
    ctx, notebook_id, source_ids, language, instructions, json_output, client_auth
):
    """Generate mind map.

    \b
    Use --json for machine-readable output.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )

            async def _generate():
                return await client.artifacts.generate_mind_map(
                    nb_id_resolved,
                    source_ids=sources,
                    language=resolve_language(language),
                    instructions=instructions,
                )

            if json_output:
                result = await _generate()
            else:
                with console.status("Generating mind map..."):
                    result = await _generate()

            _output_mind_map_result(result, json_output)

    return _run()


def _output_mind_map_result(result: Any, json_output: bool) -> None:
    """Output mind map result in appropriate format."""
    if not result:
        if json_output:
            json_error_response("GENERATION_FAILED", "Mind map generation failed")
        else:
            console.print("[yellow]No result[/yellow]")
        return

    if json_output:
        json_output_response(result)
        return

    console.print("[green]Mind map generated:[/green]")
    if isinstance(result, dict):
        console.print(f"  Note ID: {result.get('note_id', '-')}")
        mind_map = result.get("mind_map", {})
        if isinstance(mind_map, dict):
            console.print(f"  Root: {mind_map.get('name', '-')}")
            console.print(f"  Children: {len(mind_map.get('children', []))} nodes")
    else:
        console.print(result)


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
@click.option(
    "--source",
    "-s",
    "source_ids",
    multiple=True,
    help="Limit to specific source IDs",
    shell_complete=_complete_sources,
)
@click.option(
    "--language",
    default=None,
    help="Output language (default: --language > NOTEBOOKLM_HL env > config > 'en')",
)
@click.option(
    "--append",
    "append_instructions",
    default=None,
    help="Append extra instructions to the built-in prompt for non-custom formats. Has no effect with --format custom.",
)
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
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
    nb_id = require_notebook(notebook_id)

    # Smart detection: if description provided without explicit format change, treat as custom
    actual_format = report_format
    custom_prompt = None
    if description:
        if report_format == "briefing-doc":
            actual_format = "custom"
            custom_prompt = description
        else:
            custom_prompt = description

    if append_instructions and actual_format == "custom":
        click.echo(
            "Warning: --append has no effect with --format custom. Use the description argument instead.",
            err=True,
        )
        append_instructions = None

    format_map = {
        "briefing-doc": ReportFormat.BRIEFING_DOC,
        "study-guide": ReportFormat.STUDY_GUIDE,
        "blog-post": ReportFormat.BLOG_POST,
        "custom": ReportFormat.CUSTOM,
    }
    report_format_enum = format_map[actual_format]

    format_display = {
        "briefing-doc": "briefing document",
        "study-guide": "study guide",
        "blog-post": "blog post",
        "custom": "custom report",
    }[actual_format]

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )

            async def _generate():
                return await client.artifacts.generate_report(
                    nb_id_resolved,
                    source_ids=sources,
                    language=resolve_language(language),
                    report_format=report_format_enum,
                    custom_prompt=custom_prompt,
                    extra_instructions=append_instructions,
                )

            result = await generate_with_retry(_generate, max_retries, format_display, json_output)
            await handle_generation_result(
                client,
                nb_id_resolved,
                result,
                format_display,
                wait,
                json_output,
                timeout=float(timeout),
                interval=float(interval),
            )

    return _run()
