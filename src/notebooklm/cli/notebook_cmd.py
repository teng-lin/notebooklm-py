"""Notebook management CLI commands.

Commands:
    list       List all notebooks
    create     Create a new notebook
    delete     Delete a notebook
    rename     Rename a notebook
    summary    Get notebook summary with AI-generated insights
    metadata   Export notebook metadata with sources list

Note: Sharing commands moved to 'share' command group.
"""

import click

from ..client import NotebookLMClient
from .auth_runtime import with_client
from .context import clear_context, get_current_notebook, set_current_notebook
from .error_handler import _output_error
from .options import json_option, list_options, notebook_option
from .rendering import cli_print, console, json_output_response, render_list
from .resolve import require_notebook, resolve_notebook_id
from .services.confirming_mutation import MutationPlan, run_confirmed_mutation
from .services.listing import ListSpec, prepare_list


def register_notebook_commands(cli):
    """Register notebook commands on the main CLI group."""

    @cli.command("list")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @list_options
    @with_client
    def list_cmd(ctx, json_output, limit, no_truncate, client_auth):
        """List all notebooks.

        \b
        Pagination & display:
          --limit N         Show at most N notebooks (default: unlimited).
          --no-truncate     Do not truncate the Title column in the table view.
        """

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                spec = ListSpec(
                    title="Notebooks",
                    items_key="notebooks",
                    fetch=lambda client, _: client.notebooks.list(),
                    serialize=lambda nb: {
                        "id": nb.id,
                        "title": nb.title,
                        "is_owner": nb.is_owner,
                        "created_at": nb.created_at.isoformat() if nb.created_at else None,
                    },
                    columns=["ID", "Title", "Owner", "Created"],
                    row=lambda nb: [
                        nb.id,
                        nb.title,
                        "Owner" if nb.is_owner else "Shared",
                        nb.created_at.strftime("%Y-%m-%d") if nb.created_at else "-",
                    ],
                )
                render_list(
                    await prepare_list(
                        spec,
                        client,
                        notebook_id="",
                        limit=limit,
                        json_output=json_output,
                        no_truncate=no_truncate,
                    )
                )

        return _run()

    @cli.command("create")
    @click.argument("title")
    @click.option(
        "--use",
        "-u",
        "switch_context",
        is_flag=True,
        help="Set the new notebook as the current context (like 'notebooklm use <id>').",
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @with_client
    def create_cmd(ctx, title, switch_context, json_output, client_auth):
        """Create a new notebook.

        By default, creates the notebook without changing the active context.
        Pass --use (or -u) to make the new notebook the current context, so
        subsequent commands like 'source add' target it.
        """

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                nb = await client.notebooks.create(title)

                if switch_context:
                    created_str = nb.created_at.strftime("%Y-%m-%d") if nb.created_at else None
                    set_current_notebook(nb.id, nb.title, nb.is_owner, created_str)

                if json_output:
                    data: dict = {
                        "notebook": {
                            "id": nb.id,
                            "title": nb.title,
                            "created_at": nb.created_at.isoformat() if nb.created_at else None,
                        }
                    }
                    # When --use switched the active context, surface the new
                    # active notebook id at the top level so callers can
                    # branch on the field without scraping the "Context set
                    # to ..." prose or round-tripping through `status --json`.
                    if switch_context:
                        data["active_notebook_id"] = nb.id
                    json_output_response(data)
                    return

                cli_print(f"[green]Created notebook:[/green] {nb.id} - {nb.title}", ctx=ctx)
                if switch_context:
                    cli_print("[dim]Context set to new notebook[/dim]", ctx=ctx)
                else:
                    cli_print(
                        f"[dim]Tip: pass --use next time, or run 'notebooklm use {nb.id}'.[/dim]",
                        ctx=ctx,
                    )

        return _run()

    @cli.command("delete")
    @notebook_option
    @click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
    @json_option
    @with_client
    def delete_cmd(ctx, notebook_id, yes, json_output, client_auth):
        """Delete a notebook.

        Supports partial IDs - 'notebooklm delete -n abc' matches 'abc123...'
        """
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:

                async def resolve_delete(client):
                    resolved_id = await resolve_notebook_id(
                        client, notebook_id, json_output=json_output
                    )

                    # In JSON mode, refuse to prompt: ``click.confirm`` writes to
                    # stdout, which would corrupt the parseable JSON contract callers
                    # rely on. Require --yes and emit a structured error otherwise.
                    if json_output and not yes:
                        _output_error(
                            "Pass --yes to confirm deletion in --json mode",
                            code="VALIDATION_ERROR",
                            json_output=json_output,
                            exit_code=1,
                            extra={"notebook_id": resolved_id, "success": False},
                        )
                        raise AssertionError("unreachable")  # pragma: no cover

                    return {"notebook_id": resolved_id, "success": False}

                async def execute_delete(client, resolved):
                    resolved["success"] = bool(
                        await client.notebooks.delete(resolved["notebook_id"])
                    )

                plan = MutationPlan(
                    entity_label="notebook",
                    resolve=resolve_delete,
                    confirm_message="Delete notebook {resolved[notebook_id]}?",
                    execute=execute_delete,
                    serialize_success=lambda resolved: {
                        "notebook_id": resolved["notebook_id"],
                        "success": bool(resolved["success"]),
                    },
                    serialize_cancel=lambda resolved: {
                        "notebook_id": resolved["notebook_id"],
                        "success": False,
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

                resolved_id = result.resolved["notebook_id"]
                success = bool(result.resolved["success"])

                # Clear context if we deleted the current notebook. Do this
                # regardless of output mode so JSON callers don't leave a stale
                # active-notebook pointer behind.
                if success and get_current_notebook() == resolved_id:
                    clear_context()
                    context_cleared = True
                else:
                    context_cleared = False

                if json_output:
                    payload = dict(result.payload)
                    if context_cleared:
                        payload["context_cleared"] = True
                    json_output_response(payload)
                    return

                if success:
                    cli_print(f"[green]Deleted notebook:[/green] {resolved_id}", ctx=ctx)
                    if context_cleared:
                        cli_print("[dim]Cleared current notebook context[/dim]", ctx=ctx)
                else:
                    cli_print("[yellow]Delete may have failed[/yellow]", ctx=ctx)

        return _run()

    @cli.command("rename")
    @click.argument("new_title")
    @notebook_option
    @with_client
    def rename_cmd(ctx, new_title, notebook_id, client_auth):
        """Rename a notebook.

        NOTEBOOK_ID supports partial matching (e.g., 'abc' matches 'abc123...').
        """
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                resolved_id = await resolve_notebook_id(client, notebook_id)
                await client.notebooks.rename(resolved_id, new_title)
                cli_print(f"[green]Renamed notebook:[/green] {resolved_id}", ctx=ctx)
                cli_print(f"[bold]New title:[/bold] {new_title}", ctx=ctx)

        return _run()

    @cli.command("summary")
    @notebook_option
    @click.option("--topics", is_flag=True, help="Include suggested topics")
    @with_client
    def summary_cmd(ctx, notebook_id, topics, client_auth):
        """Get notebook summary with AI-generated insights.

        NOTEBOOK_ID supports partial matching (e.g., 'abc' matches 'abc123...').

        \b
        Examples:
          notebooklm summary              # Summary only
          notebooklm summary --topics     # With suggested topics
        """
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                resolved_id = await resolve_notebook_id(client, notebook_id)
                description = await client.notebooks.get_description(resolved_id)
                if description and description.summary:
                    console.print("[bold cyan]Summary:[/bold cyan]")
                    console.print(description.summary)

                    if topics and description.suggested_topics:
                        console.print("\n[bold cyan]Suggested Topics:[/bold cyan]")
                        for i, topic in enumerate(description.suggested_topics, 1):
                            console.print(f"  {i}. {topic.question}")
                else:
                    console.print("[yellow]No summary available[/yellow]")

        return _run()

    @cli.command("metadata")
    @notebook_option
    @click.option(
        "--json",
        "json_output",
        is_flag=True,
        help="Output as JSON (default: human-readable)",
    )
    @with_client
    def metadata_cmd(ctx, notebook_id, json_output, client_auth):
        """Export notebook metadata with sources list.

        Outputs notebook details (id, title, created_at, is_owner) along with
        a simplified list of sources (type, title, url).

        By default, outputs in human-readable format. Use --json for machine parsing.

        NOTEBOOK_ID supports partial matching (e.g., 'abc' matches 'abc123...').

        \b
        Examples:
          notebooklm metadata              # Human-readable for current notebook
          notebooklm metadata -n abc       # Human-readable for notebook starting with 'abc'
          notebooklm metadata --json       # JSON output
          notebooklm metadata -n abc --json  # JSON for specific notebook
        """
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                # Resolve partial ID
                resolved_id = await resolve_notebook_id(
                    client, notebook_id, json_output=json_output
                )

                # Get metadata (use notebooks.get_metadata)
                metadata = await client.notebooks.get_metadata(resolved_id)

                if json_output:
                    # JSON output
                    data = metadata.to_dict()
                    json_output_response(data)
                else:
                    # Human-readable output (default)
                    console.print(f"[bold cyan]Notebook:[/bold cyan] {metadata.title}")
                    console.print(f"[dim]ID:[/dim] {metadata.id}")
                    if metadata.created_at:
                        console.print(
                            f"[dim]Created:[/dim] {metadata.created_at.strftime('%Y-%m-%d %H:%M')}"
                        )
                    owner_status = "Owner" if metadata.is_owner else "Shared"
                    console.print(f"[dim]Access:[/dim] {owner_status}")

                    console.print(f"\n[bold]Sources ({len(metadata.sources)}):[/bold]")
                    if not metadata.sources:
                        console.print("[dim]  No sources[/dim]")
                    else:
                        for i, source in enumerate(metadata.sources, 1):
                            source_type = source.kind.value
                            title = source.title or "(untitled)"

                            # Always print the source line (use Text to avoid Rich markup interpretation)
                            from rich.text import Text

                            console.print(
                                Text(f"  {i}. "),
                                Text(f"[{source_type}]", style="default"),
                                Text(f" {title}"),
                            )
                            if source.url:
                                console.print(f"     {source.url}")

        return _run()
