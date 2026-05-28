"""Service for ``source add-research`` — research start + wait + import.

Owns research start orchestration and the optional ``--import-all`` step.
The protocol-level wait loop and task-id pinning live in
``ResearchAPI.wait_for_completion``. ADR-008 boundary: this module returns
a discriminated :class:`SourceAddResearchResult` and never touches
``console``/``exit_with_code`` — the command layer in
``cli/source_cmd.py`` owns rendering and exit-code policy. It MAY call the
shared importer, which emits text-mode status messages; under ``--json`` the
plan routes ``json_output=True`` into that helper so stdout stays parseable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..research_import import ResearchImportResult, import_research_sources

if TYPE_CHECKING:
    from ...client import NotebookLMClient

SearchSource = Literal["web", "drive"]
SearchMode = Literal["fast", "deep"]
SourceAddResearchOutcome = Literal[
    "started_no_wait",
    "start_failed",
    "completed",
    "no_research",
    "failed",
    "timeout",
    "unknown_status",
]

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
    json_output: bool = False


@dataclass(frozen=True)
class SourceAddResearchResult:
    """Discriminated outcome of an ``execute_source_add_research`` invocation.

    The command handler renders text or JSON off ``outcome`` and exits with
    the appropriate code. Non-success outcomes (``start_failed``,
    ``no_research``, ``failed``, ``timeout``, ``unknown_status``) map to
    exit code 1; ``completed`` and ``started_no_wait`` map to exit 0.
    """

    outcome: SourceAddResearchOutcome
    plan: SourceAddResearchPlan
    start_task_id: str | None = None
    poll_task_id: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    report: str = ""
    status: str | None = None
    import_result: ResearchImportResult | None = None


async def execute_source_add_research(
    client: NotebookLMClient, plan: SourceAddResearchPlan
) -> SourceAddResearchResult:
    """Start research, poll until completion, and optionally import sources.

    Returns a :class:`SourceAddResearchResult` whose ``outcome`` discriminates
    every terminal state the pre-extraction handler distinguished:

    * ``started_no_wait`` — ``--no-wait`` returned early after ``research.start``.
    * ``start_failed`` — ``research.start`` returned empty.
    * ``completed`` — wait finished with ``status == "completed"`` (may include
      an ``import_result`` if ``--import-all`` was active and sources were
      returned).
    * ``no_research`` — wait returned ``status == "no_research"`` (the wait
      API reports no active research before a task is known).
    * ``failed`` / ``timeout`` — wait returned ``status == "failed"`` or the
      wait API raised :class:`TimeoutError`.
    * ``unknown_status`` — wait returned an unexpected status string (the
      raw value is preserved in :attr:`SourceAddResearchResult.status`).

    The service is fully I/O-free except for the underlying ``client``
    awaits: it never calls ``console.print``, ``click.echo``, or
    ``exit_with_code``. The command handler owns rendering and exit-code
    policy per ADR-008.

    The wait call passes the task discriminator returned by ``research.start``
    so a second research task started mid-wait (e.g. concurrent caller, web UI,
    or retry) cannot cross-wire its sources into this task's import branch.
    Deep research uses the returned ``report_id`` for polling/import because
    ``START_DEEP_RESEARCH`` slot 0 is not stable for those follow-up RPCs.
    """
    result = await client.research.start(
        plan.notebook_id, plan.query, plan.search_source, plan.mode
    )
    if not result:
        return SourceAddResearchResult(outcome="start_failed", plan=plan)

    start_task_id = result["task_id"]
    # Deep research polls under the report id returned in slot 1 of the
    # START_DEEP_RESEARCH response; the first slot is not stable for
    # POLL_RESEARCH / IMPORT_RESEARCH.
    task_id = result.get("report_id") if plan.mode == "deep" else start_task_id
    task_id = task_id or start_task_id

    # Non-blocking mode: return immediately. Research will keep running
    # server-side; until something fires IMPORT_RESEARCH the NotebookLM
    # web UI will show an "Add sources?" modal (issue #315).
    if plan.no_wait:
        return SourceAddResearchResult(
            outcome="started_no_wait",
            plan=plan,
            start_task_id=start_task_id,
            poll_task_id=task_id,
        )

    try:
        status = await client.research.wait_for_completion(
            plan.notebook_id,
            task_id=task_id,
            timeout=float(plan.timeout),
            interval=float(_POLL_INTERVAL_S),
        )
    except TimeoutError:
        return SourceAddResearchResult(
            outcome="timeout",
            plan=plan,
            start_task_id=start_task_id,
            poll_task_id=task_id,
        )

    status_val = status.get("status", "unknown")
    sources = status.get("sources", []) or []
    report = status.get("report", "") or ""

    if status_val == "completed":
        import_result: ResearchImportResult | None = None
        if plan.import_all and sources and task_id:
            import_kwargs: dict[str, Any] = {
                "report": report,
                "cited_only": plan.cited_only,
                "max_elapsed": plan.timeout,
            }
            if plan.json_output:
                import_kwargs["json_output"] = True
            import_result = await import_research_sources(
                client,
                plan.notebook_id,
                task_id,
                sources,
                **import_kwargs,
            )
        return SourceAddResearchResult(
            outcome="completed",
            plan=plan,
            start_task_id=start_task_id,
            poll_task_id=task_id,
            sources=sources,
            report=report,
            import_result=import_result,
        )
    if status_val == "no_research":
        return SourceAddResearchResult(
            outcome="no_research",
            plan=plan,
            start_task_id=start_task_id,
            poll_task_id=task_id,
        )
    if status_val in ("failed", "timeout"):
        return SourceAddResearchResult(
            outcome="failed" if status_val == "failed" else "timeout",
            plan=plan,
            start_task_id=start_task_id,
            poll_task_id=task_id,
            sources=sources,
            report=report,
        )
    return SourceAddResearchResult(
        outcome="unknown_status",
        plan=plan,
        start_task_id=start_task_id,
        poll_task_id=task_id,
        sources=sources,
        report=report,
        status=status_val,
    )


__all__ = [
    "SearchMode",
    "SearchSource",
    "SourceAddResearchOutcome",
    "SourceAddResearchPlan",
    "SourceAddResearchResult",
    "execute_source_add_research",
]
