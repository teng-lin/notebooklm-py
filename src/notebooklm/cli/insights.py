"""Insights and analytics CLI commands.

Commands:
    summary    Get notebook summary with AI-generated insights
    analytics  Get notebook analytics
    research   Start a research session
"""

import click

from ..client import NotebookLMClient
from .helpers import (
    console,
    require_notebook,
    with_client,
)


def register_insights_commands(cli):
    """Register insights commands on the main CLI group."""

    @cli.command("summary")
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set)",
    )
    @click.option("--topics", is_flag=True, help="Include suggested topics")
    @with_client
    def summary_cmd(ctx, notebook_id, topics, client_auth):
        """Get notebook summary with AI-generated insights.

        \b
        Examples:
          notebooklm summary              # Summary only
          notebooklm summary --topics     # With suggested topics
        """
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                description = await client.notebooks.get_description(notebook_id)
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

    @cli.command("analytics")
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set)",
    )
    @with_client
    def analytics_cmd(ctx, notebook_id, client_auth):
        """Get notebook analytics."""
        notebook_id = require_notebook(notebook_id)

        async def _run():
            async with NotebookLMClient(client_auth) as client:
                analytics = await client.notebooks.get_analytics(notebook_id)
                if analytics:
                    console.print("[bold cyan]Analytics:[/bold cyan]")
                    console.print(analytics)
                else:
                    console.print("[yellow]No analytics available[/yellow]")

        return _run()

    @cli.command("research")
    @click.argument("query")
    @click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set)",
    )
    @click.option("--source", type=click.Choice(["web", "drive"]), default="web")
    @click.option("--mode", type=click.Choice(["fast", "deep"]), default="fast")
    @click.option("--import-all", is_flag=True, help="Import all found sources")
    @with_client
    def research_cmd(ctx, query, notebook_id, source, mode, import_all, client_auth):
        """Start a research session."""
        notebook_id = require_notebook(notebook_id)

        async def _run():
            import time

            async with NotebookLMClient(client_auth) as client:
                console.print(
                    f"[yellow]Starting {mode} research on {source}...[/yellow]"
                )
                result = await client.research.start(notebook_id, query, source, mode)
                if not result:
                    console.print("[red]Research failed to start[/red]")
                    raise SystemExit(1)

                task_id = result["task_id"]
                console.print(f"[dim]Task ID: {task_id}[/dim]")

                status = None
                for _ in range(60):
                    status = await client.research.poll(notebook_id)
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
                            notebook_id, task_id, sources
                        )
                        console.print(f"[green]Imported {len(imported)} sources[/green]")
                else:
                    console.print(f"[yellow]Status: {status.get('status', 'unknown')}[/yellow]")

        return _run()
