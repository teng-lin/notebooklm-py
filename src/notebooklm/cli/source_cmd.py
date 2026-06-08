"""Source management CLI commands — thin Click-handler layer (ADR-0008).

Each command builds a ``cli/services/source_*`` plan dataclass and delegates
to its executor:

* ``services/source_listing.py``   — list
* ``services/source_mutations.py`` — delete, delete-by-title, rename,
  refresh, add-drive
* ``services/source_content.py``   — data fetchers for get, fulltext, guide, stale
* ``services/source_research.py``  — add-research
* ``services/source_wait.py``      — wait
* ``services/source_add.py``       — add  (pre-T5; T5 added the executor)
* ``services/source_clean.py``     — clean (pure orchestration: classify +
  batched delete; rendering + exit codes live here in the command layer)

The full per-command listing lives in the ``source`` click group docstring
below (it is what ``notebooklm source --help`` shows).
"""

import asyncio  # noqa: F401 — re-exported for regression tests that patch source_cmd.asyncio.sleep
from typing import Any

import click

from ..client import NotebookLMClient
from ..exceptions import ValidationError
from ..types import Source

# Render/validation helpers live in ``_source_render``; re-exported here so the
# historical ``source_cmd.<helper>`` import/patch surface (and the retained command
# bodies below) keep resolving them. The F401 suppression marks every name an
# intentional re-export — three (``_available_output_path``,
# ``_exit_with_add_research_status``, ``_print_add_research_task_ids``) are used
# only by sibling helpers in ``_source_render`` and have no caller here.
from ._source_render import (  # noqa: F401
    _available_output_path,
    _classify_junk_sources,
    _emit_add_research_flag_conflict,
    _emit_source_fulltext_flag_conflict,
    _exit_with_add_research_status,
    _handle_source_mutation_error,
    _looks_like_path,
    _print_add_research_task_ids,
    _print_clean_candidates,
    _render_add_research_result,
    _render_source_add_drive_result,
    _render_source_delete_result,
    _render_source_fulltext_result,
    _render_source_get_result,
    _render_source_guide_result,
    _render_source_refresh_result,
    _render_source_rename_result,
    _render_source_stale_result,
    _render_source_wait_outcome,
    _resolve_source_fulltext_output_path,
    _validate_upload_path,
    source_add_payload,
)
from .auth_runtime import with_client
from .error_handler import _output_error, exit_with_code, output_error
from .input import read_stdin_text, resolve_prompt
from .options import (
    json_option,
    list_options,
    notebook_option,
    prompt_file_option,
    wait_polling_options,
)
from .polling_ui import status_with_elapsed
from .rendering import (
    cli_print,
    cli_status,
    console,
    get_source_type_display,
    json_output_response,
    render_list,
)
from .resolve import require_notebook, resolve_notebook_id, resolve_source_id
from .runtime import is_quiet
from .services import source_add as source_add_service
from .services.label_listing import LabelResolutionError
from .services.source_add import SourceAddExecutionPlan, execute_source_add
from .services.source_clean import (
    SourceCleanResult,
    candidates_payload,
    run_source_clean,
)
from .services.source_content import (
    SourceFulltextPlan,
    SourceGetPlan,
    SourceGuidePlan,
    SourceStalePlan,
    execute_source_fulltext,
    execute_source_get,
    execute_source_guide,
    execute_source_stale,
)
from .services.source_listing import SourceListPlan, execute_source_list
from .services.source_mutations import (
    SourceAddDrivePlan,
    SourceDeleteByTitlePlan,
    SourceDeletePlan,
    SourceMutationError,
    SourceRefreshPlan,
    SourceRenamePlan,
    execute_source_add_drive,
    execute_source_delete,
    execute_source_delete_by_title,
    execute_source_refresh,
    execute_source_rename,
    require_yes_in_json,
)
from .services.source_research import (
    SourceAddResearchPlan,
    execute_source_add_research,
    validate_add_research_flags,
)
from .services.source_wait import (
    SourceWaitPlan,
    execute_source_wait,
)


@click.group()
def source():
    """Source management commands.

    \b
    Commands:
      list             List sources in a notebook
      add              Add a source (url, text, file, youtube)
      add-drive        Add a Google Drive document
      add-research     Search web/drive and add sources from results
      get              Get source details
      fulltext         Get full indexed text content
      guide            Get AI-generated source summary and keywords
      stale            Check if source needs refresh
      wait             Wait for a source to finish processing
      clean            Remove duplicate, error, and access-blocked sources
      delete           Delete a source
      delete-by-title  Delete a source by exact title
      rename           Rename a source
      refresh          Refresh a URL/Drive source

    Partial ID Support: SOURCE_ID arguments support partial-prefix matching
    (e.g. 'abc' matches 'abc123def456...').
    """


