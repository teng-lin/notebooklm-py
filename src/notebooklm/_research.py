"""Research API for NotebookLM web/drive research.

Provides operations for starting research sessions, polling for results,
and importing discovered sources into notebooks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from . import research as _research_pub
from ._deprecation import future_errors_enabled, warn_deprecated
from ._notebook_metadata import NotebookSourceLister, create_default_source_lister
from ._research_task_parser import parse_research_task_models
from ._runtime.contracts import RpcCaller
from ._types.research import (
    ResearchSource,
    ResearchSourceInput,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from .exceptions import (
    DecodingError,
    NetworkError,
    ResearchTaskMismatchError,
    ResearchTimeoutError,
    RPCError,
    RPCTimeoutError,
    ValidationError,
)
from .rpc import RPCMethod
from .types import CitedSourceSelection

if TYPE_CHECKING:
    from .types import Source

__all__ = [
    "CitedSourceSelection",
    "ResearchAPI",
    "ResearchSource",
    "ResearchStart",
    "ResearchStatus",
    "ResearchTask",
]

logger = logging.getLogger(__name__)

# Sentinel marking "the canonical ``initial_interval`` keyword was not passed"
# in ``wait_for_completion``. The default stays this ``object()`` sentinel (not a
# literal ``5.0``) so the public-API compatibility audit's default-repr check
# sees no changed-default break; when the caller leaves it unset we resolve the
# cadence to ``_DEFAULT_RESEARCH_POLL_INTERVAL`` below.
_INITIAL_INTERVAL_UNSET: Any = object()

# Default poll cadence (seconds between status checks) used when
# ``initial_interval`` is left unset. Preserved verbatim so default-shape callers
# keep the same behavior.
_DEFAULT_RESEARCH_POLL_INTERVAL = 5.0


# ---------------------------------------------------------------------------
# IMPORT_RESEARCH timeout-verification helpers
#
# IMPORT_RESEARCH is classified NON_IDEMPOTENT_NO_RETRY in IDEMPOTENCY_REGISTRY
# (see #808): the executor will surface the first 5xx/timeout to the caller
# rather than retry blindly, because the wire protocol has no client-token
# slot and a naive retry duplicates every source. ``ResearchAPI``'s
# verification path sidesteps that constraint by snapshotting baseline
# sources before the call and matching post-call ``sources.list`` URLs
# against the request — disambiguating "server already committed but the
# response was lost" from "request truly failed". These helpers mirror the
# CLI-only logic that originally landed in PR #321 / #327; they live in the
# library now so Python API consumers get the same deep-research fix the
# CLI does (issue #315).
# ---------------------------------------------------------------------------


def _normalize_import_verification_url(url: str) -> str:
    """Lowercase scheme + host and strip a trailing slash for comparison.

    Distinct from ``notebooklm.research.normalize_citation_url`` (used for
    matching URLs cited inside report markdown): this variant drops the URL
    fragment because the server stores fragments stripped, and skips the
    trailing-punctuation strip because these URLs come from a structured
    ``sources.list`` payload rather than free-form markdown.
    """
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.query,
            "",
        )
    )


def _source_import_verification_url(source: ResearchSource) -> str | None:
    url = source.url
    if not url:
        return None
    return _normalize_import_verification_url(url)


def _requested_import_verification_urls(sources: Sequence[ResearchSource]) -> set[str]:
    return {url for source in sources if (url := _source_import_verification_url(source))}


def _no_import_verification_url_entry_count(sources: Sequence[ResearchSource]) -> int:
    return sum(1 for source in sources if _source_import_verification_url(source) is None)


def _coerce_research_source(source: ResearchSourceInput) -> ResearchSource:
    if isinstance(source, ResearchSource):
        return source
    return ResearchSource.from_public_dict(source)


def _coerce_research_sources(sources: Sequence[ResearchSourceInput]) -> list[ResearchSource]:
    return [_coerce_research_source(source) for source in sources]


def _is_importable_report_source(
    source_input: ResearchSourceInput,
    source: ResearchSource,
) -> bool:
    """Preserve the public-dict report predicate from the legacy importer."""
    if not source.is_report or not source.report_markdown:
        return False
    if isinstance(source_input, ResearchSource):
        return isinstance(source.title, str)
    return isinstance(source_input.get("title"), str) and isinstance(
        source_input.get("report_markdown"), str
    )


def _imported_source_entry(source: Source) -> dict[str, str]:
    return {"id": source.id, "title": source.title or source.url or ""}


def _merge_imported_sources(
    imported: list[dict[str, str]],
    verified_imported: list[dict[str, str]],
    verified_imported_ids: set[str],
) -> list[dict[str, str]]:
    if not verified_imported:
        return imported
    return [
        *verified_imported,
        *(entry for entry in imported if entry.get("id") not in verified_imported_ids),
    ]


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
        rpc: RpcCaller,
        *,
        source_lister: NotebookSourceLister | None = None,
    ):
        """Initialize the research API.

        Args:
            rpc: RPC dispatch surface (typically the shared client session).
            source_lister: Optional :class:`NotebookSourceLister` used by
                :meth:`import_sources_with_verification` to snapshot baseline
                source IDs before the import call and probe sources on
                timeout. When omitted, a default lister is built from
                ``rpc`` — mirrors the ``NotebooksAPI`` wiring pattern, so
                ``ResearchAPI(rpc)`` works standalone with no cross-API
                dependency.
        """
        self._rpc = rpc
        self._source_lister = source_lister or create_default_source_lister(self._rpc)

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

        Mirrors :meth:`NotebooksAPI._rpc_call` so direct ResearchAPI RPC paths
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

    async def _poll_task_models(self, notebook_id: str) -> list[ResearchTask]:
        params = [None, None, notebook_id]
        result = await self._rpc.rpc_call(
            RPCMethod.POLL_RESEARCH,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        return parse_research_task_models(result)

    @staticmethod
    def _select_polled_tasks(
        parsed_tasks: list[ResearchTask],
        *,
        notebook_id: str,
        task_id: str | None,
        warn_on_ambiguous: bool,
    ) -> list[ResearchTask]:
        # Task-id discriminator: when supplied, filter parsed_tasks down to
        # the matched task so callers iterating ``tasks`` don't see siblings.
        # When omitted but multiple tasks are in flight, surface the latent
        # cross-wire hazard via a DeprecationWarning while preserving legacy
        # latest-task selection.
        if task_id is not None:
            return [task for task in parsed_tasks if task.task_id == task_id]
        if warn_on_ambiguous and len(parsed_tasks) > 1:
            warn_deprecated(
                (
                    f"ResearchAPI.poll(notebook_id={notebook_id!r}) returned "
                    f"{len(parsed_tasks)} in-flight tasks but no task_id "
                    f"discriminator was supplied. The latest task is "
                    f"returned for back-compat, but this is ambiguous and "
                    f"may surface results for the wrong task. Pass "
                    f"task_id=<id> (from research.start) to select "
                    f"explicitly. The None default will be removed in a "
                    f"future major release."
                ),
                # No pinned removal version yet (re-pin tracked by #1363); the
                # message already says "a future major release".
                removal=None,
                # caller -> poll -> _select_polled_tasks -> warn_deprecated.
                stacklevel=4,
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
    ) -> ResearchStart | None:
        """Start a research session.

        Args:
            notebook_id: The notebook ID.
            query: The research query.
            source: "web" or "drive".
            mode: "fast" or "deep" (deep is web-only).

        Returns:
            A :class:`~notebooklm._types.research.ResearchStart` (``task_id`` /
            ``report_id`` / ``notebook_id`` / ``query`` / ``mode``), or ``None``
            when the backend returned no task. Under
            ``NOTEBOOKLM_FUTURE_ERRORS`` (#1342) an empty/non-list payload or
            falsey ``task_id`` raises ``DecodingError`` instead.

        Raises:
            ValidationError: If source/mode combination is invalid.
            DecodingError: Under the v0.8.0 preview, on an empty/non-list payload
                or falsey ``task_id`` (no task created).
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

        # 1 = Web, 2 = Drive
        source_type = 1 if source_lower == "web" else 2

        if mode_lower == "fast":
            params = [[query, source_type], None, 1, notebook_id]
            rpc_id = RPCMethod.START_FAST_RESEARCH
        else:
            params = [None, [1], [query, source_type], 5, notebook_id]
            rpc_id = RPCMethod.START_DEEP_RESEARCH

        result = await self._rpc.rpc_call(
            rpc_id,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        if result and isinstance(result, list) and len(result) > 0:
            task_id = result[0]
            # v0.8.0 preview (#1342): a falsey ``task_id`` means no task was
            # created — raise (mirrors ``_parse_generation_result``'s missing id).
            if not task_id and future_errors_enabled():
                raise DecodingError(
                    f"research.start returned no task id: {result!r}", method_id=rpc_id.value
                )
            report_id = result[1] if len(result) > 1 else None
            return ResearchStart(
                task_id=task_id,
                report_id=report_id,
                notebook_id=notebook_id,
                query=query,
                mode=mode_lower,
            )
        # v0.8.0 preview (#1342): an empty / non-list payload is couldn't-start.
        if future_errors_enabled():
            raise DecodingError(
                "research.start returned an empty / non-list payload", method_id=rpc_id.value
            )
        return None

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
                ``None`` and multiple tasks are in flight, a
                :class:`DeprecationWarning` is emitted and the *latest* task is
                returned (legacy behavior); a single in-flight task is silent.
                Migration: pass the ``task_id`` from :meth:`start` on every
                ``poll`` — the ``None`` default is removed in a future major.

        Returns:
            A :class:`~notebooklm._types.research.ResearchTask` for the selected
            task. Use attribute access:
            - ``task.task_id``: task/report identifier for the selected task
            - ``task.status``: a :class:`~notebooklm._types.research.ResearchStatus`
              (``IN_PROGRESS`` / ``COMPLETED`` / ``FAILED`` / ``NO_RESEARCH`` /
              ``NOT_FOUND``); equals the historical strings
            - ``task.query``: original research query text
            - ``task.sources``: tuple of ``ResearchSource`` (each exposes ``url``,
              ``title``, ``result_type``, ``research_task_id``, ``report_markdown``)
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
            # The ambiguity warning only applies to the unfiltered (task_id is
            # None) path; when a discriminator is pinned, _select_polled_tasks
            # filters before the warn branch. Gating it here matches
            # wait_for_completion and keeps the intent explicit.
            warn_on_ambiguous=task_id is None,
        )

        if parsed_tasks:
            return self._public_poll_result(parsed_tasks[0], parsed_tasks)

        # A concrete pinned ``task_id`` that matched nothing is a poll-observed
        # absence of that specific task — a typed ``NOT_FOUND`` sentinel
        # carrying the requested id. A falsy ``task_id`` (``None`` for the
        # unfiltered poll, or the degenerate empty string) is not a meaningful
        # discriminator, so it stays ``NO_RESEARCH`` ("nothing in flight") and
        # preserves the legacy empty-poll dict shape. See ADR-0019 Rule 4
        # (#1346).
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
        """Poll until research reaches a terminal state or times out.

        When the first poll returns a concrete ``task_id``, subsequent polls
        pass it back through :meth:`poll` as the discriminator. This prevents a
        later concurrent research task in the same notebook from substituting
        its sources/report into this wait loop.

        Args:
            notebook_id: The notebook ID.
            task_id: Optional research task discriminator. Pass the value
                returned by :meth:`start` when available.
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
            ResearchTimeoutError: If research does not reach a terminal status
                before ``timeout`` elapses. Subclass of
                :class:`WaitTimeoutError` and the built-in :class:`TimeoutError`,
                so ``except TimeoutError`` continues to catch it.
            ValueError: If ``timeout`` is negative or the poll interval is not
                positive.
            TypeError: If the resolved poll interval is not a number.
        """
        # The sentinel default means "``initial_interval`` was not supplied" —
        # fall back to the default cadence. An *explicit* non-numeric value
        # (e.g. initial_interval=None or initial_interval="1") is a caller bug;
        # fail fast with TypeError rather than silently coercing it back to the
        # default.
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
                await self._poll_task_models(notebook_id),
                notebook_id=notebook_id,
                task_id=pinned_task_id,
                warn_on_ambiguous=pinned_task_id is None,
            )
            selected_task = parsed_tasks[0] if parsed_tasks else None
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

    async def import_sources(
        self,
        notebook_id: str,
        task_id: str,
        sources: Sequence[ResearchSourceInput],
    ) -> list[dict[str, str]]:
        """Import selected research sources into the notebook.

        Args:
            notebook_id: The notebook ID.
            task_id: The research task ID.
            sources: List of sources to import, each with 'url' and 'title'.
                Deep research results from poll() may also include a report
                entry with 'report_markdown' and 'research_task_id'.

        Returns:
            List of imported sources with 'id' and 'title'.

        Note:
            The API response can be incomplete - it may return fewer items than
            were actually imported. All requested sources typically get imported
            successfully, but the return value may not reflect all of them.
            To reliably verify imports, check the notebook's source list using
            `client.sources.list(notebook_id)` after calling this method.
        """
        if not sources:
            return []
        source_inputs: list[ResearchSourceInput] = list(sources)
        source_models = _coerce_research_sources(source_inputs)
        logger.debug(
            "Importing %d research sources into notebook %s",
            len(source_models),
            notebook_id,
        )

        # Per-source ``research_task_id`` must match the caller's
        # ``task_id`` when both are present. A mismatch is the wire-crossing
        # bug — importing under the wrong task would mis-attribute
        # provenance. We do this scan BEFORE the multi-task batch check so
        # callers get the precise diagnostic (which mismatched source +
        # which task) instead of the generic "multiple tasks" message.
        for source in source_models:
            source_task_id = source.research_task_id
            if source_task_id and source_task_id != task_id:
                raise ResearchTaskMismatchError(
                    task_id=task_id,
                    source_research_task_id=source_task_id,
                )

        research_task_ids = {
            source.research_task_id for source in source_models if source.research_task_id
        }
        if len(research_task_ids) > 1:
            raise ValidationError(
                "Cannot import sources from multiple research tasks in one batch."
            )
        effective_task_id = next(iter(research_task_ids), task_id)

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
        if not valid_sources and not report_sources:
            return []

        source_array = []
        for report_source in report_sources:
            source_array.append(
                self._build_report_import_entry(
                    report_source.title,
                    report_source.report_markdown,
                )
            )
        source_array.extend(
            self._build_web_import_entry(src.url, src.title) for src in valid_sources
        )

        params = [None, [1], effective_task_id, notebook_id, source_array]

        result = await self._rpc.rpc_call(
            RPCMethod.IMPORT_RESEARCH,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        imported = []
        if result and isinstance(result, list):
            # Unwrap an ``[[src1, ...]]`` envelope via ``first[0]`` (not chained).
            if len(result) > 0 and isinstance(result[0], list) and len(result[0]) > 0:
                first = result[0]
                if isinstance(first[0], list):
                    result = first

            for src_data in result:
                if isinstance(src_data, list) and len(src_data) >= 2:
                    # Absent/non-list id envelope legitimately means "skip" (id None).
                    id_envelope = src_data[0]
                    src_id = (
                        id_envelope[0] if id_envelope and isinstance(id_envelope, list) else None
                    )
                    if src_id:
                        imported.append({"id": src_id, "title": src_data[1]})

        return imported

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
    ) -> list[dict[str, str]]:
        """Import sources with timeout-tolerant verification.

        Use this in preference to :meth:`import_sources` for deep research:
        the underlying ``IMPORT_RESEARCH`` RPC commonly responds in >30 s on
        deep-research payloads and a one-shot call times out at the client
        even when the server has already committed.

        Lifecycle:

        1. Snapshot baseline source IDs via ``client.sources.list``.
        2. Call :meth:`import_sources`.
        3. On :class:`RPCTimeoutError`, probe ``client.sources.list`` again:
           - If every requested URL appears among *new* (post-baseline)
             sources, treat as success and return the imported entries
             without retrying — the server committed before the response
             was lost.
           - Otherwise filter out URLs that are already present (the
             server committed *some* of the batch) and retry only the
             remaining sources.
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
            RPCTimeoutError: If retries exhaust the ``max_elapsed`` budget.
        """
        if not sources:
            return []
        source_inputs: list[ResearchSourceInput] = list(sources)
        source_models = _coerce_research_sources(sources)

        started_at = time.monotonic()
        delay = initial_delay
        attempt = 1
        verified_imported: list[dict[str, str]] = []
        verified_imported_ids: set[str] = set()

        requested_urls_norm = _requested_import_verification_urls(source_models)
        # Track how many non-URL entries (research reports, pasted text) the
        # request includes so concurrent no-URL additions cannot inflate the
        # synthesized return after a timeout.
        requested_no_url_count = _no_import_verification_url_entry_count(source_models)

        # Anchor verified-success on URLs of *new* sources (not on a
        # baseline→current URL delta) so concurrent additions from another
        # session and pre-existing URLs cannot satisfy the check.
        baseline_ids: set[str] | None
        try:
            baseline = await self._source_lister.list(notebook_id, strict=True)
            baseline_ids = {src.id for src in baseline}
        except (NetworkError, RPCError) as snapshot_exc:
            logger.warning(
                "Pre-import sources.list snapshot failed for %s: %s; "
                "verified-success path disabled for this call",
                notebook_id,
                snapshot_exc,
            )
            baseline_ids = None

        while True:
            try:
                imported = await self.import_sources(notebook_id, task_id, source_inputs)
                return _merge_imported_sources(imported, verified_imported, verified_imported_ids)
            except RPCTimeoutError:
                elapsed = time.monotonic() - started_at
                remaining = max_elapsed - elapsed

                if requested_urls_norm:
                    try:
                        current = await self._source_lister.list(notebook_id, strict=True)
                        new_sources = (
                            [src for src in current if src.id not in baseline_ids]
                            if baseline_ids is not None
                            else []
                        )
                        new_urls_norm = {
                            _normalize_import_verification_url(src.url)
                            for src in new_sources
                            if src.url
                        }
                        current_urls_norm = {
                            _normalize_import_verification_url(src.url)
                            for src in current
                            if src.url
                        }
                        committed_urls_norm = requested_urls_norm & new_urls_norm
                        if baseline_ids is not None and requested_urls_norm.issubset(new_urls_norm):
                            logger.warning(
                                "IMPORT_RESEARCH timed out for notebook %s but "
                                "sources.list shows all %d requested URLs among "
                                "new sources; treating as success and skipping "
                                "retry to avoid duplicate inflation",
                                notebook_id,
                                len(requested_urls_norm),
                            )
                            timeout_verified: list[dict[str, str]] = []
                            remaining_no_url = requested_no_url_count
                            for src in new_sources:
                                if (
                                    src.url
                                    and _normalize_import_verification_url(src.url)
                                    in requested_urls_norm
                                ):
                                    timeout_verified.append(_imported_source_entry(src))
                                elif not src.url and remaining_no_url > 0:
                                    timeout_verified.append(_imported_source_entry(src))
                                    remaining_no_url -= 1
                            return _merge_imported_sources(
                                timeout_verified, verified_imported, verified_imported_ids
                            )
                        source_norms = [
                            (
                                source_input,
                                source,
                                _source_import_verification_url(source),
                            )
                            for source_input, source in zip(
                                source_inputs, source_models, strict=True
                            )
                        ]
                        # Filter for retry: drop already-present URLs.
                        # Additionally, when *any* URL was verified
                        # committed, drop no-URL entries (deep-research
                        # reports): reports are appended FIRST in the
                        # IMPORT_RESEARCH payload (see
                        # ``_build_report_import_entry`` usage in
                        # ``import_sources``), so a URL newly observed after
                        # this attempt implies the report committed too.
                        # Pre-existing URLs only de-dupe URL entries; they do
                        # not prove this request committed no-URL reports.
                        # Without this guard,
                        # each retry duplicates the report server-side.
                        # When no URL committed, keep no-URL entries —
                        # the report's fate is unknown and the
                        # report-only attempt cap further down bounds
                        # the worst case.
                        drop_no_url_entries = bool(committed_urls_norm)
                        filtered_source_pairs = [
                            (source_input, source)
                            for source_input, source, url in source_norms
                            if url not in current_urls_norm
                            and not (drop_no_url_entries and url is None)
                        ]
                        if len(filtered_source_pairs) != len(source_models):
                            removed_count = len(source_models) - len(filtered_source_pairs)
                            for src in new_sources:
                                if (
                                    src.url
                                    and _normalize_import_verification_url(src.url)
                                    in committed_urls_norm
                                    and src.id not in verified_imported_ids
                                ):
                                    verified_imported.append(_imported_source_entry(src))
                                    verified_imported_ids.add(src.id)
                            source_inputs = [
                                source_input for source_input, _ in filtered_source_pairs
                            ]
                            source_models = [source for _, source in filtered_source_pairs]
                            requested_urls_norm = _requested_import_verification_urls(source_models)
                            requested_no_url_count = _no_import_verification_url_entry_count(
                                source_models
                            )
                            if not source_models:
                                logger.warning(
                                    "IMPORT_RESEARCH timed out for notebook %s "
                                    "but sources.list shows all requested URLs "
                                    "already present; treating as success and "
                                    "skipping retry to avoid duplicate inflation",
                                    notebook_id,
                                )
                                return _merge_imported_sources(
                                    [], verified_imported, verified_imported_ids
                                )
                            logger.warning(
                                "IMPORT_RESEARCH timed out for notebook %s after "
                                "%d requested source(s) were already present; "
                                "retrying with %d remaining source(s)",
                                notebook_id,
                                removed_count,
                                len(source_models),
                            )
                    except (NetworkError, RPCError) as probe_exc:
                        # CancelledError is a BaseException, not Exception, and
                        # is not in this tuple — it propagates naturally for
                        # callers that need to cancel the operation cleanly.
                        logger.warning(
                            "Failed to probe server state after timeout: %s; falling back to retry",
                            probe_exc,
                        )

                if remaining <= 0:
                    raise

                # Report-only imports (no URLs to verify) can't use the success
                # check above. Cap retries at one attempt to bound worst-case
                # duplicate inflation for report entries when timeouts persist.
                if not requested_urls_norm and attempt >= 2:
                    logger.warning(
                        "IMPORT_RESEARCH timed out for notebook %s with no URLs "
                        "to verify; giving up after %d attempts to bound "
                        "duplicate inflation",
                        notebook_id,
                        attempt,
                    )
                    raise

                sleep_for = min(delay, max_delay, remaining)
                logger.warning(
                    "IMPORT_RESEARCH timed out for notebook %s; retrying in "
                    "%.1fs (attempt %d, %.1fs elapsed)",
                    notebook_id,
                    sleep_for,
                    attempt + 1,
                    elapsed,
                )
                await asyncio.sleep(sleep_for)
                delay = min(delay * backoff_factor, max_delay)
                attempt += 1
