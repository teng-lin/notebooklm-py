"""Transport-neutral research status + wait business logic.

This is the Click-free core of the ``research`` command group's ``status`` and
``wait`` flows (distinct from ``source add-research``, which lives in
``_app/source_research.py``). It owns:

* :func:`poll_and_classify` → typed :class:`ResearchStatusResult` for
  ``research status`` (a single non-blocking poll classified into the render
  fields + the canonical ``--json`` public dict);
* :class:`ResearchWaitPlan` / :class:`ResearchWaitResult` /
  :func:`execute_research_wait` — the ``research wait`` orchestration (resolve →
  wait-for-completion → optional import), discriminated by ``outcome``; and
* :func:`validate_research_wait_flags` — the ``--cited-only`` requires
  ``--import-all`` check, raising the public
  :class:`~notebooklm.exceptions.ValidationError`.

This core returns only typed results — the ``--json`` envelope projection
(``sources_found`` / ``imported`` / ``cited_only`` keys) lives in the CLI
renderer, not here, so ``_app`` never replicates an adapter serializer.

Every transport adapter (the Click CLI today, the FastMCP server / future HTTP
surface tomorrow) drives this core and renders the typed result / raises into
its own surface + exit-code policy. The notebook-id resolver, the
(rich-coupled) source importer, and the wait-spinner context are **injected**
as callables so this module never imports the Click/Rich-coupled
``cli.resolve`` / ``cli.research_import`` helpers; the CLI adapter supplies the
live collaborators and preserves the ``import_research_sources`` /
``resolve_notebook_id`` patch seams.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn, Protocol

from ..exceptions import ValidationError

# ===========================================================================
# research status
# ===========================================================================

ResearchStatusKind = Literal["no_research", "in_progress", "completed", "other"]


@dataclass(frozen=True)
class ResearchStatusResult:
    """Classified outcome of a single ``research status`` poll.

    ``public_dict`` is the canonical ``ResearchTask.to_public_dict()`` payload
    the CLI emits verbatim under ``--json`` (byte-stable). The remaining fields
    drive the text-mode render; ``kind`` discriminates the render branch.

    ``task_id`` is the polled task's identifier (empty string for an unfiltered
    empty poll). A transport adapter that drives a start→status→import workflow
    (the MCP server) surfaces it so an agent can pin the same task for import.
    """

    kind: ResearchStatusKind
    status: str
    query: str
    sources: list[dict[str, Any]]
    summary: str
    report: str
    public_dict: dict[str, Any]
    task_id: str = ""
    # Raw backend status code carried through from ``ResearchTask.status_code``
    # (issue #1922, F10). ``None`` when the poll had no code (empty / not-found).
    # The MCP ``research_status`` tool surfaces it so an agent can distinguish
    # failure sub-codes the coarse ``status`` flattens into ``failed``.
    status_code: int | None = None


def _classify_status_kind(status_val: str) -> ResearchStatusKind:
    if status_val in ("no_research", "in_progress", "completed"):
        return status_val  # type: ignore[return-value]
    return "other"


async def poll_and_classify(
    client: Any, notebook_id: str, task_id: str | None = None
) -> ResearchStatusResult:
    """Poll research status once and classify it for the command layer.

    The typed ``ResearchTask`` returned by ``client.research.poll`` is
    serialized to the legacy ``list[dict]`` source shape + the canonical
    ``to_public_dict()`` so the CLI render + ``--json`` output stay unchanged.

    ``task_id`` (optional) is forwarded to ``client.research.poll`` as the
    task discriminator: when supplied, the poll selects that specific task (or
    returns the typed ``NOT_FOUND`` sentinel if it is not among the polled
    results); when ``None`` the unfiltered poll runs, which raises
    ``AmbiguousResearchTaskError`` if two or more tasks are in flight. The CLI
    ``research status`` command passes ``None`` (unchanged); the MCP
    ``research_status`` / ``research_import`` tools pass the agent-supplied id so
    start→status→import stays pinned to one task.
    """
    status = await client.research.poll(notebook_id, task_id)
    # ``ResearchStatus`` is a ``str`` enum; ``.value`` yields the canonical
    # lowercase code the CLI render branches + the original status command keyed
    # off (matches ``execute_research_wait``'s ``status.status.value``).
    status_val = status.status.value
    return ResearchStatusResult(
        kind=_classify_status_kind(status_val),
        status=status_val,
        query=status.query,
        sources=[src.to_public_dict() for src in status.sources],
        summary=status.summary,
        report=status.report,
        public_dict=status.to_public_dict(),
        task_id=status.task_id,
        status_code=status.status_code,
    )


async def poll_importable_research(
    client: Any, notebook_id: str, run_id: str
) -> tuple[list[dict[str, Any]], str]:
    """Poll a research run and return its ``(importable sources, report)``, or raise.

    The single shared importable-state guard for the "import a completed run's
    found sources" flow — driven by BOTH the MCP ``research_import`` tool and the
    REST ``POST .../research/{run_id}/import`` route so the ladder cannot drift
    between the two adapters.

    Polls FOR THE REQUESTED ``run_id`` (via :func:`poll_and_classify`, which
    forwards it to ``client.research.poll`` as the task discriminator) so the
    returned sources belong to that run — never the notebook's current (possibly
    different) research run's sources. Every non-importable state raises the
    public :class:`~notebooklm.exceptions.ValidationError` (each adapter maps it
    to its own surface + status), so an unfinished / failed / empty run is never
    imported as a partial success:

    * ``not_found`` — the pinned run is not among the polled runs (nothing to
      import; the typed ``NOT_FOUND`` sentinel, not a fallback to the current run);
    * ``failed`` — the run will not complete;
    * any non-``completed`` status (e.g. ``in_progress`` / ``no_research``) —
      only a completed run has a final source set;
    * ``completed`` with no sources — refuse the silent empty import.

    Returns the completed run's importable sources (the legacy ``list[dict]``
    shape) AND the run's report text on success. The report is returned so a
    caller doing cited-only selection (:func:`~notebooklm.research.select_cited_sources`)
    can match citations against it without a second poll; :func:`poll_sources_for_import`
    delegates here and drops the report for callers that import everything.

    The ``run`` noun is surface-neutral (the MCP tool documents it as the
    ``task_id``); the message names no adapter-specific route or tool so the one
    string reads cleanly on both surfaces.
    """
    status = await poll_and_classify(client, notebook_id, run_id)
    if status.status == "not_found":
        raise ValidationError(
            f"Research run {run_id!r} is not among notebook {notebook_id}'s research "
            "runs; nothing to import. Check its status first."
        )
    # Only a COMPLETED run has a final source set. Importing an
    # in_progress/no_research/failed snapshot would import a partial/empty set as
    # a "success" — refuse with an action-appropriate message.
    if status.status == "failed":
        raise ValidationError(
            f"Research run {run_id!r} failed; it will not complete — start a new "
            "research session rather than polling."
        )
    if status.status != "completed":
        raise ValidationError(
            f"Research run {run_id!r} is not complete (status {status.status!r}); poll "
            "its status until 'completed' before importing."
        )
    if not status.sources:
        raise ValidationError(f"Research run {run_id!r} completed with no sources to import.")
    # ``report`` is typed ``str`` upstream (``ResearchTask.report`` defaults to
    # ""), but coerce defensively so a drifted response that leaves it ``None``
    # can't violate the return type or break ``select_cited_sources``.
    return status.sources, status.report or ""


async def poll_sources_for_import(
    client: Any, notebook_id: str, run_id: str
) -> list[dict[str, Any]]:
    """Poll a research run and return its importable sources, or raise.

    Thin wrapper over :func:`poll_importable_research` that drops the report for
    callers (the REST import route) that import every source unconditionally. The
    importable-state guard ladder lives in :func:`poll_importable_research` so the
    two never drift.
    """
    sources, _report = await poll_importable_research(client, notebook_id, run_id)
    return sources


# ===========================================================================
# research cancel
# ===========================================================================


async def cancel_research(client: Any, notebook_id: str, run_id: str) -> None:
    """Cancel an in-flight research run via ``client.research.cancel``.

    Transport-neutral thin wrapper mirroring :func:`poll_and_classify`: the CLI
    / MCP / HTTP adapters drive this and render their own confirmation. The
    underlying RPC is **fire-and-forget** — the server returns nothing to
    confirm the cancel and does not validate ``run_id`` — so this returns
    ``None`` and never raises on an unknown id; callers confirm by polling
    afterward (a cancelled IN_PROGRESS run surfaces as ``FAILED``).

    ``run_id`` is the poll-level run id (``ResearchTask.task_id`` from
    :func:`poll_and_classify`). For DEEP research that is the ``report_id``
    returned by ``start`` (deep's ``start().task_id`` is a sessionId and does
    not cancel); for FAST research it is ``start().task_id``.
    """
    await client.research.cancel(notebook_id, run_id)


# ===========================================================================
# research wait
# ===========================================================================

ResearchWaitOutcome = Literal["no_research", "timeout", "failed", "completed"]


class ResearchImportLike(Protocol):
    """Structural shape of the injected importer's result.

    Defined structurally so the neutral core can read ``imported`` /
    ``sources`` / ``cited_selection`` (for the CLI ``--json`` projection)
    without importing the rich-coupled ``cli.research_import.ResearchImportResult``.
    """

    @property
    def imported(self) -> list[dict[str, str]]: ...
    @property
    def sources(self) -> list[dict[str, Any]]: ...
    @property
    def cited_selection(self) -> Any: ...


@dataclass(frozen=True)
class ResearchWaitPlan:
    """User-facing inputs for ``research wait``.

    Constructed by the Click handler from validated flag values. The plan is
    intentionally a value object so the handler can be tested independently of
    the service and vice-versa.
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

    The handler picks the rendering path off ``outcome``; non-success outcomes
    (``no_research``, ``timeout``, ``failed``) are converted into the
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
    import_result: ResearchImportLike | None = None

    @property
    def sources_count(self) -> int:
        return len(self.sources)


