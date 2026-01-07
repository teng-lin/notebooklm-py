"""Generate content CLI commands.

Commands:
    audio        Generate audio overview (podcast)
    video        Generate video overview
    slide-deck   Generate slide deck
    quiz         Generate quiz
    flashcards   Generate flashcards
    infographic  Generate infographic
    data-table   Generate data table
    mind-map     Generate mind map
    report       Generate report
"""

from typing import Any, Optional

import click

from ..client import NotebookLMClient
from ..types import (
    AudioFormat,
    AudioLength,
    VideoFormat,
    VideoStyle,
    QuizQuantity,
    QuizDifficulty,
    InfographicOrientation,
    InfographicDetail,
    SlideDeckFormat,
    SlideDeckLength,
    ReportFormat,
    GenerationStatus,
)
from .helpers import (
    console,
    require_notebook,
    with_client,
    json_output_response,
    json_error_response,
)


async def handle_generation_result(
    client: NotebookLMClient,
    notebook_id: str,
    result: Any,
    artifact_type: str,
    wait: bool = False,
    json_output: bool = False,
    timeout: float = 300.0,
) -> Optional[GenerationStatus]:
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
    # Handle failed generation
    if not result:
        if json_output:
            json_error_response(
                "GENERATION_FAILED",
                f"{artifact_type.title()} generation failed",
            )
        else:
            console.print(
                f"[red]{artifact_type.title()} generation failed "
                "(Google may be rate limiting)[/red]"
            )
        return None

    # Extract task_id from various result formats
    if isinstance(result, GenerationStatus):
        task_id = result.task_id
        status = result
    elif isinstance(result, dict):
        task_id = result.get("artifact_id") or result.get("task_id")
        status = result
    elif isinstance(result, list) and len(result) > 0:
        task_id = result[0]
        status = result
    else:
        task_id = None
        status = result

    # Wait for completion if requested
    if wait and task_id:
        if not json_output:
            console.print(
                f"[yellow]Generating {artifact_type}...[/yellow] Task: {task_id}"
            )
        status = await client.artifacts.wait_for_completion(
            notebook_id, task_id, timeout=timeout
        )

    # Output status
    _output_generation_status(status, artifact_type, json_output)

    return status if isinstance(status, GenerationStatus) else None


