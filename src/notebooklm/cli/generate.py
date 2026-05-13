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

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

import click

from ..client import NotebookLMClient
from ..types import (
    AudioFormat,
    AudioLength,
    GenerationStatus,
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
from .helpers import (
    console,
    json_error_response,
    json_output_response,
    require_notebook,
    resolve_notebook_id,
    resolve_source_ids,
    with_client,
)
from .language import SUPPORTED_LANGUAGES, get_language
from .options import json_option, retry_option

DEFAULT_LANGUAGE = "en"

# Retry constants
RETRY_INITIAL_DELAY = 60.0  # seconds
RETRY_MAX_DELAY = 300.0  # 5 minutes
RETRY_BACKOFF_MULTIPLIER = 2.0

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


def calculate_backoff_delay(
    attempt: int,
    initial_delay: float = RETRY_INITIAL_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    multiplier: float = RETRY_BACKOFF_MULTIPLIER,
) -> float:
    """Calculate exponential backoff delay for a retry attempt.

    Args:
        attempt: The current attempt number (0-indexed).
        initial_delay: Initial delay in seconds.
        max_delay: Maximum delay cap in seconds.
        multiplier: Backoff multiplier.

    Returns:
        Delay in seconds for this attempt.
    """
    delay = initial_delay * (multiplier**attempt)
    return min(delay, max_delay)


async def generate_with_retry(
    generate_fn: Callable[[], Awaitable[GenerationStatus | None]],
    max_retries: int,
    artifact_type: str,
    json_output: bool = False,
) -> GenerationStatus | None:
    """Generate artifact with retry on rate limit.

    Retries the generation call with exponential backoff when rate limited.
    Always makes at least one attempt, even when max_retries=0.

    Args:
        generate_fn: Async function that performs the generation.
        max_retries: Maximum number of retries (0 = no retry, just one attempt).
        artifact_type: Display name for progress messages.
        json_output: Whether to suppress console output.

    Returns:
        GenerationStatus or None if generation failed.
    """
    for attempt in range(max_retries + 1):
        result = await generate_fn()

        # Return immediately if not rate limited (success or other failure)
        if not isinstance(result, GenerationStatus) or not result.is_rate_limited:
            return result

        # Rate limited with no retries left
        if attempt >= max_retries:
            return result

        # Wait before retry
        delay = calculate_backoff_delay(attempt)
        if not json_output:
            console.print(
                f"[yellow]{artifact_type.title()} rate limited. "
                f"Retrying in {int(delay)}s (attempt {attempt + 2}/{max_retries + 1})...[/yellow]"
            )
        await asyncio.sleep(delay)

    # Unreachable, but satisfies type checker
    return None


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


async def handle_generation_result(
    client: NotebookLMClient,
    notebook_id: str,
    result: Any,
    artifact_type: str,
    wait: bool = False,
    json_output: bool = False,
    timeout: float = 300.0,
) -> GenerationStatus | None:
    """Handle generation result with optional waiting and output formatting.

    Consolidates common pattern across all generate commands:
    - Check for None/failed result
    - Optionally wait for completion
    - Output status in JSON or console format

    Args:
        client: The NotebookLM client.
        notebook_id: The notebook ID.
        result: The generation result from artifacts API.
        artifact_type: Display name for the artifact type (e.g., "audio", "video").
        wait: Whether to wait for completion.
        json_output: Whether to output as JSON.
        timeout: Timeout for waiting (default: 300s).

    Returns:
        Final GenerationStatus, or None if generation failed.
    """
    # Handle failed generation or rate limiting
    if not result:
        if json_output:
            json_error_response(
                "GENERATION_FAILED",
                f"{artifact_type.title()} generation failed",
            )
        else:
            console.print(f"[red]{artifact_type.title()} generation failed.[/red]")
        return None

    # Check for rate limiting (result exists but failed due to rate limit)
    if isinstance(result, GenerationStatus) and result.is_rate_limited:
        if json_output:
            json_error_response(
                "RATE_LIMITED",
                f"{artifact_type.title()} generation rate limited by Google",
            )
        else:
            console.print(
                f"[red]{artifact_type.title()} generation rate limited by Google.[/red]\n"
                "[yellow]Daily quota may be exceeded. Try again in 1-24 hours, "
                "or use --retry N to retry automatically.[/yellow]"
            )
        return result

    # Extract task_id from various result formats
    task_id: str | None = None
    status: Any = result
    if isinstance(result, GenerationStatus):
        task_id = result.task_id
        status = result
    elif isinstance(result, dict):
        task_id = result.get("artifact_id") or result.get("task_id")
        status = result
    elif isinstance(result, list) and len(result) > 0:
        task_id = result[0] if isinstance(result[0], str) else None
        status = result

    # Wait for completion if requested
    if wait and task_id:
        if not json_output:
            console.print(f"[yellow]Generating {artifact_type}...[/yellow] Task: {task_id}")
        status = await client.artifacts.wait_for_completion(notebook_id, task_id, timeout=timeout)

    # Output status
    _output_generation_status(status, artifact_type, json_output)

    return status if isinstance(status, GenerationStatus) else None


def _extract_task_id(status: Any) -> str | None:
    """Extract task ID from various status formats.

    Handles GenerationStatus objects, dicts with task_id/artifact_id keys,
    and lists where the first element is an ID string.
    """
    if hasattr(status, "task_id"):
        return status.task_id
    if isinstance(status, dict):
        return status.get("task_id") or status.get("artifact_id")
    if isinstance(status, list) and len(status) > 0 and isinstance(status[0], str):
        return status[0]
    return None


def _output_generation_status(status: Any, artifact_type: str, json_output: bool) -> None:
    """Output generation status in appropriate format."""
    is_complete = hasattr(status, "is_complete") and status.is_complete
    is_failed = hasattr(status, "is_failed") and status.is_failed

    if json_output:
        if is_complete:
            json_output_response(
                {
                    "task_id": getattr(status, "task_id", None),
                    "status": "completed",
                    "url": getattr(status, "url", None),
                }
            )
        elif is_failed:
            json_error_response(
                "GENERATION_FAILED",
                getattr(status, "error", None) or f"{artifact_type.title()} generation failed",
            )
        else:
            task_id = _extract_task_id(status)
            json_output_response({"task_id": task_id, "status": "pending"})
    else:
        if is_complete:
            url = getattr(status, "url", None)
            if url:
                console.print(f"[green]{artifact_type.title()} ready:[/green] {url}")
            else:
                console.print(f"[green]{artifact_type.title()} ready[/green]")
        elif is_failed:
            console.print(f"[red]Failed:[/red] {getattr(status, 'error', 'Unknown error')}")
        else:
            task_id = _extract_task_id(status)
            console.print(f"[yellow]Started:[/yellow] {task_id or status}")


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
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
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
@click.option("--source", "-s", "source_ids", multiple=True, help="Limit to specific source IDs")
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@retry_option
@json_option
@with_client
def generate_audio(
    ctx,
    description,
    notebook_id,
    audio_format,
    audio_length,
    language,
    source_ids,
    wait,
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            sources = await resolve_source_ids(client, nb_id_resolved, source_ids)

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
                client, nb_id_resolved, result, "audio", wait, json_output
            )

    return _run()


@generate.command("video")
@click.argument("description", default="", required=False)
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
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
@click.option("--source", "-s", "source_ids", multiple=True, help="Limit to specific source IDs")
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@retry_option
@json_option
@with_client
def generate_video(
    ctx,
    description,
    notebook_id,
    video_format,
    style,
    style_prompt,
    language,
    source_ids,
    wait,
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
    # Auto-select cinematic format when invoked as 'generate cinematic-video'
    if ctx.info_name == "cinematic-video":
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            sources = await resolve_source_ids(client, nb_id_resolved, source_ids)

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

            timeout = 1800.0 if is_cinematic else 600.0
            result = await generate_with_retry(_generate, max_retries, "video", json_output)
            await handle_generation_result(
                client, nb_id_resolved, result, "video", wait, json_output, timeout=timeout
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
        "Example:\n"
        '  notebooklm generate cinematic-video "documentary about quantum physics"'
    ),
)
generate.add_command(_cinematic_video_gen_cmd)


@generate.command("slide-deck")
@click.argument("description", default="", required=False)
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
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
@click.option("--source", "-s", "source_ids", multiple=True, help="Limit to specific source IDs")
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@retry_option
@json_option
@with_client
def generate_slide_deck(
    ctx,
    description,
    notebook_id,
    deck_format,
    deck_length,
    language,
    source_ids,
    wait,
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            sources = await resolve_source_ids(client, nb_id_resolved, source_ids)

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
                client, nb_id_resolved, result, "slide deck", wait, json_output
            )

    return _run()


@generate.command("revise-slide")
@click.argument("description")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "-a",
    "--artifact",
    "artifact_id",
    required=True,
    help="Slide deck artifact ID to revise",
)
@click.option(
    "--slide",
    "slide_index",
    type=int,
    required=True,
    help="Zero-based index of the slide to revise (0 = first slide)",
)
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@retry_option
@json_option
@with_client
def generate_revise_slide(
    ctx,
    description,
    notebook_id,
    artifact_id,
    slide_index,
    wait,
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
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)

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
                client, nb_id_resolved, result, "slide revision", wait, json_output
            )

    return _run()