# Default context manager used when the handler does not inject a spinner —
# the service is fully runnable in unit tests with no I/O.
@contextlib.asynccontextmanager
async def _null_wait_context() -> AsyncIterator[None]:
    yield


async def _missing_importer(*_args: Any, **_kwargs: Any) -> NoReturn:
    """Default ``import_sources`` — fails loud if invoked without injection.

    The CLI adapter always injects ``import_research_sources``; the neutral
    default is only reachable if a caller requests an import without supplying
    an importer, which is a programming error rather than a user error.
    """
    raise RuntimeError(
        "execute_research_wait requires an injected import_sources callable to import sources"
    )


WaitContextFactory = Callable[[], contextlib.AbstractAsyncContextManager[None]]
ResolveNotebookIdFn = Callable[..., Awaitable[str]]
ImportResearchSourcesFn = Callable[..., Awaitable[ResearchImportLike]]


def validate_research_wait_flags(*, import_all: bool, cited_only: bool) -> None:
    """Validate the ``research wait`` flag combination.

    ``--cited-only`` is only meaningful alongside ``--import-all``. Raises the
    public :class:`~notebooklm.exceptions.ValidationError` so each adapter maps
    it to its own error vocabulary + exit policy (the CLI keeps the historical
    text-mode ``click.UsageError`` and JSON-mode envelope branches).
    """
    if cited_only and not import_all:
        raise ValidationError("--cited-only requires --import-all")


