"""Source management CLI commands.

Commands:
    list         List sources in a notebook
    add          Add a source (url, text, file, youtube)
    get          Get source details
    fulltext     Get full indexed text content of a source
    guide        Get AI-generated source summary and keywords
    stale        Check if a URL/Drive source needs refresh
    delete       Delete a source
    rename       Rename a source
    refresh      Refresh a URL/Drive source
    add-drive    Add a Google Drive document
    add-research Search web/drive and add sources from results
"""

import asyncio
from pathlib import Path

import click
from rich.table import Table

from .._url_utils import is_youtube_url
from ..client import NotebookLMClient
from ..types import source_status_to_str
from .helpers import (
    console,
    display_research_sources,
    get_source_type_display,
    json_output_response,
    require_notebook,
    resolve_source_id,
    with_client,
)


@click.group()
def source():
    """Source management commands.

    \b
    Commands:
      list         List sources in a notebook
      add          Add a source (url, text, file, youtube)
      get          Get source details
      fulltext     Get full indexed text content
      guide        Get AI-generated source summary and keywords
      stale        Check if source needs refresh
      delete       Delete a source
      rename       Rename a source
      refresh      Refresh a URL/Drive source

    \b
    Partial ID Support:
      SOURCE_ID arguments support partial matching. Instead of typing the full
      UUID, you can use a prefix (e.g., 'abc' matches 'abc123def456...').
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
                            "type": str(src.kind),
                            "url": src.url,
                            "status": source_status_to_str(src.status),
                            "status_id": src.status,
                            "created_at": src.created_at.isoformat() if src.created_at else None,
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
            table.add_column("Status", style="yellow")

            for src in sources:
                type_display = get_source_type_display(src.kind)
                created = src.created_at.strftime("%Y-%m-%d %H:%M") if src.created_at else "-"
                status = source_status_to_str(src.status)
                table.add_row(src.id, src.title or "-", type_display, created, status)

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
            detected_type = "youtube" if is_youtube_url(content) else "url"
        elif Path(content).exists():
            file_path = Path(content).resolve()  # Resolve symlinks
            # Security: Ensure it's a regular file (not a symlink to sensitive file)
            if not file_path.is_file():
                raise click.ClickException(f"Not a regular file: {content}")
            # All files use add_file() for proper type detection
            detected_type = "file"
        else:
            detected_type = "text"
            file_title = title or "Pasted Text"

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            if detected_type == "url" or detected_type == "youtube":
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
                        "type": str(src.kind),
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
                console.print(f"[bold]Type:[/bold] {get_source_type_display(src.kind)}")
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

            if src and src is not True:
                console.print(f"[green]Source refreshed:[/green] {src.id}")
                console.print(f"[bold]Title:[/bold] {src.title}")
            elif src is True:
                console.print(f"[green]Source refreshed:[/green] {resolved_id}")
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
@click.option(
    "--no-wait",
    is_flag=True,
    help="Start research and return immediately (use 'research status/wait' to monitor)",
)
@with_client
def source_add_research(
    ctx, query, notebook_id, search_source, mode, import_all, no_wait, client_auth
):
    """Search web or drive and add sources from results.

    \b
    Examples:
      source add-research "machine learning"              # Search web
      source add-research "project docs" --from drive     # Search Google Drive
      source add-research "AI papers" --mode deep         # Deep search
      source add-research "tutorials" --import-all        # Auto-import all results
      source add-research "topic" --mode deep --no-wait   # Non-blocking deep search
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            console.print(f"[yellow]Starting {mode} research on {search_source}...[/yellow]")
            result = await client.research.start(nb_id, query, search_source, mode)
            if not result:
                console.print("[red]Research failed to start[/red]")
                raise SystemExit(1)

            task_id = result["task_id"]
            console.print(f"[dim]Task ID: {task_id}[/dim]")

            # Non-blocking mode: return immediately
            if no_wait:
                console.print(
                    "[green]Research started.[/green] "
                    "Use 'research status' or 'research wait' to monitor."
                )
                return

            status = None
            for _ in range(60):
                status = await client.research.poll(nb_id)
                if status.get("status") == "completed":
                    break
                elif status.get("status") == "no_research":
                    console.print("[red]Research failed to start[/red]")
                    raise SystemExit(1)
                await asyncio.sleep(5)
            else:
                status = {"status": "timeout"}

            if status.get("status") == "completed":
                sources = status.get("sources", [])
                console.print()
                display_research_sources(sources)

                if import_all and sources and task_id:
                    imported = await client.research.import_sources(nb_id, task_id, sources)
                    console.print(f"[green]Imported {len(imported)} sources[/green]")
            else:
                console.print(f"[yellow]Status: {status.get('status', 'unknown')}[/yellow]")

    return _run()


