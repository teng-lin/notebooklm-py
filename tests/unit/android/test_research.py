"""Evidence, wire, lifecycle, and frontend tests for private Android Research."""

from __future__ import annotations

import inspect
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    research_pb2,
    sources_pb2,
)
from notebooklm._android.research import (
    CANCEL_JOB_METHOD,
    DISCOVER_SOURCES_METHOD,
    FINISH_RUN_METHOD,
    LIST_JOBS_METHOD,
    START_DEEP_METHOD,
    START_FAST_METHOD,
    AndroidResearchAPI,
)
from notebooklm._app.research import poll_and_classify
from notebooklm._research import ResearchAPI
from notebooklm._types.enums import DiscoveryMode
from notebooklm._types.research import ResearchSource, ResearchStatus
from notebooklm._web.research import WebResearchAPI
from notebooklm.exceptions import (
    AmbiguousResearchTaskError,
    DecodingError,
    ResearchTaskMismatchError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    UnsupportedOperationError,
    ValidationError,
)
from notebooklm.types import Source

RUN_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"


@dataclass(frozen=True)
class _Lease:
    epoch: int


class _Transport:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.responses = {method: deque(values) for method, values in responses.items()}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **_: Any) -> AsyncIterator[_Lease]:
        self.scopes.append(label)
        yield _Lease(17)

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        value = self.responses[method].popleft()
        if isinstance(value, BaseException):
            raise value
        return value


class _Lister:
    def __init__(self, rows: list[list[Source]] | None = None) -> None:
        self.rows = deque(rows or [[]])
        self.calls: list[tuple[str, bool]] = []

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Source]:
        self.calls.append((notebook_id, strict))
        return self.rows.popleft() if self.rows else []


def _api(
    responses: dict[str, list[Any]], lister: _Lister | None = None
) -> tuple[AndroidResearchAPI, _Transport]:
    transport = _Transport(responses)
    return AndroidResearchAPI(transport, lister or _Lister()), transport  # type: ignore[arg-type]


def _job(run_id: str, *, status: int, report: bool = False) -> Any:
    sources = [
        research_pb2.DiscoveredSource(
            source_url="https://example.com/a",
            title="A",
            hint="Why A",
            corpus_type=research_pb2.DISCOVERED_SOURCE_CORPUS_TYPE_WEB,
            source_ordinal=1,
        )
    ]
    if report:
        sources.insert(
            0,
            research_pb2.DiscoveredSource(
                title="Report",
                corpus_type=research_pb2.DISCOVERED_SOURCE_CORPUS_TYPE_GENERATED,
                content=research_pb2.ResearchResultContent(text="# Report", kind=3),
            ),
        )
    return research_pb2.ResearchJob(
        source_discovery_job_id=run_id,
        info=research_pb2.ResearchJobInfo(
            query=research_pb2.ResearchQuery(query="q", source_type=1),
            discovery_mode=research_pb2.DEEP_RESEARCH
            if report
            else research_pb2.DEFAULT_LLM_SEARCH,
            results=research_pb2.ResearchResults(sources=sources, summary="summary"),
            status=status,
        ),
    )


def test_exact_public_manifest_and_abstract_set() -> None:
    manifest = {
        name
        for name in ResearchAPI.__dict__
        if not name.startswith("_") and callable(getattr(ResearchAPI, name))
    }
    assert manifest == {
        "start",
        "poll",
        "wait_for_completion",
        "cancel",
        "import_sources",
        "import_sources_with_verification",
        "extract_report_urls",
        "select_cited_sources",
    }
    assert ResearchAPI.__abstractmethods__ == frozenset(
        {"start", "poll", "cancel", "import_sources"}
    )
    assert AndroidResearchAPI.__abstractmethods__ == frozenset()
    for name in (
        "wait_for_completion",
        "import_sources_with_verification",
        "extract_report_urls",
        "select_cited_sources",
    ):
        assert inspect.getattr_static(WebResearchAPI, name) is inspect.getattr_static(
            ResearchAPI, name
        )
        assert inspect.getattr_static(AndroidResearchAPI, name) is inspect.getattr_static(
            ResearchAPI, name
        )


