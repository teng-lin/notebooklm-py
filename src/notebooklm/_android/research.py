"""Private Android implementation of the evidence-qualified Research slice."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from google.protobuf.empty_pb2 import Empty

from .._idempotency import mark_unconfirmed
from .._notebook_metadata import NotebookSourceLister
from .._research import _INITIAL_INTERVAL_UNSET, ResearchAPI
from .._types.research import (
    RESEARCH_RESULT_TYPE_REPORT,
    RESEARCH_SOURCE_TYPE_WEB,
    ResearchSource,
    ResearchSourceInput,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from ..exceptions import (
    DecodingError,
    NetworkError,
    RateLimitError,
    ResearchTaskMismatchError,
    ServerError,
    ValidationError,
)
from .codecs.research import (
    canonical_research_job_id,
    decode_discovered_source,
    decode_research_jobs,
)
from .errors import unsupported_operation
from .proto.google.internal.labs.tailwind.orchestration.v1 import research_pb2, sources_pb2
from .session import AndroidSession

_PROTO = cast(Any, research_pb2)
_SOURCE_PROTO = cast(Any, sources_pb2)
_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
DISCOVER_SOURCES_METHOD = f"/{_SERVICE}/DiscoverSources"
START_FAST_METHOD = f"/{_SERVICE}/DiscoverSourcesManifold"
START_DEEP_METHOD = f"/{_SERVICE}/DiscoverSourcesAsync"
LIST_JOBS_METHOD = f"/{_SERVICE}/ListDiscoverSourcesJob"
CANCEL_JOB_METHOD = f"/{_SERVICE}/CancelDiscoverSourcesJob"
FINISH_RUN_METHOD = f"/{_SERVICE}/FinishDiscoverSourcesRun"


def _canonical_uuid(value: str, *, method_id: str) -> str:
    return canonical_research_job_id(value, method_id=method_id)


def _validated_run_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError):
        raise ValidationError("run_id must be a canonical UUID") from None
    canonical = str(parsed)
    if value != canonical:
        raise ValidationError("run_id must be a canonical UUID")
    return canonical


def _validate_start(source: str, mode: str, query: str) -> tuple[str, str]:
    source_lower = source.lower()
    mode_lower = mode.lower()
    if source_lower not in ("web", "drive"):
        raise ValidationError(f"Invalid source '{source}'. Use 'web' or 'drive'.")
    if mode_lower not in ("fast", "deep"):
        raise ValidationError(f"Invalid mode '{mode}'. Use 'fast' or 'deep'.")
    if source_lower == "drive":
        if mode_lower == "deep":
            raise ValidationError("Deep Research only supports Web sources.")
        unsupported_operation("research.start fast Drive corpus")
    if not query or not query.strip():
        raise ValidationError("query must not be empty")
    return source_lower, mode_lower


class AndroidResearchAPI(ResearchAPI):
    """Android bearer-gRPC Research adapter; private until B10 promotion."""

    def __init__(self, session: AndroidSession, source_lister: NotebookSourceLister) -> None:
        self._transport = session
        super().__init__(source_lister=source_lister)

    async def _discover_sources(self, notebook_id: str, query: str) -> ResearchTask:
        """Run the separate synchronous mobile DiscoverSources operation once."""
        if not query or not query.strip():
            raise ValidationError("query must not be empty")
        async with self._transport.operation_scope("research.discover_sources") as lease:
            response = await self._transport.unary(
                DISCOVER_SOURCES_METHOD,
                _PROTO.DiscoverSourcesRequest(
                    discovery_context=_PROTO.DiscoveryContext(context=query),
                    discovery_mode=_PROTO.DEFAULT_LLM_SEARCH,
                    project_id=notebook_id,
                ),
                replay_safe=False,
                response_type=_PROTO.DiscoverSourcesResponse,
                expected_epoch=lease.epoch,
            )
            task_id = response.discover_sources_feedback_key.discover_sources_id
            task_id = _canonical_uuid(task_id, method_id=DISCOVER_SOURCES_METHOD)
            sources = tuple(
                source
                for row in response.discovered_sources
                if (
                    source := decode_discovered_source(
                        row,
                        task_id=task_id,
                        source_type=RESEARCH_SOURCE_TYPE_WEB,
                    )
                )
                is not None
            )
            return ResearchTask(
                task_id=task_id,
                status=ResearchStatus.COMPLETED,
                query=query,
                sources=sources,
                summary=response.overview,
                status_code=2,
                source_type=RESEARCH_SOURCE_TYPE_WEB,
            )

    async def start(
        self,
        notebook_id: str,
        query: str,
        source: str = "web",
        mode: str = "fast",
    ) -> ResearchStart:
        _, mode_lower = _validate_start(source, mode, query)
        query_message = _PROTO.ResearchQuery(query=query, source_type=RESEARCH_SOURCE_TYPE_WEB)
        async with self._transport.operation_scope("research.start") as lease:
            if mode_lower == "fast":
                response = await self._transport.unary(
                    START_FAST_METHOD,
                    _PROTO.DiscoverSourcesManifoldRequest(
                        query=query_message,
                        discovery_mode=_PROTO.DEFAULT_LLM_SEARCH,
                        project_id=notebook_id,
                    ),
                    replay_safe=False,
                    response_type=_PROTO.DiscoverSourcesManifoldResponse,
                    expected_epoch=lease.epoch,
                )
                run_id = _canonical_uuid(
                    response.source_discovery_job_id, method_id=START_FAST_METHOD
                )
                return ResearchStart(run_id, None, notebook_id, query, mode_lower)
            response = await self._transport.unary(
                START_DEEP_METHOD,
                _PROTO.DiscoverSourcesAsyncRequest(
                    fixed_flags=[1],
                    query=query_message,
                    discovery_mode=_PROTO.DEEP_RESEARCH,
                    project_id=notebook_id,
                ),
                replay_safe=False,
                response_type=_PROTO.DiscoverSourcesAsyncResponse,
                expected_epoch=lease.epoch,
            )
            run_id = _canonical_uuid(response.source_discovery_job_id, method_id=START_DEEP_METHOD)
            if not response.start_session_id:
                raise DecodingError(
                    "Android deep Research response omitted its diagnostic session id",
                    method_id=START_DEEP_METHOD,
                )
            return ResearchStart(
                response.start_session_id,
                run_id,
                notebook_id,
                query,
                mode_lower,
            )

    async def _list_tasks(self, notebook_id: str, *, expected_epoch: int) -> list[ResearchTask]:
        response = await self._transport.unary(
            LIST_JOBS_METHOD,
            _PROTO.ListDiscoverSourcesJobRequest(project_id=notebook_id),
            replay_safe=True,
            response_type=_PROTO.ListDiscoverSourcesJobResponse,
            expected_epoch=expected_epoch,
        )
        return decode_research_jobs(response, method_id=LIST_JOBS_METHOD)

    async def poll(self, notebook_id: str, task_id: str | None = None) -> ResearchTask:
        async with self._transport.operation_scope("research.poll") as lease:
            tasks = self._select_polled_tasks(
                await self._list_tasks(notebook_id, expected_epoch=lease.epoch),
                notebook_id=notebook_id,
                task_id=task_id,
                raise_on_ambiguous=task_id is None,
            )
            for selected_task in tasks:
                return self._public_poll_result(selected_task, tasks)
            return ResearchTask.not_found(task_id) if task_id else ResearchTask.empty()

    async def _wait_for_completion(
        self,
        notebook_id: str,
        task_id: str | None = None,
        *,
        timeout: float = 1800,
        initial_interval: float = _INITIAL_INTERVAL_UNSET,
    ) -> ResearchTask:
        async with self._transport.operation_scope("research.wait_for_completion"):
            return await super()._wait_for_completion(
                notebook_id,
                task_id,
                timeout=timeout,
                initial_interval=initial_interval,
            )

    async def cancel(self, notebook_id: str, run_id: str) -> None:
        run_id = _validated_run_id(run_id)
        async with self._transport.operation_scope("research.cancel") as lease:
            try:
                await self._transport.unary(
                    CANCEL_JOB_METHOD,
                    _PROTO.CancelDiscoverSourcesJobRequest(source_discovery_job_id=run_id),
                    replay_safe=False,
                    response_type=Empty,
                    expected_epoch=lease.epoch,
                )
            except RateLimitError:
                raise
            except (NetworkError, ServerError) as exc:
                try:
                    tasks = await self._list_tasks(notebook_id, expected_epoch=lease.epoch)
                except (NetworkError, ServerError, DecodingError):
                    raise mark_unconfirmed(exc) from None
                selected = next((task for task in tasks if task.task_id == run_id), None)
                if selected is not None and selected.status_code in (2, 4):
                    return
                raise mark_unconfirmed(exc) from None

    async def import_sources(
        self,
        notebook_id: str,
        task_id: str,
        sources: Sequence[ResearchSourceInput],
        *,
        _remaining_budget: float | None = None,
    ) -> list[dict[str, str]]:
        if not sources:
            return []
        run_id = _validated_run_id(task_id)
        entries = []
        for raw in list(sources):
            source = (
                raw if isinstance(raw, ResearchSource) else ResearchSource.from_public_dict(raw)
            )
            if source.research_task_id and source.research_task_id != run_id:
                raise ResearchTaskMismatchError(
                    task_id=run_id,
                    source_research_task_id=source.research_task_id,
                )
            if source.result_type == RESEARCH_RESULT_TYPE_REPORT and source.report_markdown:
                entries.append(
                    _SOURCE_PROTO.UserContent(
                        text_content=_SOURCE_PROTO.TextContent(
                            source_name=source.title,
                            content=source.report_markdown,
                        ),
                        text_content_type=_SOURCE_PROTO.UserContent.CONTENT_TYPE_MARKDOWN,
                    )
                )
            elif source.url:
                entries.append(
                    _SOURCE_PROTO.UserContent(
                        web_content=_SOURCE_PROTO.WebContent(
                            url=source.url,
                            source_name=source.title,
                        )
                    )
                )
        if not entries:
            return []
        async with self._transport.operation_scope("research.import_sources") as lease:
            response = await self._transport.unary(
                FINISH_RUN_METHOD,
                _PROTO.FinishDiscoverSourcesRunRequest(
                    source_discovery_job_id=run_id,
                    project_id=notebook_id,
                    user_content=entries,
                ),
                replay_safe=False,
                timeout=_remaining_budget,
                response_type=_PROTO.FinishDiscoverSourcesRunResponse,
                expected_epoch=lease.epoch,
            )
            return [
                {"id": header.source_id.id, "title": header.title}
                for header in response.sources
                if header.HasField("source_id") and header.source_id.id
            ]

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
        async with self._transport.operation_scope("research.import_sources_with_verification"):
            return await super()._import_sources_with_verification(
                notebook_id,
                task_id,
                sources,
                max_elapsed=max_elapsed,
                initial_delay=initial_delay,
                backoff_factor=backoff_factor,
                max_delay=max_delay,
                allow_duplicate=allow_duplicate,
            )


__all__ = ["AndroidResearchAPI"]
