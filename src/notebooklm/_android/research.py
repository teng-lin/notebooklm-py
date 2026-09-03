"""Android implementation of the public Research namespace."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from .._idempotency import call_unconfirmed_on_transport_loss, mark_unconfirmed
from .._notebook_metadata import NotebookSourceLister
from .._research import BaseResearchAPI, validate_discover
from .._research_import import _ANDROID_RESEARCH_IMPORT_POLICY, _ResearchImportBatch
from .._runtime.call_supervisor import OperationLease
from .._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_TIMEOUT,
    resolve_import_research_read_timeout,
)
from .._types.research import (
    RESEARCH_SOURCE_TYPE_WEB,
    RESEARCH_STATUS_CODE_COMPLETED,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from ..exceptions import (
    DecodingError,
    NetworkError,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .codecs.research import (
    canonical_research_job_id,
    decode_discovered_source,
    decode_research_jobs,
)
from .epoch import bind_workflow_epoch, reset_workflow_epoch
from .session import AndroidSession
from .upload import android_request_context


def _proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import research_pb2

    return cast(Any, research_pb2)


def _source_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import sources_pb2

    return cast(Any, sources_pb2)


def _empty_type() -> Any:
    from google.protobuf.empty_pb2 import Empty

    return Empty


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
    if not query or not query.strip():
        raise ValidationError("query must not be empty")
    return source_lower, mode_lower


class AndroidResearchAPI(BaseResearchAPI):
    """Android bearer-gRPC adapter for the complete public Research contract."""

    _import_policy = _ANDROID_RESEARCH_IMPORT_POLICY

    @asynccontextmanager
    async def _operation_scope(self, label: str) -> AsyncIterator[OperationLease]:
        async with self._transport.operation_scope(label) as lease:
            token = bind_workflow_epoch(self._transport, lease.epoch)
            try:
                yield lease
            finally:
                reset_workflow_epoch(token)

    def __init__(
        self,
        session: AndroidSession,
        source_lister: NotebookSourceLister,
        *,
        base_timeout: float | None = DEFAULT_TIMEOUT,
        import_research_timeout: float | None = AUTO_READ_TIMEOUT,
    ) -> None:
        self._transport = session
        super().__init__(
            source_lister=source_lister,
            base_timeout=base_timeout,
            import_research_timeout=import_research_timeout,
        )

    async def discover(
        self,
        notebook_id: str,
        query: str,
        *,
        mode: str = "default",
    ) -> ResearchTask:
        """Run the synchronous mobile ``DiscoverSources`` operation once.

        Same contract as :meth:`WebResearchAPI.discover`: one blocking call,
        a completed :class:`ResearchTask` whose ``task_id`` is the job the
        backend also recorded (live-verified over bearer gRPC, #2283).
        """
        query, _mode_label, discovery_mode = validate_discover(query, mode)
        async with self._transport.operation_scope("research.discover") as lease:
            response = await call_unconfirmed_on_transport_loss(
                lambda: self._transport.unary(
                    DISCOVER_SOURCES_METHOD,
                    _proto().DiscoverSourcesRequest(
                        discovery_context=_proto().DiscoveryContext(context=query),
                        discovery_mode=int(discovery_mode),
                        project_id=notebook_id,
                    ),
                    replay_safe=False,
                    response_type=_proto().DiscoverSourcesResponse,
                    expected_epoch=lease.epoch,
                ),
                method=DISCOVER_SOURCES_METHOD,
                what="DiscoverSources",
                chain=None,
            )
            try:
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
            except DecodingError as error:
                raise mark_unconfirmed(error) from None
            return ResearchTask(
                task_id=task_id,
                status=ResearchStatus.COMPLETED,
                query=query,
                sources=sources,
                summary=response.overview,
                status_code=RESEARCH_STATUS_CODE_COMPLETED,
                source_type=RESEARCH_SOURCE_TYPE_WEB,
                discovery_mode=discovery_mode,
            )

    async def start(
        self,
        notebook_id: str,
        query: str,
        source: str = "web",
        mode: str = "fast",
    ) -> ResearchStart:
        source_lower, mode_lower = _validate_start(source, mode, query)
        source_type = RESEARCH_SOURCE_TYPE_WEB if source_lower == "web" else 2
        query_message = _proto().ResearchQuery(query=query, source_type=source_type)
        async with self._transport.operation_scope("research.start") as lease:
            if mode_lower == "fast":
                response = await call_unconfirmed_on_transport_loss(
                    lambda: self._transport.unary(
                        START_FAST_METHOD,
                        _proto().DiscoverSourcesManifoldRequest(
                            query=query_message,
                            discovery_mode=_proto().DEFAULT_LLM_SEARCH,
                            project_id=notebook_id,
                        ),
                        replay_safe=False,
                        response_type=_proto().DiscoverSourcesManifoldResponse,
                        expected_epoch=lease.epoch,
                    ),
                    method=START_FAST_METHOD,
                    what="DiscoverSourcesManifold",
                    chain=None,
                )
                try:
                    run_id = _canonical_uuid(
                        response.source_discovery_job_id, method_id=START_FAST_METHOD
                    )
                except DecodingError as error:
                    raise mark_unconfirmed(error) from None
                return ResearchStart(run_id, None, notebook_id, query, mode_lower)
            response = await call_unconfirmed_on_transport_loss(
                lambda: self._transport.unary(
                    START_DEEP_METHOD,
                    _proto().DiscoverSourcesAsyncRequest(
                        fixed_flags=[1],
                        query=query_message,
                        discovery_mode=_proto().DEEP_RESEARCH,
                        project_id=notebook_id,
                    ),
                    replay_safe=False,
                    response_type=_proto().DiscoverSourcesAsyncResponse,
                    expected_epoch=lease.epoch,
                ),
                method=START_DEEP_METHOD,
                what="DiscoverSourcesAsync",
                chain=None,
            )
            try:
                run_id = _canonical_uuid(
                    response.source_discovery_job_id, method_id=START_DEEP_METHOD
                )
            except DecodingError as error:
                raise mark_unconfirmed(error) from None
            if not response.start_session_id:
                raise mark_unconfirmed(
                    DecodingError(
                        "Android deep Research response omitted its diagnostic session id",
                        method_id=START_DEEP_METHOD,
                    )
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
            _proto().ListDiscoverSourcesJobRequest(project_id=notebook_id),
            replay_safe=True,
            response_type=_proto().ListDiscoverSourcesJobResponse,
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

    async def cancel(self, notebook_id: str, run_id: str) -> None:
        run_id = _validated_run_id(run_id)
        async with self._transport.operation_scope("research.cancel") as lease:
            try:
                await self._transport.unary(
                    CANCEL_JOB_METHOD,
                    _proto().CancelDiscoverSourcesJobRequest(
                        request_context=android_request_context(),
                        source_discovery_job_id=run_id,
                    ),
                    replay_safe=False,
                    response_type=_empty_type(),
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

    async def _send_import(
        self,
        notebook_id: str,
        batch: _ResearchImportBatch,
        *,
        _remaining_budget: float | None,
    ) -> list[dict[str, str]]:
        entries = [
            _source_proto().UserContent(
                text_content=_source_proto().TextContent(
                    source_name=item.source.title,
                    content=item.source.report_markdown,
                ),
                text_content_type=_source_proto().UserContent.CONTENT_TYPE_MARKDOWN,
            )
            if item.kind == "report"
            else _source_proto().UserContent(
                web_content=_source_proto().WebContent(
                    url=item.source.url,
                    source_name=item.source.title,
                )
            )
            for item in batch.items
        ]
        async with self._transport.operation_scope("research.import_sources") as lease:
            response = await self._transport.unary(
                FINISH_RUN_METHOD,
                _proto().FinishDiscoverSourcesRunRequest(
                    source_discovery_job_id=batch.task_id,
                    project_id=notebook_id,
                    user_content=entries,
                ),
                replay_safe=False,
                timeout=resolve_import_research_read_timeout(
                    len(entries),
                    base_timeout=self._base_timeout,
                    override=self._import_research_timeout,
                    remaining_budget=_remaining_budget,
                ),
                response_type=_proto().FinishDiscoverSourcesRunResponse,
                expected_epoch=lease.epoch,
            )
            return [
                {"id": header.source_id.id, "title": header.title}
                for header in response.sources
                if header.HasField("source_id") and header.source_id.id
            ]


__all__ = ["AndroidResearchAPI"]
