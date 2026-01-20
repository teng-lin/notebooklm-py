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

import json

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..rpc import ExportType
from .helpers import (
    ARTIFACT_TYPE_MAP,
    console,
    get_artifact_type_display,
    json_output_response,
    require_notebook,
    resolve_artifact_id,
    with_client,
)


@click.group()
def artifact():
    """Artifact management commands.

    \b
    Commands:
      list      List all artifacts (or by type)
      get       Get artifact details
      rename    Rename an artifact
      delete    Delete an artifact
      export    Export to Google Docs/Sheets
      poll      Poll generation status (single check)
      wait      Wait for generation to complete (blocking)

    \b
    Partial ID Support:
      ARTIFACT_ID arguments support partial matching. Instead of typing the full
      UUID, you can use a prefix (e.g., 'abc' matches 'abc123def456...').
    """
    pass


@artifact.command("list")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--type",
    "artifact_type",
    type=click.Choice(
        [
            "all",
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
@with_client
def artifact_list(ctx, notebook_id, artifact_type, json_output, client_auth):
    """List artifacts in a notebook."""
    nb_id = require_notebook(notebook_id)
    type_filter = None if artifact_type == "all" else ARTIFACT_TYPE_MAP.get(artifact_type)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            # artifacts.list() already includes mind maps from notes system
            artifacts = await client.artifacts.list(nb_id, artifact_type=type_filter)

            nb = None
            if json_output:
                nb = await client.notebooks.get(nb_id)

            if json_output:
                data = {
                    "notebook_id": nb_id,
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

            table = Table(title=f"Artifacts in {nb_id}")
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
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
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@with_client
def artifact_get(ctx, artifact_id, notebook_id, client_auth):
    """Get artifact details.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_artifact_id(client, nb_id, artifact_id)
            art = await client.artifacts.get(nb_id, resolved_id)
            if art:
                console.print(f"[bold cyan]Artifact:[/bold cyan] {art.id}")
                console.print(f"[bold]Title:[/bold] {art.title}")
                console.print(f"[bold]Type:[/bold] {get_artifact_type_display(art)}")
                console.print(f"[bold]Status:[/bold] {art.status_str}")
                if art.created_at:
                    console.print(
                        f"[bold]Created:[/bold] {art.created_at.strftime('%Y-%m-%d %H:%M')}"
                    )
            else:
                console.print("[yellow]Artifact not found[/yellow]")

    return _run()


@artifact.command("rename")
@click.argument("artifact_id")
@click.argument("new_title")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@with_client
def artifact_rename(ctx, artifact_id, new_title, notebook_id, client_auth):
    """Rename an artifact.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_artifact_id(client, nb_id, artifact_id)

            # Check if this is a mind map (stored with notes, not artifacts)
            mind_maps = await client.notes.list_mind_maps(nb_id)
            for mm in mind_maps:
                if mm[0] == resolved_id:
                    raise click.ClickException("Mind maps cannot be renamed")

            await client.artifacts.rename(nb_id, resolved_id, new_title)
            # The rename API returns None; if no exception was raised, the operation succeeded.
            # We display the requested new_title as confirmation.
            console.print(f"[green]Renamed artifact:[/green] {resolved_id}")
            console.print(f"[bold]New title:[/bold] {new_title}")

    return _run()


@artifact.command("delete")
@click.argument("artifact_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@with_client
def artifact_delete(ctx, artifact_id, notebook_id, yes, client_auth):
    """Delete an artifact.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_artifact_id(client, nb_id, artifact_id)

            if not yes and not click.confirm(f"Delete artifact {resolved_id}?"):
                return

            # Check if this is a mind map (stored with notes)
            mind_maps = await client.notes.list_mind_maps(nb_id)
            for mm in mind_maps:
                if mm[0] == resolved_id:
                    await client.notes.delete(nb_id, resolved_id)
                    console.print(f"[yellow]Cleared mind map:[/yellow] {resolved_id}")
                    console.print(
                        "[dim]Note: Mind maps are cleared, not removed. Google may garbage collect them later.[/dim]"
                    )
                    return

            await client.artifacts.delete(nb_id, resolved_id)
            console.print(f"[green]Deleted artifact:[/green] {resolved_id}")

    return _run()


@artifact.command("export")
@click.argument("artifact_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set). Supports partial IDs.",
)
@click.option("--title", required=True, help="Title for exported document")
@click.option("--type", "export_type", type=click.Choice(["docs", "sheets"]), default="docs")
@with_client
def artifact_export(ctx, artifact_id, notebook_id, title, export_type, client_auth):
    """Export artifact to Google Docs/Sheets.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_artifact_id(client, nb_id, artifact_id)
            # Convert export_type string to ExportType enum
            export_type_enum = ExportType.SHEETS if export_type == "sheets" else ExportType.DOCS
            # Pass None for content - backend retrieves content from artifact_id
            result = await client.artifacts.export(
                nb_id, resolved_id, None, title, export_type_enum
            )
            if result:
                console.print(f"[green]Exported to Google {export_type.title()}[/green]")
                console.print(result)
            else:
                console.print("[yellow]Export may have failed[/yellow]")

    return _run()


@artifact.command("poll")
@click.argument("task_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def artifact_poll(ctx, task_id, notebook_id, client_auth):
    """Poll generation status."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            status = await client.artifacts.poll_status(nb_id, task_id)
            console.print("[bold cyan]Task Status:[/bold cyan]")
            console.print(status)

    return _run()


@artifact.command("wait")
@click.argument("artifact_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--timeout",
    default=300,
    type=int,
    help="Maximum seconds to wait (default: 300)",
)
@click.option(
    "--interval",
    default=2,
    type=int,
    help="Seconds between status checks (default: 2)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def artifact_wait(ctx, artifact_id, notebook_id, timeout, interval, json_output, client_auth):
    """Wait for artifact generation to complete.

    Blocks until the artifact is completed, failed, or timeout is reached.
    Useful for scripts and LLM agents that need to wait for generation.

    \b
    Examples:
      notebooklm artifact wait abc123 -n nb_456
      notebooklm artifact wait abc123 --timeout 600 --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_artifact_id(client, nb_id, artifact_id)

            try:
                status = await client.artifacts.wait_for_completion(
                    nb_id,
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
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("-s", "--source", "source_ids", multiple=True, help="Limit to specific sources")
@click.option("--json", "json_output", is_flag=True, help="Output JSON format")
@with_client
def artifact_suggestions(ctx, notebook_id, source_ids, json_output, client_auth):
    """Get AI-suggested report topics based on notebook content."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            ids = list(source_ids) if source_ids else None
            suggestions = await client.artifacts.suggest_reports(nb_id, ids)

            if not suggestions:
                console.print("[yellow]No suggestions available[/yellow]")
                return

            if json_output:
                data = [
                    {"title": s.title, "description": s.description, "prompt": s.prompt}
                    for s in suggestions
                ]
                console.print(json.dumps(data, indent=2))
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
