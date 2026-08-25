"""Backend-neutral research service (start / poll / wait / cancel / import).

Consumes the private semantic port: every wire call this domain makes is one
typed :mod:`notebooklm._records` operation invoked on a
:class:`~notebooklm._backend.BackendAdapter`.  What stays here is the part that
is not protocol-specific -- argument validation, task selection and ambiguity,
the wait loop's deadline and cadence, and the import batch's provenance
validation, idempotency pre-filter, and timeout reconciliation.

``ResearchAPI`` is the compatibility facade over this service; it owns the
published signatures and docstrings and adds no behavior of its own.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ._backend import (
    BACKEND_STATUS_DIAGNOSTIC,
    BackendAdapter,
    BackendError,
    BackendErrorReason,
    BackendStatus,
)
from ._deadline import RuntimeDeadline
from ._notebook_metadata import NotebookSourceLister
from ._operations import OperationDef
from ._projectors import project_research_task
from ._records import (
    RESEARCH_CANCEL_DEF,
    RESEARCH_IMPORT_DEF,
    RESEARCH_POLL_DEF,
    RESEARCH_START_DEF,
    ResearchCancelInput,
    ResearchImportEntry,
    ResearchImportEntryKind,
    ResearchImportInput,
    ResearchMode,
    ResearchPollInput,
    ResearchSearchSource,
    ResearchStartInput,
)
from ._research_import import (
    _import_research_read_timeout,
    _imported_result,
    _is_importable_report_source,
    _merge_imported_sources,
    _no_import_verification_url_entry_count,
    _normalize_import_verification_url,
    _partition_requested_sources,
    _reconcile_import_probe,
    _requested_import_verification_urls,
    _validate_research_task_provenance,
)
from ._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_TIMEOUT,
    MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT,
)
from ._types.research import (
    ResearchSource,
    ResearchSourceInput,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from .exceptions import (
    AmbiguousResearchTaskError,
    ResearchTimeoutError,
    ValidationError,
)

if TYPE_CHECKING:
    from .types import Source

# Keep research diagnostics on the historical logger channel so existing log
# filters see the same records after the service extraction.
logger = logging.getLogger("notebooklm._research")

# Sentinel for "``initial_interval`` not passed" in ``wait_for_completion``. Kept
# as ``object()`` (not literal ``5.0``) so the public-API compat default-repr
# check sees no changed-default break; unset resolves to the default below.
_INITIAL_INTERVAL_UNSET: Any = object()

# Default poll cadence (seconds) when ``initial_interval`` is unset.
_DEFAULT_RESEARCH_POLL_INTERVAL = 5.0

#: Neutral image of ``except RPCError`` — every reason whose compatibility
#: projection is an ``RPCError`` (its ``AuthError`` / ``ClientError`` /
#: ``DecodingError`` / ``RateLimitError`` / ``RPCResponseTooLargeError`` /
#: ``ServerError`` / ``UnknownRPCMethodError`` subclasses included). Reasons
#: outside it — a chat failure, an idempotency-variant refusal — were never
#: caught by the class tuples this replaces and still propagate untouched.
_RPC_FAILURE_REASONS: frozenset[BackendErrorReason] = frozenset(
    {
        BackendErrorReason.AUTH,
        BackendErrorReason.CLIENT,
        BackendErrorReason.DECODING,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.RESPONSE_TOO_LARGE,
        BackendErrorReason.RPC,
        BackendErrorReason.SERVER,
        BackendErrorReason.UNKNOWN_RPC_METHOD,
    }
)

#: Neutral image of the import loop's ``except (RPCTimeoutError, RPCError)``.
#: ``RPCTimeoutError`` is a ``NetworkError`` subclass, so a *plain* network
#: failure of the import attempt is deliberately absent: it propagated out of
#: the retry loop before this migration and must keep doing so.
_IMPORT_ATTEMPT_FAILURE_REASONS: frozenset[BackendErrorReason] = _RPC_FAILURE_REASONS | frozenset(
    {BackendErrorReason.TIMEOUT}
)

#: Neutral image of the snapshot/probe ``except (NetworkError, RPCError)``,
#: which — unlike the loop above — did catch a plain network failure.
_SOURCE_PROBE_FAILURE_REASONS: frozenset[BackendErrorReason] = (
    _IMPORT_ATTEMPT_FAILURE_REASONS | frozenset({BackendErrorReason.NETWORK})
)


def _is_import_research_failed_precondition(error: BackendError) -> bool:
    """True for IMPORT_RESEARCH's documented retry-time FAILED_PRECONDITION.

    The server rejects an ``IMPORT_RESEARCH`` call against a ``task_id`` whose
    state an earlier attempt against that same id already partially mutated —
    commonly this loop's own prior (client-timed-out) call, but not necessarily;
    documented backend behavior, not a novel failure (#1926, item F2b).
    :meth:`ResearchService.import_sources_with_verification` shares its
    post-error source probe with a timeout for this one status, but — unlike a
    timeout — accepts only a fully-verified success; a partial or absent match
    re-raises rather than retrying the rejected ``task_id``.

    Reads the adapter's normalized :class:`BackendStatus`, never a wire code:
    which gRPC number that is stays behind the port (``_web/error_policy.py``).
    The status is carried on the failure of whichever operation raised it, and
    the guarded ``try`` issues exactly one — a second operation there would
    need this predicate revisited, as the ``rpc_code`` form it replaces did.
    """
    if error.reason is not BackendErrorReason.RPC:
        return False
    diagnostics = error.diagnostics or {}
    return diagnostics.get(BACKEND_STATUS_DIAGNOSTIC) is BackendStatus.FAILED_PRECONDITION


_SEARCH_SOURCES = {source.value: source for source in ResearchSearchSource}
_MODES = {mode.value: mode for mode in ResearchMode}


def _coerce_research_source(source: ResearchSourceInput) -> ResearchSource:
    if isinstance(source, ResearchSource):
        return source
    return ResearchSource.from_public_dict(source)


def _coerce_research_sources(sources: Sequence[ResearchSourceInput]) -> list[ResearchSource]:
    return [_coerce_research_source(source) for source in sources]


class ResearchService:
    """Invoke semantic research operations and compose their workflows.

    :meth:`import_sources_with_verification` calls :meth:`import_sources`
    through ``self`` on purpose: that attempt is the seam instrumentation and
    reconciliation tests replace, so it stays late-bound.
    """

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        source_lister: NotebookSourceLister,
        base_timeout: float | None = DEFAULT_TIMEOUT,
        import_research_timeout: float | None = AUTO_READ_TIMEOUT,
    ) -> None:
        self._backend = backend
        self._source_lister = source_lister
        self._base_timeout = base_timeout
        self._import_research_timeout = import_research_timeout

    async def _invoke(
        self,
        definition: OperationDef[Any, Any],
        value: Any,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> Any:
        """Invoke one operation and let its neutral failure propagate."""
        return await self._backend.invoke(definition, value, deadline=deadline)

    async def _poll_tasks(self, notebook_id: str) -> list[ResearchTask]:
        result = await self._invoke(RESEARCH_POLL_DEF, ResearchPollInput(notebook_id))
        return [project_research_task(record) for record in result.tasks]

    @staticmethod
    def _select_polled_tasks(
        parsed_tasks: list[ResearchTask],
        *,
        notebook_id: str,
        task_id: str | None,
        raise_on_ambiguous: bool,
    ) -> list[ResearchTask]:
        # Task-id discriminator: when supplied, filter parsed_tasks down to
        # the matched task so callers iterating ``tasks`` don't see siblings.
        # When omitted but multiple tasks are in flight, the selection is
        # ambiguous (which task did the caller mean?), so raise instead of
        # silently guessing the latest task (ADR-0019: "ambiguous -> raise,
        # never silently guess"). A single in-flight task with no task_id is
        # unambiguous and still returned silently for convenience.
        if task_id is not None:
            return [task for task in parsed_tasks if task.task_id == task_id]
        if raise_on_ambiguous and len(parsed_tasks) > 1:
            raise AmbiguousResearchTaskError(
                notebook_id=notebook_id,
                task_ids=[task.task_id for task in parsed_tasks],
            )
        return parsed_tasks

    @staticmethod
    def _public_poll_result(
        selected_task: ResearchTask,
        parsed_tasks: list[ResearchTask],
    ) -> ResearchTask:
        # Carry the sibling tasks on the selected task's ``tasks`` field. The
        # sub-tasks themselves leave ``tasks`` empty (their default), matching
        # the historical nested-dict shape.
        return replace(selected_task, tasks=tuple(parsed_tasks))

    async def start(
        self,
        notebook_id: str,
        query: str,
        source: str = "web",
        mode: str = "fast",
    ) -> ResearchStart:
        """Start one research run after validating its source/mode combination."""
        logger.debug(
            "Starting %s research in notebook %s: %s",
            mode,
            notebook_id,
            query[:50] if query else "",
        )
        source_lower = source.lower()
        mode_lower = mode.lower()

        if source_lower not in _SEARCH_SOURCES:
            raise ValidationError(f"Invalid source '{source}'. Use 'web' or 'drive'.")
        if mode_lower not in _MODES:
            raise ValidationError(f"Invalid mode '{mode}'. Use 'fast' or 'deep'.")
        if mode_lower == "deep" and source_lower == "drive":
            raise ValidationError("Deep Research only supports Web sources.")

        result = await self._invoke(
            RESEARCH_START_DEF,
            ResearchStartInput(
                notebook_id=notebook_id,
                query=query,
                search_source=_SEARCH_SOURCES[source_lower],
                mode=_MODES[mode_lower],
            ),
        )
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
        """Select one task out of a single poll of ``notebook_id``."""
        logger.debug("Polling research status for notebook %s", notebook_id)
        parsed_tasks = self._select_polled_tasks(
            await self._poll_tasks(notebook_id),
            notebook_id=notebook_id,
            task_id=task_id,
            # Ambiguity raise applies only to the unfiltered (task_id is None)
            # path; a pinned discriminator filters before the raise. Matches
            # wait_for_completion.
            raise_on_ambiguous=task_id is None,
        )

        if parsed_tasks:
            # ``parsed_tasks`` is a typed ``list[ResearchTask]``; the unpack avoids
            # a ``name[int]`` positional read on a decoded payload.
            first_task, *_ = parsed_tasks
            return self._public_poll_result(first_task, parsed_tasks)

        # A pinned ``task_id`` that matched nothing is a poll-observed absence —
        # a typed ``NOT_FOUND`` sentinel carrying the id. A falsy ``task_id``
        # (``None`` or empty string) is no discriminator, so it stays
        # ``NO_RESEARCH`` and preserves the legacy empty-poll shape (ADR-0019
        # Rule 4, #1346).
        if task_id:
            return ResearchTask.not_found(task_id)

        return ResearchTask.empty()

    async def wait_for_completion(
        self,
        notebook_id: str,
        task_id: str | None = None,
        *,
        timeout: float = 1800,
        initial_interval: float = _INITIAL_INTERVAL_UNSET,
    ) -> ResearchTask:
        """Poll until research reaches a terminal state or ``timeout`` elapses."""
        # Unset sentinel → default cadence. An *explicit* non-numeric value
        # (``None``, ``"1"``) is a caller bug: fail fast with TypeError rather
        # than silently coercing it back to the default.
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

        loop = asyncio.get_running_loop()
        start = loop.time()
        pinned_task_id = task_id

        while True:
            parsed_tasks = self._select_polled_tasks(
                await self._poll_tasks(notebook_id),
                notebook_id=notebook_id,
                task_id=pinned_task_id,
                raise_on_ambiguous=pinned_task_id is None,
            )
            selected_task = next(iter(parsed_tasks), None)
            if pinned_task_id is None and selected_task is not None:
                pinned_task_id = selected_task.task_id

            status_val: ResearchStatus = (
                selected_task.status if selected_task is not None else ResearchStatus.NO_RESEARCH
            )
            if selected_task is not None and status_val in (
                ResearchStatus.COMPLETED,
                ResearchStatus.FAILED,
            ):
                return self._public_poll_result(selected_task, parsed_tasks)
            if status_val == ResearchStatus.NO_RESEARCH and pinned_task_id is None:
                return ResearchTask.empty()

            elapsed = loop.time() - start
            if elapsed >= timeout:
                task_label = pinned_task_id or "unknown"
                raise ResearchTimeoutError(
                    notebook_id,
                    task_label,
                    timeout,
                    last_status=status_val.value,
                )

            sleep_for = min(poll_interval, timeout - elapsed)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    async def cancel(self, notebook_id: str, run_id: str) -> None:
        """Cancel one in-flight research run; the response carries no verdict."""
        logger.debug("Cancelling research run %s in notebook %s", run_id, notebook_id)
        await self._invoke(RESEARCH_CANCEL_DEF, ResearchCancelInput(notebook_id, run_id))

    @staticmethod
    def _import_entries(
        source_inputs: list[ResearchSourceInput],
        source_models: list[ResearchSource],
    ) -> tuple[ResearchImportEntry, ...]:
        """Order one import batch: report entries first, then web sources.

        Deep-research reports are sent FIRST because the server commits them
        before the web rows; the timeout reconciliation below relies on that
        ordering when it decides a newly-observed URL implies the report landed.
        """
        report_source_indexes = {
            index
            for index, (source_input, source) in enumerate(
                zip(source_inputs, source_models, strict=True)
            )
            if _is_importable_report_source(source_input, source)
        }
        report_sources = [source_models[index] for index in sorted(report_source_indexes)]
        valid_sources = [
            source
            for index, source in enumerate(source_models)
            if source.url and index not in report_source_indexes
        ]
        skipped_count = len(source_models) - len(valid_sources) - len(report_sources)
        if skipped_count > 0:
            logger.warning(
                "Skipping %d source(s) that cannot be imported (missing URLs or report entries)",
                skipped_count,
            )
        return (
            *(
                ResearchImportEntry(
                    kind=ResearchImportEntryKind.REPORT,
                    title=source.title,
                    report_markdown=source.report_markdown,
                )
                for source in report_sources
            ),
            *(
                ResearchImportEntry(
                    kind=ResearchImportEntryKind.WEB,
                    title=source.title,
                    url=source.url,
                )
                for source in valid_sources
            ),
        )

    async def import_sources(
        self,
        notebook_id: str,
        task_id: str,
        sources: Sequence[ResearchSourceInput],
        *,
        _remaining_budget: float | None = None,
    ) -> list[dict[str, str]]:
        """Run one import attempt for the requested sources."""
        if not sources:
            return []
        source_inputs: list[ResearchSourceInput] = list(sources)
        source_models = _coerce_research_sources(source_inputs)
        logger.debug(
            "Importing %d research sources into notebook %s",
            len(source_models),
            notebook_id,
        )

        # Per-source ``research_task_id`` provenance: mismatches raise, a
        # multi-task batch is refused, and the effective import task id is
        # returned. Shared with ``import_sources_with_verification`` (which runs
        # it up front, before the #1961 idempotency pre-filter) so provenance is
        # validated even for entries the pre-filter would drop.
        effective_task_id = _validate_research_task_provenance(source_models, task_id)

        entries = self._import_entries(source_inputs, source_models)
        if not entries:
            return []

        result = await self._invoke(
            RESEARCH_IMPORT_DEF,
            ResearchImportInput(
                notebook_id=notebook_id,
                task_id=effective_task_id,
                entries=entries,
                attempt_timeout=_import_research_read_timeout(
                    len(entries),
                    base_timeout=self._base_timeout,
                    override=self._import_research_timeout,
                    remaining_budget=_remaining_budget,
                ),
            ),
        )
        return [{"id": record.id, "title": record.title} for record in result.imported]

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
        """Import sources, reconciling timeouts against the notebook's source list."""
        if not sources:
            return _imported_result([], [])
        source_inputs: list[ResearchSourceInput] = list(sources)
        source_models = _coerce_research_sources(sources)

        # Validate research-task provenance on the FULL requested set up front —
        # before the #1961 idempotency pre-filter can drop already-present
        # entries — so a source carrying the wrong ``research_task_id`` is
        # rejected even when its URL already exists in the notebook.
        _validate_research_task_provenance(source_models, task_id)

        started_at = time.monotonic()
        delay = initial_delay
        attempt = 1
        verified_imported: list[dict[str, str]] = []
        verified_imported_ids: set[str] = set()

        # Anchor verified-success on URLs of *new* sources (not on a
        # baseline→current URL delta) so concurrent additions from another
        # session and pre-existing URLs cannot satisfy the check. The same
        # snapshot doubles as the idempotency pre-filter baseline (#1961).
        baseline: list[Source] | None
        baseline_ids: set[str] | None
        try:
            # Research reconciliation needs every uniquely addressable row it
            # can recover, even when GET_NOTEBOOK repeats one ID with drifted
            # metadata. Envelope drift still raises in tolerant row mode; only
            # row-level skips/first-occurrence dedup remain enabled so a known
            # duplicate collision cannot disable the idempotency baseline.
            baseline = await self._source_lister.list(notebook_id, strict=False)
            baseline_ids = {src.id for src in baseline}
        except BackendError as snapshot_exc:
            if snapshot_exc.reason not in _SOURCE_PROBE_FAILURE_REASONS:
                raise
            logger.warning(
                "Pre-import sources.list snapshot failed for %s: %s; "
                "verified-success path and idempotency pre-filter disabled for this call",
                notebook_id,
                snapshot_exc,
            )
            baseline = None
            baseline_ids = None

        # Idempotency pre-filter (#1961): drop requested sources whose normalized
        # URL already exists in the notebook so a repeat import does not
        # duplicate them. Runs up front on every attempt — the timeout-retry
        # path below already filters already-present URLs; this generalizes that
        # to the happy path. Skipped when the caller opts into duplicates or the
        # baseline snapshot failed (can't tell what's already present).
        already_present: list[dict[str, str]] = []
        if not allow_duplicate and baseline is not None:
            existing_by_norm_url: dict[str, Source] = {}
            for existing in baseline:
                if existing.url:
                    existing_by_norm_url.setdefault(
                        _normalize_import_verification_url(existing.url), existing
                    )
            source_inputs, source_models, already_present = _partition_requested_sources(
                source_inputs, source_models, existing_by_norm_url
            )
            if already_present:
                logger.info(
                    "Idempotent research import into %s: skipping %d source(s) already "
                    "present by URL; importing %d new source(s)",
                    notebook_id,
                    len(already_present),
                    len(source_models),
                )
            # Every requested source was already present — nothing new to
            # import. Return without an RPC (and without entering the
            # timeout-retry loop), reporting the skipped set.
            if not source_inputs:
                return _imported_result([], already_present)

        requested_urls_norm = _requested_import_verification_urls(source_models)
        # Track how many non-URL entries (research reports, pasted text) the
        # request includes so concurrent no-URL additions cannot inflate the
        # synthesized return after a timeout.
        requested_no_url_count = _no_import_verification_url_entry_count(source_models)

        def _log_discarded_progress() -> None:
            # #2187 silent-failure-hunter finding: ``verified_imported`` (probe-
            # confirmed commits from earlier iterations) carries no signal once
            # this raises — surface it in logs so it isn't silently lost.
            if verified_imported:
                logger.error(
                    "IMPORT_RESEARCH failing for notebook %s but %d source(s) "
                    "were already confirmed imported before this failure (%s); "
                    "check sources.list rather than assuming a total loss",
                    notebook_id,
                    len(verified_imported),
                    [entry["id"] for entry in verified_imported],
                )

        last_error: BackendError | None = None
        while True:
            # Clamp this attempt's read window to what is left of ``max_elapsed``
            # (#2205): without it a late retry is *granted* the full
            # batch-scaled window — minutes of slack past a budget with seconds
            # left. This bounds what the attempt is given, not how long it can
            # take: ``read`` is an httpx inactivity slot, so connect/pool waits
            # and a byte-dribbling server still sit outside it.
            attempt_budget = max_elapsed - (time.monotonic() - started_at)
            budget_is_viable = attempt_budget >= MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT
            if last_error is not None and not budget_is_viable:
                # A retry that cannot outlast connection establishment is worse
                # than no retry: it would overrun ``max_elapsed`` (the very
                # thing the clamp exists to prevent) if run unclamped, and if
                # run clamped it still SENDS a non-idempotent IMPORT_RESEARCH
                # whose result it cannot observe — which the server may commit
                # anyway, duplicating sources. So stop, and say why.
                logger.warning(
                    "IMPORT_RESEARCH retry budget for notebook %s is exhausted "
                    "(%.1fs of the %.0fs max_elapsed left, under the %.0fs "
                    "minimum viable attempt window); giving up rather than "
                    "sending an attempt whose outcome could not be observed",
                    notebook_id,
                    attempt_budget,
                    max_elapsed,
                    MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT,
                )
                _log_discarded_progress()
                raise last_error
            try:
                imported = await self.import_sources(
                    notebook_id,
                    task_id,
                    source_inputs,
                    # The first attempt always runs on its natural window even
                    # when the budget is already spent (``max_elapsed=0`` is a
                    # documented "one shot" idiom); only retries must fit.
                    _remaining_budget=attempt_budget if budget_is_viable else None,
                )
                return _imported_result(
                    _merge_imported_sources(imported, verified_imported, verified_imported_ids),
                    already_present,
                )
            except BackendError as exc:
                if exc.reason not in _IMPORT_ATTEMPT_FAILURE_REASONS:
                    raise
                timed_out = exc.reason is BackendErrorReason.TIMEOUT
                last_error = exc
                if not timed_out and not _is_import_research_failed_precondition(exc):
                    _log_discarded_progress()
                    raise  # non-FAILED_PRECONDITION RPC failures surface at once (#2187)
                reason = "timed out" if timed_out else "hit a retry-time FAILED_PRECONDITION"
                elapsed = time.monotonic() - started_at
                remaining = max_elapsed - elapsed

                if requested_urls_norm:
                    try:
                        # As above, verification must not turn a known duplicate
                        # row collision into a blind non-idempotent retry.
                        current = await self._source_lister.list(notebook_id, strict=False)
                        outcome = _reconcile_import_probe(
                            current=current,
                            baseline_ids=baseline_ids,
                            requested_urls_norm=requested_urls_norm,
                            requested_no_url_count=requested_no_url_count,
                            source_inputs=source_inputs,
                            source_models=source_models,
                            already_verified_ids=verified_imported_ids,
                            allow_duplicate=allow_duplicate,
                        )
                        if outcome.fully_verified_entries is not None:
                            logger.warning(
                                "IMPORT_RESEARCH %s for notebook %s but "
                                "sources.list verifies every outstanding "
                                "source; treating as success and skipping "
                                "retry to avoid duplicate inflation",
                                reason,
                                notebook_id,
                            )
                            return _imported_result(
                                _merge_imported_sources(
                                    outcome.fully_verified_entries,
                                    verified_imported,
                                    verified_imported_ids,
                                ),
                                already_present,
                            )
                        if outcome.filtered:
                            verified_imported.extend(outcome.newly_verified)
                            verified_imported_ids.update(
                                entry["id"] for entry in outcome.newly_verified
                            )
                            source_inputs = outcome.source_inputs
                            source_models = outcome.source_models
                            requested_urls_norm = outcome.requested_urls_norm
                            requested_no_url_count = outcome.requested_no_url_count
                            if not timed_out:
                                logger.warning(
                                    "IMPORT_RESEARCH %s for notebook %s: %d "
                                    "of %d requested source(s) verified "
                                    "present, but the remainder can't be "
                                    "confirmed — surfacing the error instead "
                                    "of retrying the rejected task_id",
                                    reason,
                                    notebook_id,
                                    outcome.removed_count,
                                    outcome.removed_count + len(source_models),
                                )
                            else:
                                logger.warning(
                                    "IMPORT_RESEARCH %s for notebook %s after "
                                    "%d requested source(s) were already "
                                    "present; retrying with %d remaining "
                                    "source(s)",
                                    reason,
                                    notebook_id,
                                    outcome.removed_count,
                                    len(source_models),
                                )
                    except BackendError as probe_exc:
                        # CancelledError is a BaseException, not a BackendError,
                        # so it propagates naturally for callers that need to
                        # cancel the operation cleanly.
                        if probe_exc.reason not in _SOURCE_PROBE_FAILURE_REASONS:
                            raise
                        logger.warning(
                            "Failed to probe server state after %s: %s; %s",
                            reason,
                            probe_exc,
                            "falling back to retry"
                            if timed_out
                            else "surfacing the original error",
                        )

                if remaining <= 0:
                    _log_discarded_progress()
                    raise

                if not timed_out:  # no verified-success return above
                    _log_discarded_progress()
                    raise

                # Report-only imports (no URLs to verify) can't use the success
                # check above. Cap retries at one attempt to bound worst-case
                # duplicate inflation for report entries when timeouts persist.
                if not requested_urls_norm and attempt >= 2:
                    logger.warning(
                        "IMPORT_RESEARCH %s for notebook %s with no URLs "
                        "to verify; giving up after %d attempts to bound "
                        "duplicate inflation",
                        reason,
                        notebook_id,
                        attempt,
                    )
                    _log_discarded_progress()
                    raise

                sleep_for = min(delay, max_delay, remaining)
                logger.warning(
                    "IMPORT_RESEARCH %s for notebook %s; retrying in "
                    "%.1fs (attempt %d, %.1fs elapsed)",
                    reason,
                    notebook_id,
                    sleep_for,
                    attempt + 1,
                    elapsed,
                )
                await asyncio.sleep(sleep_for)
                delay = min(delay * backoff_factor, max_delay)
                attempt += 1


__all__ = ["ResearchService"]
