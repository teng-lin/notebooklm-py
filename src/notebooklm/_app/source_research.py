"""Transport-neutral ``source add-research`` business logic.

This is the Click-free core of ``cli/services/source_research.py``: it owns the
research **start → wait → optional import** workflow and the post-parse
flag-combination validation, returning a discriminated
:class:`SourceAddResearchResult` and raising the public
``notebooklm.exceptions`` hierarchy on bad inputs. Every transport adapter (the
Click CLI today, the FastMCP server / future HTTP later) drives this core and
renders the typed result into its own envelope vocabulary + exit-code policy.

Two boundary-imposed shapes are worth calling out:

* **The importer is injected, never imported.** The shared importer lives in
  ``cli/research_import.py`` and pulls in ``rich`` (status spinners), so this
  module cannot import it without breaking the ``_app`` boundary
  (``tests/_guardrails/test_app_boundary.py``). Instead
  :func:`execute_source_add_research` takes an ``import_sources`` callable and
  the CLI wrapper passes its own ``import_research_sources`` in — which also
  preserves the ``monkeypatch.setattr(source_research, "import_research_sources",
  ...)`` test seam (the wrapper reads the name at call time).
* **The import result is structural.** :attr:`SourceAddResearchResult.import_result`
  is typed against the :class:`ResearchImportOutcome` Protocol (``imported`` /
  ``sources`` / ``cited_selection``) rather than the concrete
  ``cli.research_import.ResearchImportResult`` so this module stays
  transport-neutral.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from ..exceptions import ValidationError

if TYPE_CHECKING:
    from ..client import NotebookLMClient

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

# Pinned at 5 seconds to preserve the historical ``source add-research`` poll
# cadence. ``timeout`` is passed straight through as the wait budget; see
# :func:`execute_source_add_research`.
_POLL_INTERVAL_S = 5


@runtime_checkable
class ResearchImportOutcome(Protocol):
    """Structural view of an import result consumed by the render layer.

    The concrete type is ``cli.research_import.ResearchImportResult`` (a
    ``rich``-coupled module), so this neutral Protocol captures only the three
    fields the command-layer ``--json`` envelope and text renderer read off it.
    """

    @property
    def imported(self) -> list[dict[str, str]]: ...

    @property
    def sources(self) -> list[dict[str, Any]]: ...

    @property
    def cited_selection(self) -> Any: ...


#: Signature of the injected source importer (the CLI passes
#: ``cli.research_import.import_research_sources``). Keyword arguments
#: (``report`` / ``cited_only`` / ``max_elapsed`` / ``json_output``) are
#: forwarded verbatim, so this stays ``Callable[..., Awaitable[...]]``.
ImportSourcesFn = Callable[..., Awaitable[ResearchImportOutcome]]


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
    import_result: ResearchImportOutcome | None = None


def validate_add_research_flags(*, import_all: bool, cited_only: bool, no_wait: bool) -> None:
    """Validate the ``source add-research`` flag combinations.

    Two combinations are rejected (both raise :class:`ValidationError`; the
    adapter maps that to its own surface — the CLI to a ``UsageError`` / the
    ``--json`` ``VALIDATION_ERROR`` envelope):

    * ``--cited-only`` without ``--import-all`` — cited filtering only applies
      while importing, so on its own it is a no-op the user almost certainly
      did not intend.
    * ``--no-wait`` with ``--import-all`` — importing requires the completed
      research, which ``--no-wait`` never waits for, so the combination is
      silently broken.

    Raising here (rather than ``click.UsageError``) keeps the rule
    transport-neutral; the CLI command catches :class:`ValidationError` and
    re-raises the Click-shaped error per ADR-0015.
    """
    if cited_only and not import_all:
        raise ValidationError("--cited-only requires --import-all")
    if no_wait and import_all:
        raise ValidationError(
            "--import-all requires --wait (the default) or a separate "
            "'research wait --import-all' after --no-wait."
        )


async def execute_source_add_research(
    client: NotebookLMClient,
    plan: SourceAddResearchPlan,
    *,
    import_sources: ImportSourcesFn,
) -> SourceAddResearchResult:
    """Start research, poll until completion, and optionally import sources.

    Returns a :class:`SourceAddResearchResult` whose ``outcome`` discriminates
    every terminal state:

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
    awaits and the injected ``import_sources`` callable: it never touches a
    console or exit code. The command handler owns rendering and exit-code
    policy per ADR-0008.

    Args:
        client: An open :class:`~notebooklm.client.NotebookLMClient`.
        plan: The validated, transport-neutral request.
        import_sources: The source importer to invoke under ``--import-all``.
            The CLI passes ``cli.research_import.import_research_sources``; it is
            injected (not imported) so this module stays ``rich``-free and the
            CLI's ``monkeypatch.setattr`` seam keeps landing.

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

    start_task_id = result.task_id
    # Deep research polls under the report id returned in slot 1 of the
    # START_DEEP_RESEARCH response; the first slot is not stable for
    # POLL_RESEARCH / IMPORT_RESEARCH.
    task_id = result.report_id if plan.mode == "deep" else start_task_id
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
            initial_interval=float(_POLL_INTERVAL_S),
        )
    except TimeoutError:
        return SourceAddResearchResult(
            outcome="timeout",
            plan=plan,
            start_task_id=start_task_id,
            poll_task_id=task_id,
        )

    status_val = status.status.value
    # ``import_sources`` / ``SourceAddResearchResult`` consume the legacy
    # ``list[dict]`` source shape, so serialize the typed sources here.
    sources = [src.to_public_dict() for src in status.sources]
    report = status.report or ""

    if status_val == "completed":
        import_result: ResearchImportOutcome | None = None
        if plan.import_all and sources and task_id:
            import_kwargs: dict[str, Any] = {
                "report": report,
                "cited_only": plan.cited_only,
                "max_elapsed": plan.timeout,
            }
            if plan.json_output:
                import_kwargs["json_output"] = True
            import_result = await import_sources(
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
    "ImportSourcesFn",
    "ResearchImportOutcome",
    "SearchMode",
    "SearchSource",
    "SourceAddResearchOutcome",
    "SourceAddResearchPlan",
    "SourceAddResearchResult",
    "execute_source_add_research",
    "validate_add_research_flags",
]
