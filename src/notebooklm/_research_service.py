"""Backend-neutral research service (start / poll / wait / cancel / import).

Consumes the private semantic port: every wire call this domain makes is one
typed :mod:`notebooklm._records` operation invoked on a
:class:`~notebooklm._backend.BackendAdapter`.  What stays here is the part that
is not protocol-specific -- task selection and ambiguity, the wait loop's
deadline and cadence, and the import batch's provenance validation, idempotency
pre-filter, and timeout reconciliation.

Every method speaks neutral records in both directions (P10 invariant I1,
defect N1).  Two workflows have no direct row and are sequenced here from
leaves: ``research.wait`` polls ``research.poll`` under its own total budget,
and ``research.import_verify`` sequences ``research.import`` against a
``source.list`` probe within one ``max_elapsed``.  Both carry a typed
``OperationDef``, so ``capabilities.available()`` reports them even though
``invoke`` refuses them.

``ResearchAPI`` is the compatibility facade over this service; it owns the
published signatures, the public argument validation, the coercion of a public
request into neutral candidates, and the projection of records and neutral
failures back onto the published models and exceptions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any, Protocol

from ._backend import (
    BACKEND_STATUS_DIAGNOSTIC,
    BackendAdapter,
    BackendError,
    BackendErrorReason,
    BackendStatus,
)
from ._deadline import RuntimeDeadline
from ._operations import OperationDef
from ._records import (
    RESEARCH_CANCEL_DEF,
    RESEARCH_IMPORT_DEF,
    RESEARCH_POLL_DEF,
    RESEARCH_START_DEF,
    RESEARCH_TERMINAL_STATUSES,
    ResearchCancelInput,
    ResearchImportBatchInput,
    ResearchImportCandidate,
    ResearchImportedSourceRecord,
    ResearchImportEntry,
    ResearchImportEntryKind,
    ResearchImportInput,
    ResearchImportVerifyInput,
    ResearchImportVerifyResult,
    ResearchPollInput,
    ResearchPresentSourceRecord,
    ResearchStartInput,
    ResearchStartResult,
    ResearchTaskRecord,
    ResearchTaskSelectionResult,
    ResearchTaskStatus,
    ResearchWaitInput,
    SourceRecord,
)
from ._research_import import (
    _import_research_read_timeout,
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
from .exceptions import AmbiguousResearchTaskError, ResearchTimeoutError

# Keep research diagnostics on the historical logger channel so existing log
# filters see the same records after the service extraction.
logger = logging.getLogger("notebooklm._research")

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


class SourceRecordLister(Protocol):
    """Neutral source-listing seam this service reconciles imports against.

    Deliberately narrower than the semantic read service that satisfies it: the
    reconciliation needs one tolerant listing of a notebook and nothing else.
    Naming a facade here instead (the public ``sources.list``) is what defect S7
    called out — a service reaching back up through the layer above it.
    """

    async def list(
        self, notebook_id: str, *, strict: bool = False
    ) -> list[SourceRecord]:  # pragma: no cover - structural protocol
        """List one notebook's sources as neutral records."""


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
        source_lister: SourceRecordLister,
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

    async def _poll_tasks(self, notebook_id: str) -> list[ResearchTaskRecord]:
        result = await self._invoke(RESEARCH_POLL_DEF, ResearchPollInput(notebook_id))
        return list(result.tasks)

    @staticmethod
    def _select_polled_tasks(
        parsed_tasks: list[ResearchTaskRecord],
        *,
        notebook_id: str,
        task_id: str | None,
        raise_on_ambiguous: bool,
    ) -> list[ResearchTaskRecord]:
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
    def _selection(
        selected_task: ResearchTaskRecord,
        parsed_tasks: list[ResearchTaskRecord],
    ) -> ResearchTaskSelectionResult:
        # Carry the sibling tasks alongside the selection. The sub-tasks
        # themselves stay bare records, matching the historical nested shape a
        # facade rebuilds from them.
        return ResearchTaskSelectionResult(task=selected_task, tasks=tuple(parsed_tasks))

    async def start(self, value: ResearchStartInput) -> ResearchStartResult:
        """Start one research run for an already-validated request."""
        logger.debug(
            "Starting %s research in notebook %s: %s",
            value.mode.value,
            value.notebook_id,
            value.query[:50] if value.query else "",
        )
        result: ResearchStartResult = await self._invoke(RESEARCH_START_DEF, value)
        return result

    async def poll(
        self,
        notebook_id: str,
        task_id: str | None = None,
    ) -> ResearchTaskSelectionResult:
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
            # ``parsed_tasks`` is a typed ``list[ResearchTaskRecord]``; the
            # unpack avoids a ``name[int]`` positional read on a decoded payload.
            first_task, *_ = parsed_tasks
            return self._selection(first_task, parsed_tasks)

        # A pinned ``task_id`` that matched nothing is a poll-observed absence,
        # reported as such and rendered by the facade as the typed ``NOT_FOUND``
        # sentinel carrying the id. A falsy ``task_id`` (``None`` or empty
        # string) is no discriminator, so it stays the empty poll and preserves
        # the legacy ``NO_RESEARCH`` shape (ADR-0019 Rule 4, #1346).
        if task_id:
            return ResearchTaskSelectionResult(missing_task_id=task_id)

        return ResearchTaskSelectionResult()

    async def wait_for_completion(
        self,
        value: ResearchWaitInput,
    ) -> ResearchTaskSelectionResult:
        """Poll until research reaches a terminal state or the budget elapses.

        ``value`` arrives with its cadence and budget already validated: the
        public ``initial_interval`` sentinel and the ``TypeError`` /
        ``ValueError`` it raises describe a published keyword, so they belong to
        the facade.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        pinned_task_id = value.task_id

        while True:
            parsed_tasks = self._select_polled_tasks(
                await self._poll_tasks(value.notebook_id),
                notebook_id=value.notebook_id,
                task_id=pinned_task_id,
                raise_on_ambiguous=pinned_task_id is None,
            )
            selected_task = next(iter(parsed_tasks), None)
            if pinned_task_id is None and selected_task is not None:
                pinned_task_id = selected_task.task_id

            status = (
                ResearchTaskStatus(selected_task.status)
                if selected_task is not None
                else ResearchTaskStatus.NO_RESEARCH
            )
            if selected_task is not None and status in RESEARCH_TERMINAL_STATUSES:
                return self._selection(selected_task, parsed_tasks)
            if status is ResearchTaskStatus.NO_RESEARCH and pinned_task_id is None:
                return ResearchTaskSelectionResult()

            elapsed = loop.time() - start
            if elapsed >= value.timeout:
                task_label = pinned_task_id or "unknown"
                raise ResearchTimeoutError(
                    value.notebook_id,
                    task_label,
                    value.timeout,
                    last_status=status.value,
                )

            sleep_for = min(value.poll_interval, value.timeout - elapsed)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    async def cancel(self, notebook_id: str, run_id: str) -> None:
        """Cancel one in-flight research run; the response carries no verdict."""
        logger.debug("Cancelling research run %s in notebook %s", run_id, notebook_id)
        await self._invoke(RESEARCH_CANCEL_DEF, ResearchCancelInput(notebook_id, run_id))

    @staticmethod
    def _import_entries(
        candidates: Sequence[ResearchImportCandidate],
    ) -> tuple[ResearchImportEntry, ...]:
        """Order one import batch: report entries first, then web sources.

        Deep-research reports are sent FIRST because the server commits them
        before the web rows; the timeout reconciliation below relies on that
        ordering when it decides a newly-observed URL implies the report landed.
        """
        report_sources = [c.source for c in candidates if c.report]
        valid_sources = [c.source for c in candidates if not c.report and c.source.url]
        skipped_count = len(candidates) - len(valid_sources) - len(report_sources)
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
        value: ResearchImportBatchInput,
    ) -> tuple[ResearchImportedSourceRecord, ...]:
        """Run one import attempt for the requested candidates."""
        if not value.candidates:
            return ()
        candidates = list(value.candidates)
        logger.debug(
            "Importing %d research sources into notebook %s",
            len(candidates),
            value.notebook_id,
        )

        # Per-source ``research_task_id`` provenance: mismatches raise, a
        # multi-task batch is refused, and the effective import task id is
        # returned. Shared with ``import_sources_with_verification`` (which runs
        # it up front, before the #1961 idempotency pre-filter) so provenance is
        # validated even for entries the pre-filter would drop.
        effective_task_id = _validate_research_task_provenance(candidates, value.task_id)

        entries = self._import_entries(candidates)
        if not entries:
            return ()

        result = await self._invoke(
            RESEARCH_IMPORT_DEF,
            ResearchImportInput(
                notebook_id=value.notebook_id,
                task_id=effective_task_id,
                entries=entries,
                attempt_timeout=_import_research_read_timeout(
                    len(entries),
                    base_timeout=self._base_timeout,
                    override=self._import_research_timeout,
                    remaining_budget=value.remaining_budget,
                ),
            ),
        )
        return tuple(result.imported)

    async def import_sources_with_verification(
        self,
        value: ResearchImportVerifyInput,
    ) -> ResearchImportVerifyResult:
        """Import candidates, reconciling failures against the notebook's sources."""
        notebook_id = value.notebook_id
        task_id = value.task_id
        max_elapsed = value.max_elapsed
        max_delay = value.max_delay
        allow_duplicate = value.allow_duplicate
        if not value.candidates:
            return ResearchImportVerifyResult()
        candidates = list(value.candidates)

        # Validate research-task provenance on the FULL requested set up front —
        # before the #1961 idempotency pre-filter can drop already-present
        # entries — so a source carrying the wrong ``research_task_id`` is
        # rejected even when its URL already exists in the notebook.
        _validate_research_task_provenance(candidates, task_id)

        started_at = time.monotonic()
        delay = value.initial_delay
        attempt = 1
        verified_imported: list[ResearchImportedSourceRecord] = []
        verified_imported_ids: set[str] = set()

        # Anchor verified-success on URLs of *new* sources (not on a
        # baseline→current URL delta) so concurrent additions from another
        # session and pre-existing URLs cannot satisfy the check. The same
        # snapshot doubles as the idempotency pre-filter baseline (#1961).
        baseline: list[SourceRecord] | None
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
        already_present: list[ResearchPresentSourceRecord] = []
        if not allow_duplicate and baseline is not None:
            existing_by_norm_url: dict[str, SourceRecord] = {}
            for existing in baseline:
                if existing.url:
                    existing_by_norm_url.setdefault(
                        _normalize_import_verification_url(existing.url), existing
                    )
            candidates, already_present = _partition_requested_sources(
                candidates, existing_by_norm_url
            )
            if already_present:
                logger.info(
                    "Idempotent research import into %s: skipping %d source(s) already "
                    "present by URL; importing %d new source(s)",
                    notebook_id,
                    len(already_present),
                    len(candidates),
                )
            # Every requested source was already present — nothing new to
            # import. Return without an RPC (and without entering the
            # timeout-retry loop), reporting the skipped set.
            if not candidates:
                return ResearchImportVerifyResult(already_present=tuple(already_present))

        requested_urls_norm = _requested_import_verification_urls(candidates)
        # Track how many non-URL entries (research reports, pasted text) the
        # request includes so concurrent no-URL additions cannot inflate the
        # synthesized return after a timeout.
        requested_no_url_count = _no_import_verification_url_entry_count(candidates)

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
                    [entry.id for entry in verified_imported],
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
                    ResearchImportBatchInput(
                        notebook_id=notebook_id,
                        task_id=task_id,
                        candidates=tuple(candidates),
                        # The first attempt always runs on its natural window
                        # even when the budget is already spent
                        # (``max_elapsed=0`` is a documented "one shot" idiom);
                        # only retries must fit.
                        remaining_budget=attempt_budget if budget_is_viable else None,
                    )
                )
                return ResearchImportVerifyResult(
                    imported=_merge_imported_sources(
                        imported, verified_imported, verified_imported_ids
                    ),
                    already_present=tuple(already_present),
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
                            candidates=candidates,
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
                            return ResearchImportVerifyResult(
                                imported=_merge_imported_sources(
                                    outcome.fully_verified_entries,
                                    verified_imported,
                                    verified_imported_ids,
                                ),
                                already_present=tuple(already_present),
                            )
                        if outcome.filtered:
                            verified_imported.extend(outcome.newly_verified)
                            verified_imported_ids.update(
                                entry.id for entry in outcome.newly_verified
                            )
                            candidates = outcome.candidates
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
                                    outcome.removed_count + len(candidates),
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
                                    len(candidates),
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
                delay = min(delay * value.backoff_factor, max_delay)
                attempt += 1


__all__ = ["ResearchService"]