@source.command("fulltext")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--output", "-o", type=click.Path(), help="Write content to file")
@with_client
def source_fulltext(ctx, source_id, notebook_id, json_output, output, client_auth):
    """Get full indexed text content of a source.

    Retrieves the complete text content as indexed by NotebookLM. This is the
    actual text that NotebookLM uses when answering questions about this source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Examples:
      source fulltext abc123                    # Show fulltext in terminal
      source fulltext abc123 --json             # Output as JSON
      source fulltext abc123 -o content.txt     # Save to file
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_source_id(client, nb_id, source_id)

            with console.status("Fetching fulltext content..."):
                fulltext = await client.sources.get_fulltext(nb_id, resolved_id)

            if json_output:
                from dataclasses import asdict

                json_output_response(asdict(fulltext))
                return

            if output:
                Path(output).write_text(fulltext.content, encoding="utf-8")
                console.print(f"[green]Saved {fulltext.char_count} chars to {output}[/green]")
                return

            console.print(f"[bold cyan]Source:[/bold cyan] {fulltext.source_id}")
            console.print(f"[bold]Title:[/bold] {fulltext.title}")
            console.print(f"[bold]Characters:[/bold] {fulltext.char_count:,}")
            if fulltext.url:
                console.print(f"[bold]URL:[/bold] {fulltext.url}")
            console.print()
            console.print("[bold cyan]Content:[/bold cyan]")
            # Show first 2000 chars with truncation notice
            if len(fulltext.content) > 2000:
                console.print(fulltext.content[:2000])
                console.print(
                    f"\n[dim]... ({fulltext.char_count - 2000:,} more chars, use -o to save full content)[/dim]"
                )
            else:
                console.print(fulltext.content)

    return _run()


@source.command("guide")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_guide(ctx, source_id, notebook_id, json_output, client_auth):
    """Get AI-generated source summary and keywords.

    Shows the "Source Guide" - an AI-generated overview of what a source contains,
    including a summary with highlighted keywords and topic tags.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Examples:
      source guide abc123                    # Get guide for source
      source guide abc123 --json             # Output as JSON
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_source_id(client, nb_id, source_id)

            with console.status("Generating source guide..."):
                guide = await client.sources.get_guide(nb_id, resolved_id)

            if json_output:
                data = {
                    "source_id": resolved_id,
                    "summary": guide.get("summary", ""),
                    "keywords": guide.get("keywords", []),
                }
                json_output_response(data)
                return

            summary = guide.get("summary", "").strip()
            keywords = guide.get("keywords", [])

            if not summary and not keywords:
                console.print("[yellow]No guide available for this source[/yellow]")
                return

            if summary:
                console.print("[bold cyan]Summary:[/bold cyan]")
                console.print(summary)
                console.print()

            if keywords:
                console.print("[bold cyan]Keywords:[/bold cyan]")
                console.print(", ".join(keywords))

    return _run()


@source.command("stale")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@with_client
def source_stale(ctx, source_id, notebook_id, client_auth):
    """Check if a URL/Drive source needs refresh.

    Returns exit code 0 if stale (needs refresh), 1 if fresh.
    This enables shell scripting: if notebooklm source stale ID; then refresh; fi

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Examples:
      source stale abc123              # Check if stale
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_source_id(client, nb_id, source_id)
            is_fresh = await client.sources.check_freshness(nb_id, resolved_id)

            if is_fresh:
                console.print("[green]✓ Source is fresh[/green]")
                raise SystemExit(1)  # Not stale
            else:
                console.print("[yellow]⚠ Source is stale[/yellow]")
                console.print("[dim]Run 'source refresh' to update[/dim]")
                raise SystemExit(0)  # Is stale

    return _run()


@source.command("wait")
@click.argument("source_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option(
    "--timeout",
    default=120,
    type=int,
    help="Maximum seconds to wait (default: 120)",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_wait(ctx, source_id, notebook_id, timeout, json_output, client_auth):
    """Wait for a source to finish processing.

    After adding a source, it needs to be processed before it can be used
    for chat or artifact generation. This command polls until the source
    is ready or fails.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Exit codes:
      0 - Source is ready
      1 - Source not found or processing failed
      2 - Timeout reached

    \b
    Examples:
      source wait abc123                    # Wait for source to be ready
      source wait abc123 --timeout 300      # Wait up to 5 minutes
      source wait abc123 --json             # Output status as JSON

    \b
    Subagent pattern for long-running operations:
      # In main conversation, add source then spawn subagent to wait:
      notebooklm source add https://example.com
      # Subagent runs: notebooklm source wait <source_id>
    """
    from ..types import SourceNotFoundError, SourceProcessingError, SourceTimeoutError

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_id = await resolve_source_id(client, nb_id, source_id)

            if not json_output:
                console.print(f"[dim]Waiting for source {resolved_id}...[/dim]")

            try:
                source = await client.sources.wait_until_ready(
                    nb_id,
                    resolved_id,
                    timeout=float(timeout),
                )

                if json_output:
                    data = {
                        "source_id": source.id,
                        "title": source.title,
                        "status": "ready",
                        "status_code": source.status,
                    }
                    json_output_response(data)
                else:
                    console.print(f"[green]✓ Source ready:[/green] {source.id}")
                    if source.title:
                        console.print(f"[bold]Title:[/bold] {source.title}")

            except SourceNotFoundError as e:
                if json_output:
                    data = {
                        "source_id": e.source_id,
                        "status": "not_found",
                        "error": str(e),
                    }
                    json_output_response(data)
                else:
                    console.print(f"[red]✗ Source not found:[/red] {e.source_id}")
                raise SystemExit(1) from None

            except SourceProcessingError as e:
                if json_output:
                    data = {
                        "source_id": e.source_id,
                        "status": "error",
                        "status_code": e.status,
                        "error": str(e),
                    }
                    json_output_response(data)
                else:
                    console.print(f"[red]✗ Source processing failed:[/red] {e.source_id}")
                raise SystemExit(1) from None

            except SourceTimeoutError as e:
                if json_output:
                    data = {
                        "source_id": e.source_id,
                        "status": "timeout",
                        "last_status_code": e.last_status,
                        "timeout_seconds": int(e.timeout),
                        "error": str(e),
                    }
                    json_output_response(data)
                else:
                    console.print(f"[yellow]⚠ Timeout waiting for source:[/yellow] {e.source_id}")
                    console.print(f"[dim]Last status: {e.last_status}[/dim]")
                raise SystemExit(2) from None

    return _run()
