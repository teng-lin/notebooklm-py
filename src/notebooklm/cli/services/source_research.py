"""Service for ``source add-research`` — research start + wait + import.

Owns research start orchestration and the optional ``--import-all`` step.
The protocol-level wait loop and task-id pinning live in
``ResearchAPI.wait_for_completion``. Stays in service-layer territory:
imports the rendering helpers + ``import_research_sources`` directly rather than
threading display callbacks through the executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..error_handler import exit_with_code
from ..rendering import console, display_report, display_research_sources
from ..research_import import import_research_sources

if TYPE_CHECKING:
    from ...client import NotebookLMClient

SearchSource = Literal["web", "drive"]
SearchMode = Literal["fast", "deep"]

# Pinned at 5 seconds to match the legacy ``cli/source.py`` poll cadence
# and the explanatory comment in the original ``source add-research``
# handler. ``timeout`` is divided by this value to compute the per-task
# iteration budget; see :func:`execute_source_add_research`.
_POLL_INTERVAL_S = 5


@dataclass(frozen=True)
class SourceAddResearchPlan:
    """Prepared inputs for ``execute_source_add_research``."""

    notebook_id: str
    query: str
    search_source: SearchSource
    mode: SearchMode
    import_all: bool
    cited_only: bool
    no_wait: bool
    timeout: int


async def execute_source_add_research(
    client: NotebookLMClient, plan: SourceAddResearchPlan
) -> None:
    """Start research, poll until completion, and optionally import sources.

    Exit-code contract (matches the pre-extraction handler):
        * 0 — research started + completed (or ``--no-wait`` returned early).
        * 1 — research failed to start (``research.start`` returned empty, or
          the wait API reports no active research before a task is known).

    The wait call passes the task discriminator returned by ``research.start``
    so a second research task started mid-wait (e.g. concurrent caller, web UI,
    or retry) cannot cross-wire its sources into this task's import branch.
    Deep research uses the returned ``report_id`` for polling/import because
    ``START_DEEP_RESEARCH`` slot 0 is not stable for those follow-up RPCs.
    """
    console.print(f"[yellow]Starting {plan.mode} research on {plan.search_source}...[/yellow]")
    result = await client.research.start(
        plan.notebook_id, plan.query, plan.search_source, plan.mode
    )
    if not result:
        console.print("[red]Research failed to start[/red]")
        exit_with_code(1)

    start_task_id = result["task_id"]
    # Deep research polls under the report id returned in slot 1 of the
    # START_DEEP_RESEARCH response; the first slot is not stable for
    # POLL_RESEARCH / IMPORT_RESEARCH.
    task_id = result.get("report_id") if plan.mode == "deep" else start_task_id
    task_id = task_id or start_task_id
    console.print(f"[dim]Task ID: {start_task_id}[/dim]")
    if task_id != start_task_id:
        console.print(f"[dim]Poll ID: {task_id}[/dim]")

    # Non-blocking mode: return immediately. Research will keep running
    # server-side; until something fires IMPORT_RESEARCH the NotebookLM
    # web UI will show an "Add sources?" modal (issue #315).
    if plan.no_wait:
        console.print(
            "[green]Research started.[/green] "
            "Run 'notebooklm research wait --import-all' to commit "
            "sources once it completes, otherwise the NotebookLM web "
            "UI will keep an 'Add sources?' modal open."
        )
        return

    try:
        status = await client.research.wait_for_completion(
            plan.notebook_id,
            task_id=task_id,
            timeout=float(plan.timeout),
            interval=float(_POLL_INTERVAL_S),
        )
    except TimeoutError:
        status = {"status": "timeout"}

    status_val = status.get("status", "unknown")

    if status_val == "completed":
        sources = status.get("sources", [])
        console.print()
        display_research_sources(sources)

        display_report(status.get("report", ""), json_hint=False)

        if plan.import_all and sources and task_id:
            import_result = await import_research_sources(
                client,
                plan.notebook_id,
                task_id,
                sources,
                report=status.get("report", ""),
                cited_only=plan.cited_only,
                max_elapsed=plan.timeout,
            )
            console.print(f"[green]Imported {len(import_result.imported)} sources[/green]")
    elif status_val == "no_research":
        console.print("[red]Research failed to start[/red]")
        exit_with_code(1)
    elif status_val in ("failed", "timeout"):
        message = "Research timed out" if status_val == "timeout" else "Research failed"
        console.print(f"[red]{message}[/red]")
        exit_with_code(1)
    else:
        console.print(f"[yellow]Status: {status_val}[/yellow]")
        exit_with_code(1)


__all__ = ["SourceAddResearchPlan", "execute_source_add_research"]
