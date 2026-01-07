"""Artifact management CLI commands.

Commands:
    list        List all artifacts
    get         Get artifact details
    rename      Rename an artifact
    delete      Delete an artifact
    export      Export to Google Docs/Sheets
    poll        Poll generation status
    suggestions Get AI-suggested report topics
"""

import json

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import Artifact
from .helpers import (
    console,
    require_notebook,
    with_client,
    json_output_response,
    get_artifact_type_display,
    ARTIFACT_TYPE_MAP,
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
      poll      Poll generation status
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
    type_filter = (
        None if artifact_type == "all" else ARTIFACT_TYPE_MAP.get(artifact_type)
    )

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            artifacts = await client.artifacts.list(nb_id, artifact_type=type_filter)

            # Also fetch mind maps (stored separately with notes)
            if type_filter is None or type_filter == 5:
                mind_maps = await client.notes.list_mind_maps(nb_id)
                for mm in mind_maps:
                    if isinstance(mm, list) and len(mm) > 0:
                        mm_id = mm[0] if len(mm) > 0 else ""
                        title = "Mind Map"
                        if (
                            len(mm) > 1
                            and isinstance(mm[1], list)
                            and len(mm[1]) > 4
                        ):
                            title = mm[1][4] or "Mind Map"
                        mm_artifact = Artifact(
                            id=str(mm_id),
                            title=str(title),
                            artifact_type=5,
                            status=3,
                            created_at=None,
                            variant=None,
                        )
                        artifacts.append(mm_artifact)

            nb = None
            if json_output:
                nb = await client.notebooks.get(nb_id)

            if json_output:
                def _get_status_str(art):
                    if art.is_completed:
                        return "completed"
                    elif art.is_processing:
                        return "processing"
                    return str(art.status)

                data = {
                    "notebook_id": nb_id,
                    "notebook_title": nb.title if nb else None,
                    "artifacts": [
                        {
                            "index": i,
                            "id": art.id,
                            "title": art.title,
                            "type": get_artifact_type_display(
                                art.artifact_type, art.variant, art.report_subtype
                            ).split(" ", 1)[-1],
                            "type_id": art.artifact_type,
                            "status": _get_status_str(art),
                            "status_id": art.status,
                            "created_at": art.created_at.isoformat()
                            if art.created_at
                            else None,
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
                type_display = get_artifact_type_display(
                    art.artifact_type, art.variant, art.report_subtype
                )
                created = (
                    art.created_at.strftime("%Y-%m-%d %H:%M") if art.created_at else "-"
                )
                status = (
                    "completed"
                    if art.is_completed
                    else "processing"
                    if art.is_processing
                    else str(art.status)
                )
                table.add_row(art.id, art.title, type_display, created, status)

            console.print(table)

    return _run()


@artifact.command("get")
@click.argument("artifact_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def artifact_get(ctx, artifact_id, notebook_id, client_auth):
    """Get artifact details."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            art = await client.artifacts.get(nb_id, artifact_id)
            if art:
                console.print(f"[bold cyan]Artifact:[/bold cyan] {art.id}")
                console.print(f"[bold]Title:[/bold] {art.title}")
                console.print(
                    f"[bold]Type:[/bold] {get_artifact_type_display(art.artifact_type, art.variant, art.report_subtype)}"
                )
                console.print(
                    f"[bold]Status:[/bold] {'completed' if art.is_completed else 'processing' if art.is_processing else str(art.status)}"
                )
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
    help="Notebook ID (uses current if not set)",
)
@with_client
def artifact_rename(ctx, artifact_id, new_title, notebook_id, client_auth):
    """Rename an artifact."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            # Check if this is a mind map (stored with notes, not artifacts)
            mind_maps = await client.notes.list_mind_maps(nb_id)
            for mm in mind_maps:
                if mm[0] == artifact_id:
                    raise click.ClickException("Mind maps cannot be renamed")

            art = await client.artifacts.rename(nb_id, artifact_id, new_title)
            console.print(f"[green]Renamed artifact:[/green] {art.id}")
            console.print(f"[bold]New title:[/bold] {art.title}")

    return _run()


@artifact.command("delete")
@click.argument("artifact_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@with_client
def artifact_delete(ctx, artifact_id, notebook_id, yes, client_auth):
    """Delete an artifact."""
    if not yes and not click.confirm(f"Delete artifact {artifact_id}?"):
        return

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            # Check if this is a mind map (stored with notes)
            mind_maps = await client.notes.list_mind_maps(nb_id)
            for mm in mind_maps:
                if mm[0] == artifact_id:
                    await client.notes.delete(nb_id, artifact_id)
                    console.print(f"[yellow]Cleared mind map:[/yellow] {artifact_id}")
                    console.print(
                        "[dim]Note: Mind maps are cleared, not removed. Google may garbage collect them later.[/dim]"
                    )
                    return

            await client.artifacts.delete(nb_id, artifact_id)
            console.print(f"[green]Deleted artifact:[/green] {artifact_id}")

    return _run()


@artifact.command("export")
@click.argument("artifact_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--title", required=True, help="Title for exported document")
@click.option(
    "--type", "export_type", type=click.Choice(["docs", "sheets"]), default="docs"
)
@with_client
def artifact_export(ctx, artifact_id, notebook_id, title, export_type, client_auth):
    """Export artifact to Google Docs/Sheets."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            art = await client.artifacts.get(nb_id, artifact_id)
            content = str(art) if art else ""
            result = await client.artifacts.export(
                nb_id, artifact_id, content, title, export_type
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


@artifact.command("suggestions")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--source", "source_ids", multiple=True, help="Limit to specific sources")
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
            console.print(
                '\n[dim]Use the prompt with: notebooklm generate report "<prompt>"[/dim]'
            )

    return _run()


@artifact.command("share")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--public/--private", default=False, help="Make audio public or private")
@with_client
def artifact_share(ctx, notebook_id, public, client_auth):
    """Share or unshare audio overview.

    Makes the audio overview publicly accessible or private.

    \b
    Examples:
      notebooklm artifact share --public   # Make audio public
      notebooklm artifact share --private  # Make audio private
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            result = await client.artifacts.share_audio(nb_id, public=public)

            if result:
                status = "public" if public else "private"
                console.print(f"[green]Audio is now {status}[/green]")
                console.print(result)
            else:
                console.print("[yellow]Share returned no result[/yellow]")

    return _run()