def test_research_proto_is_service_free_and_pins_six_exact_messages() -> None:
    descriptor = research_pb2.DESCRIPTOR
    assert descriptor.services_by_name == {}
    assert {
        "DiscoverSourcesRequest",
        "DiscoverSourcesResponse",
        "DiscoverSourcesManifoldRequest",
        "DiscoverSourcesManifoldResponse",
        "DiscoverSourcesAsyncRequest",
        "DiscoverSourcesAsyncResponse",
        "ListDiscoverSourcesJobRequest",
        "ListDiscoverSourcesJobResponse",
        "CancelDiscoverSourcesJobRequest",
        "FinishDiscoverSourcesRunRequest",
        "FinishDiscoverSourcesRunResponse",
    }.issubset(descriptor.message_types_by_name)


@pytest.mark.asyncio
async def test_fast_and_deep_start_use_distinct_evidence_fields_without_replay() -> None:
    api, transport = _api(
        {
            START_FAST_METHOD: [
                research_pb2.DiscoverSourcesManifoldResponse(source_discovery_job_id=RUN_ID)
            ],
            START_DEEP_METHOD: [
                research_pb2.DiscoverSourcesAsyncResponse(
                    start_session_id="diagnostic-session-value",
                    source_discovery_job_id=RUN_ID,
                )
            ],
        }
    )
    fast = await api.start("nb", "q")
    deep = await api.start("nb", "q", mode="deep")

    assert (fast.task_id, fast.report_id) == (RUN_ID, None)
    assert (deep.task_id, deep.report_id) == ("diagnostic-session-value", RUN_ID)
    fast_call, deep_call = transport.calls
    assert fast_call[1].query.query == "q" and fast_call[1].discovery_mode == 1
    assert deep_call[1].fixed_flags == [1] and deep_call[1].discovery_mode == 5
    assert fast_call[2]["replay_safe"] is False
    assert deep_call[2]["replay_safe"] is False
    assert fast_call[2]["expected_epoch"] == deep_call[2]["expected_epoch"] == 17


@pytest.mark.asyncio
async def test_synchronous_discover_sources_uses_committed_mobile_binding_once() -> None:
    api, transport = _api(
        {
            DISCOVER_SOURCES_METHOD: [
                research_pb2.DiscoverSourcesResponse(
                    discovered_sources=[
                        research_pb2.DiscoveredSource(source_url="https://example.com/a", title="A")
                    ],
                    overview="overview",
                    discover_sources_feedback_key=research_pb2.DiscoverSourcesFeedbackKey(
                        discover_sources_id=RUN_ID
                    ),
                )
            ]
        }
    )
    result = await api._discover_sources("nb", "q")
    method, request, kwargs = transport.calls[0]
    assert method == DISCOVER_SOURCES_METHOD
    assert request.discovery_context.context == "q"
    assert request.discovery_mode == research_pb2.DEFAULT_LLM_SEARCH
    assert request.project_id == "nb"
    assert kwargs["replay_safe"] is False
    assert result.task_id == RUN_ID and result.summary == "overview"


@pytest.mark.asyncio
async def test_fast_drive_rejects_before_transport_admission() -> None:
    api, transport = _api({})
    with pytest.raises(UnsupportedOperationError):
        await api.start("nb", "q", source="drive")
    assert transport.scopes == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_poll_selects_exact_historical_job_and_decodes_report() -> None:
    response = research_pb2.ListDiscoverSourcesJobResponse(
        jobs=[_job(OTHER_ID, status=1), _job(RUN_ID, status=2, report=True)]
    )
    api, transport = _api({LIST_JOBS_METHOD: [response]})
    result = await api.poll("nb", RUN_ID)

    assert result.task_id == RUN_ID
    assert result.status is ResearchStatus.COMPLETED
    assert result.discovery_mode is DiscoveryMode.DEEP_RESEARCH
    assert result.report == "# Report"
    assert [source.title for source in result.sources] == ["Report", "A"]
    assert [task.task_id for task in result.tasks] == [RUN_ID]
    assert transport.calls[0][2]["replay_safe"] is True


