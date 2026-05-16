"""Artifact management CLI commands.

Commands:
    list        List all artifacts
    get         Get artifact details
    rename      Rename an artifact
    delete      Delete an artifact
    export      Export to Google Docs/Sheets
    poll        Poll generation status (single check)
    wait        Wait for generation to complete (blocking)
    suggestions Get AI-suggested report topics
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Literal

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import ExportType
from .error_handler import _output_error, emit_cancelled_and_exit
from .helpers import (
    cli_name_to_artifact_type,
    console,
    get_artifact_type_display,
    json_output_response,
    require_notebook,
    resolve_artifact_id,
    resolve_notebook_id,
    with_client,
)
from .options import json_option, list_options, notebook_option, wait_polling_options


@contextlib.asynccontextmanager
async def _status_with_elapsed(
    message: str,
    *,
    json_output: bool = False,
    resume_hint: str | None = None,
) -> AsyncIterator[None]:
    """Show a Rich spinner with a periodically updated elapsed timer.

    Used by ``artifact wait`` so interactive callers see live feedback during
    the blocking poll. No-op (for the spinner) when
    ``json_output`` is True so stdout stays pure JSON for automation. The
    spinner is transient — it disappears on exit, leaving only the final
    completion / failure line.

    The ticker task updates the status text once per second while the wrapped
    coroutine awaits the long-running call. Cancellation is best-effort: if
    the wrapped block raises, the ticker is cancelled in ``finally`` and the
    exception propagates unchanged.

    SIGINT handling: when ``resume_hint`` is provided, a
    ``KeyboardInterrupt`` raised inside the wrapped block is caught and
    converted into a friendly cancellation message via
    :func:`emit_cancelled_and_exit`, which prints
    ``Cancelled. Resume with: <resume_hint>`` to stderr (or a structured
    ``CANCELLED`` envelope under ``--json``) and exits 130.
    """

    @contextlib.contextmanager
    def _sigint_guard():
        try:
            yield
        except KeyboardInterrupt:
            if resume_hint is None:
                raise
            emit_cancelled_and_exit(resume_hint, json_output=json_output)

    if json_output:
        with _sigint_guard():
            yield
        return
    start = time.monotonic()
    with console.status(message) as status:

        async def _ticker() -> None:
            while True:
                await asyncio.sleep(1.0)
                elapsed = int(time.monotonic() - start)
                status.update(f"{message} [{elapsed}s elapsed]")

        ticker_task = asyncio.create_task(_ticker())
        try:
            with _sigint_guard():
                yield
        finally:
            ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker_task


@click.group()
def artifact():
    """Artifact management commands.

    \b
    Commands:
      list         List all artifacts (or by type)
      get          Get artifact details
      rename       Rename an artifact
      delete       Delete an artifact
      export       Export to Google Docs/Sheets
      poll         Poll generation status (single check)
      wait         Wait for generation to complete (blocking)
      suggestions  Get AI-suggested report topics

    \b
    Partial ID Support:
      ARTIFACT_ID arguments support partial matching. Instead of typing the full
      UUID, you can use a prefix (e.g., 'abc' matches 'abc123def456...').
    """
    pass


@artifact.command("list")
@notebook_option
@click.option(
    "--type",
    "artifact_type",
    type=click.Choice(
        [
            "all",
            "audio",
            "video",
            "slide-deck",
            "quiz",
            "flashcard",
            "infographic",
            "data-table",
            "mind-map",
            "report",
        ]
    ),
    default="all",
    help="Filter by type",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@list_options
@with_client
def artifact_list(ctx, notebook_id, artifact_type, json_output, limit, no_truncate, client_auth):
    """List artifacts in a notebook.

    \b
    Pagination & display:
      --limit N         Show at most N artifacts (default: unlimited).
      --no-truncate     Do not truncate the Title column in the table view.
    """
    nb_id = require_notebook(notebook_id)
    type_filter = cli_name_to_artifact_type(artifact_type)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            # artifacts.list() already includes mind maps from notes system
            artifacts = await client.artifacts.list(nb_id_resolved, artifact_type=type_filter)
            # Client-side offset slicing.
            if limit is not None and limit >= 0:
                artifacts = artifacts[:limit]

            if json_output:
                nb = await client.notebooks.get(nb_id_resolved)
                data = {
                    "notebook_id": nb_id_resolved,
                    "notebook_title": nb.title if nb else None,
                    "artifacts": [
                        {
                            "index": i,
                            "id": art.id,
                            "title": art.title,
                            "type": get_artifact_type_display(art).split(" ", 1)[-1],
                            "type_id": art.kind.value,
                            "status": art.status_str,
                            "status_id": art.status,
                            "created_at": art.created_at.isoformat() if art.created_at else None,
                        }
                        for i, art in enumerate(artifacts, 1)
                    ],
                    "count": len(artifacts),
                }
                json_output_response(data)
                return

            if not artifacts:
                console.print(f"[yellow]No {artifact_type} artifacts found[/yellow]")
                return

            table = Table(title=f"Artifacts in {nb_id_resolved}")
            table.add_column("ID", style="cyan")
            title_overflow: Literal["fold", "ellipsis"] = "fold" if no_truncate else "ellipsis"
            table.add_column("Title", style="green", overflow=title_overflow)
            table.add_column("Type")
            table.add_column("Created", style="dim")
            table.add_column("Status", style="yellow")

            for art in artifacts:
                type_display = get_artifact_type_display(art)
                created = art.created_at.strftime("%Y-%m-%d %H:%M") if art.created_at else "-"
                table.add_row(art.id, art.title, type_display, created, art.status_str)

            console.print(table)

    return _run()


@artifact.command("get")
@click.argument("artifact_id")
@notebook_option
@json_option
@with_client
def artifact_get(ctx, artifact_id, notebook_id, json_output, client_auth):
    """Get artifact details.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )
            art = await client.artifacts.get(nb_id_resolved, resolved_id)

            # BREAKING: not-found exits 1 with a typed error instead of the
            # previous exit-0 ``found: false`` placeholder. See the matching
            # change in ``cli/source.py::source_get`` and the BREAKING entry
            # in ``CHANGELOG.md`` (Unreleased → Changed).
            #
            # The trailing ``raise AssertionError`` is unreachable at runtime
            # (``_output_error`` always raises) — it exists solely to narrow
            # ``art`` from ``Artifact | None`` to ``Artifact`` for mypy without
            # forcing a ``NoReturn`` annotation onto
            # ``error_handler._output_error`` (which would touch a module the
            # C1 spec says we must not).
            if art is None:
                _output_error(
                    "Artifact not found",
                    code="NOT_FOUND",
                    json_output=json_output,
                    exit_code=1,
                    extra={"id": resolved_id, "notebook_id": nb_id_resolved},
                )
                raise AssertionError("unreachable")  # pragma: no cover

            if json_output:
                data = {
                    "notebook_id": nb_id_resolved,
                    "id": art.id,
                    "title": art.title,
                    "type": get_artifact_type_display(art).split(" ", 1)[-1],
                    "type_id": art.kind.value,
                    "status": art.status_str,
                    "status_id": art.status,
                    "created_at": art.created_at.isoformat() if art.created_at else None,
                    "found": True,
                }
                json_output_response(data)
                return

            console.print(f"[bold cyan]Artifact:[/bold cyan] {art.id}")
            console.print(f"[bold]Title:[/bold] {art.title}")
            console.print(f"[bold]Type:[/bold] {get_artifact_type_display(art)}")
            console.print(f"[bold]Status:[/bold] {art.status_str}")
            if art.created_at:
                console.print(f"[bold]Created:[/bold] {art.created_at.strftime('%Y-%m-%d %H:%M')}")

    return _run()


