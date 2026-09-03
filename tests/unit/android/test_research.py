"""Evidence, wire, lifecycle, and frontend tests for public Android Research."""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from tests._helpers.android_supervisor import SupervisedAndroidTransport

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
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._app.research import poll_and_classify
from notebooklm._research import BaseResearchAPI, ResearchAPI
from notebooklm._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT,
    MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT,
)
from notebooklm._types.enums import DiscoveryMode
from notebooklm._types.research import ResearchSource, ResearchStatus
from notebooklm._web.research import WebResearchAPI
from notebooklm.exceptions import (
    AmbiguousResearchTaskError,
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    ResearchTaskMismatchError,
    RPCError,
    RPCTimeoutError,
    ServerError,
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
    def __init__(self, rows: list[list[Source] | BaseException] | None = None) -> None:
        self.rows = deque(rows or [[]])
        self.calls: list[tuple[str, bool]] = []

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Source]:
        self.calls.append((notebook_id, strict))
        value = self.rows.popleft() if self.rows else []
        if isinstance(value, BaseException):
            raise value
        return value


class _SupervisedLister:
    def __init__(self, transport: SupervisedAndroidTransport) -> None:
        self._transport = transport

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Source]:
        return await self._transport.unary(
            "sources.list",
            (notebook_id, strict),
            replay_safe=True,
            response_type=list,
        )


