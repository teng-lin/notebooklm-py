"""Backend-neutral Research namespace contract and shared orchestration."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import research as _research_pub
from ._idempotency import mark_unconfirmed
from ._notebook_metadata import NotebookSourceLister
from ._runtime.config import AUTO_READ_TIMEOUT, DEFAULT_TIMEOUT
from ._types.research import (
    ResearchSource,
    ResearchSourceInput,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from .exceptions import (
    AmbiguousResearchTaskError,
    NetworkError,
    ResearchTaskMismatchError,
    ResearchTimeoutError,
    RPCError,
    ServerError,
    ValidationError,
)
from .types import CitedSourceSelection, Source

_INITIAL_INTERVAL_UNSET: Any = object()
_DEFAULT_RESEARCH_POLL_INTERVAL = 5.0


def _coerce_sources(sources: Sequence[ResearchSourceInput]) -> list[ResearchSource]:
    return [
        source if isinstance(source, ResearchSource) else ResearchSource.from_public_dict(source)
        for source in sources
    ]


def _normalized_import_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, "")
    )


# Historical private seam retained for callers/tests that used the old facade.
_normalize_import_verification_url = _normalized_import_url


def _source_entry(source: Source) -> dict[str, str]:
    return {"id": source.id or "", "title": source.title or source.url or ""}


class _ImportedResearchSources(list[dict[str, str]]):
    """Compatibility list carrying URL rows skipped by idempotency preflight."""

    def __init__(
        self,
        imported: Sequence[dict[str, str]],
        already_present: Sequence[dict[str, str]],
    ) -> None:
        super().__init__(imported)
        self.already_present = list(already_present)


def _imported_result(
    imported: list[dict[str, str]], already_present: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Build the historical list-compatible import result."""
    return _ImportedResearchSources(imported, already_present)