@source.command("list")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option(
    "--label",
    "label_filter",
    default=None,
    help="Only list sources in this label (label id, partial prefix, or exact name).",
)
@list_options
@with_client
def source_list(ctx, notebook_id, json_output, label_filter, limit, no_truncate, client_auth):
    """List all sources in a notebook.

    \b
    Pagination & display:
      --limit N         Show at most N sources (default: unlimited).
      --no-truncate     Do not truncate the Title column in the table view.
      --label <id|name> Restrict the listing to a label's sources (read-only).
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            plan = SourceListPlan(
                notebook_id=nb_id_resolved,
                json_output=json_output,
                limit=limit,
                no_truncate=no_truncate,
                source_type_display=get_source_type_display,
                label_filter=label_filter,
            )
            try:
                render = await execute_source_list(client, plan)
            except LabelResolutionError as exc:
                output_error(
                    exc.message,
                    code=exc.code,
                    json_output=json_output,
                    exit_code=1,
                    extra=dict(exc.extra) if exc.extra else None,
                )
                raise AssertionError("unreachable") from None  # pragma: no cover
            render_list(render)

    return _run()


@source.command("add")
@click.argument("content")
@notebook_option
@click.option(
    "--type",
    "source_type",
    type=click.Choice(["url", "text", "file", "youtube"]),
    default=None,
    help="Source type (auto-detected if not specified)",
)
@click.option("--title", help="Custom title for text and uploaded-file sources")
@click.option(
    "--mime-type",
    help="MIME type for uploaded file sources. Overrides filename-extension inference.",
)
@click.option(
    # ``--request-timeout`` is the self-documenting canonical name (per-request
    # HTTP socket timeout, not a poll/wait budget); ``--timeout`` stays as a
    # back-compat alias. See the matching wiring on ``chat ask``.
    "--request-timeout",
    "--timeout",
    "timeout",
    default=None,
    type=float,
    help=(
        "HTTP request timeout in seconds (default: 30, from the library). "
        "Increase when adding slow URLs or large files that exceed the default. "
        "(--timeout is a back-compat alias.)"
    ),
)
@click.option(
    "--follow-symlinks",
    is_flag=True,
    default=False,
    help=(
        "Follow symbolic links when uploading a file. By default, symlinks "
        "are rejected so a workspace symlink cannot silently exfiltrate the "
        "file it points at (e.g. ~/Downloads/foo.pdf -> /etc/passwd)."
    ),
)
@click.option(
    "--allow-internal",
    is_flag=True,
    default=False,
    help=(
        "Allow URLs that point at internal hosts (``localhost``, "
        "``127.0.0.1``, private IP ranges, link-local). By default these "
        "are rejected to prevent the CLI from being used as an SSRF "
        "trampoline. Non-http(s) schemes (``file://``, ``ftp://``, ...) "
        "are rejected even with this flag."
    ),
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_add(
    ctx,
    content,
    notebook_id,
    source_type,
    title,
    mime_type,
    timeout,
    follow_symlinks,
    allow_internal,
    json_output,
    client_auth,
):
    """Add a source to a notebook.

    Source type is auto-detected (URL/file/youtube/text) — see ``--type`` to
    override. A path-shaped argument that does not exist on disk is still
    ingested as inline text but a stderr warning is emitted; pass
    ``--type text`` to suppress.
    """
    # Unix ``-`` convention: ``source add -`` reads inline text from stdin and
    # forces the text-source path. Explicit non-text types are rejected so
    # intent is not silently overwritten.
    if content == "-":
        if source_type not in {None, "text"}:
            message = (
                "Cannot use '-' (stdin) with --type "
                f"{source_type}; stdin content can only be added as text."
            )
            if json_output:
                _output_error(message, "VALIDATION_ERROR", json_output, 1)
                raise AssertionError("unreachable") from None  # pragma: no cover
            raise click.UsageError(  # cli-input-validation: stdin '-' incompatible with non-text --type
                message
            )
        content = read_stdin_text(source_label="source content")
        source_type = "text"

    nb_id = require_notebook(notebook_id)
    try:
        plan = source_add_service.build_source_add_plan(
            content=content,
            source_type=source_type,
            title=title,
            mime_type=mime_type,
            follow_symlinks=follow_symlinks,
            validate_path=_validate_upload_path,
            looks_path_shaped=_looks_like_path,
            allow_internal=allow_internal,
        )
    except source_add_service.SourceAddValidationError as exc:
        _output_error(f"Error: {exc}", "VALIDATION_ERROR", json_output, 1)
        raise AssertionError("unreachable") from None  # pragma: no cover

    for warning in plan.warnings:
        click.echo(warning, err=True)

    client_kwargs: dict = {"timeout": timeout} if timeout is not None else {}

    async def _run():
        async with NotebookLMClient(client_auth, **client_kwargs) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            execution_plan = SourceAddExecutionPlan(notebook_id=nb_id_resolved, plan=plan)
            if json_output:
                result = await execute_source_add(client, execution_plan)
                json_output_response(source_add_payload(result))
                return

            with cli_status(f"Adding {plan.detected_type} source...", ctx=ctx):
                result = await execute_source_add(client, execution_plan)
            cli_print(f"[green]Added source:[/green] {result.source.id}", ctx=ctx)

    return _run()


@source.command("get")
@click.argument("source_id")
@notebook_option
@json_option
@with_client
def source_get(ctx, source_id, notebook_id, json_output, client_auth):
    """Get source details."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            result = await execute_source_get(
                client,
                SourceGetPlan(
                    notebook_id=nb_id_resolved,
                    source_id=resolved_id,
                ),
            )
            _render_source_get_result(result, json_output=json_output)

    return _run()