def _api(
    responses: dict[str, list[Any]],
    lister: _Lister | None = None,
    **api_kwargs: Any,
) -> tuple[AndroidResearchAPI, _Transport]:
    transport = _Transport(responses)
    return (
        AndroidResearchAPI(  # type: ignore[arg-type]
            transport,
            lister or _Lister(),
            **api_kwargs,
        ),
        transport,
    )


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
        for name in BaseResearchAPI.__dict__
        if not name.startswith("_") and callable(getattr(BaseResearchAPI, name))
    }
    assert manifest == {
        "start",
        "discover",
        "poll",
        "wait_for_completion",
        "cancel",
        "import_sources",
        "import_sources_with_verification",
        "extract_report_urls",
        "select_cited_sources",
    }
    assert BaseResearchAPI.__abstractmethods__ == frozenset(
        {"start", "discover", "poll", "cancel", "import_sources"}
    )
    assert ResearchAPI is WebResearchAPI
    assert AndroidResearchAPI.__abstractmethods__ == frozenset()
    for name in (
        "wait_for_completion",
        "import_sources_with_verification",
        "extract_report_urls",
        "select_cited_sources",
    ):
        assert inspect.getattr_static(WebResearchAPI, name) is inspect.getattr_static(
            BaseResearchAPI, name
        )
        assert inspect.getattr_static(AndroidResearchAPI, name) is inspect.getattr_static(
            BaseResearchAPI, name
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
@pytest.mark.parametrize("method", [START_FAST_METHOD, START_DEEP_METHOD])
@pytest.mark.parametrize(
    "error",
    [NetworkError("response lost"), RateLimitError("throttled"), ServerError("unavailable")],
)
async def test_research_start_transport_loss_is_unconfirmed_and_never_replayed(
    method: str,
    error: RPCError,
) -> None:
    api, transport = _api({method: [error]})

    with pytest.raises(type(error)) as raised:
        await api.start("nb", "q", mode="deep" if method == START_DEEP_METHOD else "fast")

    assert raised.value is error
    assert getattr(raised.value, "unconfirmed", False) is True
    assert [call[0] for call in transport.calls] == [method]


@pytest.mark.asyncio
async def test_research_start_auth_rejection_is_not_marked_unconfirmed() -> None:
    error = AuthError("auth rejected", rpc_code=16)
    api, transport = _api({START_FAST_METHOD: [error]})

    with pytest.raises(AuthError) as raised:
        await api.start("nb", "q")

    assert raised.value is error
    assert getattr(raised.value, "unconfirmed", False) is False
    assert len(transport.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [START_FAST_METHOD, START_DEEP_METHOD])
async def test_research_start_malformed_success_is_unconfirmed(method: str) -> None:
    response = (
        research_pb2.DiscoverSourcesAsyncResponse(start_session_id="diagnostic")
        if method == START_DEEP_METHOD
        else research_pb2.DiscoverSourcesManifoldResponse()
    )
    api, transport = _api({method: [response]})

    with pytest.raises(DecodingError) as raised:
        await api.start("nb", "q", mode="deep" if method == START_DEEP_METHOD else "fast")

    assert getattr(raised.value, "unconfirmed", False) is True
    assert len(transport.calls) == 1


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
    result = await api.discover("nb", "q")
    method, request, kwargs = transport.calls[0]
    assert method == DISCOVER_SOURCES_METHOD
    assert request.discovery_context.context == "q"
    assert request.discovery_mode == research_pb2.DEFAULT_LLM_SEARCH
    assert request.project_id == "nb"
    assert kwargs["replay_safe"] is False
    assert transport.scopes == ["research.discover"]
    assert result.task_id == RUN_ID and result.summary == "overview"
    assert result.status is ResearchStatus.COMPLETED and result.status_code == 2
    assert result.discovery_mode is DiscoveryMode.DEFAULT_LLM_SEARCH
    assert result.query == "q" and result.source_type == 1
    assert [(src.url, src.title, src.research_task_id) for src in result.sources] == [
        ("https://example.com/a", "A", RUN_ID)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "query", "expected_mode", "sent_query"),
    [
        ("raw", "q", research_pb2.RAW_SEARCH, "q"),
        ("curious", "", research_pb2.CURIOUS_SEARCH, ""),
        ("CURIOUS_RAW", "", research_pb2.CURIOUS_RAW_SEARCH, ""),
        # Curious modes always send an empty context, whatever the caller passed.
        ("curious", "ignored text", research_pb2.CURIOUS_SEARCH, ""),
    ],
)
async def test_discover_sends_the_selected_mode_and_allows_an_empty_curious_query(
    mode: str, query: str, expected_mode: int, sent_query: str
) -> None:
    api, transport = _api(
        {
            DISCOVER_SOURCES_METHOD: [
                research_pb2.DiscoverSourcesResponse(
                    discover_sources_feedback_key=research_pb2.DiscoverSourcesFeedbackKey(
                        discover_sources_id=RUN_ID
                    ),
                )
            ]
        }
    )
    result = await api.discover("nb", query, mode=mode)
    _method, request, _kwargs = transport.calls[0]
    assert request.discovery_mode == expected_mode
    assert request.discovery_context.context == sent_query
    assert result.discovery_mode is DiscoveryMode(expected_mode)
    assert result.query == sent_query
    assert result.sources == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "query", "message"),
    [
        ("deep", "q", "Invalid mode 'deep'"),
        ("lite", "q", "Invalid mode 'lite'"),
        ("default", "   ", "query must not be empty"),
        ("raw", "", "query must not be empty"),
    ],
)
async def test_discover_rejects_unsupported_modes_and_empty_queries_before_the_wire(
    mode: str, query: str, message: str
) -> None:
    api, transport = _api({})
    with pytest.raises(ValidationError, match=message):
        await api.discover("nb", query, mode=mode)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_discover_without_a_job_id_raises_an_unconfirmed_decoding_error() -> None:
    api, transport = _api(
        {DISCOVER_SOURCES_METHOD: [research_pb2.DiscoverSourcesResponse(overview="o")]}
    )
    with pytest.raises(DecodingError) as raised:
        await api.discover("nb", "q")
    assert getattr(raised.value, "unconfirmed", False) is True
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_fast_drive_uses_drive_corpus_on_the_mobile_fast_route() -> None:
    api, transport = _api(
        {
            START_FAST_METHOD: [
                research_pb2.DiscoverSourcesManifoldResponse(source_discovery_job_id=RUN_ID)
            ]
        }
    )

    result = await api.start("nb", "q", source="drive")

    assert result.task_id == RUN_ID
    assert transport.scopes == ["research.start"]
    method, request, kwargs = transport.calls[0]
    assert method == START_FAST_METHOD
    assert request.query.source_type == 2
    assert request.discovery_mode == research_pb2.DEFAULT_LLM_SEARCH
    assert kwargs["replay_safe"] is False


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
@pytest.mark.parametrize(
    "job_id",
    [
        "",
        "not-a-job-id",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        RUN_ID.replace("-", ""),
    ],
)
async def test_poll_rejects_identityless_malformed_and_noncanonical_job_ids(
    job_id: str,
) -> None:
    response = research_pb2.ListDiscoverSourcesJobResponse(jobs=[_job(job_id, status=1)])
    api, _ = _api({LIST_JOBS_METHOD: [response]})

    with pytest.raises(DecodingError, match="canonical job id"):
        await api.poll("nb", RUN_ID)


