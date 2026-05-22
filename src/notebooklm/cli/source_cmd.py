"""Source management CLI commands — thin Click-handler layer (ADR-008).

Each command builds a ``cli/services/source_*`` plan dataclass and delegates
to its executor:

* ``services/source_listing.py``   — list
* ``services/source_mutations.py`` — delete, delete-by-title, rename,
  refresh, add-drive
* ``services/source_content.py``   — get, fulltext, guide, stale
* ``services/source_research.py``  — add-research
* ``services/source_wait.py``      — wait
* ``services/source_add.py``       — add  (pre-T5; T5 added the executor)
* ``services/source_clean.py``     — clean (pre-T5; T5 added the executor)

The full per-command listing lives in the ``source`` click group docstring
below (it is what ``notebooklm source --help`` shows).
"""

import asyncio  # noqa: F401 — re-exported for P1.T2 regression tests that patch source_cmd.asyncio.sleep
import os
from pathlib import Path

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import Source
from .auth_runtime import with_client
from .error_handler import _output_error, current_json_output
from .input import read_stdin_text, resolve_prompt
from .options import (
    json_option,
    list_options,
    notebook_option,
    prompt_file_option,
    wait_polling_options,
)
from .rendering import console
from .resolve import require_notebook, resolve_notebook_id, resolve_source_id
from .runtime import is_quiet
from .services import source_add as source_add_service
from .services import source_clean as source_clean_service
from .services.source_add import SourceAddExecutionPlan, execute_source_add
from .services.source_clean import SourceCleanPlan, execute_source_clean
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
    SourceRefreshPlan,
    SourceRenamePlan,
    execute_source_add_drive,
    execute_source_delete,
    execute_source_delete_by_title,
    execute_source_refresh,
    execute_source_rename,
)
from .services.source_research import SourceAddResearchPlan, execute_source_add_research
from .services.source_wait import SourceWaitPlan, execute_source_wait

# Compatibility wrappers — tests patch these names on this module. Each
# one is a one-liner forwarder to the canonical service-layer home.


def _looks_like_path(content: str) -> bool:
    """Compatibility wrapper for tests patching source-add path detection."""
    return source_add_service.looks_like_path(content)


def _validate_upload_path(content: str, follow_symlinks: bool) -> Path:
    """Compatibility wrapper for tests patching source-add upload validation."""
    try:
        return source_add_service.validate_upload_path(content, follow_symlinks)
    except source_add_service.SourceAddValidationError as exc:
        _output_error(f"Error: {exc}", "VALIDATION_ERROR", current_json_output(), 1)
        raise AssertionError("unreachable") from None  # pragma: no cover


def _classify_junk_sources(sources: list[Source]) -> list[tuple[str, str, str, str]]:
    """Compatibility wrapper for tests patching source-clean classification."""
    return source_clean_service.classify_junk_sources(sources)