@source.command("delete")
@click.argument("source_id")
@notebook_option
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def source_delete(ctx, source_id, notebook_id, yes, json_output, client_auth):
    """Delete a source."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            try:
                result = await execute_source_delete(
                    client,
                    SourceDeletePlan(
                        notebook_id=nb_id_resolved,
                        source_id=source_id,
                        yes=yes,
                        json_output=json_output,
                    ),
                    confirmer=click.confirm,
                )
            except SourceMutationError as exc:
                _handle_source_mutation_error(exc, json_output=json_output)
            _render_source_delete_result(result, json_output=json_output, ctx=ctx)

    return _run()


@source.command("delete-by-title")
@click.argument("title")
@notebook_option
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def source_delete_by_title(ctx, title, notebook_id, yes, json_output, client_auth):
    """Delete a source by exact title."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            try:
                result = await execute_source_delete_by_title(
                    client,
                    SourceDeleteByTitlePlan(
                        notebook_id=nb_id_resolved,
                        title=title,
                        yes=yes,
                        json_output=json_output,
                    ),
                    confirmer=click.confirm,
                )
            except SourceMutationError as exc:
                _handle_source_mutation_error(exc, json_output=json_output)
            _render_source_delete_result(result, json_output=json_output, ctx=ctx)

    return _run()


@source.command("rename")
@click.argument("source_id")
@click.argument("new_title")
@notebook_option
@json_option
@with_client
def source_rename(ctx, source_id, new_title, notebook_id, json_output, client_auth):
    """Rename a source."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            result = await execute_source_rename(
                client,
                SourceRenamePlan(
                    notebook_id=nb_id_resolved,
                    source_id=source_id,
                    new_title=new_title,
                    json_output=json_output,
                ),
            )
            _render_source_rename_result(result, json_output=json_output, ctx=ctx)

    return _run()


@source.command("refresh")
@click.argument("source_id")
@notebook_option
@json_option
@with_client
def source_refresh(ctx, source_id, notebook_id, json_output, client_auth):
    """Refresh a URL/Drive source."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            plan = SourceRefreshPlan(
                notebook_id=nb_id_resolved,
                source_id=source_id,
                json_output=json_output,
            )
            if json_output:
                result = await execute_source_refresh(client, plan)
            else:
                with cli_status("Refreshing source...", ctx=ctx):
                    result = await execute_source_refresh(client, plan)
            _render_source_refresh_result(result, json_output=json_output, ctx=ctx)

    return _run()


