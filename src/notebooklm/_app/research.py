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
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, NoReturn, Protocol

from ..exceptions import ValidationError
from ..types import discovery_mode_to_str

logger = logging.getLogger(__name__)

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
    # Differentiated termination reason + remediation, derived from the raw
    # code and the run's search source (issue #1964). ``termination_reason`` is
    # the string value of ``ResearchTerminationReason`` (``no_results`` /
    # ``cancelled`` / ``unknown`` / …) so adapters can serialize it directly;
    # ``reason_message`` and ``hint`` are populated only for a run that did not
    # succeed, so a caller never renders a bare ``failed`` with nothing to say.
    termination_reason: str | None = None
    reason_message: str | None = None
    hint: str | None = None
    # Always-populated task metadata recovered by #2122, carried through from
    # the matching ``ResearchTask`` attributes. Like ``status_code`` above they
    # stay OFF ``public_dict`` (the byte-stable CLI ``--json`` payload) and are
    # surfaced by the MCP tool / REST route from these fields instead.
    #
    # ``discovery_mode`` is the string label (``default_llm_search`` /
    # ``deep_research`` / …) rather than the raw code: an agent branching on
    # ``5`` is the failure mode ``_app.views`` exists to prevent. ``None`` when
    # the poll made no mode claim.
    discovery_mode: str | None = None
    # ISO-8601 strings, not ``datetime`` — every consumer of this result
    # serializes it, and ``ResearchStatusResult`` is the transport-neutral
    # projection boundary. ``None`` when the poll carried no timestamp.
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def duration_seconds(self) -> float | None:
        """Seconds between :attr:`created_at` and :attr:`updated_at`.

        DERIVED rather than stored, mirroring ``ResearchTask.duration``: three
        independent fields could disagree, and a hand-built or faked instance
        (``tests/server/fakes.py`` builds these) could carry timestamps six
        seconds apart alongside a ``duration_seconds`` of 999. ``None`` when
        either timestamp is missing or the interval is negative — the same
        slot-swap guard ``ResearchTask.duration`` applies, re-derived here
        because this projection holds the ISO strings, not the datetimes.

        It WARNS on the negative case rather than dropping it silently, and
        that warning is not redundant with ``ResearchTask.duration``'s: this
        projection is built from the timestamps directly, so on the MCP/REST
        path ``ResearchTask.duration`` is never called and its warning never
        fires. Without this one, the surface where an agent actually consumes
        the value would be the surface with no diagnostic at all.
        """
        if self.created_at is None or self.updated_at is None:
            return None
        elapsed = (
            datetime.fromisoformat(self.updated_at) - datetime.fromisoformat(self.created_at)
        ).total_seconds()
        if elapsed < 0:
            logger.warning(
                "research task %r reports updated_at (%s) BEFORE created_at (%s); "
                "reporting duration_seconds=None — the POLL_RESEARCH timestamp slots "
                "may have moved",
                self.task_id,
                self.updated_at,
                self.created_at,
            )
            return None
        return elapsed


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
    ``AmbiguousResearchTaskError`` if two or more tasks are in flight. CLI
    ``research status --run-id/--task-id``, MCP ``research_status`` /
    ``research_import``, and the REST adapter pass their caller-supplied id so
    start→status→import stays pinned to one task; the CLI flag remains
    optional for backward compatibility.
    """
    status = await client.research.poll(notebook_id, task_id)
    return classify_research_task(status)


async def discover_and_classify(
    client: Any, notebook_id: str, query: str, mode: str = "default"
) -> ResearchStatusResult:
    """Run one synchronous ``research.discover`` and classify it like a poll.

    The completed task a discovery returns is projected through the same
    :class:`ResearchStatusResult` the ``research status`` render and ``--json``
    payload consume, so the command layer needs no second render path.
    """
    task = await client.research.discover(notebook_id, query, mode=mode)
    return classify_research_task(task)


def classify_research_task(status: Any) -> ResearchStatusResult:
    """Project one typed ``ResearchTask`` into the command-layer result."""
    # ``ResearchStatus`` is a ``str`` enum; ``.value`` yields the canonical
    # lowercase code the CLI render branches + the original status command keyed
    # off (matches ``execute_research_wait``'s ``status.status.value``).
    status_val = status.status.value
    reason = status.termination_reason
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
        termination_reason=reason.value if reason is not None else None,
        reason_message=status.reason_message,
        hint=status.hint,
        discovery_mode=(
            discovery_mode_to_str(status.discovery_mode)
            if status.discovery_mode is not None
            else None
        ),
        created_at=status.created_at.isoformat() if status.created_at is not None else None,
        updated_at=status.updated_at.isoformat() if status.updated_at is not None else None,
    )


async def poll_importable_research(
    client: Any, notebook_id: str, run_id: str
) -> tuple[list[dict[str, Any]], str]:
    """Poll a research run and return its ``(importable sources, report)``, or raise.

    The single shared importable-state guard for the "import a completed run's
    found sources" flow — driven by the MCP ``research_import`` tool, the REST
    ``POST .../research/{run_id}/import`` route, and (through the pure
    :func:`classify_importable_research` half) the CLI ``research import``
    command, so the ladder cannot drift between adapters.

    Polls FOR THE REQUESTED ``run_id`` (via :func:`poll_and_classify`, which
    forwards it to ``client.research.poll`` as the task discriminator) so the
    returned sources belong to that run — never the notebook's current (possibly
    different) research run's sources; then hands the snapshot to
    :func:`classify_importable_research`, which owns every accept/refuse rule.

    The ``run`` noun is surface-neutral (the MCP tool documents it as the
    ``task_id``); the message names no adapter-specific route or tool so the one
    string reads cleanly on both surfaces.
    """
    status = await poll_and_classify(client, notebook_id, run_id)
    return classify_importable_research(status, run_id, notebook_id=notebook_id)


def classify_importable_research(
    status: ResearchStatusResult, run_id: str, *, notebook_id: str
) -> tuple[list[dict[str, Any]], str]:
    """Return a polled run's ``(importable sources, report)``, or raise.

    The pure half of :func:`poll_importable_research`: every accept/refuse rule
    lives here so an adapter that has *already* polled (the CLI ``research
    import`` command, which resolves a bare "current run" from the same poll it
    classifies) reuses the ladder instead of re-deriving a third copy of it.

    Every non-importable state raises the public
    :class:`~notebooklm.exceptions.ValidationError` (each adapter maps it to its
    own surface + status), so an unfinished / failed / empty run is never
    imported as a partial success:

    * ``not_found`` — the pinned run is not among the polled runs (nothing to
      import; the typed ``NOT_FOUND`` sentinel, not a fallback to the current run);
    * ``failed`` — the run produced no importable sources. The raised message
      names WHY when the poll carried a termination reason (a Drive search that
      matched nothing reads very differently from a cancelled or broken run —
      issue #1964), falling back to the generic wording when it did not;
    * any non-``completed`` status (e.g. ``in_progress`` / ``no_research``) —
      only a completed run has a final source set;
    * ``completed`` with no sources — refuse the silent empty import.

    Returns the completed run's importable sources (the legacy ``list[dict]``
    shape) AND the run's report text on success. The report is returned so a
    caller doing cited-only selection (:func:`~notebooklm.research.select_cited_sources`)
    can match citations against it without a second poll; :func:`poll_sources_for_import`
    delegates here and drops the report for callers that import everything.
    """
    if status.status == "not_found":
        raise ValidationError(
            f"Research run {run_id!r} is not among notebook {notebook_id}'s research "
            "runs; nothing to import. Check its status first."
        )
    # Only a COMPLETED run has a final source set. Importing an
    # in_progress/no_research/failed snapshot would import a partial/empty set as
    # a "success" — refuse with an action-appropriate message.
    if status.status == "failed":
        # A run that simply matched nothing, or was cancelled, is NOT a broken
        # run — telling the caller to "start a new research session" is the
        # wrong remediation for it (issue #1964). Those two NAMED outcomes lead
        # with their own explanation.
        #
        # Everything else keeps the historical guidance verbatim: an
        # unrecognised code is coarsened to FAILED and treated as terminal, so
        # "it will not complete" remains exactly the right advice — it just
        # gains the observed code. (Review caught the first cut sending every
        # code-carrying failure down the differentiated path, which silently
        # dropped that guidance for genuinely-broken runs.)
        detail = " ".join(part for part in (status.reason_message, status.hint) if part)
        if status.termination_reason in ("no_results", "cancelled") and detail:
            # "cannot be imported" rather than "has no sources": a run cancelled
            # mid-flight can carry partially-discovered sources.
            raise ValidationError(f"Research run {run_id!r} cannot be imported. {detail}")
        suffix = f" {status.reason_message}" if status.reason_message else ""
        raise ValidationError(
            f"Research run {run_id!r} failed; it will not complete — start a new "
            f"research session rather than polling.{suffix}"
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
# research import
# ===========================================================================


@dataclass(frozen=True)
class ResearchImportOutcome:
    """Typed outcome of an idempotent research import (#1961).

    ``newly_imported`` are the entries this call actually added;
    ``already_present`` are the requested sources skipped because their URL
    already existed in the notebook (each an ``{id, title, url}`` of the
    EXISTING source). On a repeat import ``newly_imported`` is empty and
    ``already_present`` lists the previously-imported sources.
    """

    newly_imported: list[dict[str, str]]
    already_present: list[dict[str, str]]

    @property
    def newly_imported_count(self) -> int:
        return len(self.newly_imported)

    @property
    def already_present_count(self) -> int:
        return len(self.already_present)


async def import_research_sources(
    client: Any,
    notebook_id: str,
    task_id: str,
    sources: Sequence[Any],
    *,
    allow_duplicate: bool = False,
    max_elapsed: float | None = None,
) -> ResearchImportOutcome:
    """Import a completed run's sources idempotently, reporting skips.

    Drives the timeout-tolerant ``client.research.import_sources_with_verification``
    (which pre-filters requested sources whose URL already exists in the
    notebook unless ``allow_duplicate`` is true) and lifts its ``already_present``
    side channel into a typed result, so every adapter that wants the idempotency
    contract gets it without re-implementing URL dedup: the MCP ``research_import``
    tool and the CLI ``research import`` / ``research wait --import-all``. The REST
    import route landed but deliberately does NOT come through here — it stays on
    the one-shot ``client.research.import_sources`` so a synchronous web request
    cannot block on a multi-minute reconcile loop, and so returns no
    ``already_present`` split. The first three arguments are passed positionally
    to match the underlying method's call shape.

    ``max_elapsed`` bounds the underlying **retry** loop (the IMPORT_RESEARCH RPC
    commonly runs past the client timeout on deep payloads, so it is retried with
    reconciliation). Forwarded only when given, so an adapter that does not expose
    a knob keeps the library default rather than having one imposed here.
    """
    bound = {} if max_elapsed is None else {"max_elapsed": max_elapsed}
    imported = await client.research.import_sources_with_verification(
        notebook_id,
        task_id,
        sources,
        allow_duplicate=allow_duplicate,
        **bound,
    )
    already_present = list(getattr(imported, "already_present", []) or [])
    return ResearchImportOutcome(
        newly_imported=list(imported),
        already_present=already_present,
    )


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
    task_id: str | None = None


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
    # Why a ``failed`` wait ended, plus its remediation (issue #1964). Carried
    # so the CLI can say "your Drive query matched nothing, try the filename"
    # instead of a bare "Research failed"; ``None`` on success and whenever the
    # poll carried no termination reason.
    reason_message: str | None = None
    hint: str | None = None

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
            wait_args = (
                (nb_id_resolved,) if plan.task_id is None else (nb_id_resolved, plan.task_id)
            )
            status = await client.research.wait_for_completion(
                *wait_args,
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
    # Both non-success exits carry the differentiated reason (#1964) so the
    # renderer never has to show a bare "Research failed".
    failure_detail: dict[str, Any] = {
        "query": query,
        "sources": sources,
        "report": report,
        "reason_message": status.reason_message,
        "hint": status.hint,
    }
    if status_val == "failed":
        return _terminal("failed", **failure_detail)

    # wait_for_completion only returns completed/no_research/failed; keep a
    # narrow fallback so future terminal statuses cannot be rendered as success.
    if status_val != "completed":
        return _terminal("failed", **failure_detail)

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
    "ResearchImportOutcome",
    "ResearchStatusKind",
    "ResearchStatusResult",
    "ResearchWaitOutcome",
    "ResearchWaitPlan",
    "ResearchWaitResult",
    "cancel_research",
    "classify_importable_research",
    "execute_research_wait",
    "import_research_sources",
    "poll_and_classify",
    "poll_importable_research",
    "poll_sources_for_import",
    "validate_research_wait_flags",
]