def _output_generation_status(
    status: Any, artifact_type: str, json_output: bool
) -> None:
    """Output generation status in appropriate format."""
    if json_output:
        if hasattr(status, "is_complete") and status.is_complete:
            json_output_response({
                "artifact_id": getattr(status, "task_id", None),
                "status": "completed",
                "url": getattr(status, "url", None),
            })
        elif hasattr(status, "is_failed") and status.is_failed:
            json_error_response(
                "GENERATION_FAILED",
                getattr(status, "error", None) or f"{artifact_type.title()} generation failed",
            )
        else:
            # Handle various result formats: GenerationStatus, dict, or list
            artifact_id = (
                getattr(status, "task_id", None)
                or (status.get("artifact_id") if isinstance(status, dict) else None)
                or (status[0] if isinstance(status, list) and len(status) > 0 else None)
            )
            json_output_response({"artifact_id": artifact_id, "status": "pending"})
    else:
        if hasattr(status, "is_complete") and status.is_complete:
            url = getattr(status, "url", None)
            if url:
                console.print(f"[green]{artifact_type.title()} ready:[/green] {url}")
            else:
                console.print(f"[green]{artifact_type.title()} ready[/green]")
        elif hasattr(status, "is_failed") and status.is_failed:
            console.print(f"[red]Failed:[/red] {getattr(status, 'error', 'Unknown error')}")
        else:
            # Extract task_id for cleaner display
            task_id = (
                getattr(status, "task_id", None)
                or (status.get("artifact_id") if isinstance(status, dict) else None)
                or (status[0] if isinstance(status, list) and len(status) > 0 else None)
            )
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
@click.option("--language", default="en")
@click.option(
    "--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)"
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def generate_audio(
    ctx,
    description,
    notebook_id,
    audio_format,
    audio_length,
    language,
    wait,
    json_output,
    client_auth,
):
    """Generate audio overview (podcast).

    \b
    Example:
      notebooklm generate audio "deep dive focusing on key themes"
      notebooklm generate audio "make it funny and casual" --format debate
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
            result = await client.artifacts.generate_audio(
                nb_id,
                language=language,
                instructions=description or None,
                audio_format=format_map[audio_format],
                audio_length=length_map[audio_length],
            )
            await handle_generation_result(
                client, nb_id, result, "audio", wait, json_output
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
    type=click.Choice(["explainer", "brief"]),
    default="explainer",
)
@click.option(
    "--style",
    type=click.Choice(
        [
            "auto",
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
@click.option("--language", default="en")
@click.option(
    "--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)"
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def generate_video(
    ctx, description, notebook_id, video_format, style, language, wait, json_output, client_auth
):
    """Generate video overview.

    \b
    Example:
      notebooklm generate video "a funny explainer for kids age 5"
      notebooklm generate video "professional presentation" --style classic
      notebooklm generate video --style kawaii
    """
    nb_id = require_notebook(notebook_id)
    format_map = {"explainer": VideoFormat.EXPLAINER, "brief": VideoFormat.BRIEF}
    style_map = {
        "auto": VideoStyle.AUTO_SELECT,
        "classic": VideoStyle.CLASSIC,
        "whiteboard": VideoStyle.WHITEBOARD,
        "kawaii": VideoStyle.KAWAII,
        "anime": VideoStyle.ANIME,
        "watercolor": VideoStyle.WATERCOLOR,
        "retro-print": VideoStyle.RETRO_PRINT,
        "heritage": VideoStyle.HERITAGE,
        "paper-craft": VideoStyle.PAPER_CRAFT,
    }

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            result = await client.artifacts.generate_video(
                nb_id,
                language=language,
                instructions=description or None,
                video_format=format_map[video_format],
                video_style=style_map[style],
            )
            await handle_generation_result(
                client, nb_id, result, "video", wait, json_output, timeout=600.0
            )

    return _run()


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
@click.option("--language", default="en")
@click.option(
    "--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)"
)
@with_client
def generate_slide_deck(
    ctx, description, notebook_id, deck_format, deck_length, language, wait, client_auth
):
    """Generate slide deck.

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
            result = await client.artifacts.generate_slide_deck(
                nb_id,
                language=language,
                instructions=description or None,
                slide_format=format_map[deck_format],
                slide_length=length_map[deck_length],
            )
            await handle_generation_result(
                client, nb_id, result, "slide deck", wait
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
@click.option(
    "--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard"
)
@click.option(
    "--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium"
)
@click.option(
    "--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)"
)
@with_client
def generate_quiz(ctx, description, notebook_id, quantity, difficulty, wait, client_auth):
    """Generate quiz.

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
            result = await client.artifacts.generate_quiz(
                nb_id,
                instructions=description or None,
                quantity=quantity_map[quantity],
                difficulty=difficulty_map[difficulty],
            )
            await handle_generation_result(client, nb_id, result, "quiz", wait)

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
@click.option(
    "--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard"
)
@click.option(
    "--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium"
)
@click.option(
    "--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)"
)
@with_client
def generate_flashcards(ctx, description, notebook_id, quantity, difficulty, wait, client_auth):
    """Generate flashcards.

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
            result = await client.artifacts.generate_flashcards(
                nb_id,
                instructions=description or None,
                quantity=quantity_map[quantity],
                difficulty=difficulty_map[difficulty],
            )
            await handle_generation_result(client, nb_id, result, "flashcards", wait)

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
@click.option("--language", default="en")
@click.option(
    "--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)"
)
@with_client
def generate_infographic(
    ctx, description, notebook_id, orientation, detail, language, wait, client_auth
):
    """Generate infographic.

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
            result = await client.artifacts.generate_infographic(
                nb_id,
                language=language,
                instructions=description or None,
                orientation=orientation_map[orientation],
                detail_level=detail_map[detail],
            )
            await handle_generation_result(client, nb_id, result, "infographic", wait)

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
@click.option("--language", default="en")
@click.option(
    "--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)"
)
@with_client
def generate_data_table(ctx, description, notebook_id, language, wait, client_auth):
    """Generate data table.

    \b
    Example:
      notebooklm generate data-table "comparison of key concepts"
      notebooklm generate data-table "timeline of events"
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            result = await client.artifacts.generate_data_table(
                nb_id, language=language, instructions=description
            )
            await handle_generation_result(client, nb_id, result, "data table", wait)

    return _run()


@generate.command("mind-map")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def generate_mind_map(ctx, notebook_id, client_auth):
    """Generate mind map."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            with console.status("Generating mind map..."):
                result = await client.artifacts.generate_mind_map(nb_id)

            if result:
                console.print("[green]Mind map generated:[/green]")
                if isinstance(result, dict):
                    console.print(f"  Note ID: {result.get('note_id', '-')}")
                    mind_map = result.get("mind_map", {})
                    if isinstance(mind_map, dict):
                        console.print(f"  Root: {mind_map.get('name', '-')}")
                        console.print(
                            f"  Children: {len(mind_map.get('children', []))} nodes"
                        )
                else:
                    console.print(result)
            else:
                console.print("[yellow]No result[/yellow]")

    return _run()


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
@click.option(
    "--wait/--no-wait", default=False, help="Wait for completion (default: no-wait)"
)
@with_client
def generate_report_cmd(ctx, description, report_format, notebook_id, wait, client_auth):
    """Generate a report (briefing doc, study guide, blog post, or custom).

    \b
    Examples:
      notebooklm generate report                              # briefing-doc (default)
      notebooklm generate report --format study-guide         # study guide
      notebooklm generate report --format blog-post           # blog post
      notebooklm generate report "Create a white paper..."    # custom report
      notebooklm generate report --format blog-post "Focus on key insights"
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
            result = await client.artifacts.generate_report(
                nb_id,
                report_format=report_format_enum,
                custom_prompt=custom_prompt,
            )
            await handle_generation_result(client, nb_id, result, format_display, wait)

    return _run()
