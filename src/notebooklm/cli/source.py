"""Source management CLI commands.

Commands:
    list         List sources in a notebook
    add          Add a source (url, text, file, youtube)
    get          Get source details
    delete       Delete a source
    rename       Rename a source
    refresh      Refresh a URL/Drive source
    add-drive    Add a Google Drive document
    add-research Search web/drive and add sources from results
"""

from pathlib import Path

import click
from rich.table import Table

from ..client import NotebookLMClient
from .helpers import (
    console,
    require_notebook,
    resolve_source_id,
    with_client,
    json_output_response,
    get_source_type_display,
)


@click.group()
def source():
    """Source management commands.

    \b
    Commands:
      list      List sources in a notebook
      add       Add a source (url, text, file, youtube)
      get       Get source details
      delete    Delete a source
      rename    Rename a source
      refresh   Refresh a URL/Drive source
    """
    pass


@source.command("list")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_list(ctx, notebook_id, json_output, client_auth):
    """List all sources in a notebook."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            sources = await client.sources.list(nb_id)
            nb = None
            if json_output:
                nb = await client.notebooks.get(nb_id)

            if json_output:
                data = {
                    "notebook_id": nb_id,
                    "notebook_title": nb.title if nb else None,
                    "sources": [
                        {
                            "index": i,
                            "id": src.id,
                            "title": src.title,
                            "type": src.source_type,
                            "url": src.url,
                            "created_at": src.created_at.isoformat()
                            if src.created_at
                            else None,
                        }
                        for i, src in enumerate(sources, 1)
                    ],
                    "count": len(sources),
                }
                json_output_response(data)
                return

            table = Table(title=f"Sources in {nb_id}")
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Type")
            table.add_column("Created", style="dim")

            for src in sources:
                type_display = get_source_type_display(src.source_type)
                created = (
                    src.created_at.strftime("%Y-%m-%d %H:%M") if src.created_at else "-"
                )
                table.add_row(src.id, src.title or "-", type_display, created)

            console.print(table)

    return _run()


@source.command("add")
@click.argument("content")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--type",
    "source_type",
    type=click.Choice(["url", "text", "file", "youtube"]),
    default=None,
    help="Source type (auto-detected if not specified)",
)
@click.option("--title", help="Title for text sources")
@click.option("--mime-type", help="MIME type for file sources")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_add(ctx, content, notebook_id, source_type, title, mime_type, json_output, client_auth):
    """Add a source to a notebook.

    \b
    Source type is auto-detected:
      - URLs (http/https) -> url or youtube
      - Existing files (.txt, .md) -> text
      - Other content -> text (inline)
      - Use --type to override

    \b
    Examples:
      source add https://example.com              # URL
      source add ./doc.md                         # File content as text
      source add https://youtube.com/...          # YouTube video
      source add "My notes here"                  # Inline text
      source add "My notes" --title "Research"   # Text with custom title
    """
    nb_id = require_notebook(notebook_id)

    # Auto-detect source type if not specified
    detected_type = source_type
    file_content = None
    file_title = title

    if detected_type is None:
        if content.startswith(("http://", "https://")):
            if "youtube.com" in content or "youtu.be" in content:
                detected_type = "youtube"
            else:
                detected_type = "url"
        elif Path(content).exists():
            file_path = Path(content)
            suffix = file_path.suffix.lower()
            if suffix in (".txt", ".md", ".markdown", ".rst", ".text"):
                detected_type = "text"
                file_content = file_path.read_text()
                file_title = title or file_path.name
            else:
                detected_type = "file"
        else:
            detected_type = "text"
            file_title = title or "Pasted Text"

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            if detected_type == "url":
                src = await client.sources.add_url(nb_id, content)
            elif detected_type == "youtube":
                src = await client.sources.add_url(nb_id, content)
            elif detected_type == "text":
                text_content = file_content if file_content is not None else content
                text_title = file_title or "Untitled"
                src = await client.sources.add_text(nb_id, text_title, text_content)
            elif detected_type == "file":
                src = await client.sources.add_file(nb_id, content, mime_type)

            if json_output:
                data = {
                    "source": {
                        "id": src.id,
                        "title": src.title,
                        "type": src.source_type,
                        "url": src.url,
                    }
                }
                json_output_response(data)
                return

            console.print(f"[green]Added source:[/green] {src.id}")

    if not json_output:
        with console.status(f"Adding {detected_type} source..."):
            return _run()
    return _run()


@source.command("get")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def source_get(ctx, source_id, notebook_id, client_auth):
    """Get source details.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(client, nb_id, source_id)
            src = await client.sources.get(nb_id, resolved_id)
            if src:
                console.print(f"[bold cyan]Source:[/bold cyan] {src.id}")
                console.print(f"[bold]Title:[/bold] {src.title}")
                console.print(
                    f"[bold]Type:[/bold] {get_source_type_display(src.source_type)}"
                )
                if src.url:
                    console.print(f"[bold]URL:[/bold] {src.url}")
                if src.created_at:
                    console.print(
                        f"[bold]Created:[/bold] {src.created_at.strftime('%Y-%m-%d %H:%M')}"
                    )
            else:
                console.print("[yellow]Source not found[/yellow]")

    return _run()


