"""Research management CLI commands.

Commands:
    status      Check research status (single check)
    wait        Wait for research to complete (blocking)
    import      Import a completed run's sources (no wait for the run)
    cancel      Cancel an in-flight research run (fire-and-forget)

The ``wait`` command is a thin Click handler over
:func:`notebooklm.cli.services.research.execute_research_wait`, which
injects the CLI notebook resolver, source importer, and wait context into
the transport-neutral :mod:`notebooklm._app.research` core. Task-id
pinning is handled by ``ResearchAPI.wait_for_completion``. ``status`` and
``cancel`` are thin handlers over the same neutral core
(:func:`notebooklm._app.research.poll_and_classify` /
:func:`~notebooklm._app.research.cancel_research`). ``import`` is the
never-waits-for-the-run counterpart to ``wait --import-all`` (#2206): it drives the
same neutral pair the MCP ``research_import`` tool and the REST import
route drive (:func:`~notebooklm._app.research.classify_importable_research`
+ :func:`~notebooklm._app.research.import_research_sources`), so the
importable-state ladder and the idempotency contract cannot fork a third
time. This module owns input validation, spinner I/O, rendering, and exit
codes.
"""

from typing import Any

import click

from .._app.research import (
    ResearchImportOutcome,
    ResearchStatusResult,
    cancel_research,
    classify_importable_research,
    discover_and_classify,
    poll_and_classify,
    validate_research_wait_flags,
)
from .._app.research import (
    import_research_sources as import_research_sources_core,
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
from .research_import import (
    _display_cited_import_selection,
    _select_research_sources_for_import,
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

#: Default ``research import --timeout``; matches ``research wait --timeout``.
_DEFAULT_IMPORT_TIMEOUT = 1800


@click.group()
def research():
    """Research management commands.

    \b
    Commands:
      discover  Find web sources for a query synchronously (one call)
      status    Check research status (non-blocking)
      wait      Wait for research to complete (blocking)
      import    Import a completed run's sources (no wait for the run)
      cancel    Cancel an in-flight research run (fire-and-forget)

    \b
    Use 'source add-research' to start a research session.
    These commands are for monitoring ongoing research.

    \b
    Example workflow:
      notebooklm source add-research "AI" --mode deep --no-wait
      notebooklm research status
      notebooklm research wait --import-all

    \b
    Or own the polling cadence yourself, never blocking:
      notebooklm source add-research "AI" --mode deep --no-wait
      notebooklm research status        # your loop, your interval
      notebooklm research import
    """
    pass


@research.command("discover")
@click.argument("query", required=False, default="")
@notebook_option
@click.option(
    "--mode",
    type=click.Choice(["default", "raw", "curious", "curious_raw"], case_sensitive=False),
    default="default",
    show_default=True,
    help=(
        "Search mode. 'curious' and 'curious_raw' let the backend pick a topic "
        "and accept an empty QUERY."
    ),
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def research_discover(ctx, query, notebook_id, mode, json_output, client_auth):
    """Discover web sources for QUERY in one synchronous call.

    Unlike 'source add-research', this blocks for the single round trip
    (about 8 seconds) and prints the ranked sources with the backend's overview.
    The run is also recorded, so its run id works with 'research import'.

    \b
    Examples:
      notebooklm research discover "history of the transistor"
      notebooklm research discover --mode curious
      notebooklm research discover "quantum error correction" --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            result = await discover_and_classify(client, nb_id_resolved, query, mode)
            if json_output:
                json_output_response(result.public_dict)
                return
            _render_status_result(result)
            console.print(f"[dim]Run id: {result.task_id}[/dim]")

    return _run()


@research.command("status")
@notebook_option
@click.option(
    "--run-id",
    "--task-id",
    "run_id",
    default=None,
    help="Poll one exact research run instead of the notebook's unfiltered history.",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def research_status(ctx, notebook_id, run_id, json_output, client_auth):
    """Check research status for the current notebook.

    Shows whether research is in progress, completed, or not running.

    \b
    Examples:
      notebooklm research status
      notebooklm research status --run-id <run_id>
      notebooklm research status --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            result = await poll_and_classify(client, nb_id_resolved, run_id)

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

        # The run is already completed here, so point at the command that
        # imports without waiting rather than at the blocking one (#2206).
        console.print("\n[dim]Use 'research import' to import sources[/dim]")
    else:
        console.print(f"[yellow]Status: {result.status}[/yellow]")
        # A terminal run that found nothing is not a broken run — say which it
        # was and what to do next (issue #1964). ``--json`` output is
        # deliberately untouched: it emits the byte-stable
        # ``ResearchTask.to_public_dict()`` payload, the same reason
        # ``status_code`` was kept out of it in #1922.
        _print_failure_reason(result.reason_message, result.hint)


def _print_failure_reason(reason_message: str | None, hint: str | None) -> None:
    """Print the differentiated termination reason + remediation, when present.

    Shared by the ``research status`` / ``research wait`` text renderers so the
    two cannot drift in how they explain a non-success run (issue #1964).
    """
    if reason_message:
        console.print(reason_message)
    if hint:
        console.print(f"[dim]{hint}[/dim]")


@research.command("import")
@notebook_option
@click.option(
    "--run-id",
    default=None,
    help=(
        "Run to import. Omit it and the notebook's single research run is used; "
        "with more than one run this errors rather than guessing, so pass the "
        "id ('research status' shows it)."
    ),
)
@click.option("--cited-only", is_flag=True, help="Import only report-cited sources")
@click.option(
    "--timeout",
    default=_DEFAULT_IMPORT_TIMEOUT,
    type=int,
    help=(
        "Seconds budget for the import retry loop (default: 1800). The command "
        "never waits for the RUN to finish, but IMPORT_RESEARCH itself commonly "
        "outlives a single client timeout on deep payloads and is retried with "
        "reconciliation; this bounds that. Matches 'research wait --timeout'."
    ),
)
@click.option(
    "--max-sources",
    # ``IntRange(min=1)`` rejects 0/negative at parse time, mirroring the MCP
    # tool's "omit it to import all" bound (a 0 cap would silently import
    # nothing and report success).
    type=click.IntRange(min=1),
    default=None,
    help="Import at most N sources (default: all)",
)
@click.option(
    "--allow-duplicate",
    is_flag=True,
    help="Re-add sources whose URL is already in the notebook",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def research_import(
    ctx,
    notebook_id,
    run_id,
    cited_only,
    timeout,
    max_sources,
    allow_duplicate,
    json_output,
    client_auth,
):
    """Import a completed research run's sources.

    The counterpart to 'research wait --import-all' that never waits for the RUN:
    it imports one that is ALREADY complete and fails fast when it is not. Drive
    your own polling cadence with 'research status'.

    \b
    The import RPC itself is not instant — IMPORT_RESEARCH commonly outlives a
    single client timeout on deep payloads and is retried with reconciliation,
    bounded by --timeout.

    \b
    Idempotent when the pre-import snapshot succeeds: a source whose URL is
    already in the notebook is reported as already-present instead of duplicated
    (--allow-duplicate re-adds it), so a repeat import reads as "0 new, N already
    present" rather than a no-op. Two limits are worth knowing before you re-run
    one: a deep run's report row has no URL to dedupe on and is imported every
    time, and if the snapshot itself fails the filter is skipped entirely. After
    interrupting an import, check 'source list' before re-running it.

    \b
    Examples:
      notebooklm research import
      notebooklm research import --run-id <run_id> --cited-only
      notebooklm research import --max-sources 10 --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            # ONE poll does both jobs: it resolves a bare "current run" (no
            # --run-id) AND feeds the shared importable-state ladder, so the
            # fail-fast path spends the same single RPC either way.
            status = await poll_and_classify(client, nb_id_resolved, run_id)
            resolved_run_id = run_id or status.task_id
            if not resolved_run_id:
                raise ValidationError(
                    "No research run found for this notebook; start one with "
                    "'source add-research', or pass --run-id."
                )
            # Every refuse rule (not_found / failed / not-complete / empty) lives
            # in the neutral classifier the MCP tool and REST route also drive.
            sources, report = classify_importable_research(
                status, resolved_run_id, notebook_id=nb_id_resolved
            )
            selected, cited_selection = _select_research_sources_for_import(
                sources, report, cited_only
            )
            # Selection then bounding, matching the MCP tool's order: narrow to
            # cited sources first, then cap. The cap is applied BEFORE the
            # selection is reported so the count printed is the count imported.
            if max_sources is not None:
                selected = selected[:max_sources]
            if cited_selection is not None and not json_output:
                _display_cited_import_selection(cited_selection, selected_count=len(selected))
            # The import is the one slow step here, so it gets the spinner (and
            # with it the canonical "Cancelled. Resume with: ..." SIGINT
            # envelope) the way ``research wait`` wraps its poll loop. The hint
            # is built from the RESOLVED notebook/run plus the selection flags:
            # a bare 'research import' would retarget the active notebook, go
            # ambiguous instead of re-pinning this run, and silently drop the
            # filters, so following it could import sources the user excluded.
            async with status_with_elapsed(
                f"Importing {len(selected)} sources...",
                json_output=json_output,
                resume_hint=_import_resume_hint(
                    notebook_id=nb_id_resolved,
                    run_id=resolved_run_id,
                    cited_only=cited_only,
                    max_sources=max_sources,
                    allow_duplicate=allow_duplicate,
                    timeout=timeout,
                    json_output=json_output,
                ),
            ):
                outcome = await import_research_sources_core(
                    client,
                    nb_id_resolved,
                    resolved_run_id,
                    selected,
                    allow_duplicate=allow_duplicate,
                    max_elapsed=timeout,
                )
            _render_import_result(
                outcome,
                run_id=resolved_run_id,
                sources_found=len(sources),
                sources_selected=len(selected),
                cited_selection=cited_selection,
                json_output=json_output,
            )

    return _run()


def _import_resume_hint(
    *,
    notebook_id: str,
    run_id: str,
    cited_only: bool,
    max_sources: int | None,
    allow_duplicate: bool,
    timeout: int,
    json_output: bool,
) -> str:
    """Build a resume command that re-runs THIS import, not a different one.

    Re-pins the resolved notebook and run (a bare re-run would target the active
    notebook and, with several runs in flight, fail as ambiguous) and carries
    every flag that changes WHAT is imported, so following the hint cannot widen
    the import past what the user asked for. ``--json`` rides along too: the
    cancellation envelope is machine-readable, so automation that executes the
    hint it just parsed must not get human-readable output back.
    """
    parts = ["notebooklm research import", "-n", notebook_id, "--run-id", run_id]
    if cited_only:
        parts.append("--cited-only")
    if max_sources is not None:
        parts += ["--max-sources", str(max_sources)]
    if allow_duplicate:
        parts.append("--allow-duplicate")
    if timeout != _DEFAULT_IMPORT_TIMEOUT:
        parts += ["--timeout", str(timeout)]
    if json_output:
        parts.append("--json")
    return " ".join(parts)


def _render_import_result(
    outcome: ResearchImportOutcome,
    *,
    run_id: str,
    sources_found: int,
    sources_selected: int,
    cited_selection: Any,
    json_output: bool,
) -> None:
    """Render a ``research import`` outcome (exit 0 — an idempotent skip is success).

    Keeps the CLI's own ``--json`` vocabulary rather than mirroring the MCP
    tool's: ``imported`` is a COUNT here (as in ``research wait --json``) with the
    rows under ``imported_sources``, and the idempotency split the CLI could not
    report before (#2206) rides alongside as ``already_present`` /
    ``already_present_sources``.
    """
    if json_output:
        payload: dict[str, Any] = {
            "status": (
                "already_imported"
                if not outcome.newly_imported and outcome.already_present
                else "imported"
            ),
            "run_id": run_id,
            "sources_found": sources_found,
            "sources_selected": sources_selected,
            "imported": outcome.newly_imported_count,
            "imported_sources": outcome.newly_imported,
            "already_present": outcome.already_present_count,
            "already_present_sources": outcome.already_present,
        }
        if cited_selection is not None:
            payload["cited_only"] = True
            payload["cited_only_fallback"] = cited_selection.used_fallback
        json_output_response(payload)
        return

    console.print(f"[green]Imported {outcome.newly_imported_count} sources[/green]")
    if outcome.already_present:
        console.print(
            f"[dim]{outcome.already_present_count} already present "
            f"(skipped; use --allow-duplicate to re-add)[/dim]"
        )


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
    "--run-id",
    "--task-id",
    "run_id",
    default=None,
    help="Wait for one exact research run instead of selecting from notebook history.",
)
@click.option(
    "--timeout",
    # 1800, matching ``source add-research`` (#2142). The legacy 5-minute cap sat
    # below every observed deep run: 374 s live, 358 s in
    # ``research_deep_poll_long.yaml``. Fast runs settle in seconds, so the raised
    # ceiling costs them nothing — it is a cap, not a wait.
    default=1800,
    type=int,
    help=(
        "Per-phase seconds budget for (a) the research-completion poll loop "
        "and (b) the --import-all retry loop (default: 1800). Each phase gets "
        "the full budget independently, so worst-case total wall time is up to "
        "2× this value. Matches 'source add-research --timeout' semantics."
    ),
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
    ctx,
    notebook_id,
    run_id,
    timeout,
    interval,
    import_all,
    cited_only,
    json_output,
    client_auth,
):
    """Wait for research to complete.

    Blocks until research is completed or timeout is reached.
    Useful for scripts and LLM agents that need to wait for deep research.

    \b
    Examples:
      notebooklm research wait
      notebooklm research wait --run-id <run_id>
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
        task_id=run_id,
    )

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            # Inject the wait spinner as the polling-loop context so the
            # service stays I/O-free and unit-testable. SIGINT inside the
            # spinner emits the canonical "Cancelled. Resume with: ..."
            # envelope per :func:`emit_cancelled_and_exit`.
            def _wait_context():
                resume_hint = "notebooklm research status"
                if plan.task_id is not None:
                    resume_hint += f" -n {plan.notebook_id} --run-id {plan.task_id}"
                return status_with_elapsed(
                    "Waiting for research to complete...",
                    json_output=plan.json_output,
                    resume_hint=resume_hint,
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
            # Unlike ``research status --json`` (byte-stable public dict), this
            # payload is CLI-owned, so the reason travels on the machine
            # surface too (issue #1964).
            if result.reason_message:
                failed_payload["reason_message"] = result.reason_message
            if result.hint:
                failed_payload["hint"] = result.hint
            json_output_response(failed_payload)
        else:
            if result.query:
                console.print(f"[red]Research failed:[/red] {result.query}")
            else:
                console.print("[red]Research failed[/red]")
            _print_failure_reason(result.reason_message, result.hint)
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
