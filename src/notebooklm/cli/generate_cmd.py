"""Generate content CLI commands — thin Click handlers (P3.T1 ADR-008).

All validation, enum mapping, retry/wait orchestration, and output
dispatch live in ``cli/services/generate.py``. Tests patch
``NotebookLMClient`` / ``console`` / ``json_error_response`` /
``json_output_response`` / ``get_language`` / ``_output_mind_map_result``
as module-level attributes here, so those names remain imported at
module scope and ``_output_mind_map_result`` + ``resolve_language``
remain defined inline rather than re-exported.
"""

import os
from typing import Any

import click
from click.core import ParameterSource

from ..client import NotebookLMClient
from .auth_runtime import with_client
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
from .resolve import require_notebook
from .services.artifact_generation import (
    GenerationOutcome,
)
from .services.generate import (
    _INFOGRAPHIC_STYLE_MAP,
    GenerationExecutionResult,
    GenerationPlanValidationError,
    build_generation_plan,
    execute_generation,
)

DEFAULT_LANGUAGE = "en"


def resolve_language(language: str | None) -> str:
    """Resolve language from CLI flag, NOTEBOOKLM_HL env, config, or default.

    Priority: ``--language`` flag > ``NOTEBOOKLM_HL`` env var > config file
    > "en" default. Uses explicit None checks to avoid treating empty
    string as falsy. Validates each candidate against the supported list.

    Invalid codes route through :func:`output_error` per ADR-015: under
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


def _output_generation_outcome(outcome: GenerationOutcome, json_output: bool) -> None:
    """Render a generation outcome and apply command-layer exit policy."""
    if outcome.status in {"failed", "rate_limited"}:
        message = outcome.error or f"{outcome.artifact_type.title()} generation failed"
        if outcome.hint is None:
            output_error(message, outcome.error_code, json_output, outcome.exit_code)
        else:
            output_error(
                message,
                outcome.error_code,
                json_output,
                outcome.exit_code,
                hint=outcome.hint,
            )
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
            console.print(f"[green]{outcome.artifact_type.title()} ready:[/green] {outcome.url}")
        else:
            console.print(f"[green]{outcome.artifact_type.title()} ready[/green]")
    else:
        console.print(f"[yellow]Started:[/yellow] {outcome.task_id or outcome.raw_status}")


def _render_generation_result(result: GenerationExecutionResult, json_output: bool) -> None:
    if result.kind == "mind-map":
        _output_mind_map_result(result.mind_map, json_output)
        return
    if result.generation is None:
        output_error(
            f"{result.display_name.title()} generation failed",
            "GENERATION_FAILED",
            json_output,
            1,
        )
        raise AssertionError("unreachable")  # pragma: no cover
    _output_generation_outcome(result.generation, json_output)


# Click-handler params that are not part of the service-layer raw_args
# contract. ``ctx`` carries the parameter-source probe; ``client_auth`` is
# the AuthTokens injected by ``@with_client``; ``prompt_file`` has already
# been merged into ``description`` via ``resolve_prompt`` by the time the
# handler calls ``_run_generate``.
_NON_RAW_ARG_KEYS = frozenset({"ctx", "client_auth", "prompt_file"})


def _run_generate(*, kind: str, **handler_locals: Any) -> Any:
    """Bridge a Click handler invocation into the service-layer pipeline.

    Each handler calls ``_run_generate(kind="...", **locals())`` after
    resolving its description prompt. This shim filters out the
    handler-only keys (``ctx`` / ``client_auth`` / ``prompt_file``),
    runs ``require_notebook``, builds the plan (Click-time validation
    runs here, so UsageErrors surface synchronously), then opens the
    client and dispatches the async executor. Returns the coroutine the
    ``@with_client`` decorator will run via ``asyncio.run``.
    """
    ctx = handler_locals["ctx"]
    client_auth = handler_locals["client_auth"]
    raw_args = {k: v for k, v in handler_locals.items() if k not in _NON_RAW_ARG_KEYS}
    raw_args["notebook_id"] = require_notebook(raw_args["notebook_id"])
    try:
        plan = build_generation_plan(
            kind,
            raw_args,
            parameter_explicit=lambda name: (
                ctx.get_parameter_source(name) == ParameterSource.COMMANDLINE
            ),
            language_resolver=resolve_language,
        )
    except GenerationPlanValidationError as exc:
        output_error(exc.message, exc.code, raw_args["json_output"], 1)
        raise AssertionError("unreachable") from exc  # pragma: no cover

    if not plan.json_output:
        for line in plan.warnings:
            click.echo(line, err=True)

    async def _run() -> Any:
        async with NotebookLMClient(client_auth) as client:
            result = await execute_generation(
                plan,
                client,
                retry_sink=(
                    None
                    if plan.json_output
                    else lambda event: console.print(
                        f"[yellow]{plan.display_name.title()} rate limited. "
                        f"Retrying in {int(event.delay)}s "
                        f"(attempt {event.next_attempt_number}/{event.total_attempts})...[/yellow]"
                    )
                ),
                wait_context=lambda message, resume_hint: status_with_elapsed(
                    message,
                    json_output=plan.json_output,
                    resume_hint=resume_hint,
                ),
                wait_start_sink=(
                    None
                    if plan.json_output
                    else lambda task_id: console.print(
                        f"[yellow]Generating {plan.display_name}...[/yellow] Task: {task_id}"
                    )
                ),
                mind_map_context=lambda: status_with_elapsed(
                    "Generating mind map...",
                    json_output=plan.json_output,
                ),
            )
            _render_generation_result(result, plan.json_output)

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
)
@click.option(
    "--length",
    "audio_length",
    type=click.Choice(["short", "default", "long"]),
    default="default",
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
    return _run_generate(kind="audio", **locals())


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
    # ctx.info_name distinguishes the `cinematic-video` alias (which shares
    # this callback) from the canonical `video` command. The alias kind
    # enforces `--format cinematic` and the longer Veo-3 timeout default;
    # see services/generate.py _build_cinematic_video_plan.
    kind = "cinematic-video" if ctx.info_name == "cinematic-video" else "video"
    return _run_generate(kind=kind, **{k: v for k, v in locals().items() if k != "kind"})


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
)
@click.option(
    "--length",
    "deck_length",
    type=click.Choice(["default", "short"]),
    default="default",
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
    return _run_generate(kind="slide-deck", **locals())


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
    return _run_generate(kind="revise-slide", **locals())


@generate.command("quiz")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option("--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
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
    return _run_generate(kind="quiz", **locals())


@generate.command("flashcards")
@click.argument("description", default="", required=False)
@prompt_file_option
@notebook_option
@click.option("--quantity", type=click.Choice(["fewer", "standard", "more"]), default="standard")
@click.option("--difficulty", type=click.Choice(["easy", "medium", "hard"]), default="medium")
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
    return _run_generate(kind="flashcards", **locals())


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
    return _run_generate(kind="infographic", **locals())


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
    return _run_generate(kind="data-table", **locals())


@generate.command("mind-map")
@notebook_option
@multi_source_option
@language_option
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
    return _run_generate(kind="mind-map", **locals())


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
    return _run_generate(kind="report", **locals())
