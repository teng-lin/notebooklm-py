"""Service layer for ``research wait`` (ADR-008 Click-to-service extraction).

The CLI ``research wait`` command was a 130-line Click handler that mixed
plan construction (parsing flags + validation), polling-loop orchestration
(with task-id pinning per P1.T2), and I/O rendering (spinner + text/JSON
output + exit codes). This module owns the plan + orchestration; the Click
handler in ``cli/research_cmd.py`` owns the rendering and exit-code
decisions.

Contract
--------

* :class:`ResearchWaitPlan` — frozen dataclass of user inputs.
* :class:`ResearchWaitResult` — discriminated result returned to the handler
  (``outcome`` ∈ ``{"no_research", "timeout", "completed"}``).
* :func:`execute_research_wait` — async orchestrator. Pure with respect to
  CLI I/O: it never calls ``console.print``, ``click.echo``, or
  ``exit_with_code``. It MAY call the injected ``import_sources`` callable
  which currently emits log messages and (in text mode) its own Rich
  status spinner; that I/O is part of the importer, not this service.

Task-id pinning (P1.T2)
-----------------------

The first poll that returns a ``task_id`` pins it; subsequent polls pass
``task_id=<pinned>`` so a concurrent research task started mid-wait cannot
substitute its sources or report into this wait. Preserved verbatim from
the pre-extraction handler — the characterization test
``TestTaskIdPinning::test_task_id_pinned_after_first_discovery`` is the
regression guard.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..research_import import ResearchImportResult, import_research_sources
from ..resolve import resolve_notebook_id
from .polling import poll_until

ResearchWaitOutcome = Literal["no_research", "timeout", "completed"]


@dataclass(frozen=True)
class ResearchWaitPlan:
    """User-facing inputs for ``research wait``.

    Constructed by the Click handler from validated flag values. The plan is
    intentionally a value object so the handler can be tested independently
    of the service and vice-versa.
    """

    notebook_id: str
    timeout: int
    interval: int
    import_all: bool = False
    cited_only: bool = False
    json_output: bool = False


@dataclass(frozen=True)
class ResearchWaitResult:
    """Discriminated outcome of a ``research wait`` invocation.

    The handler picks the rendering path off ``outcome``; non-success
    outcomes (``no_research``, ``timeout``) are converted into the
    appropriate ``exit_with_code(1)`` by the handler. ``completed`` returns
    exit-code 0 regardless of whether ``import_result`` is populated.
    """

    outcome: ResearchWaitOutcome
    notebook_id: str
    timeout: int
    task_id: str | None = None
    query: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    report: str = ""
    import_result: ResearchImportResult | None = None

    @property
    def sources_count(self) -> int:
        return len(self.sources)


# Default context manager used when the handler does not inject a spinner —
# the service is fully runnable in unit tests with no I/O.
@contextlib.asynccontextmanager
async def _null_wait_context() -> AsyncIterator[None]:
    yield


WaitContextFactory = Callable[[], contextlib.AbstractAsyncContextManager[None]]
ResolveNotebookIdFn = Callable[..., Awaitable[str]]
ImportResearchSourcesFn = Callable[..., Awaitable[ResearchImportResult]]


async def execute_research_wait(
    plan: ResearchWaitPlan,
    *,
    client: Any,
    wait_context: WaitContextFactory = _null_wait_context,
    resolve_id: ResolveNotebookIdFn = resolve_notebook_id,
    import_sources: ImportResearchSourcesFn = import_research_sources,
) -> ResearchWaitResult:
    """Resolve, poll-with-pinned-task-id, and optionally import.

    Args:
        plan: User inputs validated by the Click handler.
        client: An open :class:`~notebooklm.client.NotebookLMClient`. The
            service does NOT open or close the client — the handler owns
            that lifecycle so multiple service calls can share one client.
        wait_context: Zero-arg factory returning an async context manager
            that wraps the polling loop. Defaults to a no-op context. The
            CLI handler injects ``status_with_elapsed(...)`` so the spinner
            and SIGINT-to-cancelled translation live inside this block.
        resolve_id: Override for :func:`notebooklm.cli.resolve.resolve_notebook_id`
            (test seam).
        import_sources: Override for
            :func:`notebooklm.cli.research_import.import_research_sources`
            (test seam).

    Returns:
        A :class:`ResearchWaitResult` whose ``outcome`` discriminates the
        three terminal states. The service NEVER raises ``SystemExit`` and
        NEVER prints — the handler decides exit codes and rendering.

    Notes:
        * Task-id pinning (P1.T2) — once the first poll returns a
          ``task_id``, subsequent polls pin to it via the ``task_id=``
          discriminator on ``client.research.poll``.
        * Import is only invoked when ``plan.import_all`` is true AND the
          completed status has sources AND a ``task_id`` was discovered.
          (The third guard preserves the pre-extraction handler's behavior
          exactly — without a task_id the importer has nothing to verify
          against.)
    """
    nb_id_resolved = await resolve_id(client, plan.notebook_id, json_output=plan.json_output)

    # Closure-captured pinned task_id. Once set, every subsequent poll
    # passes it as the discriminator — this is the P1.T2 fix.
    task_id: str | None = None

    async def _fetch_status() -> dict[str, Any]:
        nonlocal task_id
        current_status = await client.research.poll(nb_id_resolved, task_id=task_id)
        if task_id is None:
            task_id = current_status.get("task_id")
        return current_status

    def _is_terminal(current_status: dict[str, Any]) -> bool:
        status_val = current_status.get("status", "unknown")
        # Both ``no_research`` and ``completed`` terminate the poll loop;
        # the handler distinguishes them by ``outcome``. (Pre-extraction,
        # ``no_research`` exited inside the fetch closure via
        # ``exit_with_code`` — this version returns the value up and lets
        # the handler render + exit.)
        return status_val in ("no_research", "completed")

    async with wait_context():
        poll_result = await poll_until(
            _fetch_status,
            _is_terminal,
            timeout=float(plan.timeout),
            interval=float(plan.interval),
        )

    def _terminal(outcome: ResearchWaitOutcome, **extra: Any) -> ResearchWaitResult:
        """Build a terminal result with the common notebook/timeout/task_id fields."""
        return ResearchWaitResult(
            outcome=outcome,
            notebook_id=nb_id_resolved,
            timeout=plan.timeout,
            task_id=task_id,
            **extra,
        )

    # Check timeout before inspecting status — keeps control flow readable
    # (claude[bot] #5: avoid signalling that status_val matters on the
    # timeout branch).
    if poll_result.timed_out:
        return _terminal("timeout")

    status = poll_result.value
    status_val = status.get("status", "unknown")

    if status_val == "no_research":
        return _terminal("no_research")

    # status_val == "completed"
    sources = status.get("sources", [])
    query = status.get("query", "")
    report = status.get("report", "")

    import_result: ResearchImportResult | None = None
    if plan.import_all and sources and task_id:
        # In text mode the importer renders its own "Importing sources..."
        # status; in JSON mode it stays silent. The kwarg delta below mirrors
        # the pre-extraction handler exactly.
        import_kwargs: dict[str, Any] = {
            "report": report,
            "cited_only": plan.cited_only,
            "max_elapsed": plan.timeout,
        }
        if plan.json_output:
            import_kwargs["json_output"] = True
        else:
            import_kwargs["status_message"] = "Importing sources..."
        import_result = await import_sources(
            client,
            nb_id_resolved,
            task_id,
            sources,
            **import_kwargs,
        )

    return _terminal(
        "completed",
        query=query,
        sources=sources,
        report=report,
        import_result=import_result,
    )


__all__ = [
    "ResearchWaitOutcome",
    "ResearchWaitPlan",
    "ResearchWaitResult",
    "execute_research_wait",
]
