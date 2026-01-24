"""Note management CLI commands.

Commands:
    list    List all notes
    create  Create a new note
    get     Get note content
    save    Update note content
    rename  Rename a note
    delete  Delete a note
"""

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import Note
from .helpers import (
    console,
    json_output_response,
    output_result,
    require_notebook,
    resolve_note_id,
    resolve_notebook_id,
    should_confirm,
    with_client,
)
from .options import json_option


def _note_preview(content: str | None, max_len: int = 50) -> str:
    """Generate a preview string from note content.

    Args:
        content: The note content (may be None or empty)
        max_len: Maximum preview length before truncation

    Returns:
        Truncated preview with "..." suffix if needed, or empty string
    """
    if not content:
        return ""
    if len(content) > max_len:
        return content[:max_len] + "..."
    return content


@click.group()
def note():
    """Note management commands.

    \b
    Commands:
      list    List all notes
      create  Create a new note
      get     Get note content
      save    Update note content
      delete  Delete a note

    \b
    Partial ID Support:
      NOTE_ID arguments support partial matching. Instead of typing the full
      UUID, you can use a prefix (e.g., 'abc' matches 'abc123def456...').
    """
    pass


@note.command("list")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@json_option
@with_client
def note_list(ctx, notebook_id, json_output, client_auth):
    """List all notes in a notebook."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_nb_id = await resolve_notebook_id(client, nb_id)
            notes = await client.notes.list(resolved_nb_id)
            # Filter to Note instances once
            note_items = [n for n in notes if isinstance(n, Note)]

            def render():
                if not note_items:
                    console.print("[yellow]No notes found[/yellow]")
                    return

                table = Table(title=f"Notes in {resolved_nb_id}")
                table.add_column("ID", style="cyan")
                table.add_column("Title", style="green")
                table.add_column("Preview", style="dim", max_width=50)

                for n in note_items:
                    table.add_row(n.id, n.title or "Untitled", _note_preview(n.content))

                console.print(table)

            output_result(
                json_output,
                {
                    "notebook_id": resolved_nb_id,
                    "notes": [
                        {
                            "id": n.id,
                            "title": n.title or "Untitled",
                            "preview": _note_preview(n.content),
                        }
                        for n in note_items
                    ],
                    "count": len(note_items),
                },
                render,
            )

    return _run()


@note.command("create")
@click.argument("content", default="", required=False)
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("-t", "--title", default="New Note", help="Note title")
@json_option
@with_client
def note_create(ctx, content, notebook_id, title, json_output, client_auth):
    """Create a new note.

    \b
    Examples:
      notebooklm note create                        # Empty note with default title
      notebooklm note create "My note content"     # Note with content
      notebooklm note create "Content" -t "Title"  # Note with title and content
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_nb_id = await resolve_notebook_id(client, nb_id)
            result = await client.notes.create(resolved_nb_id, title, content)

            def render():
                if result:
                    console.print("[green]Note created[/green]")
                    console.print(result)
                else:
                    console.print("[yellow]Creation may have failed[/yellow]")

            output_result(
                json_output,
                {
                    "notebook_id": resolved_nb_id,
                    "note_id": result if isinstance(result, str) else None,
                    "title": title,
                    "created": bool(result),
                },
                render,
            )

    return _run()


@note.command("get")
@click.argument("note_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@json_option
@with_client
def note_get(ctx, note_id, notebook_id, json_output, client_auth):
    """Get note content.

    NOTE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_nb_id = await resolve_notebook_id(client, nb_id)
            resolved_id = await resolve_note_id(client, resolved_nb_id, note_id)
            n = await client.notes.get(resolved_nb_id, resolved_id)

            if json_output:
                if n and isinstance(n, Note):
                    json_output_response(
                        {
                            "notebook_id": resolved_nb_id,
                            "note_id": n.id,
                            "title": n.title or "Untitled",
                            "content": n.content or "",
                        }
                    )
                else:
                    json_output_response(
                        {
                            "notebook_id": resolved_nb_id,
                            "note_id": resolved_id,
                            "error": "Note not found",
                        }
                    )
                return

            if n and isinstance(n, Note):
                console.print(f"[bold cyan]ID:[/bold cyan] {n.id}")
                console.print(f"[bold cyan]Title:[/bold cyan] {n.title or 'Untitled'}")
                console.print(f"[bold cyan]Content:[/bold cyan]\n{n.content or ''}")
            else:
                console.print("[yellow]Note not found[/yellow]")

    return _run()


@note.command("save")
@click.argument("note_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--title", help="New title")
@click.option("--content", help="New content")
@json_option
@with_client
def note_save(ctx, note_id, notebook_id, title, content, json_output, client_auth):
    """Update note content.

    NOTE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    if not title and not content:
        if json_output:
            json_output_response({"error": "Provide --title and/or --content"})
            raise SystemExit(1)
        raise click.ClickException("Provide --title and/or --content")

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_nb_id = await resolve_notebook_id(client, nb_id)
            resolved_id = await resolve_note_id(client, resolved_nb_id, note_id)
            await client.notes.update(resolved_nb_id, resolved_id, content=content, title=title)

            output_result(
                json_output,
                {
                    "notebook_id": resolved_nb_id,
                    "note_id": resolved_id,
                    "updated": True,
                    "new_title": title,
                    "new_content": content,
                },
                lambda: console.print(f"[green]Note updated:[/green] {resolved_id}"),
            )

    return _run()


@note.command("rename")
@click.argument("note_id")
@click.argument("new_title")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@json_option
@with_client
def note_rename(ctx, note_id, new_title, notebook_id, json_output, client_auth):
    """Rename a note.

    NOTE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_nb_id = await resolve_notebook_id(client, nb_id)
            resolved_id = await resolve_note_id(client, resolved_nb_id, note_id)
            # Get current note to preserve content
            n = await client.notes.get(resolved_nb_id, resolved_id)
            if not n or not isinstance(n, Note):
                output_result(
                    json_output,
                    {
                        "notebook_id": resolved_nb_id,
                        "note_id": resolved_id,
                        "error": "Note not found",
                    },
                    lambda: console.print("[yellow]Note not found[/yellow]"),
                )
                return

            await client.notes.update(
                resolved_nb_id, resolved_id, content=n.content or "", title=new_title
            )

            output_result(
                json_output,
                {"notebook_id": resolved_nb_id, "note_id": resolved_id, "new_title": new_title},
                lambda: console.print(f"[green]Note renamed:[/green] {new_title}"),
            )

    return _run()


@note.command("delete")
@click.argument("note_id")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def note_delete(ctx, note_id, notebook_id, yes, json_output, client_auth):
    """Delete a note.

    NOTE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            resolved_nb_id = await resolve_notebook_id(client, nb_id)
            resolved_id = await resolve_note_id(client, resolved_nb_id, note_id)

            if should_confirm(yes, json_output) and not click.confirm(
                f"Delete note {resolved_id}?"
            ):
                return

            await client.notes.delete(resolved_nb_id, resolved_id)

            output_result(
                json_output,
                {"notebook_id": resolved_nb_id, "note_id": resolved_id, "deleted": True},
                lambda: console.print(f"[green]Deleted note:[/green] {resolved_id}"),
            )

    return _run()
