"""Research management CLI commands.

Commands:
    status      Check research status (single check)
    wait        Wait for research to complete (blocking)

The ``wait`` command is a thin Click handler over
:func:`notebooklm.cli.services.research.execute_research_wait` — the polling
loop, P1.T2 task-id pinning, and import orchestration live in the service.
This module owns input validation, spinner I/O, rendering, and exit codes.
"""

from typing import Any

import click

from ..client import NotebookLMClient
from .auth_runtime import with_client
from .error_handler import _output_error, exit_with_code
from .options import notebook_option
from .rendering import (
    console,
    display_report,
    display_research_sources,
    json_output_response,
)
from .resolve import (
    require_notebook,
    resolve_notebook_id,
)
from .services.polling import status_with_elapsed
from .services.research import (
    ResearchWaitPlan,
    ResearchWaitResult,
    execute_research_wait,
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
@notebook_option
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
@notebook_option
@click.option(
    "--timeout",
    default=300,
    type=int,
    help="Maximum seconds to wait (default: 300)",
)
@click.option(
    "--interval",
    default=5,
    # ``IntRange(min=1)`` rejects 0/negative at parse time; mirrors the
    # ``wait_polling_options`` guard in ``cli/options.py`` so every poll
    # loop in the CLI enforces a positive sleep interval.
    type=click.IntRange(min=1),
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
        # Per ADR-015 §2: under --json this flag-combination conflict must
        # emit the typed JSON envelope and exit 1 (VALIDATION_ERROR), not
        # ride Click's parse-time UsageError path (exit 2, usage text on
        # stderr, no JSON on stdout). Under text mode we preserve the
        # existing Click UX so interactive users still get the
        # ``Usage: ... / Error: ...`` formatting.
        if json_output:
            _output_error(
                "--cited-only requires --import-all",
                "VALIDATION_ERROR",
                json_output,
                1,
            )
        raise click.UsageError("--cited-only requires --import-all")

    nb_id = require_notebook(notebook_id)
    plan = ResearchWaitPlan(
        notebook_id=nb_id,
        timeout=timeout,
        interval=interval,
        import_all=import_all,
        cited_only=cited_only,
        json_output=json_output,
    )

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            # Inject the wait spinner as the polling-loop context so the
            # service stays I/O-free and unit-testable. SIGINT inside the
            # spinner emits the canonical "Cancelled. Resume with: ..."
            # envelope per :func:`emit_cancelled_and_exit`.
            def _wait_context():
                return status_with_elapsed(
                    "Waiting for research to complete...",
                    json_output=plan.json_output,
                    resume_hint="notebooklm research status",
                )

            result = await execute_research_wait(
                plan,
                client=client,
                wait_context=_wait_context,
            )
            _render_wait_result(plan, result)

    return _run()


def _render_wait_result(plan: ResearchWaitPlan, result: ResearchWaitResult) -> None:
    """Render a :class:`ResearchWaitResult` and exit on non-success outcomes.

    The handler owns all CLI I/O — text vs JSON, exit codes, "Imported N
    sources" line — so the service can stay pure (and unit-testable without
    a CliRunner).
    """
    if result.outcome == "no_research":
        if plan.json_output:
            json_output_response({"status": "no_research", "error": "No research running"})
        else:
            console.print("[red]No research running[/red]")
        exit_with_code(1)

    if result.outcome == "timeout":
        if plan.json_output:
            json_output_response(
                {"status": "timeout", "error": f"Timed out after {result.timeout}s"}
            )
        else:
            console.print(f"[yellow]Timed out after {result.timeout} seconds[/yellow]")
        exit_with_code(1)

    if result.outcome == "failed":
        if plan.json_output:
            failed_payload: dict[str, Any] = {"status": "failed", "error": "Research failed"}
            if result.query:
                failed_payload["query"] = result.query
            if result.sources:
                failed_payload["sources"] = result.sources
                failed_payload["sources_found"] = result.sources_count
            if result.report:
                failed_payload["report"] = result.report
            json_output_response(failed_payload)
        else:
            if result.query:
                console.print(f"[red]Research failed:[/red] {result.query}")
            else:
                console.print("[red]Research failed[/red]")
        exit_with_code(1)

    # outcome == "completed"
    if plan.json_output:
        payload: dict[str, Any] = {
            "status": "completed",
            "query": result.query,
            "sources_found": result.sources_count,
            "sources": result.sources,
            "report": result.report,
        }
        import_result = result.import_result
        if import_result is not None:
            if import_result.cited_selection is not None:
                payload["cited_only"] = True
                payload["cited_sources_selected"] = len(import_result.sources)
                payload["cited_only_fallback"] = import_result.cited_selection.used_fallback
            payload["imported"] = len(import_result.imported)
            payload["imported_sources"] = import_result.imported
        json_output_response(payload)
        return

    # Text mode
    console.print(f"[green]✓ Research completed:[/green] {result.query}")
    display_research_sources(result.sources)
    display_report(result.report)
    import_result = result.import_result
    if import_result is not None:
        console.print(f"[green]Imported {len(import_result.imported)} sources[/green]")