class ResearchAPI(ABC):
    """Backend-neutral eight-callable Research namespace."""

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
            if result.status in (ResearchStatus.COMPLETED, ResearchStatus.FAILED):
                return result
            if result.status == ResearchStatus.NO_RESEARCH and pinned is None:
                return result
            elapsed = loop.time() - started
            if elapsed >= timeout:
                raise ResearchTimeoutError(
                    notebook_id,
                    pinned or "unknown",
                    timeout,
                    last_status=result.status.value,
                )
            await asyncio.sleep(min(interval, timeout - elapsed))

    @abstractmethod
    async def cancel(self, notebook_id: str, run_id: str) -> None:
        """Cancel one exact run without replaying the mutation."""

    @abstractmethod
    async def import_sources(
        self,
        notebook_id: str,
        task_id: str,
        sources: Sequence[ResearchSourceInput],
        *,
        _remaining_budget: float | None = None,
    ) -> list[dict[str, str]]:
        """Issue one non-replayed raw import request."""

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
        """Import with evidence-bound read-back and retry policy."""
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
        """Import with exact-ID and normalized-URL reconciliation.

        Reports are sent once. URL rows are retried only when baseline and
        post-failure reads prove the exact missing subset and no concurrent row
        makes attribution ambiguous.
        """
        if not sources:
            return []
        inputs = list(sources)
        models = _coerce_sources(inputs)
        for source in models:
            if source.research_task_id and source.research_task_id != task_id:
                raise ResearchTaskMismatchError(
                    task_id=task_id, source_research_task_id=source.research_task_id
                )
        provenance = {source.research_task_id for source in models if source.research_task_id}
        if len(provenance) > 1:
            raise ValidationError(
                "Cannot import sources from multiple research tasks in one batch."
            )

        try:
            baseline = await self._source_lister.list(notebook_id, strict=False)
        except (NetworkError, RPCError):
            baseline = None
        baseline_ids = None if baseline is None else {source.id for source in baseline}
        already_present: list[dict[str, str]] = []
        if baseline is not None and not allow_duplicate:
            existing_by_url: dict[str, list[Source]] = defaultdict(list)
            for existing_source in baseline:
                if existing_source.url:
                    existing_by_url[_normalized_import_url(existing_source.url)].append(
                        existing_source
                    )
            kept_inputs: list[ResearchSourceInput] = []
            kept_models: list[ResearchSource] = []
            for source_input, source in zip(inputs, models, strict=True):
                matches = (
                    existing_by_url.get(_normalized_import_url(source.url), [])
                    if source.url
                    else []
                )
                if len(matches) > 1:
                    raise ValidationError(
                        f"Multiple existing sources match research URL {source.url!r}."
                    )
                if matches:
                    already_present.append(_source_entry(matches[0]))
                else:
                    kept_inputs.append(source_input)
                    kept_models.append(source)
            inputs, models = kept_inputs, kept_models
        if not inputs:
            return _imported_result([], already_present)

        has_report = any(not source.url for source in models)
        started = time.monotonic()
        delay = initial_delay
        verified: list[dict[str, str]] = []
        while True:
            budget = max_elapsed - (time.monotonic() - started)
            try:
                imported = await self.import_sources(
                    notebook_id,
                    task_id,
                    inputs,
                    _remaining_budget=max(0.0, budget),
                )
            except (NetworkError, ServerError) as exc:
                if has_report or baseline_ids is None:
                    raise mark_unconfirmed(exc) from None
                current = await self._probe_after_ambiguous_import(notebook_id, exc)
                inputs, models, landed = self._reconcile_url_subset(
                    current=current,
                    baseline_ids=baseline_ids,
                    inputs=inputs,
                    models=models,
                )
                verified.extend(landed)
                if not inputs:
                    return _imported_result(verified, already_present)
                remaining = max_elapsed - (time.monotonic() - started)
                if remaining <= 0:
                    raise mark_unconfirmed(exc) from None
                await asyncio.sleep(min(delay, max_delay, remaining))
                delay = min(delay * backoff_factor, max_delay)
                continue

            requested = {_normalized_import_url(source.url) for source in models if source.url}
            returned_ids = {entry.get("id", "") for entry in imported}
            if requested and len(returned_ids) < len(requested) and baseline_ids is not None:
                try:
                    current = await self._source_lister.list(notebook_id, strict=False)
                except (NetworkError, RPCError):
                    current = []
                by_url: dict[str, list[Source]] = defaultdict(list)
                for current_source in current:
                    if current_source.id not in baseline_ids and current_source.url:
                        by_url[_normalized_import_url(current_source.url)].append(current_source)
                for url in requested:
                    matches = by_url.get(url, [])
                    landed_source = matches[0] if len(matches) == 1 else None
                    if landed_source is not None and landed_source.id not in returned_ids:
                        imported.append(_source_entry(landed_source))
                        returned_ids.add(landed_source.id)
            return _imported_result([*verified, *imported], already_present)

    async def _probe_after_ambiguous_import(
        self, notebook_id: str, original: NetworkError | ServerError
    ) -> list[Source]:
        try:
            return await self._source_lister.list(notebook_id, strict=False)
        except (NetworkError, RPCError):
            raise mark_unconfirmed(original) from None

    @staticmethod
    def _reconcile_url_subset(
        *,
        current: Sequence[Source],
        baseline_ids: set[str],
        inputs: list[ResearchSourceInput],
        models: list[ResearchSource],
    ) -> tuple[list[ResearchSourceInput], list[ResearchSource], list[dict[str, str]]]:
        new_rows = [source for source in current if source.id not in baseline_ids]
        requested = {_normalized_import_url(source.url) for source in models if source.url}
        if any(
            not source.url or _normalized_import_url(source.url) not in requested
            for source in new_rows
        ):
            raise mark_unconfirmed(
                RPCError("UNRESOLVED — concurrent source additions prevent safe import retry.")
            )
        by_url: dict[str, list[Source]] = defaultdict(list)
        for source in new_rows:
            if source.url:
                by_url[_normalized_import_url(source.url)].append(source)
        if any(len(matches) != 1 for matches in by_url.values()):
            raise mark_unconfirmed(
                RPCError("UNRESOLVED — import read-back is not uniquely attributable.")
            )
        kept_inputs: list[ResearchSourceInput] = []
        kept_models: list[ResearchSource] = []
        landed: list[dict[str, str]] = []
        for source_input, model in zip(inputs, models, strict=True):
            matches = by_url.get(_normalized_import_url(model.url), []) if model.url else []
            if matches:
                landed.append(_source_entry(matches[0]))
            else:
                kept_inputs.append(source_input)
                kept_models.append(model)
        return kept_inputs, kept_models, landed

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
    "CitedSourceSelection",
    "ResearchAPI",
    "ResearchSource",
    "ResearchStart",
    "ResearchStatus",
    "ResearchTask",
]