@pytest.mark.asyncio
async def test_unpinned_poll_is_ambiguous_and_pinned_miss_is_not_found() -> None:
    response = research_pb2.ListDiscoverSourcesJobResponse(
        jobs=[_job(OTHER_ID, status=1), _job(RUN_ID, status=2)]
    )
    api, _ = _api({LIST_JOBS_METHOD: [response, response]})
    with pytest.raises(AmbiguousResearchTaskError):
        await api.poll("nb")
    missing = await api.poll("nb", "33333333-3333-4333-8333-333333333333")
    assert missing.status is ResearchStatus.NOT_FOUND
    assert missing.tasks == ()


@pytest.mark.asyncio
async def test_wait_timeout_holds_outer_scope_and_threads_one_epoch() -> None:
    api, transport = _api(
        {
            LIST_JOBS_METHOD: [
                research_pb2.ListDiscoverSourcesJobResponse(jobs=[_job(RUN_ID, status=1)])
            ]
        }
    )
    with pytest.raises(TimeoutError):
        await api.wait_for_completion("nb", RUN_ID, timeout=0, initial_interval=1)
    assert transport.scopes == ["research.wait_for_completion", "research.poll"]
    assert transport.calls[0][2]["expected_epoch"] == 17


@pytest.mark.asyncio
async def test_cancel_lost_response_resolves_only_by_exact_id_poll() -> None:
    api, transport = _api(
        {
            CANCEL_JOB_METHOD: [ServerError("lost")],
            LIST_JOBS_METHOD: [
                research_pb2.ListDiscoverSourcesJobResponse(
                    jobs=[_job(OTHER_ID, status=1), _job(RUN_ID, status=4)]
                )
            ],
        }
    )
    await api.cancel("nb", RUN_ID)
    assert [call[0] for call in transport.calls] == [CANCEL_JOB_METHOD, LIST_JOBS_METHOD]
    assert transport.calls[0][2]["replay_safe"] is False
    assert transport.calls[1][2]["replay_safe"] is True


@pytest.mark.asyncio
async def test_cancel_lost_response_still_in_progress_is_unconfirmed_without_resend() -> None:
    api, transport = _api(
        {
            CANCEL_JOB_METHOD: [ServerError("lost")],
            LIST_JOBS_METHOD: [
                research_pb2.ListDiscoverSourcesJobResponse(jobs=[_job(RUN_ID, status=1)])
            ],
        }
    )
    with pytest.raises(ServerError) as caught:
        await api.cancel("nb", RUN_ID)
    assert getattr(caught.value, "unconfirmed", False) is True
    assert [call[0] for call in transport.calls].count(CANCEL_JOB_METHOD) == 1


@pytest.mark.asyncio
async def test_noncanonical_ids_fail_before_mutation_and_malformed_start_fails_decode() -> None:
    api, transport = _api(
        {
            START_FAST_METHOD: [
                research_pb2.DiscoverSourcesManifoldResponse(source_discovery_job_id="not-a-run-id")
            ]
        }
    )
    with pytest.raises(ValidationError):
        await api.cancel("nb", "not-a-run-id")
    assert transport.scopes == []
    with pytest.raises(DecodingError):
        await api.start("nb", "q")


@pytest.mark.asyncio
async def test_finish_encodes_url_and_markdown_and_never_replays() -> None:
    api, transport = _api(
        {
            FINISH_RUN_METHOD: [
                research_pb2.FinishDiscoverSourcesRunResponse(
                    sources=[
                        research_pb2.ImportedSourceHeader(
                            source_id=read_pb2.SourceId(id=OTHER_ID), title="A"
                        )
                    ]
                )
            ]
        }
    )
    result = await api.import_sources(
        "nb",
        RUN_ID,
        [
            ResearchSource("https://example.com/a", "A", research_task_id=RUN_ID),
            ResearchSource(
                "",
                "Report",
                result_type=5,
                research_task_id=RUN_ID,
                report_markdown="# Report",
            ),
        ],
    )
    request = transport.calls[0][1]
    assert request.user_content[0].web_content.source_name == "A"
    assert request.user_content[1].text_content.content == "# Report"
    assert (
        request.user_content[1].text_content_type == sources_pb2.UserContent.CONTENT_TYPE_MARKDOWN
    )
    assert transport.calls[0][2]["replay_safe"] is False
    assert result == [{"id": OTHER_ID, "title": "A"}]