@artifact.command("rename")
@click.argument("artifact_id")
@click.argument("new_title")
@notebook_option
@json_option
@with_client
def artifact_rename(ctx, artifact_id, new_title, notebook_id, json_output, client_auth):
    """Rename an artifact.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )

            # Check if this is a mind map (stored with notes, not artifacts)
            mind_maps = await client.notes.list_mind_maps(nb_id_resolved)
            for mm in mind_maps:
                if mm[0] == resolved_id:
                    raise click.ClickException("Mind maps cannot be renamed")

            await client.artifacts.rename(nb_id_resolved, resolved_id, new_title)
            # The rename API returns None; if no exception was raised, the operation succeeded.
            # We display the requested new_title as confirmation.
            if json_output:
                json_output_response({"id": resolved_id, "renamed": True, "new_title": new_title})
            else:
                console.print(f"[green]Renamed artifact:[/green] {resolved_id}")
                console.print(f"[bold]New title:[/bold] {new_title}")

    return _run()


@artifact.command("delete")
@click.argument("artifact_id")
@notebook_option
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def artifact_delete(ctx, artifact_id, notebook_id, yes, json_output, client_auth):
    """Delete an artifact.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )

            # ``--json`` implies ``--yes`` so a script that pipes ``--json`` but
            # forgets ``-y`` does not hang on ``click.confirm``'s stdin read
            # (which would also clobber JSON stdout purity). Interactive users
            # who want a confirm prompt should omit ``--json``; scripts that
            # explicitly want the confirm round-trip can omit ``--json``.
            if not yes and not json_output and not click.confirm(f"Delete artifact {resolved_id}?"):
                return

            # Check if this is a mind map (stored with notes)
            mind_maps = await client.notes.list_mind_maps(nb_id_resolved)
            for mm in mind_maps:
                if mm[0] == resolved_id:
                    await client.notes.delete(nb_id_resolved, resolved_id)
                    if json_output:
                        json_output_response(
                            {
                                "id": resolved_id,
                                "deleted": True,
                                "kind": "mind_map",
                                "note": (
                                    "Mind maps are cleared, not removed. "
                                    "Google may garbage collect them later."
                                ),
                            }
                        )
                    else:
                        console.print(f"[yellow]Cleared mind map:[/yellow] {resolved_id}")
                        console.print(
                            "[dim]Note: Mind maps are cleared, not removed. Google may garbage collect them later.[/dim]"
                        )
                    return

            await client.artifacts.delete(nb_id_resolved, resolved_id)
            if json_output:
                json_output_response({"id": resolved_id, "deleted": True})
            else:
                console.print(f"[green]Deleted artifact:[/green] {resolved_id}")

    return _run()