async def execute_research_wait(
    plan: ResearchWaitPlan,
    *,
    client: Any,
    resolve_id: ResolveNotebookIdFn,
    wait_context: WaitContextFactory = _null_wait_context,
    import_sources: ImportResearchSourcesFn = _missing_importer,
) -> ResearchWaitResult:
    """Resolve, wait for completion, and optionally import.

    Args:
        plan: User inputs validated by the Click handler.
        client: An open :class:`~notebooklm.client.NotebookLMClient`. The
            service does NOT open or close the client — the handler owns that
            lifecycle so multiple service calls can share one client.
        resolve_id: Injected notebook-id resolver (the CLI passes its
            ``cli.resolve.resolve_notebook_id``).
        wait_context: Zero-arg factory returning an async context manager that
            wraps the polling loop. Defaults to a no-op context. The CLI handler
            injects ``status_with_elapsed(...)`` so the spinner and
            SIGINT-to-cancelled translation live inside this block.
        import_sources: Injected source importer (the CLI passes its
            rich-coupled ``cli.research_import.import_research_sources``).

    Returns:
        A :class:`ResearchWaitResult` whose ``outcome`` discriminates the
        terminal states. The service NEVER raises ``SystemExit`` and NEVER
        prints — the handler decides exit codes and rendering.

    Notes:
        * Task-id pinning is handled by
          ``client.research.wait_for_completion``.
        * Import is only invoked when ``plan.import_all`` is true AND the
          completed status has sources AND a ``task_id`` was discovered. (The
          third guard is required because without a task_id the importer has
          nothing to verify against.)
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
    # ``ResearchWaitResult`` / the importer consume the legacy ``list[dict]``
    # source shape, so serialize the typed sources here.
    sources = [src.to_public_dict() for src in status.sources]
    report = status.report

    if status_val == "no_research":
        return _terminal("no_research")
    if status_val == "failed":
        return _terminal("failed", query=query, sources=sources, report=report)

    # wait_for_completion only returns completed/no_research/failed; keep a
    # narrow fallback so future terminal statuses cannot be rendered as success.
    if status_val != "completed":
        return _terminal("failed", query=query, sources=sources, report=report)

    import_result: ResearchImportLike | None = None
    if plan.import_all and sources and task_id:
        # In text mode the importer renders its own "Importing sources..."
        # status; in JSON mode it stays silent.
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
    "ResearchImportLike",
    "ResearchStatusKind",
    "ResearchStatusResult",
    "ResearchWaitOutcome",
    "ResearchWaitPlan",
    "ResearchWaitResult",
    "cancel_research",
    "execute_research_wait",
    "poll_and_classify",
    "poll_importable_research",
    "poll_sources_for_import",
    "validate_research_wait_flags",
]
