"""Note management CLI commands.

Commands:
    list    List all notes
    create  Create a new note
    get     Get note content
    save    Update note content
    rename  Rename a note
    delete  Delete a note
"""

from dataclasses import asdict
from typing import Any

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import Note
from .error_handler import _output_error
from .helpers import (
    console,
    json_output_response,
    read_stdin_text,
    require_notebook,
    resolve_note_id,
    resolve_notebook_id,
    with_client,
)
from .options import json_option, notebook_option


@click.group()
def note():
    """Note management commands.

    \b
    Commands:
      list    List all notes
      create  Create a new note
      get     Get note content
      save    Update note content
      rename  Rename a note
      delete  Delete a note

    \b
    Partial ID Support:
      NOTE_ID arguments support partial matching. Instead of typing the full
      UUID, you can use a prefix (e.g., 'abc' matches 'abc123def456...').
    """
    pass


@note.command("list")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def note_list(ctx, notebook_id, json_output, client_auth):
    """List all notes in a notebook."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            notes = await client.notes.list(nb_id_resolved)

            if json_output:
                serialized = [
                    {
                        "id": n.id,
                        "title": n.title or "Untitled",
                        "preview": (n.content or "")[:100],
                    }
                    for n in notes
                    if isinstance(n, Note)
                ]
                json_output_response(
                    {
                        "notebook_id": nb_id_resolved,
                        "notes": serialized,
                        "count": len(serialized),
                    }
                )
                return

            if not notes:
                console.print("[yellow]No notes found[/yellow]")
                return

            table = Table(title=f"Notes in {nb_id_resolved}")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Title", style="green")
            table.add_column("Preview", style="dim", max_width=50)

            for n in notes:
                if isinstance(n, Note):
                    preview = n.content[:50] if n.content else ""
                    table.add_row(
                        n.id,
                        n.title or "Untitled",
                        preview + "..." if len(n.content or "") > 50 else preview,
                    )

            console.print(table)

    return _run()


@note.command("create")
@click.argument("content", default="", required=False)
@click.option(
    "--content",
    "content_flag",
    default=None,
    help=(
        "Note content (or '-' to read from stdin). Mutually exclusive with the "
        "positional CONTENT argument."
    ),
)
@notebook_option
@click.option("-t", "--title", default="New Note", help="Note title")
@json_option
@with_client
def note_create(ctx, content, content_flag, notebook_id, title, json_output, client_auth):
    """Create a new note.

    \b
    Examples:
      notebooklm note create                        # Empty note with default title
      notebooklm note create "My note content"     # Note with content
      notebooklm note create "Content" -t "Title"  # Note with title and content
      cat notes.md | notebooklm note create --content -    # Content from stdin
      cat notes.md | notebooklm note create -              # Same, positional form
    """
    # Resolve content from one of (positional CONTENT, --content,
    # stdin). Positional and --content are mutually exclusive so the failure
    # mode on accidental double-pass is loud instead of a silent precedence.
    # ``content`` defaults to ``""`` (Click's ``default=""``) so we can't
    # distinguish "user passed empty" from "user passed nothing"; the explicit
    # ``content_flag is not None`` check means ``--content ""`` still wins.
    if content and content_flag is not None:
        raise click.UsageError(
            "Cannot use both the positional CONTENT argument and --content. Choose one."
        )
    if content_flag is not None:
        content = content_flag
    if content == "-":
        content = read_stdin_text(source_label="content")

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            result = await client.notes.create(nb_id_resolved, title, content)

            # The notes.create RPC returns a nested list whose first element is
            # the new note ID, e.g. ["note_xyz", ["note_xyz", content, ...]].
            # Extract it defensively for the JSON shape.
            new_id: str | None = None
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, str):
                    new_id = first

            if json_output:
                if result and new_id:
                    json_output_response(
                        {
                            "id": new_id,
                            "notebook_id": nb_id_resolved,
                            "title": title,
                            "created": True,
                        }
                    )
                else:
                    json_output_response(
                        {
                            "id": None,
                            "notebook_id": nb_id_resolved,
                            "title": title,
                            "created": False,
                            "error": "Creation may have failed",
                        }
                    )
                return

            if result:
                console.print("[green]Note created[/green]")
                console.print(result)
            else:
                console.print("[yellow]Creation may have failed[/yellow]")

    return _run()


@note.command("get")
@click.argument("note_id")
@notebook_option
@json_option
@with_client
def note_get(ctx, note_id, notebook_id, json_output, client_auth):
    """Get note content.

    NOTE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_note_id(
                client, nb_id_resolved, note_id, json_output=json_output
            )
            n = await client.notes.get(nb_id_resolved, resolved_id)

            # BREAKING: not-found exits 1 with a typed error instead of
            # the previous exit-0 ``found: false`` placeholder. The backend
            # may return ``None`` (or any non-``Note`` sentinel) when
            # the row was deleted between the partial-ID resolve and this
            # ``get``; treat any non-``Note`` as missing. See
            # ``docs/cli-exit-codes.md`` and the BREAKING entry in
            # ``CHANGELOG.md`` (Unreleased → Changed).
            #
            # The trailing ``raise AssertionError`` is unreachable at runtime
            # (``_output_error`` always raises) — it exists solely to narrow
            # ``n`` from ``Note | None`` to ``Note`` for mypy without forcing a
            # ``NoReturn`` annotation onto ``error_handler._output_error``
            # (which would touch a module the C1 spec says we must not).
            if not isinstance(n, Note):
                _output_error(
                    "Note not found",
                    code="NOT_FOUND",
                    json_output=json_output,
                    exit_code=1,
                    extra={"id": resolved_id, "notebook_id": nb_id_resolved},
                )
                raise AssertionError("unreachable")  # pragma: no cover

            if json_output:
                # Mirror the Note dataclass shape; ``json_output_response``
                # uses ``default=str`` which handles ``datetime`` fields.
                # Inject ``found: True`` so callers can disambiguate the
                # success and failure shapes by a single key (the failure
                # path emits the typed ``{error, code, message, ...}``
                # envelope); without it both shapes would be falsy on
                # ``data.get("found")``.
                payload = asdict(n)
                payload["found"] = True
                json_output_response(payload)
                return

            console.print(f"[bold cyan]ID:[/bold cyan] {n.id}")
            console.print(f"[bold cyan]Title:[/bold cyan] {n.title or 'Untitled'}")
            console.print(f"[bold cyan]Content:[/bold cyan]\n{n.content or ''}")

    return _run()