@artifact.command("export")
@click.argument("artifact_id")
@notebook_option
@click.option("--title", required=True, help="Title for exported document")
@click.option("--type", "export_type", type=click.Choice(["docs", "sheets"]), default="docs")
@json_option
@with_client
def artifact_export(ctx, artifact_id, notebook_id, title, export_type, json_output, client_auth):
    """Export artifact to Google Docs/Sheets.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )
            # Convert export_type string to ExportType enum
            export_type_enum = ExportType.SHEETS if export_type == "sheets" else ExportType.DOCS
            # Pass None for content - backend retrieves content from artifact_id
            result = await client.artifacts.export(
                nb_id_resolved, resolved_id, None, title, export_type_enum
            )

            if json_output:
                json_output_response(
                    {
                        "id": resolved_id,
                        "exported": bool(result),
                        "export_type": export_type,
                        "title": title,
                        "result": result,
                    }
                )
                return

            if result:
                console.print(f"[green]Exported to Google {export_type.title()}[/green]")
                console.print(result)
            else:
                console.print("[yellow]Export may have failed[/yellow]")

    return _run()


@artifact.command("poll")
@click.argument("task_id")
@notebook_option
@json_option
@with_client
def artifact_poll(ctx, task_id, notebook_id, json_output, client_auth):
    """Single non-blocking generation status check.

    \b
    TASK_ID is the identifier returned by `notebooklm generate <type>` (it
    appears in the `task_id` field of the JSON payload, or after `Started:`
    in the human-readable output). Pass it through unchanged — `poll` does
    NOT prefix-match against `artifact list`, so a freshly-issued task_id
    works even before the artifact appears in the list.

    \b
    Note: this is the same identifier `wait` accepts. The API uses one ID
    that serves as both the generation task_id (during creation) and the
    artifact_id (once listed); the difference is operational, not semantic:
      - `poll`: one-shot check, accepts the raw task_id from `generate`.
      - `wait`: blocks until terminal, prefix-matches against `artifact list`.

    \b
    Examples:
      # Right after `generate audio` returns task_id "abc123def...":
      notebooklm artifact poll abc123def
      # JSON output for scripting:
      notebooklm artifact poll abc123def --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            status = await client.artifacts.poll_status(nb_id_resolved, task_id)

            if json_output:
                # Mirror the GenerationStatus dataclass fields so automation can
                # introspect status / url / error without parsing prose. Direct
                # attribute access (rather than getattr) matches how
                # ``artifact wait`` consumes the same dataclass and surfaces
                # type drift instead of swallowing it.
                json_output_response(
                    {
                        "task_id": status.task_id,
                        "status": status.status,
                        "url": status.url,
                        "error": status.error,
                        "error_code": status.error_code,
                        "metadata": status.metadata,
                    }
                )
                return

            console.print("[bold cyan]Task Status:[/bold cyan]")
            console.print(status)

    return _run()