@pytest.mark.asyncio
async def test_poll_rejects_duplicate_job_ids_even_with_contradictory_statuses() -> None:
    response = research_pb2.ListDiscoverSourcesJobResponse(
        jobs=[_job(RUN_ID, status=1), _job(RUN_ID, status=2)]
    )
    api, _ = _api({LIST_JOBS_METHOD: [response]})

    with pytest.raises(DecodingError, match="duplicate job id"):
        await api.poll("nb", RUN_ID)


@pytest.mark.asyncio
async def test_omitted_proto_status_remains_nonterminal_and_unevidenced() -> None:
    response = research_pb2.ListDiscoverSourcesJobResponse(jobs=[_job(RUN_ID, status=0)])
    api, _ = _api({LIST_JOBS_METHOD: [response]})

    result = await api.poll("nb", RUN_ID)

    assert result.status is ResearchStatus.IN_PROGRESS
    assert result.status_code is None
    assert result.termination_reason is None


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
    cancel_request = transport.calls[0][1]
    assert cancel_request.source_discovery_job_id == RUN_ID
    assert cancel_request.HasField("request_context")
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
async def test_cancel_duplicate_readback_keeps_original_outcome_unconfirmed() -> None:
    response = research_pb2.ListDiscoverSourcesJobResponse(
        jobs=[_job(RUN_ID, status=1), _job(RUN_ID, status=4)]
    )
    api, transport = _api({CANCEL_JOB_METHOD: [ServerError("lost")], LIST_JOBS_METHOD: [response]})

    with pytest.raises(ServerError, match="lost") as caught:
        await api.cancel("nb", RUN_ID)

    assert getattr(caught.value, "unconfirmed", False) is True
    assert [call[0] for call in transport.calls] == [CANCEL_JOB_METHOD, LIST_JOBS_METHOD]


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
    assert transport.calls[0][2]["timeout"] == (
        DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + 2 * DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
    )
    assert result == [{"id": OTHER_ID, "title": "A"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_timeout", "import_timeout", "remaining_budget", "expected_timeout"),
    [
        pytest.param(600.0, AUTO_READ_TIMEOUT, None, 600.0, id="base-floor"),
        pytest.param(600.0, 90.0, None, 90.0, id="explicit-override"),
        pytest.param(600.0, None, None, None, id="inherit-base"),
        pytest.param(30.0, 900.0, 45.0, 45.0, id="remaining-budget-clamp"),
    ],
)
async def test_finish_resolves_android_import_timeout_like_web(
    base_timeout: float,
    import_timeout: float | None,
    remaining_budget: float | None,
    expected_timeout: float | None,
) -> None:
    api, transport = _api(
        {FINISH_RUN_METHOD: [research_pb2.FinishDiscoverSourcesRunResponse()]},
        base_timeout=base_timeout,
        import_research_timeout=import_timeout,
    )

    await api.import_sources(
        "nb",
        RUN_ID,
        [ResearchSource("https://example.com/a", "A")],
        _remaining_budget=remaining_budget,
    )

    assert transport.calls[0][2]["timeout"] == expected_timeout


@pytest.mark.asyncio
async def test_zero_elapsed_import_uses_one_natural_android_observation_window() -> None:
    api, transport = _api(
        {
            FINISH_RUN_METHOD: [
                research_pb2.FinishDiscoverSourcesRunResponse(
                    sources=[
                        research_pb2.ImportedSourceHeader(
                            source_id=read_pb2.SourceId(id="source-a"), title="A"
                        )
                    ]
                )
            ]
        },
        _Lister([[]]),
    )

    result = await api.import_sources_with_verification(
        "nb",
        RUN_ID,
        [ResearchSource("https://example.com/a", "A")],
        max_elapsed=0,
    )

    assert result == [{"id": "source-a", "title": "A"}]
    finish_calls = [call for call in transport.calls if call[0] == FINISH_RUN_METHOD]
    assert len(finish_calls) == 1
    assert finish_calls[0][2]["timeout"] == (
        DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
    )


@pytest.mark.asyncio
async def test_retry_below_minimum_observation_window_never_sends_second_finish() -> None:
    api, transport = _api(
        {FINISH_RUN_METHOD: [RPCTimeoutError("lost", timeout_seconds=10)]},
        _Lister([[], []]),
    )

    with pytest.raises(RPCTimeoutError) as caught:
        await api.import_sources_with_verification(
            "nb",
            RUN_ID,
            [ResearchSource("https://example.com/a", "A")],
            max_elapsed=MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT - 1,
            initial_delay=0,
        )

    assert getattr(caught.value, "unconfirmed", False) is True
    finish_calls = [call for call in transport.calls if call[0] == FINISH_RUN_METHOD]
    assert len(finish_calls) == 1
    assert finish_calls[0][2]["timeout"] == (
        DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
    )


@pytest.mark.asyncio
async def test_already_present_side_channel_matches_web_shape_and_deduplicates_requests() -> None:
    existing = Source(id="source-a", title="Existing A", url="https://example.com/a/")
    api, transport = _api({}, _Lister([[existing]]))

    result = await api.import_sources_with_verification(
        "nb",
        RUN_ID,
        [
            ResearchSource("https://example.com/a", "A"),
            ResearchSource("HTTPS://EXAMPLE.COM/a/", "A repeated"),
        ],
    )

    assert result == []
    assert result.already_present == [
        {"id": "source-a", "title": "Existing A", "url": "https://example.com/a/"}
    ]
    assert transport.calls == []


@pytest.mark.asyncio
async def test_allow_duplicate_timeout_requires_a_new_exact_source_before_success() -> None:
    existing = Source(id="baseline-a", title="Existing A", url="https://example.com/a")
    duplicate = Source(id="source-a-2", title="New A", url="https://example.com/a")
    api, transport = _api(
        {FINISH_RUN_METHOD: [RPCTimeoutError("lost", timeout_seconds=10)]},
        _Lister([[existing], [existing, duplicate]]),
    )

    result = await api.import_sources_with_verification(
        "nb",
        RUN_ID,
        [ResearchSource("https://example.com/a", "A")],
        allow_duplicate=True,
    )

    assert result == [{"id": "source-a-2", "title": "New A"}]
    assert result.already_present == []
    assert [call[0] for call in transport.calls].count(FINISH_RUN_METHOD) == 1


@pytest.mark.asyncio
async def test_timeout_then_failed_precondition_reconciles_each_exact_id_without_third_write() -> (
    None
):
    first = ResearchSource("https://example.com/a", "A")
    second = ResearchSource("https://example.com/b", "B")
    landed_first = Source(id="source-a", title="A", url=first.url)
    landed_second = Source(id="source-b", title="B", url=second.url)
    api, transport = _api(
        {
            FINISH_RUN_METHOD: [
                RPCTimeoutError("lost", timeout_seconds=10),
                RPCError("rejected retry", rpc_code=9),
            ]
        },
        _Lister([[], [landed_first], [landed_first, landed_second]]),
    )

    result = await api.import_sources_with_verification(
        "nb", RUN_ID, [first, second], initial_delay=0
    )

    assert result == [
        {"id": "source-a", "title": "A"},
        {"id": "source-b", "title": "B"},
    ]
    assert [call[0] for call in transport.calls].count(FINISH_RUN_METHOD) == 2


@pytest.mark.asyncio
async def test_retry_failed_precondition_without_new_exact_id_keeps_prior_write_unconfirmed() -> (
    None
):
    first = ResearchSource("https://example.com/a", "A")
    second = ResearchSource("https://example.com/b", "B")
    landed_first = Source(id="source-a", title="A", url=first.url)
    write_error = RPCTimeoutError("lost", timeout_seconds=10)
    failed_precondition = RPCError("rejected retry", rpc_code=9)
    api, transport = _api(
        {
            FINISH_RUN_METHOD: [
                write_error,
                failed_precondition,
            ]
        },
        _Lister([[], [landed_first], [landed_first]]),
    )

    with pytest.raises(RPCTimeoutError, match="lost") as caught:
        await api.import_sources_with_verification("nb", RUN_ID, [first, second], initial_delay=0)

    assert caught.value is write_error
    assert caught.value.__cause__ is failed_precondition
    assert getattr(caught.value, "unconfirmed", False) is True
    classified = classify(caught.value)
    assert classified.category is ErrorCategory.RPC
    assert classified.retriable is False
    assert [call[0] for call in transport.calls].count(FINISH_RUN_METHOD) == 2


@pytest.mark.asyncio
async def test_retry_failed_precondition_probe_failure_keeps_prior_write_unconfirmed() -> None:
    first = ResearchSource("https://example.com/a", "A")
    second = ResearchSource("https://example.com/b", "B")
    landed_first = Source(id="source-a", title="A", url=first.url)
    write_error = RPCTimeoutError("lost", timeout_seconds=10)
    failed_precondition = RPCError("rejected retry", rpc_code=9)
    probe_error = AuthError("probe credentials expired")
    api, transport = _api(
        {FINISH_RUN_METHOD: [write_error, failed_precondition]},
        _Lister([[], [landed_first], probe_error]),
    )

    with pytest.raises(RPCTimeoutError, match="lost") as caught:
        await api.import_sources_with_verification("nb", RUN_ID, [first, second], initial_delay=0)

    assert caught.value is write_error
    assert caught.value.__cause__ is probe_error
    assert getattr(caught.value, "unconfirmed", False) is True
    assert classify(caught.value).retriable is False
    assert [call[0] for call in transport.calls].count(FINISH_RUN_METHOD) == 2


@pytest.mark.asyncio
async def test_failed_baseline_still_allows_one_successful_finish_without_retry() -> None:
    api, transport = _api(
        {FINISH_RUN_METHOD: [research_pb2.FinishDiscoverSourcesRunResponse()]},
        _Lister([ServerError("baseline unavailable", rpc_code=14)]),
    )

    result = await api.import_sources_with_verification(
        "nb", RUN_ID, [ResearchSource("https://example.com/a", "A")]
    )

    assert result == []
    assert [call[0] for call in transport.calls] == [FINISH_RUN_METHOD]


@pytest.mark.asyncio
async def test_baseline_auth_error_propagates_without_sending_finish() -> None:
    auth_error = AuthError("reauthenticate")
    api, transport = _api({}, _Lister([auth_error]))

    with pytest.raises(AuthError) as caught:
        await api.import_sources_with_verification(
            "nb", RUN_ID, [ResearchSource("https://example.com/a", "A")]
        )

    assert caught.value is auth_error
    assert transport.calls == []


@pytest.mark.asyncio
async def test_finish_rate_limit_propagates_without_reconciliation_or_retry() -> None:
    rate_limit = RateLimitError("slow down", retry_after=7)
    lister = _Lister([[]])
    api, transport = _api({FINISH_RUN_METHOD: [rate_limit]}, lister)

    with pytest.raises(RateLimitError) as caught:
        await api.import_sources_with_verification(
            "nb", RUN_ID, [ResearchSource("https://example.com/a", "A")]
        )

    assert caught.value is rate_limit
    assert caught.value.retry_after == 7
    assert lister.calls == [("nb", False)]
    assert [call[0] for call in transport.calls] == [FINISH_RUN_METHOD]


@pytest.mark.parametrize(
    "enrichment_error",
    [
        pytest.param(AuthError("reauthenticate"), id="auth"),
        pytest.param(RateLimitError("slow down", retry_after=3), id="rate-limit"),
    ],
)
@pytest.mark.asyncio
async def test_successful_finish_returns_confirmed_raw_result_when_enrichment_fails(
    enrichment_error: BaseException,
) -> None:
    api, transport = _api(
        {
            FINISH_RUN_METHOD: [
                research_pb2.FinishDiscoverSourcesRunResponse(
                    sources=[
                        research_pb2.ImportedSourceHeader(
                            source_id=read_pb2.SourceId(id="source-a"), title="A"
                        )
                    ]
                )
            ]
        },
        _Lister([[], enrichment_error]),
    )

    result = await api.import_sources_with_verification(
        "nb",
        RUN_ID,
        [
            ResearchSource("https://example.com/a", "A"),
            ResearchSource("https://example.com/b", "B"),
        ],
    )

    assert result == [{"id": "source-a", "title": "A"}]
    assert [call[0] for call in transport.calls].count(FINISH_RUN_METHOD) == 1


@pytest.mark.asyncio
async def test_failed_probe_marks_timeout_unconfirmed_without_resending_finish() -> None:
    write_error = RPCTimeoutError("lost", timeout_seconds=10)
    probe_error = RPCError("probe unavailable", rpc_code=14)
    api, transport = _api(
        {FINISH_RUN_METHOD: [write_error]},
        _Lister([[], probe_error]),
    )

    with pytest.raises(RPCTimeoutError) as caught:
        await api.import_sources_with_verification(
            "nb", RUN_ID, [ResearchSource("https://example.com/a", "A")]
        )

    assert caught.value is write_error
    assert caught.value.__cause__ is probe_error
    assert getattr(caught.value, "unconfirmed", False) is True
    assert [call[0] for call in transport.calls].count(FINISH_RUN_METHOD) == 1


@pytest.mark.parametrize(
    "probe_error",
    [
        pytest.param(AuthError("probe credentials expired"), id="auth"),
        pytest.param(RateLimitError("slow down", retry_after=3), id="rate-limit"),
    ],
)
@pytest.mark.asyncio
async def test_probe_typed_failure_marks_write_unconfirmed_and_non_retriable(
    probe_error: BaseException,
) -> None:
    write_error = RPCTimeoutError("lost", timeout_seconds=10)
    api, transport = _api(
        {FINISH_RUN_METHOD: [write_error]},
        _Lister([[], probe_error]),
    )

    with pytest.raises(RPCTimeoutError, match="lost") as caught:
        await api.import_sources_with_verification(
            "nb", RUN_ID, [ResearchSource("https://example.com/a", "A")]
        )

    assert caught.value is write_error
    assert caught.value.__cause__ is probe_error
    assert getattr(caught.value, "unconfirmed", False) is True
    classified = classify(caught.value)
    assert classified.category is ErrorCategory.RPC
    assert classified.retriable is False
    assert [call[0] for call in transport.calls].count(FINISH_RUN_METHOD) == 1


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
async def test_real_supervisor_finish_completes_in_one_epoch_during_graceful_drain() -> None:
    transport = SupervisedAndroidTransport()
    baseline_started = asyncio.Event()
    baseline_release = asyncio.Event()

    async def _baseline(_request: Any, _kwargs: dict[str, Any]) -> list[Source]:
        baseline_started.set()
        await baseline_release.wait()
        return []

    transport.handlers["sources.list"] = _baseline
    transport.handlers[FINISH_RUN_METHOD] = research_pb2.FinishDiscoverSourcesRunResponse(
        sources=[
            research_pb2.ImportedSourceHeader(source_id=read_pb2.SourceId(id="source-a"), title="A")
        ]
    )
    api = AndroidResearchAPI(transport, _SupervisedLister(transport))  # type: ignore[arg-type]
    task = asyncio.create_task(
        api.import_sources_with_verification(
            "nb",
            RUN_ID,
            [ResearchSource("https://example.com/a", "A")],
            max_elapsed=0,
        )
    )
    await baseline_started.wait()

    await transport.supervisor.stop_accepting(1)
    baseline_release.set()

    assert await task == [{"id": "source-a", "title": "A"}]
    assert [method for method, _request, _kwargs in transport.calls] == [
        "sources.list",
        FINISH_RUN_METHOD,
    ]
    assert transport.calls[1][2]["expected_epoch"] == 1
    assert transport.calls[1][2]["replay_safe"] is False
    assert transport.calls[1][2]["timeout"] == (
        DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
    )
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_real_supervisor_finish_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    baseline_started = asyncio.Event()
    baseline_release = asyncio.Event()

    async def _baseline(_request: Any, _kwargs: dict[str, Any]) -> list[Source]:
        baseline_started.set()
        await baseline_release.wait()
        return []

    transport.handlers["sources.list"] = _baseline
    transport.handlers[FINISH_RUN_METHOD] = research_pb2.FinishDiscoverSourcesRunResponse()
    api = AndroidResearchAPI(transport, _SupervisedLister(transport))  # type: ignore[arg-type]
    task = asyncio.create_task(
        api.import_sources_with_verification(
            "nb", RUN_ID, [ResearchSource("https://example.com/a", "A")]
        )
    )
    await baseline_started.wait()

    old_generation = await transport.force_close_and_reopen()
    baseline_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == ["sources.list"]
    assert old_generation.in_flight == 0


@pytest.mark.asyncio
async def test_real_supervisor_caller_cancellation_settles_import_scope_without_finish() -> None:
    transport = SupervisedAndroidTransport()
    baseline_started = asyncio.Event()
    never_release = asyncio.Event()

    async def _baseline(_request: Any, _kwargs: dict[str, Any]) -> list[Source]:
        baseline_started.set()
        await never_release.wait()
        return []

    transport.handlers["sources.list"] = _baseline
    api = AndroidResearchAPI(transport, _SupervisedLister(transport))  # type: ignore[arg-type]
    task = asyncio.create_task(
        api.import_sources_with_verification(
            "nb", RUN_ID, [ResearchSource("https://example.com/a", "A")]
        )
    )
    await baseline_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [method for method, _request, _kwargs in transport.calls] == ["sources.list"]
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_real_supervisor_cancel_exact_readback_completes_during_graceful_drain() -> None:
    transport = SupervisedAndroidTransport()
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()

    async def _cancel(_request: Any, _kwargs: dict[str, Any]) -> ServerError:
        cancel_started.set()
        await cancel_release.wait()
        return ServerError("lost")

    transport.handlers[CANCEL_JOB_METHOD] = _cancel
    transport.handlers[LIST_JOBS_METHOD] = research_pb2.ListDiscoverSourcesJobResponse(
        jobs=[_job(OTHER_ID, status=1), _job(RUN_ID, status=4)]
    )
    api = AndroidResearchAPI(transport, _SupervisedLister(transport))  # type: ignore[arg-type]
    task = asyncio.create_task(api.cancel("nb", RUN_ID))
    await cancel_started.wait()

    await transport.supervisor.stop_accepting(1)
    cancel_release.set()

    await task
    assert [method for method, _request, _kwargs in transport.calls] == [
        CANCEL_JOB_METHOD,
        LIST_JOBS_METHOD,
    ]
    assert transport.calls[0][2]["replay_safe"] is False
    assert [call[2]["expected_epoch"] for call in transport.calls] == [1, 1]
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_real_supervisor_cancel_readback_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    cancel_started = asyncio.Event()
    cancel_release = asyncio.Event()

    async def _cancel(_request: Any, _kwargs: dict[str, Any]) -> ServerError:
        cancel_started.set()
        await cancel_release.wait()
        return ServerError("lost")

    transport.handlers[CANCEL_JOB_METHOD] = _cancel
    transport.handlers[LIST_JOBS_METHOD] = research_pb2.ListDiscoverSourcesJobResponse()
    api = AndroidResearchAPI(transport, _SupervisedLister(transport))  # type: ignore[arg-type]
    task = asyncio.create_task(api.cancel("nb", RUN_ID))
    await cancel_started.wait()

    old_generation = await transport.force_close_and_reopen()
    cancel_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == [CANCEL_JOB_METHOD]
    assert old_generation.in_flight == 0


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