@generate.command("quiz")
@click.argument("description", default="", required=False)
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
@click.option("--source", "-s", "source_ids", multiple=True, help="Limit to specific source IDs")
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@retry_option
@json_option
@with_client
def generate_quiz(
    ctx,
    description,
    notebook_id,
    quantity,
    difficulty,
    source_ids,
    wait,
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            sources = await resolve_source_ids(client, nb_id_resolved, source_ids)

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
                client, nb_id_resolved, result, "quiz", wait, json_output
            )

    return _run()


@generate.command("flashcards")
@click.argument("description", default="", required=False)
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
@click.option("--source", "-s", "source_ids", multiple=True, help="Limit to specific source IDs")
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@retry_option
@json_option
@with_client
def generate_flashcards(
    ctx,
    description,
    notebook_id,
    quantity,
    difficulty,
    source_ids,
    wait,
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            sources = await resolve_source_ids(client, nb_id_resolved, source_ids)

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
                client, nb_id_resolved, result, "flashcards", wait, json_output
            )

    return _run()


@generate.command("infographic")
@click.argument("description", default="", required=False)
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
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
@click.option("--source", "-s", "source_ids", multiple=True, help="Limit to specific source IDs")
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@retry_option
@json_option
@with_client
def generate_infographic(
    ctx,
    description,
    notebook_id,
    orientation,
    detail,
    style,
    language,
    source_ids,
    wait,
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            sources = await resolve_source_ids(client, nb_id_resolved, source_ids)

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
                client, nb_id_resolved, result, "infographic", wait, json_output
            )

    return _run()


