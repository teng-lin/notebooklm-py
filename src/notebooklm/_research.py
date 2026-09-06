"""Backend-neutral Research namespace contract and shared orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from . import research as _research_pub
from ._idempotency import (
    OperationJournal,
    attach_reconciliation_report,
    bind_operation_journal_entries,
    mark_unconfirmed,
    reconciliation_report,
)
from ._notebook_metadata import NotebookSourceLister
from ._research_import import (
    _WEB_RESEARCH_IMPORT_POLICY,
    _already_present_source_entry,
    _classify_research_import,
    _coerce_research_sources,
    _imported_result,
    _normalize_import_verification_url,
    _ResearchImportBatch,
    _ResearchImportPolicy,
    _validate_import_task_id,
    _validate_research_task_provenance,
)
from ._runtime.call_supervisor import OperationLease
from ._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_TIMEOUT,
    MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT,
)
from ._types.enums import DiscoveryMode
from ._types.research import (
    ResearchSource,
    ResearchSourceInput,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from .exceptions import (
    AmbiguousResearchTaskError,
    AuthError,
    NetworkError,
    RateLimitError,
    ResearchTimeoutError,
    RPCError,
    ServerError,
    ValidationError,
)
from .outcomes import CommitState
from .types import CitedSourceSelection, Source

_INITIAL_INTERVAL_UNSET: Any = object()
_DEFAULT_RESEARCH_POLL_INTERVAL = 5.0
logger = logging.getLogger("notebooklm._research")


def _only_source(candidates: Sequence[Source]) -> Source | None:
    """Return the sole source in *candidates*, without positional indexing."""
    if len(candidates) != 1:
        return None
    for candidate in candidates:
        return candidate
    return None


#: ``research.discover()`` mode labels → the backend ``DiscoveryMode`` each one
#: sends. These are the four modes the web "Discover sources" dialog can emit
#: (live-verified on both transports, #2283): ``deep`` (5) is rejected by the
#: synchronous route and ``lite`` (6) faults server-side, so neither is offered.
DISCOVER_MODES: dict[str, DiscoveryMode] = {
    "default": DiscoveryMode.DEFAULT_LLM_SEARCH,
    "raw": DiscoveryMode.RAW_SEARCH,
    "curious": DiscoveryMode.CURIOUS_SEARCH,
    "curious_raw": DiscoveryMode.CURIOUS_RAW_SEARCH,
}

#: The "I'm feeling curious" modes: the dialog sends an empty query and the
#: backend picks the topic itself, so an empty ``query`` is valid only here.
_CURIOUS_DISCOVER_MODES = frozenset({"curious", "curious_raw"})


def validate_discover(query: str, mode: str) -> tuple[str, str, DiscoveryMode]:
    """Validate ``research.discover()`` inputs shared by every backend.

    Returns ``(query_to_send, mode_label, discovery_mode)``. The curious modes
    always send an empty query — that is the wire shape the dialog's "curious"
    buttons emit and what makes the backend pick the topic — so a caller's
    text is dropped there rather than turning the call into a query search.
    """
    if not isinstance(query, str):
        raise ValidationError("query must be a string")
    if not isinstance(mode, str):
        raise ValidationError("mode must be a string")
    mode_lower = mode.lower()
    if mode_lower not in DISCOVER_MODES:
        raise ValidationError(
            f"Invalid mode '{mode}'. Use one of: " + ", ".join(sorted(DISCOVER_MODES)) + "."
        )
    if mode_lower in _CURIOUS_DISCOVER_MODES:
        return "", mode_lower, DISCOVER_MODES[mode_lower]
    if not query.strip():
        raise ValidationError(
            "query must not be empty (only the 'curious' and 'curious_raw' modes accept one)"
        )
    return query, mode_lower, DISCOVER_MODES[mode_lower]


class BaseResearchAPI(ABC):
    """Backend-neutral nine-callable Research namespace."""

    _import_policy: _ResearchImportPolicy = _WEB_RESEARCH_IMPORT_POLICY

    @abstractmethod
    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""
        raise NotImplementedError

    def __init__(
        self,
        *,
        source_lister: NotebookSourceLister,
        base_timeout: float | None = DEFAULT_TIMEOUT,
        import_research_timeout: float | None = AUTO_READ_TIMEOUT,
    ) -> None:
        self._source_lister = source_lister
        self._base_timeout = base_timeout
        self._import_research_timeout = import_research_timeout

    @staticmethod
    def _normalize_url(url: str) -> str:
        return _research_pub.normalize_url(url)

    @classmethod
    def extract_report_urls(cls, report: str) -> set[str]:
        return _research_pub.extract_report_urls(report)

    @classmethod
    def select_cited_sources(
        cls, sources: Sequence[ResearchSourceInput], report: str
    ) -> CitedSourceSelection:
        return _research_pub.select_cited_sources(sources, report)

    @abstractmethod
    async def start(
        self,
        notebook_id: str,
        query: str,
        source: str = "web",
        mode: str = "fast",
    ) -> ResearchStart:
        """Start one backend research run."""

    @abstractmethod
    async def discover(
        self,
        notebook_id: str,
        query: str,
        *,
        mode: str = "default",
    ) -> ResearchTask:
        """Run one synchronous web discovery and return its completed task."""

    @abstractmethod
    async def poll(self, notebook_id: str, task_id: str | None = None) -> ResearchTask:
        """Poll and select one exact research run."""

    async def wait_for_completion(
        self,
        notebook_id: str,
        task_id: str | None = None,
        *,
        timeout: float = 1800,
        initial_interval: float = _INITIAL_INTERVAL_UNSET,
    ) -> ResearchTask:
        """Poll one pinned run until it reaches a terminal state."""
        return await self._wait_for_completion(
            notebook_id,
            task_id,
            timeout=timeout,
            initial_interval=initial_interval,
        )

    async def _wait_for_completion(
        self,
        notebook_id: str,
        task_id: str | None = None,
        *,
        timeout: float = 1800,
        initial_interval: float = _INITIAL_INTERVAL_UNSET,
    ) -> ResearchTask:
        """Poll one pinned run until it reaches a terminal state."""
        async with self._operation_scope("research.wait_for_completion"):
            return await self._wait_for_completion_in_scope(
                notebook_id,
                task_id,
                timeout=timeout,
                initial_interval=initial_interval,
            )

    async def _wait_for_completion_in_scope(
        self,
        notebook_id: str,
        task_id: str | None = None,
        *,
        timeout: float = 1800,
        initial_interval: float = _INITIAL_INTERVAL_UNSET,
    ) -> ResearchTask:
        if initial_interval is _INITIAL_INTERVAL_UNSET:
            interval = _DEFAULT_RESEARCH_POLL_INTERVAL
        elif isinstance(initial_interval, bool) or not isinstance(initial_interval, (int, float)):
            raise TypeError("poll interval must be a number")
        else:
            interval = float(initial_interval)
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if interval <= 0:
            raise ValueError("poll interval must be positive")

        loop = asyncio.get_running_loop()
        started = loop.time()
        pinned = task_id
        while True:
            result = await self.poll(notebook_id, pinned)
            if pinned is None and result.task_id:
                pinned = result.task_id
            observed_status = self._wait_observed_status(result)
            if observed_status in (ResearchStatus.COMPLETED, ResearchStatus.FAILED):
                return result
            if observed_status == ResearchStatus.NO_RESEARCH and pinned is None:
                return result
            elapsed = loop.time() - started
            if elapsed >= timeout:
                raise ResearchTimeoutError(
                    notebook_id,
                    pinned or "unknown",
                    timeout,
                    last_status=observed_status.value,
                )
            await asyncio.sleep(min(interval, timeout - elapsed))

    def _wait_observed_status(self, result: ResearchTask) -> ResearchStatus:
        """Return the backend's historical wait-only status projection."""
        return result.status

    @abstractmethod
    async def cancel(self, notebook_id: str, run_id: str) -> None:
        """Cancel one exact run without replaying the mutation."""

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
                when this one send starts; clamps the read timeout so the
                mutation cannot outlive that workflow budget (#2205).
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
        if not sources:
            return []

        policy = self._import_policy
        validated_task_id = _validate_import_task_id(task_id, policy)
        source_inputs = list(sources)
        source_models = _coerce_research_sources(source_inputs)
        if policy.log_classification:
            logger.debug(
                "Importing %d research sources into notebook %s",
                len(source_models),
                notebook_id,
            )
        effective_task_id = _validate_research_task_provenance(
            source_models,
            validated_task_id,
        )
        batch = _classify_research_import(
            source_inputs,
            source_models,
            task_id=effective_task_id,
            policy=policy,
        )
        if policy.log_classification and batch.skipped_count:
            logger.warning(
                "Skipping %d source(s) that cannot be imported (missing URLs or report entries)",
                batch.skipped_count,
            )
        if not batch.items:
            return []
        entry = OperationJournal("research.import_sources").new_entry(method="import_sources")
        with bind_operation_journal_entries(entry):
            return await self._send_import(
                notebook_id,
                batch,
                _remaining_budget=_remaining_budget,
            )

    @abstractmethod
    async def _send_import(
        self,
        notebook_id: str,
        batch: _ResearchImportBatch,
        *,
        _remaining_budget: float | None,
    ) -> list[dict[str, str]]:
        """Encode, send, and decode one backend import mutation."""

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
        """Import once, then inspect on an uncertain outcome.

        The timing knobs bound only the read-only candidate-inspection cadence;
        they never authorize another ``import_sources`` mutation.
        """
        return await self._import_sources_with_verification(
            notebook_id,
            task_id,
            sources,
            max_elapsed=max_elapsed,
            initial_delay=initial_delay,
            backoff_factor=backoff_factor,
            max_delay=max_delay,
            allow_duplicate=allow_duplicate,
        )

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
        """Import once and report bounded normalized-URL candidates after loss."""
        async with self._operation_scope("research.import_sources_with_verification"):
            return await self._import_sources_with_verification_in_scope(
                notebook_id,
                task_id,
                sources,
                max_elapsed=max_elapsed,
                initial_delay=initial_delay,
                backoff_factor=backoff_factor,
                max_delay=max_delay,
                allow_duplicate=allow_duplicate,
            )

    async def _import_sources_with_verification_in_scope(
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
        if not sources:
            return _imported_result([], [])
        inputs = list(sources)
        models = _coerce_research_sources(inputs)
        _validate_research_task_provenance(models, task_id)

        try:
            baseline = await self._source_lister.list(notebook_id, strict=False)
        except (AuthError, RateLimitError):
            raise
        except (NetworkError, ServerError):
            baseline = None
        baseline_ids = None if baseline is None else {source.id for source in baseline}
        already_present: list[dict[str, str]] = []
        if baseline is not None and not allow_duplicate:
            existing_by_url: dict[str, Source] = {}
            for existing_source in baseline:
                if existing_source.url:
                    existing_by_url.setdefault(
                        _normalize_import_verification_url(existing_source.url), existing_source
                    )
            kept_inputs: list[ResearchSourceInput] = []
            kept_models: list[ResearchSource] = []
            already_present_ids: set[str] = set()
            for source_input, source in zip(inputs, models, strict=True):
                existing = (
                    existing_by_url.get(_normalize_import_verification_url(source.url))
                    if source.url
                    else None
                )
                if existing is not None:
                    existing_id = existing.id or ""
                    if existing_id not in already_present_ids:
                        already_present_ids.add(existing_id)
                        already_present.append(_already_present_source_entry(existing))
                else:
                    kept_inputs.append(source_input)
                    kept_models.append(source)
            inputs, models = kept_inputs, kept_models
        if not inputs:
            return _imported_result([], already_present)

        started = time.monotonic()
        budget = max_elapsed - (time.monotonic() - started)
        try:
            imported = await self.import_sources(
                notebook_id,
                task_id,
                inputs,
                _remaining_budget=(
                    budget if budget >= MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT else None
                ),
            )
        except (NetworkError, RPCError) as failure:
            state = getattr(failure, "commit_state", CommitState.UNKNOWN)
            if state in (CommitState.REJECTED, CommitState.NOT_SENT):
                raise
            mark_unconfirmed(failure, operation="research.import_sources")

            current: Sequence[Source] = ()
            delay = max(0.0, initial_delay)
            requested_urls = {
                _normalize_import_verification_url(source.url) for source in models if source.url
            }
            candidate_ids: list[str] = []
            visible_urls: set[str] = set()
            while requested_urls:
                try:
                    current = await self._source_lister.list(notebook_id, strict=False)
                except (NetworkError, RPCError):
                    current = ()
                else:
                    candidate_ids = []
                    visible_urls = set()
                    for current_source in current:
                        if (
                            baseline_ids is not None and current_source.id in baseline_ids
                        ) or not current_source.url:
                            continue
                        normalized = _normalize_import_verification_url(current_source.url)
                        if normalized in requested_urls:
                            candidate_ids.append(current_source.id)
                            visible_urls.add(normalized)
                    if candidate_ids:
                        break

                remaining = max_elapsed - (time.monotonic() - started)
                sleep_for = min(delay, max(0.0, max_delay), remaining)
                if sleep_for <= 0:
                    break
                await asyncio.sleep(sleep_for)
                delay = min(delay * backoff_factor, max_delay)

            unresolved = [
                source.url
                for source in models
                if source.url and _normalize_import_verification_url(source.url) not in visible_urls
            ]
            unresolved.extend(source.title for source in models if not source.url)
            attach_reconciliation_report(
                failure,
                reconciliation_report(candidate_ids, unresolved),
                operation="research.import_sources",
            )
            raise

        return _imported_result(imported, already_present)

    @staticmethod
    def _select_polled_tasks(
        parsed_tasks: list[ResearchTask],
        *,
        notebook_id: str,
        task_id: str | None,
        raise_on_ambiguous: bool,
    ) -> list[ResearchTask]:
        if task_id is not None:
            return [task for task in parsed_tasks if task.task_id == task_id]
        if raise_on_ambiguous and len(parsed_tasks) > 1:
            raise AmbiguousResearchTaskError(
                notebook_id=notebook_id, task_ids=[task.task_id for task in parsed_tasks]
            )
        return parsed_tasks

    @staticmethod
    def _public_poll_result(
        selected_task: ResearchTask, parsed_tasks: list[ResearchTask]
    ) -> ResearchTask:
        return replace(selected_task, tasks=tuple(parsed_tasks))


__all__ = [
    "BaseResearchAPI",
    "CitedSourceSelection",
    "ResearchSource",
    "ResearchStart",
    "ResearchStatus",
    "ResearchTask",
]