@source.command("delete")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@with_client
def source_delete(ctx, source_id, notebook_id, yes, client_auth):
    """Delete a source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(client, nb_id, source_id)

            if not yes and not click.confirm(f"Delete source {resolved_id}?"):
                return

            success = await client.sources.delete(nb_id, resolved_id)
            if success:
                console.print(f"[green]Deleted source:[/green] {resolved_id}")
            else:
                console.print("[yellow]Delete may have failed[/yellow]")

    return _run()


@source.command("rename")
@click.argument("source_id")
@click.argument("new_title")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def source_rename(ctx, source_id, new_title, notebook_id, client_auth):
    """Rename a source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(client, nb_id, source_id)
            src = await client.sources.rename(nb_id, resolved_id, new_title)
            console.print(f"[green]Renamed source:[/green] {src.id}")
            console.print(f"[bold]New title:[/bold] {src.title}")

    return _run()


@source.command("refresh")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def source_refresh(ctx, source_id, notebook_id, client_auth):
    """Refresh a URL/Drive source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(client, nb_id, source_id)
            with console.status("Refreshing source..."):
                src = await client.sources.refresh(nb_id, resolved_id)

            if src:
                console.print(f"[green]Source refreshed:[/green] {src.id}")
                console.print(f"[bold]Title:[/bold] {src.title}")
            else:
                console.print("[yellow]Refresh returned no result[/yellow]")

    return _run()


@source.command("add-drive")
@click.argument("file_id")
@click.argument("title")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--mime-type",
    type=click.Choice(["google-doc", "google-slides", "google-sheets", "pdf"]),
    default="google-doc",
    help="Document type (default: google-doc)",
)
@with_client
def source_add_drive(ctx, file_id, title, notebook_id, mime_type, client_auth):
    """Add a Google Drive document as a source."""
    from ..rpc import DriveMimeType

    nb_id = require_notebook(notebook_id)
    mime_map = {
        "google-doc": DriveMimeType.GOOGLE_DOC.value,
        "google-slides": DriveMimeType.GOOGLE_SLIDES.value,
        "google-sheets": DriveMimeType.GOOGLE_SHEETS.value,
        "pdf": DriveMimeType.PDF.value,
    }
    mime = mime_map[mime_type]

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            with console.status("Adding Drive source..."):
                src = await client.sources.add_drive(nb_id, file_id, title, mime)

            console.print(f"[green]Added Drive source:[/green] {src.id}")
            console.print(f"[bold]Title:[/bold] {src.title}")

    return _run()


@source.command("add-research")
@click.argument("query")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
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
@with_client
def source_add_research(ctx, query, notebook_id, search_source, mode, import_all, client_auth):
    """Search web or drive and add sources from results.

    \b
    Examples:
      source add-research "machine learning"              # Search web
      source add-research "project docs" --from drive     # Search Google Drive
      source add-research "AI papers" --mode deep         # Deep search
      source add-research "tutorials" --import-all        # Auto-import all results
    """
    import time

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            console.print(
                f"[yellow]Starting {mode} research on {search_source}...[/yellow]"
            )
            result = await client.research.start(nb_id, query, search_source, mode)
            if not result:
                console.print("[red]Research failed to start[/red]")
                raise SystemExit(1)

            task_id = result["task_id"]
            console.print(f"[dim]Task ID: {task_id}[/dim]")

            status = None
            for _ in range(60):
                status = await client.research.poll(nb_id)
                if status.get("status") == "completed":
                    break
                elif status.get("status") == "no_research":
                    console.print("[red]Research failed to start[/red]")
                    raise SystemExit(1)
                time.sleep(5)
            else:
                status = {"status": "timeout"}

            if status.get("status") == "completed":
                sources = status.get("sources", [])
                console.print(f"\n[green]Found {len(sources)} sources[/green]")

                if import_all and sources and task_id:
                    imported = await client.research.import_sources(
                        nb_id, task_id, sources
                    )
                    console.print(f"[green]Imported {len(imported)} sources[/green]")
            else:
                console.print(f"[yellow]Status: {status.get('status', 'unknown')}[/yellow]")

    return _run()