def _print_clean_candidates(candidates: list[tuple[str, str, str, str]]) -> None:
    """Print a Rich table summarizing sources that will (or would) be deleted."""
    table = Table(title=f"{len(candidates)} source(s) flagged for cleanup")
    table.add_column("ID", style="dim", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Status")
    table.add_column("Reason")
    for sid, title, status, reason in candidates:
        display_title = title if title else "[dim](no title)[/dim]"
        table.add_row(sid[:8], display_title, status, reason)
    console.print(table)


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
@list_options
@with_client
def source_list(ctx, notebook_id, json_output, limit, no_truncate, client_auth):
    """List all sources in a notebook.

    \b
    Pagination & display:
      --limit N         Show at most N sources (default: unlimited).
      --no-truncate     Do not truncate the Title column in the table view.
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
            )
            await execute_source_list(client, plan)

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
# DEPRECATION-REMOVAL v0.6.0: see services/source_add.py for the rationale.
@click.option(
    "--mime-type",
    help=(
        "[Deprecated] MIME type for file sources — unused; the server "
        "derives MIME from the filename extension. Drive sources retain "
        "this option (see ``source add-drive``)."
    ),
)
@click.option(
    "--timeout",
    default=None,
    type=float,
    help=(
        "HTTP request timeout in seconds (default: 30, from the library). "
        "Increase when adding slow URLs or large files that exceed the default."
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
    # forces the text-source path. Intercepted BEFORE auto-detection so a
    # single dash never falls into the path-shaped warning.
    if content == "-":
        content = read_stdin_text(source_label="source content")
        source_type = "text"

    nb_id = require_notebook(notebook_id)
    plan = source_add_service.build_source_add_plan(
        content=content,
        source_type=source_type,
        title=title,
        mime_type=mime_type,
        follow_symlinks=follow_symlinks,
        suppress_file_mime_deprecation=os.environ.get("NOTEBOOKLM_QUIET_DEPRECATIONS") == "1",
        validate_path=_validate_upload_path,
        looks_path_shaped=_looks_like_path,
    )

    for warning in plan.warnings:
        click.echo(warning, err=True)

    client_kwargs: dict = {}
    if timeout is not None:
        client_kwargs["timeout"] = timeout

    async def _run():
        async with NotebookLMClient(client_auth, **client_kwargs) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            await execute_source_add(
                client,
                SourceAddExecutionPlan(
                    notebook_id=nb_id_resolved,
                    plan=plan,
                    json_output=json_output,
                ),
                ctx=ctx,
            )

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
            await execute_source_get(
                client,
                SourceGetPlan(
                    notebook_id=nb_id_resolved,
                    source_id=resolved_id,
                    json_output=json_output,
                ),
            )

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
            await execute_source_delete(
                client,
                SourceDeletePlan(
                    notebook_id=nb_id_resolved,
                    source_id=source_id,
                    yes=yes,
                    json_output=json_output,
                ),
                ctx=ctx,
            )

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
            await execute_source_delete_by_title(
                client,
                SourceDeleteByTitlePlan(
                    notebook_id=nb_id_resolved,
                    title=title,
                    yes=yes,
                    json_output=json_output,
                ),
                ctx=ctx,
            )

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
            await execute_source_rename(
                client,
                SourceRenamePlan(
                    notebook_id=nb_id_resolved,
                    source_id=source_id,
                    new_title=new_title,
                    json_output=json_output,
                ),
                ctx=ctx,
            )

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
            await execute_source_refresh(
                client,
                SourceRefreshPlan(
                    notebook_id=nb_id_resolved,
                    source_id=source_id,
                    json_output=json_output,
                ),
                ctx=ctx,
            )

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
            await execute_source_add_drive(
                client,
                SourceAddDrivePlan(
                    notebook_id=nb_id_resolved,
                    file_id=file_id,
                    title=title,
                    mime_type=mime_type,
                    json_output=json_output,
                ),
                ctx=ctx,
            )

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
    client_auth,
):
    """Search web or drive and add sources from results.

    See ``--from``, ``--mode``, ``--import-all``, ``--cited-only``,
    ``--no-wait``, and ``--timeout``. Read the query from a file with
    ``--prompt-file``.
    """
    query = resolve_prompt(query, prompt_file, "query", required=True)
    if cited_only and not import_all:
        raise click.UsageError("--cited-only requires --import-all")
    # P1.T2 bug 7: --no-wait + --import-all is silently broken — refuse it.
    if no_wait and import_all:
        raise click.UsageError(
            "--import-all requires --wait (the default) or a separate "
            "'research wait --import-all' after --no-wait."
        )

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            await execute_source_add_research(
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
                ),
            )

    return _run()


@source.command("fulltext")
@click.argument("source_id")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--output", "-o", type=click.Path(), help="Write content to file")
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["text", "markdown"]),
    default="text",
    help="Content format: text (default) or markdown",
)
@with_client
def source_fulltext(ctx, source_id, notebook_id, json_output, output, output_format, client_auth):
    """Get full content of a source.

    Use ``--format markdown`` for a rich version with headings/tables/links.
    Text mode truncates at 2000 chars; ``-o FILE`` writes the full content.
    JSON mode emits the full ``SourceFulltext`` payload, or with ``-o`` a
    ``{path, bytes, source_id, title}`` envelope (content goes to the file
    only, not duplicated to stdout).
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            await execute_source_fulltext(
                client,
                SourceFulltextPlan(
                    notebook_id=nb_id_resolved,
                    source_id=resolved_id,
                    json_output=json_output,
                    output=output,
                    output_format=output_format,
                ),
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
            await execute_source_guide(
                client,
                SourceGuidePlan(
                    notebook_id=nb_id_resolved,
                    source_id=resolved_id,
                    json_output=json_output,
                ),
            )

    return _run()


@source.command("stale")
@click.argument("source_id")
@notebook_option
@json_option
@with_client
def source_stale(ctx, source_id, notebook_id, json_output, client_auth):
    """Check if a URL/Drive source needs refresh.

    Exit 0 if stale (needs refresh), 1 if fresh — enables shell scripting
    ``if notebooklm source stale ID; then refresh; fi``. Inverted exit-code
    semantics are intentional and apply to ``--json`` too (see
    docs/cli-exit-codes.md). Branch on the JSON ``stale`` field when the
    predicate-style exit code is awkward.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            await execute_source_stale(
                client,
                SourceStalePlan(
                    notebook_id=nb_id_resolved,
                    source_id=resolved_id,
                    json_output=json_output,
                ),
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
            await execute_source_wait(
                client,
                SourceWaitPlan(
                    notebook_id=nb_id_resolved,
                    source_id=resolved_id,
                    timeout=float(timeout),
                    interval=float(interval),
                    json_output=json_output,
                ),
            )

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
            await execute_source_clean(
                client,
                SourceCleanPlan(
                    notebook_id=nb_id_resolved,
                    dry_run=dry_run,
                    yes=yes,
                    json_output=json_output,
                    quiet_mode=quiet_mode,
                ),
                ctx=ctx,
                classify_sources=_classify_junk_sources,
                on_candidates=_print_clean_candidates,
            )

    return _run()