@source.command("add-drive")
@click.argument("file_id")
@click.argument("title")
@notebook_option
@click.option(
    "--mime-type",
    type=click.Choice(["google-doc", "google-slides", "google-sheets", "pdf"]),
    default="google-doc",
    help="Document type (default: google-doc)",
)
@json_option
@with_client
def source_add_drive(ctx, file_id, title, notebook_id, mime_type, json_output, client_auth):
    """Add a Google Drive document as a source."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            plan = SourceAddDrivePlan(
                notebook_id=nb_id_resolved,
                file_id=file_id,
                title=title,
                mime_type=mime_type,
            )
            if json_output:
                result = await execute_source_add_drive(client, plan)
            else:
                with cli_status("Adding Drive source...", ctx=ctx):
                    result = await execute_source_add_drive(client, plan)
            _render_source_add_drive_result(result, json_output=json_output, ctx=ctx)

    return _run()


@source.command("add-research")
@click.argument("query", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--from",
    "search_source",
    type=click.Choice(["web", "drive"]),
    default="web",
    help="Search source (default: web)",
)
@click.option(
    "--mode",
    type=click.Choice(["fast", "deep"]),
    default="fast",
    help="Search mode (default: fast)",
)
@click.option("--import-all", is_flag=True, help="Import all found sources")
@click.option("--cited-only", is_flag=True, help="With --import-all, import only cited sources")
@click.option(
    "--no-wait",
    is_flag=True,
    help="Start research and return immediately (use 'research status/wait' to monitor)",
)
@click.option(
    "--timeout",
    default=1800,
    type=int,
    help=(
        "Per-phase seconds budget for (a) the research-completion poll "
        "loop and (b) the --import-all retry loop (default: 1800). Each "
        "phase gets the full budget independently, so worst-case total "
        "wall time is up to 2× this value. Matches 'research wait "
        "--timeout' semantics. Bumping this is required for deep "
        "research that runs longer than the legacy 5-minute cap — "
        "otherwise the CLI gives up before IMPORT_RESEARCH fires and "
        "the NotebookLM web UI is left showing an 'Add sources?' modal."
    ),
)
@json_option
@with_client
def source_add_research(
    ctx,
    query,
    prompt_file,
    notebook_id,
    search_source,
    mode,
    import_all,
    cited_only,
    no_wait,
    timeout,
    json_output,
    client_auth,
):
    """Search web or drive and add sources from results.

    See ``--from``, ``--mode``, ``--import-all``, ``--cited-only``,
    ``--no-wait``, and ``--timeout``. Read the query from a file with
    ``--prompt-file``.
    """
    query = resolve_prompt(query, prompt_file, "query", required=True)
    # Flag-combination rules live in the neutral ``_app`` core as a pure
    # ``validate_*`` raising ``ValidationError``; the command maps that to the
    # CLI conflict contract (ADR-0015 §2): under --json route through the typed
    # envelope, otherwise preserve Click's parser-style ``UsageError`` (exit 2
    # with usage text) so interactive callers still see the canonical prose.
    try:
        validate_add_research_flags(import_all=import_all, cited_only=cited_only, no_wait=no_wait)
    except ValidationError as exc:
        _emit_add_research_flag_conflict(str(exc), json_output=json_output)

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            if not json_output:
                console.print(f"[yellow]Starting {mode} research on {search_source}...[/yellow]")
            result = await execute_source_add_research(
                client,
                SourceAddResearchPlan(
                    notebook_id=nb_id_resolved,
                    query=query,
                    search_source=search_source,
                    mode=mode,
                    import_all=import_all,
                    cited_only=cited_only,
                    no_wait=no_wait,
                    timeout=timeout,
                    json_output=json_output,
                ),
            )
            _render_add_research_result(result, json_output=json_output)

    return _run()


@source.command("fulltext")
@click.argument("source_id")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--output", "-o", type=click.Path(), help="Write content to file")
@click.option("--no-clobber", is_flag=True, help="Fail if the output file exists")
@click.option("--force", is_flag=True, help="Overwrite an existing output file")
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["text", "markdown"]),
    default="text",
    help="Content format: text (default) or markdown",
)
@with_client
def source_fulltext(
    ctx,
    source_id,
    notebook_id,
    json_output,
    output,
    no_clobber,
    force,
    output_format,
    client_auth,
):
    """Get full content of a source.

    Use ``--format markdown`` for a rich version with headings/tables/links.
    Text mode truncates at 2000 chars; ``-o FILE`` writes the full content.
    Existing output files are auto-renamed unless ``--force`` or
    ``--no-clobber`` is supplied.
    JSON mode emits the full ``SourceFulltext`` payload, or with ``-o`` a
    ``{path, bytes, source_id, title, kind}`` envelope (content goes to the file
    only, not duplicated to stdout).
    """
    nb_id = require_notebook(notebook_id)
    if force and no_clobber:
        _emit_source_fulltext_flag_conflict(
            "Cannot specify both --force and --no-clobber",
            json_output=json_output,
        )
    output_path = (
        _resolve_source_fulltext_output_path(
            output,
            force=force,
            no_clobber=no_clobber,
            json_output=json_output,
        )
        if output
        else None
    )

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            plan = SourceFulltextPlan(
                notebook_id=nb_id_resolved,
                source_id=resolved_id,
                output_format=output_format,
            )
            if json_output:
                result = await execute_source_fulltext(client, plan)
            else:
                with console.status("Fetching fulltext content..."):
                    result = await execute_source_fulltext(client, plan)
            _render_source_fulltext_result(
                result,
                json_output=json_output,
                output=output_path,
            )

    return _run()


@source.command("guide")
@click.argument("source_id")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_guide(ctx, source_id, notebook_id, json_output, client_auth):
    """Get AI-generated source summary and keywords.

    Shows the "Source Guide" — an AI-generated overview with a summary,
    highlighted keywords, and topic tags.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            plan = SourceGuidePlan(
                notebook_id=nb_id_resolved,
                source_id=resolved_id,
            )
            if json_output:
                result = await execute_source_guide(client, plan)
            else:
                with console.status("Generating source guide..."):
                    result = await execute_source_guide(client, plan)
            _render_source_guide_result(result, json_output=json_output)

    return _run()


