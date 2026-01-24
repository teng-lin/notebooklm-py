"""Research management CLI commands.

Commands:
    status      Check research status (single check)
    wait        Wait for research to complete (blocking)
"""

import asyncio

import click

from ..client import NotebookLMClient
from .helpers import (
    console,
    display_research_sources,
    json_output_response,
    require_notebook,
    with_client,
)
from .options import json_option


@click.group()
def research():
    """Research management commands.

    \b
    Commands:
      status    Check research status (non-blocking)
      wait      Wait for research to complete (blocking)

    \b
    Use 'source add-research' to start a research session.
    These commands are for monitoring ongoing research.

    \b
    Example workflow:
      notebooklm source add-research "AI" --mode deep --no-wait
      notebooklm research status
      notebooklm research wait --import-all
    """
    pass


@research.command("status")
@click.option(
    "-n",
    "--notebook",
    "notebook_id",
    default=None,
    help="Notebook ID (uses current if not set)",
)
@json_option
@with_client
def research_status(ctx, notebook_id, json_output, client_auth):
    """Check research status for the current notebook.

    Shows whether research is in progress, completed, or not running.

    \b
    Examples:
      notebooklm research status
      notebooklm research status --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            status = await client.research.poll(nb_id)

            if json_output:
                json_output_response(status)
                return

            status_val = status.get("status", "unknown")

            if status_val == "no_research":
                console.print("[dim]No research running[/dim]")
            elif status_val == "in_progress":
                query = status.get("query", "")
                console.print(f"[yellow]Research in progress:[/yellow] {query}")
                console.print("[dim]Use 'research wait' to wait for completion[/dim]")
            elif status_val == "completed":
                query = status.get("query", "")
                sources = status.get("sources", [])
                summary = status.get("summary", "")
                console.print(f"[green]Research completed:[/green] {query}")
                display_research_sources(sources)

                if summary:
                    console.print(f"\n[bold]Summary:[/bold]\n{summary[:500]}")

                console.print("\n[dim]Use 'research wait --import-all' to import sources[/dim]")
            else:
                console.print(f"[yellow]Status: {status_val}[/yellow]")

    return _run()


@research.command("wait")
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
    default=5,
    type=int,
    help="Seconds between status checks (default: 5)",
)
@click.option("--import-all", is_flag=True, help="Import all found sources when done")
@json_option
@with_client
def research_wait(ctx, notebook_id, timeout, interval, import_all, json_output, client_auth):
    """Wait for research to complete.

    Blocks until research is completed or timeout is reached.
    Useful for scripts and LLM agents that need to wait for deep research.

    \b
    Examples:
      notebooklm research wait
      notebooklm research wait --timeout 600 --import-all
      notebooklm research wait --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            max_iterations = max(1, timeout // interval)
            status = None
            task_id = None

            with console.status("Waiting for research to complete..."):
                for _ in range(max_iterations):
                    status = await client.research.poll(nb_id)
                    status_val = status.get("status", "unknown")

                    if status_val == "completed":
                        task_id = status.get("task_id")
                        break
                    elif status_val == "no_research":
                        if json_output:
                            json_output_response(
                                {"status": "no_research", "error": "No research running"}
                            )
                        else:
                            console.print("[red]No research running[/red]")
                        raise SystemExit(1)

                    await asyncio.sleep(interval)
                else:
                    if json_output:
                        json_output_response(
                            {"status": "timeout", "error": f"Timed out after {timeout}s"}
                        )
                    else:
                        console.print(f"[yellow]Timed out after {timeout} seconds[/yellow]")
                    raise SystemExit(1)

            # Research completed
            sources = status.get("sources", [])
            query = status.get("query", "")

            if json_output:
                result = {
                    "status": "completed",
                    "query": query,
                    "sources_found": len(sources),
                    "sources": sources,
                }
                if import_all and sources and task_id:
                    imported = await client.research.import_sources(nb_id, task_id, sources)
                    result["imported"] = len(imported)
                    result["imported_sources"] = imported
                json_output_response(result)
            else:
                console.print(f"[green]✓ Research completed:[/green] {query}")
                display_research_sources(sources)

                if import_all and sources and task_id:
                    with console.status("Importing sources..."):
                        imported = await client.research.import_sources(nb_id, task_id, sources)
                    console.print(f"[green]Imported {len(imported)} sources[/green]")

    return _run()