@pytest.mark.asyncio
async def test_import_rejects_cross_run_source_before_operation_scope() -> None:
    api, transport = _api({})
    with pytest.raises(ResearchTaskMismatchError):
        await api.import_sources(
            "nb",
            RUN_ID,
            [ResearchSource("https://example.com/a", "A", research_task_id=OTHER_ID)],
        )
    assert transport.scopes == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_url_timeout_retries_only_proven_missing_subset() -> None:
    first = ResearchSource("https://example.com/a", "A", research_task_id=RUN_ID)
    second = ResearchSource("https://example.com/b", "B", research_task_id=RUN_ID)
    lister = _Lister(
        [
            [],
            [Source(id="source-a", title="A", url="https://example.com/a")],
        ]
    )
    api, transport = _api(
        {
            FINISH_RUN_METHOD: [
                RPCTimeoutError("lost", timeout_seconds=10),
                research_pb2.FinishDiscoverSourcesRunResponse(
                    sources=[
                        research_pb2.ImportedSourceHeader(
                            source_id=read_pb2.SourceId(id="source-b"), title="B"
                        )
                    ]
                ),
            ]
        },
        lister,
    )
    result = await api.import_sources_with_verification(
        "nb", RUN_ID, [first, second], initial_delay=0
    )
    assert result == [
        {"id": "source-a", "title": "A"},
        {"id": "source-b", "title": "B"},
    ]
    finish_requests = [call[1] for call in transport.calls if call[0] == FINISH_RUN_METHOD]
    assert [entry.web_content.url for entry in finish_requests[0].user_content] == [
        first.url,
        second.url,
    ]
    assert [entry.web_content.url for entry in finish_requests[1].user_content] == [second.url]


@pytest.mark.asyncio
async def test_url_timeout_with_unrelated_concurrent_row_is_ambiguous_without_resend() -> None:
    lister = _Lister(
        [
            [],
            [Source(id="other", title="Other", url="https://unrelated.example")],
        ]
    )
    api, transport = _api(
        {FINISH_RUN_METHOD: [RPCTimeoutError("lost", timeout_seconds=10)]}, lister
    )
    with pytest.raises(RPCError) as caught:
        await api.import_sources_with_verification(
            "nb",
            RUN_ID,
            [ResearchSource("https://example.com/a", "A", research_task_id=RUN_ID)],
        )
    assert getattr(caught.value, "unconfirmed", False) is True
    assert [call[0] for call in transport.calls].count(FINISH_RUN_METHOD) == 1


@pytest.mark.asyncio
async def test_report_timeout_has_zero_resend_and_marks_outcome_unconfirmed() -> None:
    api, transport = _api(
        {FINISH_RUN_METHOD: [RPCTimeoutError("lost", timeout_seconds=10)]},
        _Lister([[]]),
    )
    with pytest.raises(RPCTimeoutError) as caught:
        await api.import_sources_with_verification(
            "nb",
            RUN_ID,
            [ResearchSource("", "Report", result_type=5, report_markdown="# Report")],
        )
    assert getattr(caught.value, "unconfirmed", False) is True
    assert [call[0] for call in transport.calls] == [FINISH_RUN_METHOD]


@pytest.mark.asyncio
async def test_transport_neutral_frontend_classifies_real_android_result() -> None:
    api, _ = _api(
        {
            LIST_JOBS_METHOD: [
                research_pb2.ListDiscoverSourcesJobResponse(jobs=[_job(RUN_ID, status=2)])
            ]
        }
    )
    client = type("Client", (), {"research": api})()
    result = await poll_and_classify(client, "nb", RUN_ID)
    assert result.kind == "completed"
    assert result.task_id == RUN_ID
    assert result.sources == [
        {
            "url": "https://example.com/a",
            "title": "A",
            "result_type": 1,
            "research_task_id": RUN_ID,
            "source_ordinal": 1,
            "hint": "Why A",
        }
    ]