@source.command("stale")
@click.argument("source_id")
@notebook_option
@click.option(
    "--exit-on-stale",
    is_flag=True,
    default=False,
    help=(
        "Use inverted predicate exit codes (0=stale, 1=fresh) for "
        "back-compat with ``if notebooklm source stale ID; then refresh; fi`` "
        "shell idioms. By default the command follows the standard CLI "
        "convention (0=success, 1=error); branch on the JSON ``stale`` "
        "field for the freshness result."
    ),
)
@json_option
@with_client
def source_stale(ctx, source_id, notebook_id, exit_on_stale, json_output, client_auth):
    """Check if a URL/Drive source needs refresh.

    Default exit codes follow the standard CLI convention: ``0`` when the
    freshness check completes (regardless of the result), ``1`` on error
    (validation, auth, network, not-found, etc.). Branch on the JSON
    ``stale`` field (or stdout text) to decide whether to refresh.

    Pass ``--exit-on-stale`` to opt into the back-compat inverted-predicate
    semantics — exit ``0`` if stale, ``1`` if fresh — so the shell idiom
    ``if notebooklm source stale --exit-on-stale ID; then refresh; fi``
    keeps working for scripts written against the prior default. See
    ``docs/cli-exit-codes.md`` for the full rationale.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            result = await execute_source_stale(
                client,
                SourceStalePlan(
                    notebook_id=nb_id_resolved,
                    source_id=resolved_id,
                ),
            )
            _render_source_stale_result(
                result, json_output=json_output, exit_on_stale=exit_on_stale
            )

    return _run()


@source.command("wait")
@click.argument("source_id")
@notebook_option
@wait_polling_options(default_timeout=120, default_interval=1)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_wait(ctx, source_id, notebook_id, timeout, interval, json_output, client_auth):
    """Wait for a source to finish processing.

    Polls until the source is ready or fails. Exit code 0=ready, 1=missing or
    processing failed, 2=timeout. Spawn this in a subagent after ``source
    add`` returns so the main conversation can continue.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            outcome = await execute_source_wait(
                client,
                SourceWaitPlan(
                    notebook_id=nb_id_resolved,
                    source_id=resolved_id,
                    timeout=float(timeout),
                    interval=float(interval),
                ),
                wait_context=lambda: status_with_elapsed(
                    f"Waiting for source {resolved_id} to finish processing...",
                    json_output=json_output,
                    # Parallel hint: ``source wait`` has no separate ``source
                    # poll`` command, so the resume IS re-running the same wait.
                    resume_hint=f"notebooklm source wait {resolved_id}",
                ),
            )
            _render_source_wait_outcome(outcome, json_output=json_output)

    return _run()