@note.command("save")
@click.argument("note_id")
@notebook_option
@click.option("--title", help="New title")
@click.option("--content", help="New content")
@json_option
@with_client
def note_save(ctx, note_id, notebook_id, title, content, json_output, client_auth):
    """Update note content.

    NOTE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    # Validate up-front so we don't make a network round-trip for a no-op.
    # The early-return must yield a coroutine because ``@with_client`` feeds
    # whatever this function returns into ``asyncio.run`` — returning ``None``
    # here would surface as the misleading "a coroutine was expected, got None"
    # UNEXPECTED_ERROR path that this command silently produced before.
    if not title and not content:

        async def _no_changes():
            if json_output:
                # ``notebook_id`` is the raw CLI argument here (may be ``None``
                # when the user relies on context); we include it for shape
                # parity with every other ``--json`` response in this module
                # so callers can rely on the key always being present.
                json_output_response(
                    {
                        "id": note_id,
                        "notebook_id": notebook_id,
                        "saved": False,
                        "error": "Provide --title and/or --content",
                    }
                )
                return
            console.print("[yellow]Provide --title and/or --content[/yellow]")

        return _no_changes()

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_note_id(
                client, nb_id_resolved, note_id, json_output=json_output
            )
            await client.notes.update(nb_id_resolved, resolved_id, content=content, title=title)

            if json_output:
                payload: dict[str, Any] = {
                    "id": resolved_id,
                    "notebook_id": nb_id_resolved,
                    "saved": True,
                }
                if title is not None:
                    payload["title"] = title
                if content is not None:
                    payload["content"] = content
                json_output_response(payload)
                return

            console.print(f"[green]Note updated:[/green] {resolved_id}")

    return _run()


@note.command("rename")
@click.argument("note_id")
@click.argument("new_title")
@notebook_option
@json_option
@with_client
def note_rename(ctx, note_id, new_title, notebook_id, json_output, client_auth):
    """Rename a note.

    NOTE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_note_id(
                client, nb_id_resolved, note_id, json_output=json_output
            )
            # Get current note to preserve content
            n = await client.notes.get(nb_id_resolved, resolved_id)
            if not isinstance(n, Note):
                if json_output:
                    json_output_response(
                        {
                            "id": resolved_id,
                            "notebook_id": nb_id_resolved,
                            "renamed": False,
                            "error": "Note not found",
                        }
                    )
                    return
                console.print("[yellow]Note not found[/yellow]")
                return

            await client.notes.update(
                nb_id_resolved, resolved_id, content=n.content or "", title=new_title
            )

            if json_output:
                json_output_response(
                    {
                        "id": resolved_id,
                        "notebook_id": nb_id_resolved,
                        "title": new_title,
                        "renamed": True,
                    }
                )
                return

            console.print(f"[green]Note renamed:[/green] {new_title}")

    return _run()


@note.command("delete")
@click.argument("note_id")
@notebook_option
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_note_id(
                client, nb_id_resolved, note_id, json_output=json_output
            )

            # In JSON mode, refuse to prompt: ``click.confirm`` writes to
            # stdout, which would corrupt the parseable JSON contract callers
            # rely on (a `subprocess.check_output(...) -> json.loads(...)`
            # script would silently hang waiting for stdin). Surface a typed
            # error and exit cleanly so the caller can re-run with ``--yes``.
            if json_output and not yes:
                json_output_response(
                    {
                        "id": resolved_id,
                        "notebook_id": nb_id_resolved,
                        "deleted": False,
                        "error": "Pass --yes to confirm deletion in --json mode",
                    }
                )
                return

            if not yes and not click.confirm(f"Delete note {resolved_id}?"):
                return

            await client.notes.delete(nb_id_resolved, resolved_id)

            if json_output:
                json_output_response(
                    {
                        "id": resolved_id,
                        "notebook_id": nb_id_resolved,
                        "deleted": True,
                    }
                )
                return

            console.print(f"[green]Deleted note:[/green] {resolved_id}")

    return _run()
