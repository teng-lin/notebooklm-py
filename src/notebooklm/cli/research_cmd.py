"""Research management CLI commands.

Commands:
    status      Check research status (single check)
    wait        Wait for research to complete (blocking)
    cancel      Cancel an in-flight research run (fire-and-forget)

The ``wait`` command is a thin Click handler over
:func:`notebooklm.cli.services.research.execute_research_wait`, which
injects the CLI notebook resolver, source importer, and wait context into
the transport-neutral :mod:`notebooklm._app.research` core. Task-id
pinning is handled by ``ResearchAPI.wait_for_completion``. ``status`` and
``cancel`` are thin handlers over the same neutral core
(:func:`notebooklm._app.research.poll_and_classify` /
:func:`~notebooklm._app.research.cancel_research`). This module owns
input validation, spinner I/O, rendering, and exit codes.
"""

from typing import Any

import click

from .._app.research import (
    ResearchStatusResult,
    cancel_research,
    poll_and_classify,
    validate_research_wait_flags,
)
from ..exceptions import ValidationError
from .auth_runtime import resolve_client_factory, with_client
from .error_handler import _output_error, exit_with_code
from .options import notebook_option
from .polling_ui import status_with_elapsed
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
      cancel    Cancel an in-flight research run (fire-and-forget)

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
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            result = await poll_and_classify(client, nb_id_resolved)

            if json_output:
                # The classified result carries the canonical
                # ``ResearchTask.to_public_dict()`` payload so JSON is unchanged.
                json_output_response(result.public_dict)
                return

            _render_status_result(result)

    return _run()


def _render_status_result(result: ResearchStatusResult) -> None:
    """Render a classified ``research status`` poll in text mode."""
    if result.kind == "no_research":
        console.print("[dim]No research running[/dim]")
    elif result.kind == "in_progress":
        console.print(f"[yellow]Research in progress:[/yellow] {result.query}")
        console.print("[dim]Use 'research wait' to wait for completion[/dim]")
    elif result.kind == "completed":
        console.print(f"[green]Research completed:[/green] {result.query}")
        display_research_sources(result.sources)

        if result.summary:
            console.print(f"\n[bold]Summary:[/bold]\n{result.summary[:_SUMMARY_PREVIEW_CHARS]}")

        display_report(result.report)

        console.print("\n[dim]Use 'research wait --import-all' to import sources[/dim]")
    else:
        console.print(f"[yellow]Status: {result.status}[/yellow]")


@research.command("cancel")
@click.argument("run_id")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def research_cancel(ctx, run_id, notebook_id, json_output, client_auth):
    """Cancel an in-flight research run.

    RUN_ID is the run's poll-level id — the ``task_id`` shown by
    'research status'. For DEEP research that is the report_id from start, NOT
    the deep start task_id (which is a sessionId and will not cancel anything).

    \b
    Fire-and-forget: the server reports neither success nor failure, so a
    cancel cannot be confirmed from this command — run 'research status'
    afterward (a cancelled in-progress run shows as failed).

    \b
    Examples:
      notebooklm research cancel <run_id>
      notebooklm research cancel <run_id> --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            # Fire-and-forget: ``cancel`` returns None and does not raise on an
            # unknown id (the server returns nothing to branch on). If no
            # exception was raised, the request was accepted — confirm by polling.
            await cancel_research(client, nb_id_resolved, run_id)
            if json_output:
                json_output_response({"run_id": run_id, "cancel_requested": True})
            else:
                console.print(f"[green]Cancel requested for run:[/green] {run_id}")
                console.print(
                    "[dim]Fire-and-forget — run 'research status' to confirm "
                    "(a cancelled run shows as failed)[/dim]"
                )

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
    try:
        validate_research_wait_flags(import_all=import_all, cited_only=cited_only)
    except ValidationError as exc:
        # Per ADR-0015 §2: under --json this flag-combination conflict must
        # emit the typed JSON envelope and exit 1 (VALIDATION_ERROR), not
        # ride Click's parse-time UsageError path (exit 2, usage text on
        # stderr, no JSON on stdout). Under text mode we preserve the
        # existing Click UX so interactive users still get the
        # ``Usage: ... / Error: ...`` formatting.
        if json_output:
            _output_error(str(exc), "VALIDATION_ERROR", json_output, 1)
        raise click.UsageError(  # cli-input-validation: --cited-only requires --import-all
            str(exc)
        ) from exc

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
        async with resolve_client_factory(ctx)(client_auth) as client:
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
        # The CLI owns the ``--json`` envelope projection (``_app`` returns only
        # the typed result); ``_completed_wait_payload`` rebuilds the historical
        # completed payload (base keys + optional cited/imported keys) verbatim.
        json_output_response(_completed_wait_payload(result))
        return

    # Text mode
    console.print(f"[green]✓ Research completed:[/green] {result.query}")
    display_research_sources(result.sources)
    display_report(result.report)
    import_result = result.import_result
    if import_result is not None:
        console.print(f"[green]Imported {len(import_result.imported)} sources[/green]")


def _completed_wait_payload(result: ResearchWaitResult) -> dict[str, Any]:
    """Project a completed :class:`ResearchWaitResult` into the ``--json`` envelope.

    Lives in the CLI adapter (not ``_app``) because the keys are the CLI's own
    vocabulary (``sources_found`` / ``imported`` / ``cited_only``). Mirrors the
    historical ``research wait --json`` completed payload byte-for-byte: the
    base keys plus the optional cited-only + imported keys when an import ran.
    """
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
    return payload
