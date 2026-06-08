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

from .._app.notes import (
    execute_note_create,
    execute_note_delete,
    execute_note_get,
    execute_note_rename,
    execute_note_save,
    resolve_note_for_delete,
)
from ..client import NotebookLMClient
from ..types import Note
from .auth_runtime import with_client
from .error_handler import _output_error
from .input import read_stdin_text
from .options import json_option, notebook_option
from .rendering import cli_print, console, json_output_response, render_list
from .resolve import require_notebook, resolve_note_id, resolve_notebook_id
from .services.confirming_mutation import MutationPlan, run_confirmed_mutation
from .services.listing import ListSpec, prepare_list


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

            async def fetch_notes(client: NotebookLMClient, notebook_id: str) -> list[Note]:
                notes = await client.notes.list(notebook_id)
                return [note for note in notes if isinstance(note, Note)]

            async def envelope_extras(
                _client: NotebookLMClient, notebook_id: str
            ) -> dict[str, str]:
                return {"notebook_id": notebook_id}

            spec = ListSpec(
                title="Notes in {notebook_id}",
                items_key="notes",
                fetch=fetch_notes,
                serialize=lambda n: {
                    "id": n.id,
                    "title": n.title or "Untitled",
                    "preview": (n.content or "")[:100],
                },
                columns=["ID", "Title", "Preview"],
                column_options={
                    "ID": {"style": "cyan", "no_wrap": True},
                    "Title": {"style": "green"},
                    "Preview": {"style": "dim", "max_width": 50},
                },
                row=lambda n: [
                    n.id,
                    n.title or "Untitled",
                    (
                        f"{n.content[:50]}..."
                        if len(n.content or "") > 50
                        else (n.content[:50] if n.content else "")
                    ),
                ],
                envelope_extras=envelope_extras,
                include_index=False,
                empty_message="[yellow]No notes found[/yellow]",
            )
            render_list(
                await prepare_list(
                    spec,
                    client,
                    notebook_id=nb_id_resolved,
                    limit=None,
                    json_output=json_output,
                )
            )

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
        raise click.UsageError(  # cli-input-validation: positional CONTENT and --content are mutually exclusive
            "Cannot use both the positional CONTENT argument and --content. Choose one."
        )
    if content_flag is not None:
        content = content_flag
    if content == "-":
        content = read_stdin_text(source_label="content")

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            create_result = await execute_note_create(
                client,
                nb_id,
                title,
                content,
                resolve_notebook_id=resolve_notebook_id,
                json_output=json_output,
            )
            nb_id_resolved = create_result.notebook_id
            new_id = create_result.note_id

            if json_output:
                if create_result.created:
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

            if create_result.raw:
                cli_print("[green]Note created[/green]", ctx=ctx)
                cli_print(create_result.raw, ctx=ctx)
            else:
                cli_print("[yellow]Creation may have failed[/yellow]", ctx=ctx)

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
            get_result = await execute_note_get(
                client,
                nb_id,
                note_id,
                resolve_notebook_id=resolve_notebook_id,
                resolve_note_id=resolve_note_id,
                json_output=json_output,
            )
            nb_id_resolved = get_result.notebook_id
            resolved_id = get_result.note_id
            n = get_result.note

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
            # (which would change the shared error helper's typing contract).
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
            cli_print("[yellow]Provide --title and/or --content[/yellow]", ctx=ctx)

        return _no_changes()

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            save_result = await execute_note_save(
                client,
                nb_id,
                note_id,
                title=title,
                content=content,
                resolve_notebook_id=resolve_notebook_id,
                resolve_note_id=resolve_note_id,
                json_output=json_output,
            )
            nb_id_resolved = save_result.notebook_id
            resolved_id = save_result.note_id

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

            cli_print(f"[green]Note updated:[/green] {resolved_id}", ctx=ctx)

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
            # The rename core resolves both ids, fetches the current note to
            # preserve its content, and performs the update. The note may have
            # disappeared between ``resolve_note_id`` and the content-preserving
            # ``get`` (e.g. a concurrent ``note delete`` won the race), in which
            # case ``found`` is ``False``. We funnel that through the same
            # typed-error path as ``note get``'s Path B (resolve→missing) rather
            # than the previous exit-0 ``{renamed: false, error: ...}``
            # placeholder so ``set -e`` / ``check_call`` callers can branch on
            # the exit code without parsing prose. See
            # ``docs/cli-exit-codes.md`` and the BREAKING entry in
            # ``CHANGELOG.md`` (Unreleased → Changed).
            rename_result = await execute_note_rename(
                client,
                nb_id,
                note_id,
                new_title,
                resolve_notebook_id=resolve_notebook_id,
                resolve_note_id=resolve_note_id,
                json_output=json_output,
            )
            nb_id_resolved = rename_result.notebook_id
            resolved_id = rename_result.note_id
            if not rename_result.found:
                _output_error(
                    "Note not found",
                    code="NOT_FOUND",
                    json_output=json_output,
                    exit_code=1,
                    extra={"id": resolved_id, "notebook_id": nb_id_resolved},
                )
                raise AssertionError("unreachable")  # pragma: no cover

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

            cli_print(f"[green]Note renamed:[/green] {new_title}", ctx=ctx)

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

            async def resolve_delete(client):
                nb_id_resolved, resolved_id = await resolve_note_for_delete(
                    client,
                    nb_id,
                    note_id,
                    resolve_notebook_id=resolve_notebook_id,
                    resolve_note_id=resolve_note_id,
                    json_output=json_output,
                )

                # In JSON mode, refuse to prompt: ``click.confirm`` writes to
                # stdout, which would corrupt the parseable JSON contract callers
                # rely on. Preserve the typed error + exit-1 contract.
                if json_output and not yes:
                    _output_error(
                        "Pass --yes to confirm deletion in --json mode",
                        code="VALIDATION_ERROR",
                        json_output=json_output,
                        exit_code=1,
                        extra={"id": resolved_id, "notebook_id": nb_id_resolved},
                    )
                    raise AssertionError("unreachable")  # pragma: no cover

                return {"notebook_id": nb_id_resolved, "note_id": resolved_id}

            async def execute_delete(client, resolved):
                await execute_note_delete(client, resolved["notebook_id"], resolved["note_id"])

            plan = MutationPlan(
                entity_label="note",
                resolve=resolve_delete,
                confirm_message="Delete note {resolved[note_id]}?",
                execute=execute_delete,
                serialize_success=lambda resolved: {
                    "id": resolved["note_id"],
                    "notebook_id": resolved["notebook_id"],
                    "deleted": True,
                },
                serialize_cancel=lambda resolved: {
                    "id": resolved["note_id"],
                    "notebook_id": resolved["notebook_id"],
                    "deleted": False,
                    "status": "cancelled",
                },
            )
            result = await run_confirmed_mutation(
                plan,
                client,
                yes=yes,
                json_output=json_output,
                confirmer=click.confirm,
            )
            if result.status == "cancelled":
                return

            if json_output:
                json_output_response(result.payload)
                return

            resolved_id = result.resolved["note_id"]
            cli_print(f"[green]Deleted note:[/green] {resolved_id}", ctx=ctx)

    return _run()