@source.command("clean")
@notebook_option
@click.option(
    "--dry-run", is_flag=True, help="Show what would be deleted without actually deleting"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def source_clean(ctx, notebook_id, dry_run, yes, json_output, client_auth):
    """Automatically remove duplicate, error, and access-blocked sources."""
    nb_id = require_notebook(notebook_id)
    quiet_mode = is_quiet(ctx)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)

            async def _list_sources(notebook_id_inner: str) -> list[Source]:
                if json_output:
                    return await client.sources.list(notebook_id_inner)
                with cli_status("Fetching sources for cleanup...", ctx=ctx):
                    return await client.sources.list(notebook_id_inner)

            # In --json mode, never prompt — automation cannot
            # answer the question. Pass a non-interactive ``confirm_delete``
            # that always declines; once the service returns ``cancelled`` we
            # synthesize a structured ``CONFIRM_REQUIRED`` error below.
            confirm_delete = (
                (lambda count: False)
                if json_output
                else (lambda count: click.confirm(f"Delete {count} source(s)?"))
            )

            suppress_status = json_output or quiet_mode
            cb_candidates = None if suppress_status else _print_clean_candidates
            cb_delete_start = (
                None
                if suppress_status
                else lambda count: cli_print(
                    f"[dim]Cleaning {count} source(s) (in chunks of 10)...[/dim]",
                    ctx=ctx,
                )
            )

            result: SourceCleanResult = await run_source_clean(
                notebook_id=nb_id_resolved,
                dry_run=dry_run,
                yes=yes,
                list_sources=_list_sources,
                delete_source=client.sources.delete,
                confirm_delete=confirm_delete,
                on_candidates=cb_candidates,
                on_delete_start=cb_delete_start,
                classify_sources=_classify_junk_sources,
            )

            _dispatch_source_clean_result(result, json_output=json_output, yes=yes, ctx=ctx)

    return _run()


def _dispatch_source_clean_result(
    result: SourceCleanResult,
    *,
    json_output: bool,
    yes: bool,
    ctx: click.Context | None,
) -> None:
    """Render the source-clean outcome and exit per the result's status.

    Owns the Click-side rendering + exit-code policy, kept separate from
    ``execute_source_clean`` in :mod:`.services.source_clean`.
    Keeping presentation here lets the service module stay free of
    ``click`` / ``..rendering`` / ``..error_handler`` imports.
    """
    candidate_payload = candidates_payload(result.candidates)

    if json_output:
        # Synthesize structured error when --json + no --yes
        # left candidates uncleaned. ``require_yes_in_json`` raises a typed
        # source-mutation error for the command layer — it never returns.
        if result.status == "cancelled" and not yes:
            try:
                require_yes_in_json(
                    action="clean",
                    extra={
                        "notebook_id": result.notebook_id,
                        "candidate_count": result.candidate_count,
                        "candidates": candidate_payload,
                    },
                )
            except SourceMutationError as exc:
                _handle_source_mutation_error(exc, json_output=json_output)

        payload: dict[str, Any] = {
            "action": "clean",
            "notebook_id": result.notebook_id,
            "status": result.status,
            "candidates": candidate_payload,
            "deleted_count": result.deleted_count,
            "failure_count": result.failure_count,
        }
        if result.status != "already_clean":
            payload["candidate_count"] = result.candidate_count
        if result.status == "completed":
            payload["failures"] = [{"id": sid, "error": err} for sid, err in result.failures]
        json_output_response(payload)
        # Partial-failure exits non-zero so shell automation
        # (set -e, CI) sees the failure.
        if result.failures:
            exit_with_code(1)
        return

    if result.status == "already_clean":
        cli_print(
            "[green]Notebook is already clean. No junk sources found.[/green]",
            ctx=ctx,
        )
        return

    if result.status == "dry_run":
        cli_print(
            f"[yellow]Dry run: would delete {result.candidate_count} source(s).[/yellow]",
            ctx=ctx,
        )
        return

    if result.status == "cancelled":
        return

    if result.failures:
        # Failure summary is an error diagnostic, so it must remain visible
        # under root ``--quiet`` (policy: errors are never silenced; see
        # ``cli/rendering.py``). Use ``console.print`` directly here instead
        # of ``cli_print`` so the diagnostic is not swallowed when the user
        # passes ``--quiet``.
        console.print(
            f"[yellow]Cleaned {result.deleted_count} source(s). "
            f"{len(result.failures)} deletion(s) failed.[/yellow]",
        )
        for sid, err in result.failures[:5]:
            console.print(f"  [red]{sid}:[/red] {err}")
        if len(result.failures) > 5:
            console.print(
                f"  [dim]...and {len(result.failures) - 5} more[/dim]",
            )
        # Text-mode parity with JSON-mode exit code.
        exit_with_code(1)
        return

    cli_print(
        f"[green]Successfully cleaned {result.deleted_count} source(s).[/green]",
        ctx=ctx,
    )
