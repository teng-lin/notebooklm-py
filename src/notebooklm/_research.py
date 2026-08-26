"""Research API for NotebookLM web/drive research.

Provides operations for starting research sessions, polling for results,
and importing discovered sources into notebooks.

This module is the public-facing compatibility facade. Every research
operation is executed by the backend-neutral
:class:`~notebooklm._research_service.ResearchService`, which invokes typed
semantic operations on the private backend port; the wire grammar those
operations encode and decode lives in ``_web/codec/research.py``.

Everything public about the domain is owned here (P10 R6.4): the published
signatures, the source/mode and poll-cadence validation, the coercion of a
requested source — a :class:`ResearchSource` or a loose mapping — into a
neutral :class:`ResearchImportCandidate`, the projection of the service's
records back onto :class:`ResearchTask` / :class:`ResearchStart` and the
historical ``list[dict[str, str]]`` import shape, and the reconstruction of
public exceptions from neutral :class:`~notebooklm._backend.BackendError`
records. The service below sees none of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from . import research as _research_pub
from ._backend import BackendAdapter
from ._read_services import SourceReadService
from ._research_service import ResearchService, SourceRecordLister
from ._runtime.config import AUTO_READ_TIMEOUT, DEFAULT_TIMEOUT
from ._semantic.compat import project_backend_call
from ._semantic.projectors import project_research_task
from ._semantic.records import (
    ResearchImportBatchInput,
    ResearchImportCandidate,
    ResearchImportedSourceRecord,
    ResearchImportVerifyInput,
    ResearchImportVerifyResult,
    ResearchMode,
    ResearchSearchSource,
    ResearchSourceRecord,
    ResearchStartInput,
    ResearchTaskSelectionResult,
    ResearchWaitInput,
    SourceRecord,
)
from ._types.research import (
    ResearchSource,
    ResearchSourceInput,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from .exceptions import ValidationError
from .types import CitedSourceSelection

__all__ = [
    "CitedSourceSelection",
    "ResearchAPI",
    "ResearchSource",
    "ResearchStart",
    "ResearchStatus",
    "ResearchTask",
]

# Sentinel for "``initial_interval`` not passed" in ``wait_for_completion``. Kept
# as ``object()`` (not literal ``5.0``) so the public-API compat default-repr
# check sees no changed-default break; unset resolves to the default below.
_INITIAL_INTERVAL_UNSET: Any = object()

# Default poll cadence (seconds) when ``initial_interval`` is unset.
_DEFAULT_RESEARCH_POLL_INTERVAL = 5.0

_SEARCH_SOURCES = {source.value: source for source in ResearchSearchSource}
_MODES = {mode.value: mode for mode in ResearchMode}


class _MissingSourceLister:
    """Standalone seam that fails only when import verification needs sources."""

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[SourceRecord]:
        del notebook_id, strict
        raise RuntimeError(
            "ResearchAPI.import_sources_with_verification requires a "
            "composition-injected source lister"
        )


def _coerce_research_source(source: ResearchSourceInput) -> ResearchSource:
    if isinstance(source, ResearchSource):
        return source
    return ResearchSource.from_public_dict(source)


def _is_importable_report_source(
    source_input: ResearchSourceInput,
    source: ResearchSource,
) -> bool:
    """Preserve the public-dict report predicate from the legacy importer.

    Reads the request in the shape it arrived in, which is exactly why it lives
    above the port: a mapping has to carry both a string ``title`` and a string
    ``report_markdown`` to count, while a :class:`ResearchSource` needs only the
    former because its ``report_markdown`` is already typed.
    """
    if not source.is_report or not source.report_markdown:
        return False
    if isinstance(source_input, ResearchSource):
        return isinstance(source.title, str)
    return isinstance(source_input.get("title"), str) and isinstance(
        source_input.get("report_markdown"), str
    )


def _record_research_source(source: ResearchSource) -> ResearchSourceRecord:
    """Capture one public research source as its neutral record."""
    return ResearchSourceRecord(
        url=source.url,
        title=source.title,
        result_type=source.result_type,
        research_task_id=source.research_task_id,
        report_markdown=source.report_markdown,
        source_ordinal=source.source_ordinal,
        hint=source.hint,
    )


def _import_candidates(
    sources: Sequence[ResearchSourceInput],
) -> tuple[ResearchImportCandidate, ...]:
    """Lift a public import request into the neutral batch the service runs.

    The report verdict is resolved once, here, against the shape the caller
    used; every later stage (batch ordering, the idempotency pre-filter, the
    post-failure reconciliation) reads the resolved flag, so the two parallel
    ``source_inputs`` / ``source_models`` lists those stages used to keep
    index-aligned collapse into one list.
    """
    # Falsy-guarded rather than iterated blind: the service used to take the
    # request itself and short-circuit on ``not sources``, which also absorbed
    # an out-of-contract ``None``. Coercion happens here now, so the guard has
    # to move with it or that call would raise TypeError instead of returning
    # empty (pinned by test_research.py's none-sources cases).
    if not sources:
        return ()
    candidates: list[ResearchImportCandidate] = []
    for source_input in sources:
        model = _coerce_research_source(source_input)
        candidates.append(
            ResearchImportCandidate(
                source=_record_research_source(model),
                report=_is_importable_report_source(source_input, model),
            )
        )
    return tuple(candidates)


def _project_selection(selection: ResearchTaskSelectionResult) -> ResearchTask:
    """Render one neutral poll selection as the published lifecycle result.

    The three neutral outcomes map onto the three shapes callers have always
    seen: a selection carries its siblings on ``tasks``, a pinned-but-absent id
    becomes the typed ``NOT_FOUND`` sentinel, and an empty poll becomes
    ``NO_RESEARCH`` (ADR-0019 Rule 4, #1346).
    """
    if selection.task is not None:
        # Sub-tasks leave ``tasks`` empty (their default), matching the
        # historical nested shape.
        return replace(
            project_research_task(selection.task),
            tasks=tuple(project_research_task(task) for task in selection.tasks),
        )
    if selection.missing_task_id is not None:
        return ResearchTask.not_found(selection.missing_task_id)
    return ResearchTask.empty()


def _imported_entry(record: ResearchImportedSourceRecord) -> dict[str, str]:
    return {"id": record.id, "title": record.title}


class _ImportedResearchSources(list):
    """Newly-imported source entries carrying the already-present ones (#1961).

    :meth:`ResearchAPI.import_sources_with_verification` pre-filters requested
    sources whose (normalized) URL already exists in the notebook so a repeat
    import does not duplicate them. This ``list`` subclass keeps every list
    behavior existing callers rely on (iteration, ``len``, indexing, JSON
    serialization) — the wrapped items ARE the newly-imported entries — while
    exposing the deduped ``already_present`` entries as a side channel for
    callers (the ``_app`` import wrapper) that want an idempotency report.

    The public method's return annotation stays ``list[dict[str, str]]`` on
    purpose: the annotation is what the public-API compat gate inspects, so this
    runtime-only subclass adds the side channel without a return-type break.
    """

    already_present: list[dict[str, str]]

    def __init__(
        self,
        iterable: Sequence[dict[str, str]] = (),
        already_present: Sequence[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(iterable)
        self.already_present = list(already_present or [])


def _imported_result(
    imported: list[dict[str, str]],
    already_present: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Wrap newly-imported entries in the side-channel carrier (#1961).

    Returns a :class:`_ImportedResearchSources` typed as the historical
    ``list[dict[str, str]]`` so callers see no annotation change.
    """
    return _ImportedResearchSources(imported, already_present)


def _project_import_verification(result: ResearchImportVerifyResult) -> list[dict[str, str]]:
    """Render the neutral verify result as the historical list + side channel."""
    return _imported_result(
        [_imported_entry(record) for record in result.imported],
        [
            {"id": record.id, "title": record.title, "url": record.url}
            for record in result.already_present
        ],
    )


class ResearchAPI:
    """Operations for research sessions (web/drive search).

    Provides methods for starting research, polling for results, and
    importing discovered sources into notebooks.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            # Start research
            task = await client.research.start(notebook_id, "quantum computing")

            # Poll for results (typed attribute access; ``== "completed"``
            # still works because ResearchStatus is a str enum)
            result = await client.research.poll(notebook_id)
            if result.status == "completed":
                # Import selected sources
                imported = await client.research.import_sources(
                    notebook_id, task.task_id, result.sources[:5]
                )
    """

    def __init__(
        self,
        *,
        source_lister: SourceRecordLister | None = None,
        base_timeout: float | None = DEFAULT_TIMEOUT,
        import_research_timeout: float | None = AUTO_READ_TIMEOUT,
        _backend: BackendAdapter | None = None,
    ):
        """Initialize the research API.

        Args:
            base_timeout: The owning client's configured ``timeout=``. The
                batch-scaled IMPORT_RESEARCH window is floored at it so a
                caller's larger explicit budget is never silently shortened
                (#2205). Standalone construction keeps the historical behavior
                via the shared 30 s default.
            import_research_timeout: Per-attempt read window for
                IMPORT_RESEARCH, read exactly like ``chat_timeout``: unset
                (default) keeps the batch-scaled, ``base_timeout``-floored
                window; a value replaces both; ``None`` inherits
                ``base_timeout`` verbatim.
            source_lister: Optional neutral source lister used by
                :meth:`import_sources_with_verification` to snapshot baseline
                source records before the import call and probe sources on
                failure. Production construction injects the semantic
                :class:`~notebooklm._read_services.SourceReadService` at the
                client composition root; left unset with a backend present, one
                is built over that backend, because reconciling an import
                against the notebook's own sources is not an optional
                capability of this API (P10 R6.4 replaced the public
                ``sources.list`` facade call this used to make).
            _backend: Private semantic backend supplied by the client
                composition root.
        """
        self._source_lister: SourceRecordLister = (
            source_lister
            if source_lister is not None
            else (SourceReadService(_backend) if _backend is not None else _MissingSourceLister())
        )
        self._base_timeout = base_timeout
        self._import_research_timeout = import_research_timeout
        self._service = (
            ResearchService(
                _backend,
                source_lister=self._source_lister,
                base_timeout=base_timeout,
                import_research_timeout=import_research_timeout,
            )
            if _backend is not None
            else None
        )

    def _require_service(self) -> ResearchService:
        """Return the composition-root service for the migrated research domain."""
        if self._service is None:
            raise RuntimeError("ResearchAPI semantic backend was not configured")
        return self._service

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize source/report URLs for citation matching.

        Thin wrapper retained for backward compatibility. Delegates to
        :func:`notebooklm.research.normalize_url`.
        """
        return _research_pub.normalize_url(url)

    @classmethod
    def extract_report_urls(cls, report: str) -> set[str]:
        """Extract normalized URLs from research report markdown/text.

        Thin wrapper retained for backward compatibility. Delegates to
        :func:`notebooklm.research.extract_report_urls`.
        """
        return _research_pub.extract_report_urls(report)

    @classmethod
    def select_cited_sources(
        cls,
        sources: Sequence[ResearchSourceInput],
        report: str,
    ) -> CitedSourceSelection:
        """Return research sources cited by the completed report.

        Thin wrapper retained for backward compatibility. Delegates to
        :func:`notebooklm.research.select_cited_sources`.
        """
        return _research_pub.select_cited_sources(sources, report)

    async def start(
        self,
        notebook_id: str,
        query: str,
        source: str = "web",
        mode: str = "fast",
    ) -> ResearchStart:
        """Start a research session.

        Args:
            notebook_id: The notebook ID.
            query: The research query.
            source: "web" or "drive".
            mode: "fast" or "deep" (deep is web-only).

        Returns:
            A :class:`~notebooklm._types.research.ResearchStart` (``task_id`` /
            ``report_id`` / ``notebook_id`` / ``query`` / ``mode``).

        Raises:
            ValidationError: If source/mode combination is invalid.
            ResearchStartUnavailableError: If deep research returns no run.
            DecodingError: On a "couldn't-start" payload — an empty/non-list
                result or a falsey ``task_id`` (no task created); #1342.

        .. versionchanged:: 0.8.0
            **Breaking change:** a "couldn't-start" payload now raises
            :class:`DecodingError` instead of returning ``None``, and the return
            type narrows from ``ResearchStart | None`` to ``ResearchStart``
            (#1342).
        """
        source_lower = source.lower()
        mode_lower = mode.lower()
        if source_lower not in _SEARCH_SOURCES:
            raise ValidationError(f"Invalid source '{source}'. Use 'web' or 'drive'.")
        if mode_lower not in _MODES:
            raise ValidationError(f"Invalid mode '{mode}'. Use 'fast' or 'deep'.")
        if mode_lower == "deep" and source_lower == "drive":
            raise ValidationError("Deep Research only supports Web sources.")

        result = await project_backend_call(
            self._require_service().start(
                ResearchStartInput(
                    notebook_id=notebook_id,
                    query=query,
                    search_source=_SEARCH_SOURCES[source_lower],
                    mode=_MODES[mode_lower],
                )
            )
        )
        # ``notebook_id`` / ``query`` / ``mode`` are the caller's own request
        # echoed back, not backend evidence; only the two ids are decoded.
        return ResearchStart(
            task_id=result.task_id,
            report_id=result.report_id,
            notebook_id=notebook_id,
            query=query,
            mode=mode_lower,
        )

    async def poll(
        self,
        notebook_id: str,
        task_id: str | None = None,
    ) -> ResearchTask:
        """Poll for research results.

        Args:
            notebook_id: The notebook ID.
            task_id: Optional discriminator selecting a specific research task
                when more than one is in flight against the same notebook.
                When set, the returned ``task_id`` / ``status`` / ``query`` /
                ``sources`` / ``summary`` / ``report`` fields describe the
                matched task, and ``tasks`` contains only that task. When
                ``None`` and two or more tasks are in flight, the selection is
                ambiguous and an
                :class:`~notebooklm.exceptions.AmbiguousResearchTaskError` is
                raised — pass the ``task_id`` from :meth:`start` to select
                explicitly. A single in-flight task is returned silently.

        .. versionchanged:: 0.8.0
            ``task_id=None`` with two or more in-flight tasks now raises
            ``AmbiguousResearchTaskError`` instead of warning and returning the
            latest task (signature unchanged; single task still returned).

        Returns:
            A :class:`~notebooklm._types.research.ResearchTask` for the selected
            task. Use attribute access:
            - ``task.task_id``: task/report identifier for the selected task
            - ``task.status``: a :class:`~notebooklm._types.research.ResearchStatus`
              (``IN_PROGRESS`` / ``COMPLETED`` / ``FAILED`` / ``NO_RESEARCH`` /
              ``NOT_FOUND``); equals the historical strings
            - ``task.query``: original research query text
            - ``task.sources``: tuple of ``ResearchSource`` (each exposes ``url``,
              ``title``, ``result_type``, ``research_task_id``, ``report_markdown``,
              ``source_ordinal``)
            - ``task.summary``: summary text when present
            - ``task.report``: extracted deep-research report markdown, if present
            - ``task.tasks``: all parsed research tasks visible at this poll
              (filtered to the matched task when ``task_id`` is set)

            Use attribute access (``result.status``).

            When a non-empty ``task_id`` is supplied but no in-flight task
            matches, the return is ``ResearchTask.not_found(task_id)`` (status
            ``NOT_FOUND``, empty ``tasks``) — the *poll-observed absence* of that
            task (a typed lifecycle sentinel, not a raise; ADR-0019 Rule 4),
            distinct from the unfiltered empty poll, which stays ``NO_RESEARCH``.
        """
        return _project_selection(
            await project_backend_call(self._require_service().poll(notebook_id, task_id))
        )

    async def wait_for_completion(
        self,
        notebook_id: str,
        task_id: str | None = None,
        *,
        timeout: float = 1800,
        initial_interval: float = _INITIAL_INTERVAL_UNSET,
    ) -> ResearchTask:
        """Poll until research reaches a terminal state or times out.

        When the first poll returns a concrete ``task_id``, subsequent polls
        pass it back through :meth:`poll` as the discriminator. This prevents a
        later concurrent research task in the same notebook from substituting
        its sources/report into this wait loop.

        Args:
            notebook_id: The notebook ID.
            task_id: Optional research task discriminator. Pass the value
                returned by :meth:`start` when available. When ``None`` and two
                or more tasks are in flight on the first poll,
                :class:`~notebooklm.exceptions.AmbiguousResearchTaskError` is
                raised; a single in-flight task is selected and pinned silently.
            timeout: Maximum seconds to wait.
            initial_interval: Seconds between status checks (default: 5). This
                is the canonical poll-interval keyword, matching
                :meth:`SourcesAPI.wait_until_ready` and
                :meth:`ArtifactsAPI.wait_for_completion`.

        Returns:
            The final :meth:`poll` result (a
            :class:`~notebooklm._types.research.ResearchTask`) for
            ``COMPLETED`` or ``FAILED`` statuses. ``NO_RESEARCH`` is returned
            immediately only when no task id is known; for a known/pinned task
            it can be a transient live-API state before the task appears in
            ``POLL_RESEARCH``. Unlike :meth:`poll`, this method never returns
            ``NOT_FOUND`` — a pinned task that is temporarily absent from a poll
            is treated as a transient replication-lag condition and keeps
            polling until it appears, reaches a terminal state, or times out.
            Use attribute access (``result.status``).

        Raises:
            AmbiguousResearchTaskError: If ``task_id`` is ``None`` and two or
                more tasks are in flight on the first poll (pass ``task_id``).
            ResearchTimeoutError: If research does not reach a terminal status
                before ``timeout`` elapses. Subclass of
                :class:`WaitTimeoutError` and the built-in :class:`TimeoutError`,
                so ``except TimeoutError`` continues to catch it.
            ValueError: If ``timeout`` is negative or the poll interval is not
                positive.
            TypeError: If the resolved poll interval is not a number.
        """
        # An *explicit* non-numeric value (``None``, ``"1"``) is a caller bug:
        # fail fast with TypeError rather than silently coercing it back to the
        # default cadence the unset sentinel resolves to.
        if initial_interval is _INITIAL_INTERVAL_UNSET:
            poll_interval = _DEFAULT_RESEARCH_POLL_INTERVAL
        elif isinstance(initial_interval, bool) or not isinstance(initial_interval, (int, float)):
            raise TypeError("poll interval must be a number")
        else:
            poll_interval = float(initial_interval)

        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")

        return _project_selection(
            await project_backend_call(
                self._require_service().wait_for_completion(
                    ResearchWaitInput(
                        notebook_id=notebook_id,
                        task_id=task_id,
                        timeout=timeout,
                        poll_interval=poll_interval,
                    )
                )
            )
        )

    async def cancel(self, notebook_id: str, run_id: str) -> None:
        """Request cancellation of an in-flight research run.

        Cancellation is fire-and-forget: a successful call returns ``None``,
        but the server's empty response does not confirm that the run changed
        state. Unknown and already-terminal run IDs are silent no-ops; poll the
        run afterward when confirmation is required.

        Args:
            notebook_id: The notebook used for request routing.
            run_id: The poll-level run ID from ``poll().task_id``. For deep
                research this is the ``report_id`` returned by :meth:`start`,
                not deep research's ``start().task_id`` session ID. For fast
                research it is ``start().task_id``.

        Returns:
            ``None``. The response carries no cancellation success signal.

        Raises:
            NetworkError: If the cancellation request fails at the network layer.
            RPCError: If the cancellation RPC fails.

        Note:
            ``notebook_id`` is routing context, not a scoping boundary; the
            server identifies the research run by ``run_id``.
        """
        await project_backend_call(self._require_service().cancel(notebook_id, run_id))

    async def import_sources(
        self,
        notebook_id: str,
        task_id: str,
        sources: Sequence[ResearchSourceInput],
        *,
        _remaining_budget: float | None = None,
    ) -> list[dict[str, str]]:
        """Import selected research sources into the notebook.

        Args:
            notebook_id: The notebook ID.
            task_id: The research task ID.
            sources: List of sources to import, each with 'url' and 'title'.
                Deep research results from poll() may also include a report
                entry with 'report_markdown' and 'research_task_id'.
            _remaining_budget: Internal. What is left of
                :meth:`import_sources_with_verification`'s ``max_elapsed``
                when this attempt starts; clamps the per-attempt read timeout
                so one attempt cannot outlive that loop's deadline (#2205).
                Not part of the public contract — direct callers leave it
                unset and get the full batch-scaled window.

        Returns:
            List of imported sources with 'id' and 'title'.

        Note:
            The API response can be incomplete - it may return fewer items than
            were actually imported. All requested sources typically get imported
            successfully, but the return value may not reflect all of them.
            To reliably verify imports, check the notebook's source list using
            `client.sources.list(notebook_id)` after calling this method.
        """
        imported = await project_backend_call(
            self._require_service().import_sources(
                ResearchImportBatchInput(
                    notebook_id=notebook_id,
                    task_id=task_id,
                    candidates=_import_candidates(sources),
                    remaining_budget=_remaining_budget,
                )
            )
        )
        return [_imported_entry(record) for record in imported]

    async def import_sources_with_verification(
        self,
        notebook_id: str,
        task_id: str,
        sources: Sequence[ResearchSourceInput],
        *,
        max_elapsed: float = 1800,
        initial_delay: float = 5,
        backoff_factor: float = 2,
        max_delay: float = 60,
        allow_duplicate: bool = False,
    ) -> list[dict[str, str]]:
        """Import sources with timeout-tolerant verification.

        Use this in preference to :meth:`import_sources` for deep research:
        the underlying ``IMPORT_RESEARCH`` RPC commonly responds in >30 s on
        deep-research payloads and a one-shot call times out at the client
        even when the server has already committed.

        Idempotency (#1961): unless ``allow_duplicate`` is true, requested
        sources whose normalized URL already exists among the notebook's
        current sources are pre-filtered out of *every* import attempt (not
        just the timeout-retry path), so re-importing the same completed task
        does not duplicate its sources. Report / pasted-text entries have no
        dedupable URL and are always imported. The return value is a plain
        ``list`` of the *newly-imported* entries; callers wanting the skipped
        set read ``already_present`` off it (see :class:`_ImportedResearchSources`).
        When the baseline snapshot fails, or ``allow_duplicate`` is true, no
        pre-filter is applied (historical behavior).

        Lifecycle:

        1. Snapshot baseline sources via ``client.sources.list`` (also the URL
           set used for the idempotency pre-filter above).
        2. Call :meth:`import_sources`.
        3. On :class:`RPCTimeoutError`, probe ``client.sources.list``: if every
           requested URL now appears among *new* sources, treat as success;
           otherwise filter out already-present URLs and retry the remainder.
           IMPORT_RESEARCH's documented ``FAILED_PRECONDITION`` (#2187, #1926
           F2b) shares only the verified-success half — anything less
           re-raises rather than retrying the rejected task_id blindly.
        4. Bound total elapsed time by ``max_elapsed``; back off between
           retries (capped by ``max_delay``).
        5. Report-only imports (no URLs to verify) cap retries at one
           attempt to bound duplicate-inflation worst case.

        This method preserves the #808 ``NON_IDEMPOTENT_NO_RETRY``
        classification of the raw ``IMPORT_RESEARCH`` RPC: the executor
        still refuses to retry internally; the safe retry happens here,
        anchored on the pre-call snapshot, which is the disambiguation
        the #808 analysis said was unavailable to the executor.

        Raises:
            RPCTimeoutError: If retries exhaust ``max_elapsed``.
            RPCError: Immediately for any non-FAILED_PRECONDITION error, or
                once a FAILED_PRECONDITION's post-error verification fails to
                confirm every requested URL landed — no budget is spent on it.
        """
        return _project_import_verification(
            await project_backend_call(
                self._require_service().import_sources_with_verification(
                    ResearchImportVerifyInput(
                        notebook_id=notebook_id,
                        task_id=task_id,
                        candidates=_import_candidates(sources),
                        max_elapsed=max_elapsed,
                        initial_delay=initial_delay,
                        backoff_factor=backoff_factor,
                        max_delay=max_delay,
                        allow_duplicate=allow_duplicate,
                    )
                )
            )
        )
