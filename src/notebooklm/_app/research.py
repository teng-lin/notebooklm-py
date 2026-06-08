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
    """

    kind: ResearchStatusKind
    status: str
    query: str
    sources: list[dict[str, Any]]
    summary: str
    report: str
    public_dict: dict[str, Any]


def _classify_status_kind(status_val: str) -> ResearchStatusKind:
    if status_val in ("no_research", "in_progress", "completed"):
        return status_val  # type: ignore[return-value]
    return "other"


async def poll_and_classify(client: Any, notebook_id: str) -> ResearchStatusResult:
    """Poll research status once and classify it for the command layer.

    The typed ``ResearchTask`` returned by ``client.research.poll`` is
    serialized to the legacy ``list[dict]`` source shape + the canonical
    ``to_public_dict()`` so the CLI render + ``--json`` output stay unchanged.
    """
    status = await client.research.poll(notebook_id)
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
    )


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
    "execute_research_wait",
    "poll_and_classify",
    "validate_research_wait_flags",
]
