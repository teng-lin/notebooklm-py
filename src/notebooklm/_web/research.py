"""Research API for NotebookLM web/drive research.

Provides operations for starting research sessions, polling for results,
and importing discovered sources into notebooks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .._idempotency import call_unconfirmed_on_transport_loss, mark_unconfirmed
from .._notebook_metadata import NotebookSourceLister
from .._research import BaseResearchAPI, validate_discover
from .._research_import import (
    _WEB_RESEARCH_IMPORT_POLICY,
    _coerce_research_sources,
    _import_research_read_timeout,
    _imported_result,
    _is_import_research_failed_precondition,
    _merge_imported_sources,
    _no_import_verification_url_entry_count,
    _normalize_import_verification_url,
    _partition_requested_sources,
    _reconcile_import_probe,
    _requested_import_verification_urls,
    _ResearchImportBatch,
    _validate_research_task_provenance,
)
from .._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_TIMEOUT,
    MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT,
)
from .._types.research import (
    RESEARCH_SOURCE_TYPE_DRIVE,
    RESEARCH_SOURCE_TYPE_WEB,
    ResearchSource,
    ResearchSourceInput,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from ..exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    ResearchStartUnavailableError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    ValidationError,
)
from ..rpc import RPCMethod
from ..types import CitedSourceSelection
from .contracts import RpcCaller
from .notebooks import create_default_source_lister
from .rows.research import ImportedSourceRow, ResearchStartRow, unwrap_import_rows
from .rows.research_task import parse_discover_task, parse_research_task_models

if TYPE_CHECKING:
    from ..types import Source

__all__ = [
    "CitedSourceSelection",
    "ResearchAPI",
    "ResearchSource",
    "ResearchStart",
    "ResearchStatus",
    "ResearchTask",
    "WebResearchAPI",
]

# Preserve the historical logger key across the whole-module move.
logger = logging.getLogger("notebooklm._research")


def _is_deep_start_null_result_error(exc: RPCError) -> bool:
    method_id = RPCMethod.START_DEEP_RESEARCH.value
    # The decoder raises one of two stable messages for a wrb.fr null payload,
    # with or without an attached status code (see ``_web/wire/decoder.py``). We match
    # on those stable phrases rather than the obfuscated method id / raw status
    # code, which the decoder deliberately keeps OUT of the human-readable
    # message (#1921). If the wording drifts, fall through and re-raise the
    # original RPCError rather than overclassifying unrelated failures.
    null_result_markers = ("rejected this request", "returned an empty result")
    return (
        exc.method_id == method_id
        and method_id in exc.found_ids
        and any(marker in str(exc).lower() for marker in null_result_markers)
    )


class WebResearchAPI(BaseResearchAPI):
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

    _import_policy = _WEB_RESEARCH_IMPORT_POLICY

    def __init__(
        self,
        rpc: RpcCaller,
        *,
        source_lister: NotebookSourceLister | None = None,
        base_timeout: float | None = DEFAULT_TIMEOUT,
        import_research_timeout: float | None = AUTO_READ_TIMEOUT,
    ):
        """Initialize the research API.

        Args:
            rpc: RPC dispatch surface (typically the shared client session).
            base_timeout: The owning client's configured ``timeout=``. The
                batch-scaled IMPORT_RESEARCH window is floored at it so a
                caller's larger explicit budget is never silently shortened
                (#2205). Standalone ``WebResearchAPI(rpc)`` keeps the historical
                behavior via the shared 30 s default.
            import_research_timeout: Per-attempt read window for
                IMPORT_RESEARCH, read exactly like ``chat_timeout``: unset
                (default) keeps the batch-scaled, ``base_timeout``-floored
                window; a value replaces both; ``None`` inherits
                ``base_timeout`` verbatim.
            source_lister: Optional :class:`NotebookSourceLister` used by
                :meth:`import_sources_with_verification` to snapshot baseline
                source IDs before the import call and probe sources on
                timeout. When omitted, a default lister is built from
                ``rpc`` — mirrors the ``WebNotebooksAPI`` wiring pattern, so
                ``WebResearchAPI(rpc)`` works standalone with no cross-API
                dependency.
        """
        self._rpc = rpc
        super().__init__(
            source_lister=source_lister or create_default_source_lister(self._rpc),
            base_timeout=base_timeout,
            import_research_timeout=import_research_timeout,
        )

    async def _rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        """Delegate through the current RPC caller for late-bound overrides.

        Mirrors :meth:`WebNotebooksAPI._rpc_call` so direct WebResearchAPI RPC paths
        pick up post-construction changes to the underlying caller's
        ``rpc_call`` method (advanced tests / instrumentation).
        """
        return await self._rpc.rpc_call(
            method,
            params,
            source_path=source_path,
            allow_null=allow_null,
            _is_retry=_is_retry,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
        )

    @staticmethod
    def _build_report_import_entry(title: str, markdown: str) -> list[Any]:
        """Build the special deep-research report entry used by IMPORT_RESEARCH."""
        return [None, [title, markdown], None, 3, None, None, None, None, None, None, 3]

    @staticmethod
    def _build_web_import_entry(url: str, title: str) -> list[Any]:
        """Build a standard web-source import entry used by IMPORT_RESEARCH."""
        return [None, None, [url, title], None, None, None, None, None, None, None, 2]

    async def _poll_task_models(self, notebook_id: str) -> list[ResearchTask]:
        params = [None, None, notebook_id]
        result = await self._rpc.rpc_call(
            RPCMethod.POLL_RESEARCH,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        return parse_research_task_models(result)

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
        logger.debug(
            "Starting %s research in notebook %s: %s",
            mode,
            notebook_id,
            query[:50] if query else "",
        )
        source_lower = source.lower()
        mode_lower = mode.lower()

        if source_lower not in ("web", "drive"):
            raise ValidationError(f"Invalid source '{source}'. Use 'web' or 'drive'.")
        if mode_lower not in ("fast", "deep"):
            raise ValidationError(f"Invalid mode '{mode}'. Use 'fast' or 'deep'.")
        if mode_lower == "deep" and source_lower == "drive":
            raise ValidationError("Deep Research only supports Web sources.")

        # Same constants the read side decodes ``task_info[1][1]`` with, so the
        # round trip has one definition of the tag rather than two (#1964).
        source_type = (
            RESEARCH_SOURCE_TYPE_WEB if source_lower == "web" else RESEARCH_SOURCE_TYPE_DRIVE
        )

        # The whole research feature is Google's "DiscoverSources" pipeline:
        # fast -> DiscoverSourcesManifold, deep -> DiscoverSourcesAsync,
        # POLL_RESEARCH -> ListDiscoverSourcesJob, IMPORT_RESEARCH ->
        # FinishDiscoverSourcesRun. "Research" is our label for that pipeline.
        if mode_lower == "fast":
            params = [[query, source_type], None, 1, notebook_id]
            rpc_id = RPCMethod.START_FAST_RESEARCH
        else:
            params = [None, [1], [query, source_type], 5, notebook_id]
            rpc_id = RPCMethod.START_DEEP_RESEARCH

        try:
            result = await call_unconfirmed_on_transport_loss(
                lambda: self._rpc.rpc_call(
                    rpc_id,
                    params,
                    source_path=f"/notebook/{notebook_id}",
                ),
                method=rpc_id,
                what=f"research.start ({mode_lower})",
            )
        except (AuthError, RateLimitError, ServerError, NetworkError):
            raise
        except RPCError as exc:
            if mode_lower == "deep" and _is_deep_start_null_result_error(exc):
                raise ResearchStartUnavailableError(
                    notebook_id,
                    mode_lower,
                    method_id=exc.method_id,
                    raw_response=exc.raw_response,
                    rpc_code=exc.rpc_code,
                    found_ids=exc.found_ids,
                ) from exc
            raise

        if result and isinstance(result, list) and len(result) > 0:
            start_row = ResearchStartRow(result)
            task_id = start_row.task_id_raw
            # v0.8.0 (#1342): a falsey ``task_id`` means no task was created —
            # raise (mirrors ``_parse_generation_result``'s missing id).
            if not task_id:
                raise DecodingError(
                    f"research.start returned no task id: {result!r}", method_id=rpc_id.value
                )
            report_id = start_row.report_id
            return ResearchStart(
                task_id=task_id,
                report_id=report_id,
                notebook_id=notebook_id,
                query=query,
                mode=mode_lower,
            )
        # v0.8.0 (#1342): an empty / non-list payload is couldn't-start — raise.
        raise DecodingError(
            "research.start returned an empty / non-list payload", method_id=rpc_id.value
        )

    async def discover(
        self,
        notebook_id: str,
        query: str,
        *,
        mode: str = "default",
    ) -> ResearchTask:
        """Discover web sources synchronously (the "Discover sources" dialog call).

        One blocking ``DISCOVER_SOURCES`` round trip (about 8 s live) that
        returns the ranked sources and an overview, instead of the
        :meth:`start` → :meth:`poll` cycle. The backend also records the call
        as a completed job, so the returned ``task_id`` works with
        :meth:`import_sources` and :meth:`cancel` exactly like a polled run.

        Args:
            notebook_id: The notebook ID.
            query: What to look for. Required for ``default`` / ``raw``;
                ignored (sent empty) for the curious modes.
            mode: ``"default"`` (LLM-ranked search), ``"raw"`` (plain search),
                ``"curious"`` / ``"curious_raw"`` (the backend picks a topic;
                ``query`` is dropped and the returned task's ``query`` is
                ``""``). Web sources only — the Drive corpus fails
                server-side on this route.

        Returns:
            A completed :class:`~notebooklm._types.research.ResearchTask`
            (``status`` ``COMPLETED``, ``sources``, ``summary`` = the overview,
            ``discovery_mode`` = the mode sent).

        Raises:
            ValidationError: On an unknown ``mode`` or an empty ``query``
                outside the curious modes.
            DecodingError: If the response carries no job id.
        """
        query, _mode_label, discovery_mode = validate_discover(query, mode)
        logger.debug(
            "Discovering sources (%s) in notebook %s: %s",
            _mode_label,
            notebook_id,
            query[:50] if query else "",
        )
        # Same request message as START_FAST_RESEARCH (``DiscoverSourcesRequest``):
        # [DiscoveryContext{context, corpus}, RequestContext, DiscoveryMode, project_id].
        params = [[query, RESEARCH_SOURCE_TYPE_WEB], None, int(discovery_mode), notebook_id]
        try:
            result = await self._rpc_call(
                RPCMethod.DISCOVER_SOURCES,
                params,
                source_path=f"/notebook/{notebook_id}",
            )
        except (NetworkError, ServerError) as exc:
            # The request may have reached the server and recorded a job
            # before its response was lost; internal retries are already off
            # (NON_IDEMPOTENT_NO_RETRY), and the marker stops callers from
            # blindly re-running a quota-bearing search. Same outcome the
            # Android adapter's ``call_unconfirmed_on_transport_loss`` gives.
            raise mark_unconfirmed(exc) from None
        try:
            return parse_discover_task(result, query=query, discovery_mode=discovery_mode)
        except DecodingError as error:
            # The call already ran (and recorded a job) — a payload we cannot
            # read is an unconfirmed write, same as the Android adapter.
            raise mark_unconfirmed(error) from None

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
        logger.debug("Polling research status for notebook %s", notebook_id)
        parsed_tasks = self._select_polled_tasks(
            await self._poll_task_models(notebook_id),
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

    def _wait_observed_status(self, result: ResearchTask) -> ResearchStatus:
        """Preserve Web wait's pre-neutralization pinned-absence status."""
        if result.status is ResearchStatus.NOT_FOUND:
            return ResearchStatus.NO_RESEARCH
        return result.status

    async def cancel(self, notebook_id: str, run_id: str) -> None:
        """Cancel an in-flight research (DiscoverSources) run.

        Fire-and-forget. An IN_PROGRESS run transitions to a terminal
        ``FAILED`` state shortly after this call; cancelling an
        already-terminal run is a silent no-op.

        Args:
            notebook_id: Routing context only (sets the request ``source-path``).
                **Not a scoping or authorization boundary**: the server keys the
                cancel solely on ``run_id`` — live-verified that a valid
                ``run_id`` is cancelled even when ``notebook_id`` names a
                different / non-existent notebook (or is empty). Pass the run's
                real notebook for correct routing, but do not rely on it to
                prevent cancelling a run from the "wrong" notebook.
            run_id: The **poll-level** run id — i.e. ``task.task_id`` from
                :meth:`poll` (equivalently ``ResearchTask.task_id``). For a
                **deep** research run started via :meth:`start`, this is the
                ``report_id`` returned by ``start`` — live-verified: deep's
                ``start().task_id`` is a *sessionId* that :meth:`poll` reports as
                ``NOT_FOUND``, and cancelling with it is a silent no-op (the run
                keeps running); only ``report_id`` actually stops a deep run. For
                a **fast** run it is ``start().task_id`` (fast returns no
                ``report_id``). When in doubt, pass the ``task_id`` surfaced by
                :meth:`poll` — for both modes that is the value the server
                accepts.

        Returns:
            ``None``. This is **fire-and-forget**: the server returns an empty
            payload (``[]``) unconditionally and does **not** validate ``run_id``
            (an unknown / garbage id also yields ``[]``), so the response carries
            no success signal and this method never raises on an unknown id. The
            only way to confirm a cancel took effect is to :meth:`poll`
            afterward — live-verified that a cancelled IN_PROGRESS run surfaces
            as ``FAILED`` within a few seconds, and that re-cancelling an
            already-terminal run is a silent no-op.
        """
        logger.debug("Cancelling research run %s in notebook %s", run_id, notebook_id)
        # Field 3 carries the run id; the optional field-1 client context is
        # omitted to match ``_poll_task_models`` (``[None, None, <id>]``). Routed
        # through ``self._rpc_call`` so a post-construction override of the RPC
        # caller (advanced tests / instrumentation) is honoured.
        await self._rpc_call(
            RPCMethod.CANCEL_RESEARCH,
            [None, None, run_id],
            source_path=f"/notebook/{notebook_id}",
        )

    async def _send_import(
        self,
        notebook_id: str,
        batch: _ResearchImportBatch,
        *,
        _remaining_budget: float | None,
    ) -> list[dict[str, str]]:
        source_array = [
            self._build_report_import_entry(item.source.title, item.source.report_markdown)
            if item.kind == "report"
            else self._build_web_import_entry(item.source.url, item.source.title)
            for item in batch.items
        ]

        result = await self._rpc.rpc_call(
            RPCMethod.IMPORT_RESEARCH,
            [None, [1], batch.task_id, notebook_id, source_array],
            source_path=f"/notebook/{notebook_id}",
            read_timeout=_import_research_read_timeout(
                len(source_array),
                base_timeout=self._base_timeout,
                override=self._import_research_timeout,
                remaining_budget=_remaining_budget,
            ),
        )
        imported = []
        # ``unwrap_import_rows`` centralises the ``[[src1, ...]]`` envelope probe
        # behind the research row adapter; an unrecognised shape → ``[]``.
        for src_data in unwrap_import_rows(result):
            row = ImportedSourceRow(src_data)
            if not row.is_well_formed:
                continue
            # An absent / non-list id envelope legitimately means "skip" (id None).
            src_id = row.source_id
            if src_id:
                imported.append({"id": src_id, "title": row.title_slot})

        return imported

    async def _import_sources_with_verification(
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
        except (NetworkError, RPCError) as snapshot_exc:
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

        last_error: RPCTimeoutError | RPCError | None = None
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
            except (RPCTimeoutError, RPCError) as exc:
                last_error = exc
                if isinstance(exc, RPCError) and not _is_import_research_failed_precondition(exc):
                    _log_discarded_progress()
                    raise  # non-FAILED_PRECONDITION RPCErrors surface immediately (#2187)
                reason = (
                    "timed out"
                    if isinstance(exc, RPCTimeoutError)
                    else "hit a retry-time FAILED_PRECONDITION"
                )
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
                            if isinstance(exc, RPCError):
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
                    except (NetworkError, RPCError) as probe_exc:
                        # CancelledError is a BaseException, not Exception, and
                        # is not in this tuple — it propagates naturally for
                        # callers that need to cancel the operation cleanly.
                        logger.warning(
                            "Failed to probe server state after %s: %s; %s",
                            reason,
                            probe_exc,
                            "falling back to retry"
                            if not isinstance(exc, RPCError)
                            else "surfacing the original error",
                        )

                if remaining <= 0:
                    _log_discarded_progress()
                    raise

                if isinstance(exc, RPCError):  # no verified-success return above
                    _log_discarded_progress()
                    raise

                # Report-only imports cannot be reconciled: the retained live
                # LoadSource round trip did not expose submitted Markdown.
                # Never resend after a lost response.
                if not requested_urls_norm:
                    logger.warning(
                        "IMPORT_RESEARCH %s for notebook %s with no URLs "
                        "to verify; giving up after the first attempt to avoid "
                        "duplicate inflation",
                        reason,
                        notebook_id,
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


# Backward-compatible private Web-module spelling. Composition imports the
# explicit backend class; existing direct imports keep resolving to the Web
# implementation without making the neutral base import this module.
ResearchAPI = WebResearchAPI
