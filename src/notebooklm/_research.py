"""Research API for NotebookLM web/drive research.

Provides operations for starting research sessions, polling for results,
and importing discovered sources into notebooks.

This module is the public-facing compatibility facade. Every research
operation is executed by the backend-neutral
:class:`~notebooklm._research_service.ResearchService`, which invokes typed
semantic operations on the private backend port; the wire grammar those
operations encode and decode lives in ``_web/codec/research.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from . import research as _research_pub
from ._backend import BackendAdapter
from ._backend_compat import project_backend_call
from ._notebook_metadata import NotebookSourceLister
from ._research_service import _INITIAL_INTERVAL_UNSET, ResearchService
from ._runtime.config import AUTO_READ_TIMEOUT, DEFAULT_TIMEOUT
from ._types.research import (
    ResearchSource,
    ResearchSourceInput,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from .types import CitedSourceSelection

__all__ = [
    "CitedSourceSelection",
    "ResearchAPI",
    "ResearchSource",
    "ResearchStart",
    "ResearchStatus",
    "ResearchTask",
]


class _MissingSourceLister:
    """Standalone seam that fails only when import verification needs sources."""

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Any]:
        del notebook_id, strict
        raise RuntimeError(
            "ResearchAPI.import_sources_with_verification requires a "
            "composition-injected source lister"
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
        source_lister: NotebookSourceLister | None = None,
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
            source_lister: Optional :class:`NotebookSourceLister` used by
                :meth:`import_sources_with_verification` to snapshot baseline
                source IDs before the import call and probe sources on
                timeout. Production construction injects this dependency at
                the client composition root.
            _backend: Private semantic backend supplied by the client
                composition root.
        """
        self._source_lister: NotebookSourceLister = (
            source_lister if source_lister is not None else _MissingSourceLister()
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
        return await project_backend_call(
            self._require_service().start(notebook_id, query, source, mode)
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
        return await project_backend_call(self._require_service().poll(notebook_id, task_id))

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
        return await project_backend_call(
            self._require_service().wait_for_completion(
                notebook_id,
                task_id,
                timeout=timeout,
                initial_interval=initial_interval,
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
        return await project_backend_call(
            self._require_service().import_sources(
                notebook_id,
                task_id,
                sources,
                _remaining_budget=_remaining_budget,
            )
        )

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
        return await project_backend_call(
            self._require_service().import_sources_with_verification(
                notebook_id,
                task_id,
                sources,
                max_elapsed=max_elapsed,
                initial_delay=initial_delay,
                backoff_factor=backoff_factor,
                max_delay=max_delay,
                allow_duplicate=allow_duplicate,
            )
        )
