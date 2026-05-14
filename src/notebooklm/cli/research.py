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
    display_report,
    display_research_sources,
    import_research_sources,
    json_output_response,
    require_notebook,
    resolve_notebook_id,
    with_client,
)

# UI-only cap for the research summary preview shown in `research status` /
# `research wait`. Unlike RPC error previews (see
# :func:`notebooklm.exceptions._truncate_response_preview`), this is a
# user-facing display cap — not a leak-prevention truncation — and intentionally
# does not respect ``NOTEBOOKLM_DEBUG`` (users can re-fetch the full summary
# with the underlying API or with `research wait --import-all`).
_SUMMARY_PREVIEW_CHARS = 500


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
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
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
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            status = await client.research.poll(nb_id_resolved)

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
                    console.print(f"\n[bold]Summary:[/bold]\n{summary[:_SUMMARY_PREVIEW_CHARS]}")

                display_report(status.get("report", ""))

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
@click.option("--cited-only", is_flag=True, help="With --import-all, import only cited sources")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def research_wait(
    ctx, notebook_id, timeout, interval, import_all, cited_only, json_output, client_auth
):
    """Wait for research to complete.

    Blocks until research is completed or timeout is reached.
    Useful for scripts and LLM agents that need to wait for deep research.

    \b
    Examples:
      notebooklm research wait
      notebooklm research wait --timeout 600 --import-all
      notebooklm research wait --import-all --cited-only
      notebooklm research wait --json
    """
    if cited_only and not import_all:
        raise click.UsageError("--cited-only requires --import-all")

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            max_iterations = max(1, timeout // interval)
            status = None
            task_id = None

            async def _poll_loop() -> bool:
                """Poll until completion or terminal state; returns True on success."""
                nonlocal status, task_id
                for _ in range(max_iterations):
                    status = await client.research.poll(nb_id_resolved)
                    status_val = status.get("status", "unknown")

                    if status_val == "completed":
                        task_id = status.get("task_id")
                        return True
                    elif status_val == "no_research":
                        if json_output:
                            json_output_response(
                                {"status": "no_research", "error": "No research running"}
                            )
                        else:
                            console.print("[red]No research running[/red]")
                        raise SystemExit(1)

                    await asyncio.sleep(interval)

                if json_output:
                    json_output_response(
                        {"status": "timeout", "error": f"Timed out after {timeout}s"}
                    )
                else:
                    console.print(f"[yellow]Timed out after {timeout} seconds[/yellow]")
                raise SystemExit(1)

            if json_output:
                await _poll_loop()
            else:
                with console.status("Waiting for research to complete..."):
                    await _poll_loop()

            # Research completed — _poll_loop either populated `status` or
            # raised SystemExit, so a non-None status is guaranteed here.
            assert status is not None
            sources = status.get("sources", [])
            query = status.get("query", "")

            report = status.get("report", "")

            if json_output:
                result = {
                    "status": "completed",
                    "query": query,
                    "sources_found": len(sources),
                    "sources": sources,
                    "report": report,
                }
                if import_all and sources and task_id:
                    import_result = await import_research_sources(
                        client,
                        nb_id_resolved,
                        task_id,
                        sources,
                        report=report,
                        cited_only=cited_only,
                        max_elapsed=timeout,
                        json_output=True,
                    )
                    if import_result.cited_selection is not None:
                        result["cited_only"] = True
                        result["cited_sources_selected"] = len(import_result.sources)
                        result["cited_only_fallback"] = import_result.cited_selection.used_fallback
                    result["imported"] = len(import_result.imported)
                    result["imported_sources"] = import_result.imported
                json_output_response(result)
            else:
                console.print(f"[green]✓ Research completed:[/green] {query}")
                display_research_sources(sources)

                display_report(report)

                if import_all and sources and task_id:
                    import_result = await import_research_sources(
                        client,
                        nb_id_resolved,
                        task_id,
                        sources,
                        report=report,
                        cited_only=cited_only,
                        max_elapsed=timeout,
                        status_message="Importing sources...",
                    )
                    console.print(f"[green]Imported {len(import_result.imported)} sources[/green]")

    return _run()