@artifact.command("wait")
@click.argument("artifact_id")
@notebook_option
@wait_polling_options(default_timeout=300, default_interval=2)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def artifact_wait(ctx, artifact_id, notebook_id, timeout, interval, json_output, client_auth):
    """Block until artifact generation finishes (or times out).

    \b
    ARTIFACT_ID is an identifier from `notebooklm artifact list` — it can be
    a full UUID or a unique prefix (e.g., `abc` matches `abc123def...`).
    Wait blocks until status is `completed`, `failed`, or `--timeout`
    elapses; useful for scripts and LLM agents that need a synchronous gate.

    \b
    Note: this is the same identifier `poll` accepts. The API uses one ID
    that serves as both the generation task_id (during creation) and the
    artifact_id (once listed); the difference is operational, not semantic:
      - `poll`: one-shot check, accepts the raw task_id from `generate`.
      - `wait`: blocks until terminal, prefix-matches against `artifact list`.

    \b
    Examples:
      # After `artifact list` shows id "abc123def...":
      notebooklm artifact wait abc123 -n nb_456
      # Long-running generation with longer ceiling, JSON for scripting:
      notebooklm artifact wait abc123 --timeout 600 --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )

            try:
                # Wrap the blocking poll in a transient spinner so interactive
                # users see progress feedback during the wait.
                # The status line includes the artifact ID and a live
                # elapsed-seconds counter. No-op under --json so stdout stays
                # pure JSON.
                #
                # ``resume_hint`` plumbs the canonical M2 cancellation message
                # (``Cancelled. Resume with: notebooklm artifact poll <id>``)
                # so Ctrl-C during the wait surfaces a resume command instead
                # of a Python KeyboardInterrupt traceback. Same hint shape as
                # ``generate <kind> --wait`` because both polling loops resume
                # via ``artifact poll``.
                async with _status_with_elapsed(
                    f"Waiting for artifact {resolved_id} to complete...",
                    json_output=json_output,
                    resume_hint=f"notebooklm artifact poll {resolved_id}",
                ):
                    status = await client.artifacts.wait_for_completion(
                        nb_id_resolved,
                        resolved_id,
                        poll_interval=float(interval),
                        timeout=float(timeout),
                    )

                if json_output:
                    data = {
                        "artifact_id": resolved_id,
                        "status": status.status,
                        "url": status.url,
                        "error": status.error,
                    }
                    json_output_response(data)
                    # Any non-completed status is an error for automation;
                    # intentionally stricter than the non-JSON path (which
                    # exits 0 for unknown/pending statuses). Without this,
                    # automation sees a JSON payload with an "error" message
                    # but the command still exits 0.
                    if status.status != "completed":
                        raise SystemExit(1)
                else:
                    if status.status == "completed":
                        console.print(f"[green]✓ Artifact completed:[/green] {resolved_id}")
                        if status.url:
                            console.print(f"[dim]URL:[/dim] {status.url}")
                    elif status.error:
                        console.print(f"[red]✗ Generation failed:[/red] {status.error}")
                        raise SystemExit(1)
                    else:
                        console.print(f"[yellow]Status:[/yellow] {status.status}")

            except TimeoutError:
                if json_output:
                    json_output_response(
                        {
                            "artifact_id": resolved_id,
                            "status": "timeout",
                            "error": f"Timed out after {timeout} seconds",
                        }
                    )
                else:
                    console.print(f"[red]✗ Timeout after {timeout}s[/red]")
                raise SystemExit(1) from None

    return _run()


@artifact.command("suggestions")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output JSON format")
@with_client
def artifact_suggestions(ctx, notebook_id, json_output, client_auth):
    """Get AI-suggested report topics based on notebook content."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            suggestions = await client.artifacts.suggest_reports(nb_id_resolved)

            if json_output:
                data = [
                    {"title": s.title, "description": s.description, "prompt": s.prompt}
                    for s in suggestions
                ]
                json_output_response(data)
                return

            if not suggestions:
                console.print("[yellow]No suggestions available[/yellow]")
                return

            table = Table(title="Suggested Reports")
            table.add_column("#", style="dim")
            table.add_column("Title", style="green")
            table.add_column("Description")

            for i, suggestion in enumerate(suggestions, 1):
                table.add_row(str(i), suggestion.title, suggestion.description)

            console.print(table)
            console.print('\n[dim]Use the prompt with: notebooklm generate report "<prompt>"[/dim]')

    return _run()
