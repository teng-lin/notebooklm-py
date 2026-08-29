"""Offline B3 source-write, reconciliation, and content contract tests."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest
from tests._helpers.android_supervisor import SupervisedAndroidTransport

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.v1 import source_settings_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.sources import (
    ADD_SOURCES_METHOD,
    ADD_TENTATIVE_SOURCES_METHOD,
    DELETE_SOURCES_METHOD,
    GENERATE_DOCUMENT_GUIDES_METHOD,
    GET_PROJECT_METHOD,
    LOAD_SOURCE_METHOD,
    MUTATE_SOURCE_METHOD,
    AndroidSourcesAPI,
)
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm.exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    SourceAddError,
    SourceNotFoundError,
    SourceTimeoutError,
    UnsupportedOperationError,
)
from notebooklm.types import Source, SourceStatus

NOTEBOOK_ID = "00000000-0000-4000-8000-000000000100"
SOURCE_A = "00000000-0000-4000-8000-000000000101"
SOURCE_B = "00000000-0000-4000-8000-000000000102"
URL_A = " https://example.invalid/%2f?a=1#fragment "
URL_B = "https://example.invalid/second"


def _uncertain_transport_errors() -> list[Exception]:
    return [
        AuthError("auth", method_id=ADD_TENTATIVE_SOURCES_METHOD, rpc_code=16),
        RateLimitError("rate", method_id=ADD_TENTATIVE_SOURCES_METHOD, rpc_code=8),
        ServerError("server", method_id=ADD_TENTATIVE_SOURCES_METHOD, rpc_code=14),
        NetworkError("network", method_id=ADD_TENTATIVE_SOURCES_METHOD),
        RPCTimeoutError("timeout", timeout_seconds=1.0, method_id=ADD_TENTATIVE_SOURCES_METHOD),
    ]


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


Handler = Callable[[Any, dict[str, Any]], Any]


class FakeTransport:
    """Recording direct-test transport with one epoch-bearing workflow scope."""

    def __init__(self) -> None:
        self.handlers: dict[str, Handler | deque[Any] | Any] = {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease()

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        result = self.handlers[method]
        if isinstance(result, deque):
            result = result.popleft()
        if callable(result):
            result = result(request, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result


def _api(transport: FakeTransport) -> AndroidSourcesAPI:
    return AndroidSourcesAPI(
        cast(AndroidSession, transport),
        cast(AndroidUploadPipeline, object()),
    )


def _source(
    source_id: str,
    *,
    title: str = "Example",
    url: str = URL_A,
    status: int = source_settings_pb2.SOURCE_STATUS_PENDING,
) -> read_pb2.Source:
    return read_pb2.Source(
        source_id=read_pb2.SourceId(id=source_id),
        title=title,
        metadata=read_pb2.SourceMetadata(
            original_source_content_type=read_pb2.SOURCE_CONTENT_TYPE_URL,
            webpage_metadata=read_pb2.WebpageMetadata(url=url),
        ),
        settings=source_settings_pb2.SourceSettings(status=status),
    )


def _project(*sources: read_pb2.Source) -> read_pb2.GetProjectResponse:
    return read_pb2.GetProjectResponse(
        project=read_pb2.Project(id=NOTEBOOK_ID, title="Notebook", sources=sources)
    )


def _registration_handler(ids: list[str]) -> Handler:
    def _handle(request: Any, kwargs: dict[str, Any]) -> Any:
        assert kwargs["replay_safe"] is False
        assert kwargs["expected_epoch"] == 7
        return sources_pb2.AddTentativeSourcesResponse(
            tentative_sources=[
                _source(source_id, title=metadata.name, status=0)
                for metadata, source_id in zip(
                    request.tentative_sources_metadata,
                    ids,
                    strict=True,
                )
            ]
        )

    return _handle


def _successful_transport(
    *,
    commit_sources: list[read_pb2.Source] | None = None,
    project_sources: list[read_pb2.Source] | None = None,
) -> FakeTransport:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A])
    transport.handlers[ADD_SOURCES_METHOD] = sources_pb2.AddSourcesResponse(
        sources=commit_sources or [_source(SOURCE_A)]
    )
    transport.handlers[GET_PROJECT_METHOD] = _project(
        *(project_sources or [_source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_COMPLETE)])
    )
    return transport


@pytest.mark.asyncio
async def test_add_url_preserves_wire_bytes_correlates_id_and_refines_status() -> None:
    transport = _successful_transport()
    result = await _api(transport).add_url(NOTEBOOK_ID, URL_A)

    assert result.id == SOURCE_A
    assert result.status is SourceStatus.READY
    assert transport.scopes == ["source.add_url"]
    assert [call[0] for call in transport.calls] == [
        ADD_TENTATIVE_SOURCES_METHOD,
        ADD_SOURCES_METHOD,
        GET_PROJECT_METHOD,
    ]
    registration = transport.calls[0][1]
    assert registration.project_id == NOTEBOOK_ID
    assert len(registration.tentative_sources_metadata) == 1
    correlation = registration.tentative_sources_metadata[0].name
    assert correlation.startswith("nblm-")
    assert len(correlation) == 37
    commit = transport.calls[1][1]
    assert commit.user_content[0].web_content.url == URL_A
    assert commit.user_content[0].tentative_source_id.id == SOURCE_A
    assert (
        commit.user_content[0].web_content.SerializeToString()
        == b"\n" + bytes([len(URL_A)]) + URL_A.encode()
    )
    assert transport.calls[1][2]["replay_safe"] is False
    assert transport.calls[2][2]["replay_safe"] is True


@pytest.mark.asyncio
async def test_add_url_error_row_is_committed_result_when_not_waiting() -> None:
    error_row = _source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_ERROR)
    tentative_row = _source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_TENTATIVE)
    transport = _successful_transport(commit_sources=[error_row], project_sources=[tentative_row])

    result = await _api(transport).add_url(NOTEBOOK_ID, URL_A, wait=False)

    assert result.status is SourceStatus.ERROR


@pytest.mark.asyncio
async def test_clean_registration_omission_is_known_failure_not_unconfirmed() -> None:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = sources_pb2.AddTentativeSourcesResponse()

    with pytest.raises(SourceAddError) as raised:
        await _api(transport).add_url(NOTEBOOK_ID, URL_A)

    assert getattr(raised.value, "unconfirmed", False) is False
    assert [call[0] for call in transport.calls] == [ADD_TENTATIVE_SOURCES_METHOD]


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_id", ["", "source-a", "NOT-A-UUID"])
async def test_noncanonical_registration_ids_are_sanitized_unconfirmed(
    malformed_id: str,
) -> None:
    transport = FakeTransport()

    def _malformed(request: Any, kwargs: dict[str, Any]) -> Any:
        del kwargs
        return sources_pb2.AddTentativeSourcesResponse(
            tentative_sources=[
                _source(malformed_id, title=request.tentative_sources_metadata[0].name)
            ]
        )

    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _malformed
    with pytest.raises(SourceAddError) as raised:
        await _api(transport).add_url(NOTEBOOK_ID, URL_A)
    assert getattr(raised.value, "unconfirmed", False) is True
    assert type(raised.value.cause) in {type(None)}
    assert [call[0] for call in transport.calls] == [ADD_TENTATIVE_SOURCES_METHOD]


@pytest.mark.asyncio
async def test_malformed_registration_envelope_never_leaks_attribute_error() -> None:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = object()
    with pytest.raises(SourceAddError) as raised:
        await _api(transport).add_url(NOTEBOOK_ID, URL_A)
    assert getattr(raised.value, "unconfirmed", False) is True
    assert isinstance(raised.value.cause, RPCError)


@pytest.mark.asyncio
async def test_duplicate_returned_name_is_unconfirmed_and_unexpected_row_is_isolated() -> None:
    transport = FakeTransport()

    def _duplicate(request: Any, kwargs: dict[str, Any]) -> Any:
        del kwargs
        first, second = request.tentative_sources_metadata
        return sources_pb2.AddTentativeSourcesResponse(
            tentative_sources=[
                _source(SOURCE_A, title=first.name),
                _source("00000000-0000-4000-8000-000000000103", title=first.name),
                _source(SOURCE_B, title=second.name),
                _source("00000000-0000-4000-8000-000000000104", title="unexpected"),
            ]
        )

    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _duplicate
    transport.handlers[ADD_SOURCES_METHOD] = sources_pb2.AddSourcesResponse()
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_B, url=URL_B))
    results = await _api(transport)._add_urls_batch(NOTEBOOK_ID, [URL_A, URL_B])
    assert results[0].error is not None
    assert getattr(results[0].error, "unconfirmed", False) is True
    assert results[1].source is not None and results[1].source.id == SOURCE_B
    commit = next(call[1] for call in transport.calls if call[0] == ADD_SOURCES_METHOD)
    assert [entry.tentative_source_id.id for entry in commit.user_content] == [SOURCE_B]


@pytest.mark.asyncio
async def test_uncertain_registration_is_marked_and_never_replayed_or_probed() -> None:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = RPCError(
        "sanitized",
        rpc_code=14,
    )

    with pytest.raises(SourceAddError) as raised:
        await _api(transport).add_url(NOTEBOOK_ID, URL_A)

    assert getattr(raised.value, "unconfirmed", False) is True
    assert str(raised.value).startswith("UNRESOLVED")
    assert [call[0] for call in transport.calls] == [ADD_TENTATIVE_SOURCES_METHOD]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", _uncertain_transport_errors())
async def test_typed_uncertain_registration_is_wrapped_for_single_and_batch(
    transport_error: Exception,
) -> None:
    for batch in (False, True):
        transport = FakeTransport()
        transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = transport_error
        api = _api(transport)

        if batch:
            outcomes = await api._add_urls_batch(NOTEBOOK_ID, [URL_A, URL_B])
            failures = [item.error for item in outcomes]
        else:
            with pytest.raises(SourceAddError) as raised:
                await api.add_url(NOTEBOOK_ID, URL_A)
            failures = [raised.value]

        assert all(error is not None for error in failures)
        assert all(getattr(error, "unconfirmed", False) for error in failures)
        assert [call[0] for call in transport.calls] == [ADD_TENTATIVE_SOURCES_METHOD]
        assert DELETE_SOURCES_METHOD not in [call[0] for call in transport.calls]


@pytest.mark.asyncio
async def test_uncertain_commit_uses_one_exact_id_read_and_never_replays_write() -> None:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A])
    transport.handlers[ADD_SOURCES_METHOD] = RPCError("sanitized", rpc_code=14)
    transport.handlers[GET_PROJECT_METHOD] = _project(
        _source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_PENDING)
    )

    result = await _api(transport).add_url(NOTEBOOK_ID, URL_A)

    assert result.id == SOURCE_A
    assert [call[0] for call in transport.calls].count(ADD_SOURCES_METHOD) == 1
    assert [call[0] for call in transport.calls].count(GET_PROJECT_METHOD) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("rpc_code", [4, 14, 16])
async def test_each_write_is_dispatched_once_after_auth_timeout_or_disconnect(
    rpc_code: int,
) -> None:
    registration = FakeTransport()
    registration.handlers[ADD_TENTATIVE_SOURCES_METHOD] = RPCError("safe", rpc_code=rpc_code)
    with pytest.raises(SourceAddError):
        await _api(registration).add_url(NOTEBOOK_ID, URL_A)
    assert [call[0] for call in registration.calls] == [ADD_TENTATIVE_SOURCES_METHOD]

    commit = FakeTransport()
    commit.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A])
    commit.handlers[ADD_SOURCES_METHOD] = RPCError("safe", rpc_code=rpc_code)
    commit.handlers[GET_PROJECT_METHOD] = _project()
    with pytest.raises(SourceAddError):
        await _api(commit).add_url(NOTEBOOK_ID, URL_A)
    methods = [call[0] for call in commit.calls]
    assert methods.count(ADD_TENTATIVE_SOURCES_METHOD) == 1
    assert methods.count(ADD_SOURCES_METHOD) == 1
    assert methods.count(GET_PROJECT_METHOD) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", _uncertain_transport_errors())
async def test_typed_uncertain_commit_uses_one_read_without_replay_or_cleanup(
    transport_error: Exception,
) -> None:
    transport = _successful_transport()
    transport.handlers[ADD_SOURCES_METHOD] = transport_error

    source = await _api(transport).add_url(NOTEBOOK_ID, URL_A)

    assert source.id == SOURCE_A
    methods = [call[0] for call in transport.calls]
    assert methods.count(ADD_TENTATIVE_SOURCES_METHOD) == 1
    assert methods.count(ADD_SOURCES_METHOD) == 1
    assert methods.count(GET_PROJECT_METHOD) == 1
    assert DELETE_SOURCES_METHOD not in methods


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", _uncertain_transport_errors())
async def test_typed_failed_readback_preserves_affirmative_response_proof(
    transport_error: Exception,
) -> None:
    transport = _successful_transport(
        commit_sources=[_source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_COMPLETE)]
    )
    transport.handlers[GET_PROJECT_METHOD] = transport_error

    source = await _api(transport).add_url(NOTEBOOK_ID, URL_A)

    assert source.id == SOURCE_A
    assert source.status is SourceStatus.READY
    methods = [call[0] for call in transport.calls]
    assert methods.count(ADD_SOURCES_METHOD) == 1
    assert methods.count(GET_PROJECT_METHOD) == 1
    assert DELETE_SOURCES_METHOD not in methods
    assert DELETE_SOURCES_METHOD not in methods


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_status", "read_status", "expected"),
    [
        (
            source_settings_pb2.SOURCE_STATUS_PENDING,
            source_settings_pb2.SOURCE_STATUS_COMPLETE,
            SourceStatus.READY,
        ),
        (
            source_settings_pb2.SOURCE_STATUS_PENDING,
            source_settings_pb2.SOURCE_STATUS_ERROR,
            SourceStatus.ERROR,
        ),
        (
            source_settings_pb2.SOURCE_STATUS_COMPLETE,
            source_settings_pb2.SOURCE_STATUS_TENTATIVE,
            SourceStatus.READY,
        ),
        (
            source_settings_pb2.SOURCE_STATUS_ERROR,
            source_settings_pb2.SOURCE_STATUS_TENTATIVE,
            SourceStatus.ERROR,
        ),
    ],
)
async def test_commit_proof_refinement_and_stale_read_non_erasure(
    response_status: int,
    read_status: int,
    expected: SourceStatus,
) -> None:
    transport = _successful_transport(
        commit_sources=[_source(SOURCE_A, status=response_status)],
        project_sources=[_source(SOURCE_A, status=read_status)],
    )
    result = await _api(transport).add_url(NOTEBOOK_ID, URL_A)
    assert result.status is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_status", "read_status"),
    [
        (
            source_settings_pb2.SOURCE_STATUS_COMPLETE,
            source_settings_pb2.SOURCE_STATUS_ERROR,
        ),
        (
            source_settings_pb2.SOURCE_STATUS_ERROR,
            source_settings_pb2.SOURCE_STATUS_COMPLETE,
        ),
    ],
)
async def test_incompatible_terminal_commit_evidence_is_unresolved(
    response_status: int,
    read_status: int,
) -> None:
    transport = _successful_transport(
        commit_sources=[_source(SOURCE_A, status=response_status)],
        project_sources=[_source(SOURCE_A, status=read_status)],
    )
    with pytest.raises(SourceAddError) as raised:
        await _api(transport).add_url(NOTEBOOK_ID, URL_A)
    assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_failed_readback_keeps_affirmative_response_but_epoch_failure_propagates() -> None:
    transport = _successful_transport(
        commit_sources=[_source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_PENDING)]
    )
    transport.handlers[GET_PROJECT_METHOD] = RPCError("safe", rpc_code=14)
    result = await _api(transport).add_url(NOTEBOOK_ID, URL_A)
    assert result.status is SourceStatus.PROCESSING

    retired = _successful_transport()
    retired.handlers[GET_PROJECT_METHOD] = RuntimeError("retired epoch")
    with pytest.raises(RuntimeError, match="retired epoch"):
        await _api(retired).add_url(NOTEBOOK_ID, URL_A)
    assert DELETE_SOURCES_METHOD not in [call[0] for call in retired.calls]


@pytest.mark.asyncio
async def test_unproved_commit_statuses_are_unconfirmed_not_tentative_success() -> None:
    for status in (
        source_settings_pb2.SOURCE_STATUS_UNSPECIFIED,
        source_settings_pb2.SOURCE_STATUS_TENTATIVE,
        source_settings_pb2.SOURCE_STATUS_PENDING_DELETION,
        99,
    ):
        transport = _successful_transport(
            commit_sources=[_source(SOURCE_A, status=status)],
            project_sources=[_source(SOURCE_A, status=status)],
        )
        with pytest.raises(SourceAddError) as raised:
            await _api(transport).add_url(NOTEBOOK_ID, URL_A)
        assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_batch_registration_truncation_and_commit_truncation_are_stage_specific() -> None:
    transport = FakeTransport()

    def _register(request: Any, kwargs: dict[str, Any]) -> Any:
        del kwargs
        return sources_pb2.AddTentativeSourcesResponse(
            tentative_sources=[_source(SOURCE_A, title=request.tentative_sources_metadata[0].name)]
        )

    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _register
    transport.handlers[ADD_SOURCES_METHOD] = sources_pb2.AddSourcesResponse()
    transport.handlers[GET_PROJECT_METHOD] = _project(
        _source(SOURCE_A, url=URL_A, status=source_settings_pb2.SOURCE_STATUS_PENDING)
    )

    results = await _api(transport)._add_urls_batch(NOTEBOOK_ID, [URL_A, URL_B])

    assert results[0].source is not None and results[0].source.id == SOURCE_A
    assert results[1].error is not None
    assert getattr(results[1].error, "unconfirmed", False) is False
    commit = next(call[1] for call in transport.calls if call[0] == ADD_SOURCES_METHOD)
    assert [item.web_content.url for item in commit.user_content] == [URL_A]


@pytest.mark.asyncio
async def test_batch_duplicate_urls_keep_occurrence_correlation_and_order() -> None:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A, SOURCE_B])
    transport.handlers[ADD_SOURCES_METHOD] = sources_pb2.AddSourcesResponse()
    transport.handlers[GET_PROJECT_METHOD] = _project(
        _source(SOURCE_A, url=URL_B),
        _source(SOURCE_B, url=URL_B),
    )

    results = await _api(transport)._add_urls_batch(NOTEBOOK_ID, [URL_B, URL_B])

    assert [item.url for item in results] == [URL_B, URL_B]
    assert [item.source.id if item.source else None for item in results] == [SOURCE_A, SOURCE_B]
    names = transport.calls[0][1].tentative_sources_metadata
    assert names[0].name != names[1].name


@pytest.mark.asyncio
async def test_duplicate_registration_ids_exclude_every_involved_entry_from_commit() -> None:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A, SOURCE_A])

    results = await _api(transport)._add_urls_batch(NOTEBOOK_ID, [URL_A, URL_B])

    assert all(item.error is not None for item in results)
    assert all(getattr(item.error, "unconfirmed", False) for item in results)
    assert [call[0] for call in transport.calls] == [ADD_TENTATIVE_SOURCES_METHOD]


@pytest.mark.asyncio
async def test_youtube_rejection_and_empty_batch_have_zero_io() -> None:
    transport = FakeTransport()
    api = _api(transport)
    with pytest.raises(UnsupportedOperationError):
        await api.add_url(NOTEBOOK_ID, "https://youtu.be/abcdefghijk")
    with pytest.raises(UnsupportedOperationError):
        await api._add_urls_batch(
            NOTEBOOK_ID,
            ["https://www.youtube.com/watch?v=abcdefghijk"],
        )
    assert await api._add_urls_batch(NOTEBOOK_ID, []) == []
    assert transport.calls == []
    assert transport.scopes == []


class _OrderedSources(AndroidSourcesAPI):
    def __init__(self, session: AndroidSession, order: list[str]) -> None:
        self.order = order
        super().__init__(session, cast(AndroidUploadPipeline, object()))

    async def wait_until_ready(self, notebook_id: str, source_id: str, **kwargs: Any) -> Source:
        del notebook_id, kwargs
        self.order.append("wait")
        return Source(id=source_id, title="upstream", status=SourceStatus.READY)

    async def rename(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        return_object: bool = True,
    ) -> Source | None:
        del notebook_id, return_object
        self.order.append("rename")
        return Source(id=source_id, title=new_title)


@pytest.mark.asyncio
async def test_wait_precedes_best_effort_title_finalization() -> None:
    order: list[str] = []
    transport = _successful_transport()
    result = await _OrderedSources(cast(AndroidSession, transport), order).add_url(
        NOTEBOOK_ID,
        URL_A,
        wait=True,
        title="  requested  ",
    )
    assert order == ["wait", "rename"]
    assert result.title == "requested"


@pytest.mark.asyncio
async def test_blank_title_skips_mutation_and_normal_failure_is_best_effort() -> None:
    blank = _successful_transport()
    result = await _api(blank).add_url(NOTEBOOK_ID, URL_A, title="   ")
    assert result.id == SOURCE_A
    assert MUTATE_SOURCE_METHOD not in [call[0] for call in blank.calls]

    failed = _successful_transport()
    failed.handlers[MUTATE_SOURCE_METHOD] = RPCError("safe", rpc_code=14)
    result = await _api(failed).add_url(NOTEBOOK_ID, URL_A, title="Requested")
    assert result.title == "Example"
    assert [call[0] for call in failed.calls].count(MUTATE_SOURCE_METHOD) == 1


@pytest.mark.asyncio
async def test_title_cancellation_and_readiness_timeout_dispatch_no_later_mutation() -> None:
    cancelled = _successful_transport()
    cancelled.handlers[MUTATE_SOURCE_METHOD] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _api(cancelled).add_url(NOTEBOOK_ID, URL_A, title="Requested")
    assert [call[0] for call in cancelled.calls].count(MUTATE_SOURCE_METHOD) == 1

    class _TimeoutSources(AndroidSourcesAPI):
        async def wait_until_ready(
            self,
            notebook_id: str,
            source_id: str,
            **kwargs: Any,
        ) -> Source:
            del notebook_id, kwargs
            raise SourceTimeoutError(source_id, 0.01)

    timed = _successful_transport()
    with pytest.raises(SourceTimeoutError):
        await _TimeoutSources(
            cast(AndroidSession, timed), cast(AndroidUploadPipeline, object())
        ).add_url(
            NOTEBOOK_ID,
            URL_A,
            wait=True,
            wait_timeout=0.01,
            title="Requested",
        )
    assert MUTATE_SOURCE_METHOD not in [call[0] for call in timed.calls]


@pytest.mark.asyncio
async def test_delete_and_rename_use_non_replayed_exact_wire_shapes() -> None:
    transport = FakeTransport()
    transport.handlers[DELETE_SOURCES_METHOD] = __import__(
        "google.protobuf.empty_pb2",
        fromlist=["Empty"],
    ).Empty()
    transport.handlers[MUTATE_SOURCE_METHOD] = _source(SOURCE_A, title="Renamed")
    api = _api(transport)

    await api.delete(NOTEBOOK_ID, SOURCE_A)
    renamed = await api.rename(NOTEBOOK_ID, SOURCE_A, "Renamed")

    delete_request = transport.calls[0][1]
    mutate_request = transport.calls[1][1]
    assert [item.id for item in delete_request.source_ids] == [SOURCE_A]
    assert mutate_request.source_id.id == SOURCE_A
    assert mutate_request.mutations[0].change_title.title == "Renamed"
    assert all(call[2]["replay_safe"] is False for call in transport.calls)
    assert renamed is not None and renamed.title == "Renamed"


@pytest.mark.asyncio
async def test_null_rename_echo_hydrates_exact_id_and_detects_miss() -> None:
    transport = FakeTransport()
    transport.handlers[MUTATE_SOURCE_METHOD] = read_pb2.Source()
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_A, title="Hydrated"))
    result = await _api(transport).rename(NOTEBOOK_ID, SOURCE_A, "Requested")
    assert result is not None and result.title == "Hydrated"

    missing = FakeTransport()
    missing.handlers[MUTATE_SOURCE_METHOD] = read_pb2.Source()
    missing.handlers[GET_PROJECT_METHOD] = _project()
    with pytest.raises(SourceNotFoundError):
        await _api(missing).rename(NOTEBOOK_ID, SOURCE_A, "Requested", return_object=False)


@pytest.mark.asyncio
async def test_rename_readback_finishes_during_graceful_drain_in_one_epoch() -> None:
    transport = SupervisedAndroidTransport()
    mutation_started = asyncio.Event()
    mutation_release = asyncio.Event()

    async def _mutate(_request: Any, _kwargs: dict[str, Any]) -> Any:
        mutation_started.set()
        await mutation_release.wait()
        return read_pb2.Source()

    transport.handlers[MUTATE_SOURCE_METHOD] = _mutate
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_A, title="Hydrated"))
    task = asyncio.create_task(_api(cast(Any, transport)).rename(NOTEBOOK_ID, SOURCE_A, "Hydrated"))
    await mutation_started.wait()

    await transport.supervisor.stop_accepting(1)
    mutation_release.set()

    result = await task
    assert result is not None and result.title == "Hydrated"
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in transport.calls] == [1, 1]
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_rename_readback_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    mutation_started = asyncio.Event()
    mutation_release = asyncio.Event()

    async def _mutate(_request: Any, _kwargs: dict[str, Any]) -> Any:
        mutation_started.set()
        await mutation_release.wait()
        return read_pb2.Source()

    transport.handlers[MUTATE_SOURCE_METHOD] = _mutate
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_A))
    task = asyncio.create_task(_api(cast(Any, transport)).rename(NOTEBOOK_ID, SOURCE_A, "Hydrated"))
    await mutation_started.wait()

    old_generation = await transport.force_close_and_reopen()
    mutation_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == [MUTATE_SOURCE_METHOD]
    assert old_generation.in_flight == 0
    assert transport.supervisor._current is not None
    assert transport.supervisor._current.epoch == 2


@pytest.mark.asyncio
async def test_not_found_delete_is_idempotent_and_rename_maps_domain_error() -> None:
    deleted = FakeTransport()
    deleted.handlers[DELETE_SOURCES_METHOD] = RPCError("safe", rpc_code=5)
    assert await _api(deleted).delete(NOTEBOOK_ID, SOURCE_A) is None

    renamed = FakeTransport()
    renamed.handlers[MUTATE_SOURCE_METHOD] = RPCError("safe", rpc_code=5)
    with pytest.raises(SourceNotFoundError) as raised:
        await _api(renamed).rename(NOTEBOOK_ID, SOURCE_A, "new")
    assert raised.value.source_id == SOURCE_A


@pytest.mark.asyncio
async def test_guide_and_fulltext_decode_only_captured_flat_fields() -> None:
    transport = FakeTransport()
    transport.handlers[GENERATE_DOCUMENT_GUIDES_METHOD] = (
        sources_pb2.GenerateDocumentGuidesResponse(
            guides=[
                sources_pb2.DocumentGuide(
                    source=sources_pb2.InputSource(source_id=read_pb2.SourceId(id=SOURCE_A)),
                    snippet=sources_pb2.Snippet(text_snippet="Summary"),
                    main_ideas=sources_pb2.MainIdeas(text_ideas=["one", "two"]),
                )
            ]
        )
    )
    transport.handlers[LOAD_SOURCE_METHOD] = sources_pb2.LoadSourceResponse(
        source=_source(SOURCE_A, title="Document"),
        plain_text=sources_pb2.PlainTextSourceContent(header="ignored", body="plain body"),
        markdown_string="# markdown",
    )
    api = _api(transport)

    guide = await api.get_guide(NOTEBOOK_ID, SOURCE_A)
    fulltext = await api.get_fulltext(NOTEBOOK_ID, SOURCE_A)

    assert guide.summary == "Summary"
    assert guide.keywords == ("one", "two")
    assert fulltext.content == "plain body"
    assert fulltext.char_count == 10
    assert fulltext.document.blocks == ()
    assert transport.calls[0][2]["replay_safe"] is True
    assert transport.calls[1][2]["replay_safe"] is True


@pytest.mark.asyncio
async def test_cancellation_between_registration_and_commit_dispatches_no_later_stage() -> None:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _api(transport).add_url(NOTEBOOK_ID, URL_A)
    assert [call[0] for call in transport.calls] == [ADD_TENTATIVE_SOURCES_METHOD]
