"""Service layer for ``research wait`` (ADR-008 Click-to-service extraction).

The CLI ``research wait`` command was a 130-line Click handler that mixed
plan construction (parsing flags + validation), wait orchestration, and I/O
rendering (spinner + text/JSON output + exit codes). This module owns the plan
and orchestration; the Click
handler in ``cli/research_cmd.py`` owns the rendering and exit-code
decisions.

Contract
--------

* :class:`ResearchWaitPlan` — frozen dataclass of user inputs.
* :class:`ResearchWaitResult` — discriminated result returned to the handler
  (``outcome`` ∈ ``{"no_research", "timeout", "failed", "completed"}``).
* :func:`execute_research_wait` — async orchestrator. Pure with respect to
  CLI I/O: it never calls ``console.print``, ``click.echo``, or
  ``exit_with_code``. It MAY call the injected ``import_sources`` callable
  which currently emits log messages and (in text mode) its own Rich
  status spinner; that I/O is part of the importer, not this service.

Task-id pinning (P1.T2)
-----------------------

The protocol-level pinning invariant lives in
``ResearchAPI.wait_for_completion``. This service delegates the wait loop to
the Python API so CLI and library callers share the same cross-wire guard.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..research_import import ResearchImportResult, import_research_sources
from ..resolve import resolve_notebook_id

ResearchWaitOutcome = Literal["no_research", "timeout", "failed", "completed"]


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
    outcomes (``no_research``, ``timeout``, ``failed``) are converted into the
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
    """Resolve, wait for completion, and optionally import.

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
        terminal states. The service NEVER raises ``SystemExit`` and
        NEVER prints — the handler decides exit codes and rendering.

    Notes:
        * Task-id pinning is handled by
          ``client.research.wait_for_completion``.
        * Import is only invoked when ``plan.import_all`` is true AND the
          completed status has sources AND a ``task_id`` was discovered.
          (The third guard preserves the pre-extraction handler's behavior
          exactly — without a task_id the importer has nothing to verify
          against.)
    """
    nb_id_resolved = await resolve_id(client, plan.notebook_id, json_output=plan.json_output)

    async with wait_context():
        try:
            status = await client.research.wait_for_completion(
                nb_id_resolved,
                timeout=float(plan.timeout),
                initial_interval=float(plan.interval),
            )
        except TimeoutError:
            return ResearchWaitResult(
                outcome="timeout",
                notebook_id=nb_id_resolved,
                timeout=plan.timeout,
            )

    task_id = status.task_id or None

    def _terminal(outcome: ResearchWaitOutcome, **extra: Any) -> ResearchWaitResult:
        return ResearchWaitResult(
            outcome=outcome,
            notebook_id=nb_id_resolved,
            timeout=plan.timeout,
            task_id=task_id,
            **extra,
        )

    status_val = status.status.value
    query = status.query
    # ``ResearchWaitResult`` / ``import_research_sources`` consume the legacy
    # ``list[dict]`` source shape, so serialize the typed sources here.
    sources = [src.to_public_dict() for src in status.sources]
    report = status.report

    if status_val == "no_research":
        return _terminal("no_research")
    if status_val == "failed":
        return _terminal(
            "failed",
            query=query,
            sources=sources,
            report=report,
        )

    # wait_for_completion only returns completed/no_research/failed; keep a
    # narrow fallback so future terminal statuses cannot be rendered as success.
    if status_val != "completed":
        return _terminal("failed", query=query, sources=sources, report=report)

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
