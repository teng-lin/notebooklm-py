"""Service for ``source add-research`` — research start + poll + import.

Owns the polling loop (task-id-pinned per P1.T2 bug 6) and the optional
``--import-all`` step. Stays in service-layer territory: imports the
rendering helpers + ``import_research_sources`` directly rather than
threading display callbacks through the executor — the resulting code
matches the pre-extraction Click handler line-for-line so the
characterization-test snapshots survive byte-for-byte.
"""

from __future__ import annotations

import asyncio
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
        * 1 — research failed to start (``no_research`` from the server).

    The polling loop is pinned to the ``task_id`` returned by
    ``research.start`` so a second research task started mid-wait (e.g.
    concurrent caller, web UI, or retry) cannot cross-wire its sources
    into this task's import branch (P1.T2 bug 6).
    """
    console.print(f"[yellow]Starting {plan.mode} research on {plan.search_source}...[/yellow]")
    result = await client.research.start(
        plan.notebook_id, plan.query, plan.search_source, plan.mode
    )
    if not result:
        console.print("[red]Research failed to start[/red]")
        exit_with_code(1)

    task_id = result["task_id"]
    console.print(f"[dim]Task ID: {task_id}[/dim]")

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

    # Poll budget mirrors ``research wait --timeout``: total seconds
    # divided by the 5 s interval. The legacy hardcoded 60-iteration cap
    # stranded deep research (#315) because the import branch below is
    # gated on ``status == "completed"``.
    status: dict | None = None
    for _ in range(max(1, plan.timeout // _POLL_INTERVAL_S)):
        status = await client.research.poll(plan.notebook_id, task_id=task_id)
        if status.get("status") == "completed":
            break
        elif status.get("status") == "no_research":
            console.print("[red]Research failed to start[/red]")
            exit_with_code(1)
        await asyncio.sleep(_POLL_INTERVAL_S)
    else:
        status = {"status": "timeout"}

    assert status is not None  # for mypy — loop above always assigns

    if status.get("status") == "completed":
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
    else:
        console.print(f"[yellow]Status: {status.get('status', 'unknown')}[/yellow]")


__all__ = ["SourceAddResearchPlan", "execute_source_add_research"]