@generate.command("data-table")
@click.argument("description")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--language",
    default=None,
    help="Output language (default: --language > NOTEBOOKLM_HL env > config > 'en')",
)
@click.option("--source", "-s", "source_ids", multiple=True, help="Limit to specific source IDs")
@click.option("--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)")
@retry_option
@json_option
@with_client
def generate_data_table(
    ctx,
    description,
    notebook_id,
    language,
    source_ids,
    wait,
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
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            sources = await resolve_source_ids(client, nb_id_resolved, source_ids)

            async def _generate():
                return await client.artifacts.generate_data_table(
                    nb_id_resolved,
                    source_ids=sources,
                    language=resolve_language(language),
                    instructions=description,
                )

            result = await generate_with_retry(_generate, max_retries, "data table", json_output)
            await handle_generation_result(
                client, nb_id_resolved, result, "data table", wait, json_output
            )

    return _run()


@generate.command("mind-map")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--source", "-s", "source_ids", multiple=True, help="Limit to specific source IDs")
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            sources = await resolve_source_ids(client, nb_id_resolved, source_ids)

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
@click.option(
    "--format",
    "report_format",
    type=click.Choice(["briefing-doc", "study-guide", "blog-post", "custom"]),
    default="briefing-doc",
    help="Report format (default: briefing-doc)",
)
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--source", "-s", "source_ids", multiple=True, help="Limit to specific source IDs")
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
@retry_option
@json_option
@with_client
def generate_report_cmd(
    ctx,
    description,
    report_format,
    notebook_id,
    source_ids,
    language,
    append_instructions,
    wait,
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            sources = await resolve_source_ids(client, nb_id_resolved, source_ids)

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
                client, nb_id_resolved, result, format_display, wait, json_output
            )

    return _run()
