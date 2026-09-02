"""Offline source-write, reconciliation, and content contract tests."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from tests._helpers.android_supervisor import SupervisedAndroidTransport

from notebooklm._android.codecs.documents import decode_document, tailwind_doc_plain_text
from notebooklm._android.codecs.sources import select_document_guide
from notebooklm._android.phenotype import PhenotypeError, PhenotypeTokenProvider
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    read_pb2,
    sources_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1.agency import (
    supported_pb2 as agency_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.v1 import source_settings_pb2
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import source_content_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.sources import (
    ADD_SOURCES_METHOD,
    ADD_TENTATIVE_SOURCES_METHOD,
    CHECK_SOURCE_FRESHNESS_METHOD,
    DELETE_SOURCES_METHOD,
    GENERATE_DOCUMENT_GUIDES_METHOD,
    GET_PROJECT_METHOD,
    LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD,
    LOAD_SOURCE_METHOD,
    MUTATE_SOURCE_METHOD,
    REFRESH_SOURCE_METHOD,
    AndroidSourcesAPI,
)
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm._types.research import SourceGuide
from notebooklm._types.sources import PlayBookExportReason
from notebooklm.exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    NonIdempotentRetryError,
    PlayBookNotExportableError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    SourceAddError,
    SourceNotFoundError,
    SourceTimeoutError,
    ValidationError,
)
from notebooklm.types import Source, SourceStatus

NOTEBOOK_ID = "00000000-0000-4000-8000-000000000100"
SOURCE_A = "00000000-0000-4000-8000-000000000101"
SOURCE_B = "00000000-0000-4000-8000-000000000102"
URL_A = " https://example.invalid/%2f?a=1#fragment "
URL_B = "https://example.invalid/second"


def _uncertain_transport_errors() -> list[Exception]:
    return [
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
        self.timeline: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease()

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.timeline.append(method)
        self.calls.append((method, request, kwargs))
        if method == GET_PROJECT_METHOD and method not in self.handlers:
            return _project(_source(SOURCE_A), _source(SOURCE_B))
        result = self.handlers[method]
        if isinstance(result, deque):
            result = result.popleft()
        if callable(result):
            result = result(request, kwargs)
        if isinstance(result, BaseException):
            raise result
        return result

    async def prepare_metadata(
        self,
        metadata_augmentor: Any,
        *,
        expected_epoch: int,
    ) -> tuple[tuple[str, str | bytes], ...]:
        assert expected_epoch == 7
        self.timeline.append("prepare_metadata")
        return tuple(await metadata_augmentor("fake-bearer"))


class FakePhenotype:
    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = deque(outcomes)
        self.calls: list[tuple[str, bool]] = []

    async def experiment_metadata(
        self,
        bearer: str,
        *,
        force: bool = False,
    ) -> tuple[tuple[str, bytes], ...]:
        self.calls.append((bearer, force))
        if self.outcomes:
            outcome = self.outcomes.popleft()
            if isinstance(outcome, BaseException):
                raise outcome
            return cast(tuple[tuple[str, bytes], ...], outcome)
        return (("x-phenotype-bin", b"metadata"),)


def _api(
    transport: FakeTransport,
    *,
    phenotype: FakePhenotype | None = None,
) -> AndroidSourcesAPI:
    return AndroidSourcesAPI(
        cast(AndroidSession, transport),
        cast(AndroidUploadPipeline, object()),
        phenotype=cast(PhenotypeTokenProvider, phenotype or FakePhenotype()),
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
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = ServerError(
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
    transport.handlers[ADD_SOURCES_METHOD] = ServerError("sanitized", rpc_code=14)
    transport.handlers[GET_PROJECT_METHOD] = _project(
        _source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_PENDING)
    )

    result = await _api(transport).add_url(NOTEBOOK_ID, URL_A)

    assert result.id == SOURCE_A
    assert [call[0] for call in transport.calls].count(ADD_SOURCES_METHOD) == 1
    assert [call[0] for call in transport.calls].count(GET_PROJECT_METHOD) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error", _uncertain_transport_errors())
async def test_each_write_is_dispatched_once_after_transport_loss(
    transport_error: Exception,
) -> None:
    registration = FakeTransport()
    registration.handlers[ADD_TENTATIVE_SOURCES_METHOD] = transport_error
    with pytest.raises(SourceAddError):
        await _api(registration).add_url(NOTEBOOK_ID, URL_A)
    assert [call[0] for call in registration.calls] == [ADD_TENTATIVE_SOURCES_METHOD]

    commit = FakeTransport()
    commit.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A])
    commit.handlers[ADD_SOURCES_METHOD] = transport_error
    commit.handlers[GET_PROJECT_METHOD] = _project()
    with pytest.raises(SourceAddError):
        await _api(commit).add_url(NOTEBOOK_ID, URL_A)
    methods = [call[0] for call in commit.calls]
    assert methods.count(ADD_TENTATIVE_SOURCES_METHOD) == 1
    assert methods.count(ADD_SOURCES_METHOD) == 1
    assert methods.count(GET_PROJECT_METHOD) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        AuthError("auth", method_id=ADD_TENTATIVE_SOURCES_METHOD, rpc_code=16),
        RPCError("invalid", method_id=ADD_TENTATIVE_SOURCES_METHOD, rpc_code=3),
    ],
)
async def test_confirmed_registration_rejections_are_not_marked_unconfirmed(
    error: RPCError,
) -> None:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = error

    with pytest.raises(type(error)) as raised:
        await _api(transport).add_text(NOTEBOOK_ID, "Title", "Body")

    assert raised.value is error
    assert getattr(raised.value, "unconfirmed", False) is False
    assert [call[0] for call in transport.calls] == [ADD_TENTATIVE_SOURCES_METHOD]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "drive"])
async def test_text_and_drive_tentative_registration_loss_is_unconfirmed(kind: str) -> None:
    transport = FakeTransport()
    error = RPCTimeoutError(
        "registration timed out",
        timeout_seconds=1.0,
        method_id=ADD_TENTATIVE_SOURCES_METHOD,
    )
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = error
    api = _api(transport)

    with pytest.raises(SourceAddError) as raised:
        if kind == "text":
            await api.add_text(NOTEBOOK_ID, "Title", "Body")
        else:
            await api.add_drive(NOTEBOOK_ID, "drive-id", "Drive title")

    assert getattr(raised.value, "unconfirmed", False) is True
    assert raised.value.cause is error
    assert [method for method, _request, _kwargs in transport.calls] == [
        ADD_TENTATIVE_SOURCES_METHOD
    ]


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
async def test_youtube_single_uses_exact_video_branch() -> None:
    youtube_url = "https://youtu.be/abcdefghijk"
    transport = _successful_transport()

    result = await _api(transport).add_url(NOTEBOOK_ID, youtube_url)

    assert result.id == SOURCE_A
    commit = next(call[1] for call in transport.calls if call[0] == ADD_SOURCES_METHOD)
    content = commit.user_content[0]
    assert content.video_content.youtube_url == youtube_url
    assert not content.HasField("web_content")
    assert content.tentative_source_id.id == SOURCE_A
    assert commit.HasField("request_context")


@pytest.mark.asyncio
async def test_mixed_url_batch_uses_web_and_video_branches_in_order() -> None:
    youtube_url = "https://www.youtube.com/watch?v=abcdefghijk"
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = _registration_handler([SOURCE_A, SOURCE_B])
    transport.handlers[ADD_SOURCES_METHOD] = sources_pb2.AddSourcesResponse()
    transport.handlers[GET_PROJECT_METHOD] = _project(
        _source(SOURCE_A, url=URL_A),
        _source(SOURCE_B, url=youtube_url),
    )

    outcomes = await _api(transport)._add_urls_batch(NOTEBOOK_ID, [URL_A, youtube_url])

    assert [item.source.id if item.source else None for item in outcomes] == [SOURCE_A, SOURCE_B]
    commit = next(call[1] for call in transport.calls if call[0] == ADD_SOURCES_METHOD)
    assert commit.user_content[0].web_content.url == URL_A
    assert not commit.user_content[0].HasField("video_content")
    assert commit.user_content[1].video_content.youtube_url == youtube_url
    assert not commit.user_content[1].HasField("web_content")


@pytest.mark.asyncio
async def test_empty_url_batch_has_zero_io() -> None:
    transport = FakeTransport()

    assert await _api(transport)._add_urls_batch(NOTEBOOK_ID, []) == []
    assert transport.calls == []
    assert transport.scopes == []


@pytest.mark.asyncio
async def test_add_text_uses_registered_exact_content_and_rejects_idempotent_opt_in() -> None:
    transport = _successful_transport()

    result = await _api(transport).add_text(NOTEBOOK_ID, "Title", "Body")

    assert result.id == SOURCE_A
    assert transport.scopes == ["source.add_text"]
    commit = next(call[1] for call in transport.calls if call[0] == ADD_SOURCES_METHOD)
    content = commit.user_content[0]
    assert content.text_content.source_name == "Title"
    assert content.text_content.content == "Body"
    assert content.text_content_type == sources_pb2.UserContent.CONTENT_TYPE_TEXT
    assert content.tentative_source_id.id == SOURCE_A

    before_io = FakeTransport()
    with pytest.raises(NonIdempotentRetryError, match="cannot be marked idempotent"):
        await _api(before_io).add_text(NOTEBOOK_ID, "Title", "Body", idempotent=True)
    assert before_io.calls == []
    assert before_io.scopes == []


@pytest.mark.asyncio
async def test_add_drive_uses_exact_content_and_validates_identifier_before_io() -> None:
    transport = _successful_transport()
    transport.handlers[MUTATE_SOURCE_METHOD] = sources_pb2.MutateSourceResponse(
        source=_source(SOURCE_A, title="Drive title")
    )

    result = await _api(transport).add_drive(
        NOTEBOOK_ID,
        "drive-document-id",
        "Drive title",
        mime_type="application/vnd.google-apps.presentation",
    )

    assert result.id == SOURCE_A
    assert result.title == "Drive title"
    assert transport.scopes == ["source.add_drive", "source.rename"]
    commit = next(call[1] for call in transport.calls if call[0] == ADD_SOURCES_METHOD)
    content = commit.user_content[0]
    assert content.google_drive_content.document_id == "drive-document-id"
    assert content.google_drive_content.mime_type == "application/vnd.google-apps.presentation"
    assert content.google_drive_content.can_download is True
    assert content.google_drive_content.source_name == "Drive title"
    assert content.tentative_source_id.id == SOURCE_A
    mutate = next(call[1] for call in transport.calls if call[0] == MUTATE_SOURCE_METHOD)
    assert mutate.source_id.id == SOURCE_A
    assert mutate.mutations[0].change_title.title == "Drive title"

    before_io = FakeTransport()
    with pytest.raises(ValidationError, match="cannot be empty"):
        await _api(before_io).add_drive(NOTEBOOK_ID, "  ", "Title")
    assert before_io.calls == []
    assert before_io.scopes == []


@pytest.mark.asyncio
async def test_check_freshness_maps_empty_success_to_true_and_explicit_false() -> None:
    transport = FakeTransport()
    transport.handlers[CHECK_SOURCE_FRESHNESS_METHOD] = deque(
        [
            sources_pb2.CheckSourceFreshnessResponse(),
            sources_pb2.CheckSourceFreshnessResponse(
                source_freshness=sources_pb2.SourceFreshness(
                    source_id=read_pb2.SourceId(id=SOURCE_A),
                    is_fresh=False,
                )
            ),
        ]
    )
    api = _api(transport)

    assert await api.check_freshness(NOTEBOOK_ID, SOURCE_A) is True
    assert await api.check_freshness(NOTEBOOK_ID, SOURCE_A) is False
    freshness_calls = [call for call in transport.calls if call[0] == CHECK_SOURCE_FRESHNESS_METHOD]
    assert len(freshness_calls) == 2
    for _, request, kwargs in freshness_calls:
        assert request.source_id.id == SOURCE_A
        assert request.HasField("request_context")
        assert kwargs == {
            "replay_safe": True,
            "response_type": sources_pb2.CheckSourceFreshnessResponse,
            "expected_epoch": 7,
        }


@pytest.mark.asyncio
async def test_check_freshness_rejects_a_different_nonempty_echoed_source_id() -> None:
    transport = FakeTransport()
    transport.handlers[CHECK_SOURCE_FRESHNESS_METHOD] = sources_pb2.CheckSourceFreshnessResponse(
        source_freshness=sources_pb2.SourceFreshness(
            source_id=read_pb2.SourceId(id=SOURCE_B),
            is_fresh=True,
        )
    )

    with pytest.raises(DecodingError, match="unexpected source id"):
        await _api(transport).check_freshness(NOTEBOOK_ID, SOURCE_A)


@pytest.mark.asyncio
async def test_refresh_uses_exact_native_request_and_public_none_contract() -> None:
    transport = FakeTransport()
    transport.handlers[CHECK_SOURCE_FRESHNESS_METHOD] = sources_pb2.CheckSourceFreshnessResponse(
        source_freshness=sources_pb2.SourceFreshness(
            source_id=read_pb2.SourceId(id=SOURCE_A),
            is_fresh=False,
        )
    )
    transport.handlers[REFRESH_SOURCE_METHOD] = sources_pb2.RefreshSourceResponse(
        source=read_pb2.Source(source_id=read_pb2.SourceId(id=SOURCE_A))
    )

    result = await _api(transport).refresh(NOTEBOOK_ID, SOURCE_A)

    assert result is None
    [(freshness_method, freshness_request, freshness_kwargs), (method, request, kwargs)] = (
        transport.calls[1:]
    )
    assert freshness_method == CHECK_SOURCE_FRESHNESS_METHOD
    assert freshness_request.source_id.id == SOURCE_A
    assert freshness_request.HasField("request_context")
    assert freshness_kwargs == {
        "replay_safe": True,
        "response_type": sources_pb2.CheckSourceFreshnessResponse,
        "expected_epoch": 7,
    }
    assert method == REFRESH_SOURCE_METHOD
    assert request.source_id.id == SOURCE_A
    assert request.HasField("request_context")
    assert request.request_context.client_type == 3
    assert kwargs == {
        "replay_safe": False,
        "response_type": sources_pb2.RefreshSourceResponse,
        "expected_epoch": 7,
    }
    assert transport.scopes == ["sources.refresh"]


@pytest.mark.asyncio
async def test_refresh_is_a_noop_when_source_is_already_fresh() -> None:
    transport = FakeTransport()
    transport.handlers[CHECK_SOURCE_FRESHNESS_METHOD] = sources_pb2.CheckSourceFreshnessResponse(
        source_freshness=sources_pb2.SourceFreshness(
            source_id=read_pb2.SourceId(id=SOURCE_A),
            is_fresh=True,
        )
    )

    result = await _api(transport).refresh(NOTEBOOK_ID, SOURCE_A)

    assert result is None
    [(method, request, kwargs)] = transport.calls[1:]
    assert method == CHECK_SOURCE_FRESHNESS_METHOD
    assert request.source_id.id == SOURCE_A
    assert kwargs == {
        "replay_safe": True,
        "response_type": sources_pb2.CheckSourceFreshnessResponse,
        "expected_epoch": 7,
    }
    assert transport.scopes == ["sources.refresh"]


@pytest.mark.asyncio
async def test_refresh_rejects_a_different_nonempty_echoed_source_id() -> None:
    transport = FakeTransport()
    transport.handlers[CHECK_SOURCE_FRESHNESS_METHOD] = sources_pb2.CheckSourceFreshnessResponse(
        source_freshness=sources_pb2.SourceFreshness(
            source_id=read_pb2.SourceId(id=SOURCE_A),
            is_fresh=False,
        )
    )
    transport.handlers[REFRESH_SOURCE_METHOD] = sources_pb2.RefreshSourceResponse(
        source=read_pb2.Source(source_id=read_pb2.SourceId(id=SOURCE_B))
    )

    with pytest.raises(DecodingError, match="unexpected source id"):
        await _api(transport).refresh(NOTEBOOK_ID, SOURCE_A)


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
    transport.handlers[MUTATE_SOURCE_METHOD] = sources_pb2.MutateSourceResponse(
        source=_source(SOURCE_A, title="Renamed")
    )
    api = _api(transport)

    await api.delete(NOTEBOOK_ID, SOURCE_A)
    renamed = await api.rename(NOTEBOOK_ID, SOURCE_A, "Renamed")

    delete_request = next(
        request for method, request, _ in transport.calls if method == DELETE_SOURCES_METHOD
    )
    mutate_request = next(
        request for method, request, _ in transport.calls if method == MUTATE_SOURCE_METHOD
    )
    assert [item.id for item in delete_request.source_ids] == [SOURCE_A]
    assert mutate_request.source_id.id == SOURCE_A
    assert mutate_request.mutations[0].change_title.title == "Renamed"
    assert mutate_request.HasField("request_context")
    mutation_calls = [
        call for call in transport.calls if call[0] in {DELETE_SOURCES_METHOD, MUTATE_SOURCE_METHOD}
    ]
    assert all(call[2]["replay_safe"] is False for call in mutation_calls)
    assert renamed is not None and renamed.title == "Renamed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "global_method"),
    [
        ("rename", MUTATE_SOURCE_METHOD),
        ("refresh", REFRESH_SOURCE_METHOD),
        ("get_fulltext", LOAD_SOURCE_METHOD),
    ],
)
async def test_global_source_operations_reject_cross_notebook_ids_before_io(
    operation: str,
    global_method: str,
) -> None:
    transport = FakeTransport()
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_B))
    api = _api(transport)

    with pytest.raises(SourceNotFoundError):
        if operation == "rename":
            await api.rename(NOTEBOOK_ID, SOURCE_A, "Renamed")
        elif operation == "refresh":
            await api.refresh(NOTEBOOK_ID, SOURCE_A)
        else:
            await api.get_fulltext(NOTEBOOK_ID, SOURCE_A)

    assert [method for method, _request, _kwargs in transport.calls] == [GET_PROJECT_METHOD]
    assert global_method not in [method for method, _request, _kwargs in transport.calls]


@pytest.mark.asyncio
async def test_delete_absent_or_cross_notebook_source_is_idempotent_without_global_io() -> None:
    transport = FakeTransport()
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_B))

    assert await _api(transport).delete(NOTEBOOK_ID, SOURCE_A) is None

    assert [method for method, _request, _kwargs in transport.calls] == [GET_PROJECT_METHOD]
    assert DELETE_SOURCES_METHOD not in [method for method, _request, _kwargs in transport.calls]


@pytest.mark.asyncio
async def test_null_rename_echo_hydrates_exact_id_and_detects_miss() -> None:
    transport = FakeTransport()
    transport.handlers[MUTATE_SOURCE_METHOD] = sources_pb2.MutateSourceResponse()
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_A, title="Hydrated"))
    result = await _api(transport).rename(NOTEBOOK_ID, SOURCE_A, "Requested")
    assert result is not None and result.title == "Hydrated"

    missing = FakeTransport()
    missing.handlers[MUTATE_SOURCE_METHOD] = sources_pb2.MutateSourceResponse()
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
        return sources_pb2.MutateSourceResponse()

    transport.handlers[MUTATE_SOURCE_METHOD] = _mutate
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_A, title="Hydrated"))
    task = asyncio.create_task(_api(cast(Any, transport)).rename(NOTEBOOK_ID, SOURCE_A, "Hydrated"))
    await mutation_started.wait()

    await transport.supervisor.stop_accepting(1)
    mutation_release.set()

    result = await task
    assert result is not None and result.title == "Hydrated"
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in transport.calls] == [
        1,
        1,
        1,
    ]
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_rename_readback_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    mutation_started = asyncio.Event()
    mutation_release = asyncio.Event()

    async def _mutate(_request: Any, _kwargs: dict[str, Any]) -> Any:
        mutation_started.set()
        await mutation_release.wait()
        return sources_pb2.MutateSourceResponse()

    transport.handlers[MUTATE_SOURCE_METHOD] = _mutate
    transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_A))
    task = asyncio.create_task(_api(cast(Any, transport)).rename(NOTEBOOK_ID, SOURCE_A, "Hydrated"))
    await mutation_started.wait()

    old_generation = await transport.force_close_and_reopen()
    mutation_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == [
        GET_PROJECT_METHOD,
        MUTATE_SOURCE_METHOD,
    ]
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
    transport.handlers[LOAD_SOURCE_METHOD] = source_content_pb2.WireLoadSourceResponse(
        source=_source(SOURCE_A, title="Document"),
        plain_text=sources_pb2.PlainTextSourceContent(header="ignored", body="plain body"),
        markdown_string="# markdown",
    )
    api = _api(transport)

    guide = await api.get_guide(NOTEBOOK_ID, SOURCE_A)
    fulltext = await api.get_fulltext(NOTEBOOK_ID, SOURCE_A)
    markdown = await api.get_fulltext(NOTEBOOK_ID, SOURCE_A, output_format="markdown")

    assert guide.summary == "Summary"
    assert guide.keywords == ("one", "two")
    assert fulltext.content == "plain body"
    assert fulltext.char_count == 10
    assert fulltext.document.blocks == ()
    assert markdown.content == "# markdown"
    guide_call = next(
        call for call in transport.calls if call[0] == GENERATE_DOCUMENT_GUIDES_METHOD
    )
    fulltext_call = next(call for call in transport.calls if call[0] == LOAD_SOURCE_METHOD)
    assert guide_call[2]["replay_safe"] is True
    assert fulltext_call[2]["replay_safe"] is True
    assert (
        fulltext_call[2]["response_type"].DESCRIPTOR.full_name
        == "notebooklm.internal.android.wire.v1.WireLoadSourceResponse"
    )


def _guide(
    *,
    source_id: str | None,
    summary: str = "Summary",
    ideas: tuple[str, ...] = ("one", "two"),
) -> sources_pb2.DocumentGuide:
    """Build a ``DocumentGuide``; ``source_id=None`` omits the optional echo."""

    guide = sources_pb2.DocumentGuide(
        snippet=sources_pb2.Snippet(text_snippet=summary),
        main_ideas=sources_pb2.MainIdeas(text_ideas=list(ideas)),
    )
    if source_id is not None:
        guide.source.CopyFrom(sources_pb2.InputSource(source_id=read_pb2.SourceId(id=source_id)))
    return guide


def _guides_transport(*guides: sources_pb2.DocumentGuide) -> FakeTransport:
    transport = FakeTransport()
    transport.handlers[GENERATE_DOCUMENT_GUIDES_METHOD] = (
        sources_pb2.GenerateDocumentGuidesResponse(guides=list(guides))
    )
    return transport


@pytest.mark.asyncio
async def test_guide_accepts_the_sole_unlabelled_guide() -> None:
    """Repeat guide reads omit ``DocumentGuide`` #1 entirely (issue #2276)."""
    transport = _guides_transport(_guide(source_id=None, summary="URL summary"))

    guide = await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert guide.summary == "URL summary"
    assert guide.keywords == ("one", "two")


@pytest.mark.asyncio
async def test_guide_accepts_an_unlabelled_guide_whose_echo_is_an_empty_source() -> None:
    """``source`` present but ``source_id`` absent is still *unlabelled*."""
    unlabelled = _guide(source_id=None, summary="Empty echo")
    unlabelled.source.CopyFrom(sources_pb2.InputSource())
    transport = _guides_transport(unlabelled)

    guide = await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert guide.summary == "Empty echo"


@pytest.mark.asyncio
async def test_guide_rejects_a_populated_mismatched_echo_with_diagnostics() -> None:
    transport = _guides_transport(_guide(source_id=SOURCE_B))

    with pytest.raises(DecodingError) as raised:
        await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert raised.value.method_id == GENERATE_DOCUMENT_GUIDES_METHOD
    assert raised.value.found_ids == [SOURCE_B]
    # The counts and ids live in the message, which a CI traceback prints in
    # full; ``raw_response`` is truncated to 80 characters unless NOTEBOOKLM_DEBUG=1.
    assert f"requested={SOURCE_A}" in str(raised.value)
    assert "guides=1" in str(raised.value)
    assert f"observed=[{SOURCE_B}]" in str(raised.value)
    # Field tags, never wire bytes: guide #1 present, #2 snippet present.
    assert raised.value.raw_response == "[1,2,3]"


@pytest.mark.asyncio
async def test_guide_rejects_multiple_guides_without_an_exact_match() -> None:
    """Past one guide the response is ambiguous, so an unlabelled row is fatal."""
    transport = _guides_transport(_guide(source_id=None), _guide(source_id=SOURCE_B))

    with pytest.raises(DecodingError) as raised:
        await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert raised.value.found_ids == ["<unlabelled>", SOURCE_B]
    assert "guides=2" in str(raised.value)
    assert f"observed=[<unlabelled>, {SOURCE_B}]" in str(raised.value)
    # The unlabelled guide is reported as missing tag 1, not as bytes.
    assert raised.value.raw_response == "[2,3 | 1,2,3]"


@pytest.mark.asyncio
async def test_guide_prefers_the_exact_match_beside_an_unlabelled_sibling() -> None:
    """Where the relaxed rule and the multi-guide rule meet, the label wins."""
    transport = _guides_transport(
        _guide(source_id=None, summary="unlabelled"),
        _guide(source_id=SOURCE_A, summary="labelled"),
    )

    guide = await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert guide.summary == "labelled"


@pytest.mark.asyncio
async def test_guide_accepts_an_echo_populated_with_an_empty_id() -> None:
    """``SourceId(id="")`` is the third unlabelled shape and reads the same."""
    empty_id = _guide(source_id="", summary="Empty id")
    transport = _guides_transport(empty_id)

    guide = await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert guide.summary == "Empty id"


def test_select_document_guide_rejects_an_empty_response() -> None:
    """The zero-guide case is the caller's to map; the codec refuses it."""
    with pytest.raises(ValueError, match="non-empty guides list"):
        select_document_guide(
            sources_pb2.GenerateDocumentGuidesResponse(),
            source_id=SOURCE_A,
            method_id=GENERATE_DOCUMENT_GUIDES_METHOD,
        )


@pytest.mark.asyncio
async def test_guide_rejects_duplicate_matching_echoes() -> None:
    transport = _guides_transport(_guide(source_id=SOURCE_A), _guide(source_id=SOURCE_A))

    with pytest.raises(DecodingError) as raised:
        await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert "duplicate source ids" in str(raised.value)
    assert raised.value.found_ids == [SOURCE_A, SOURCE_A]


@pytest.mark.asyncio
async def test_guide_failure_diagnostics_never_carry_guide_content() -> None:
    """A rejected guide must not splice source-derived text into the error.

    ``raw_response`` is spliced into ``str()``/``repr()`` of RPC errors and
    ``NOTEBOOKLM_DEBUG=1`` opts out of its truncation, so a wire-byte preview
    of any length could disclose a model-written summary of the user's source
    -- an unlabelled guide's payload starts with ``#2 snippet``. Only field
    tags are reported.
    """
    secret_summary = "PRIVATE SUMMARY OF THE USER SOURCE " * 5
    transport = _guides_transport(
        _guide(source_id=None, summary=secret_summary, ideas=("private idea",)),
        _guide(source_id=SOURCE_B, summary="other"),
    )

    with pytest.raises(DecodingError) as raised:
        await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    rendered = f"{raised.value!s} {raised.value!r} {raised.value.raw_response}"
    assert "PRIVATE" not in rendered
    assert "private idea" not in rendered
    assert raised.value.raw_response == "[2,3 | 1,2,3]"


@pytest.mark.asyncio
async def test_guide_field_tags_report_fields_the_schema_does_not_model() -> None:
    """An unmodelled tag is how a *moved* label would look, so it must show."""
    guide = _guide(source_id=None)
    # Tag 7 is absent from ``DocumentGuide``; protobuf keeps it as unknown.
    guide.MergeFromString(guide.SerializeToString() + b"\x3a\x00")
    transport = _guides_transport(guide, _guide(source_id=SOURCE_B))

    with pytest.raises(DecodingError) as raised:
        await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert raised.value.raw_response == "[2,3,7 | 1,2,3]"


@pytest.mark.asyncio
async def test_guide_maps_an_empty_response_to_an_empty_guide() -> None:
    """ADR-0019 derived read: absence is data, not an error (issue #2278).

    Live-verified parity: the web backend returns ``SourceGuide("", ())`` for a
    nonexistent source id, and Android now does the same instead of raising
    ``SourceNotFoundError``.
    """
    transport = _guides_transport()

    guide = await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert guide == SourceGuide(summary="", keywords=())


@pytest.mark.asyncio
async def test_guide_maps_backend_not_found_to_an_empty_guide() -> None:
    """rpc code 5 is "no guide for this id", not "the source is gone".

    A live probe of a nonexistent id returns NOT_FOUND from
    ``GenerateDocumentGuides`` while web returns an empty guide for the same
    input, so this is the branch that carries the parity.
    """
    transport = FakeTransport()
    transport.handlers[GENERATE_DOCUMENT_GUIDES_METHOD] = RPCError("gone", rpc_code=5)

    guide = await _api(transport).get_guide(NOTEBOOK_ID, SOURCE_A)

    assert guide == SourceGuide(summary="", keywords=())


@pytest.mark.asyncio
async def test_derived_source_reads_do_not_pre_flight_ownership() -> None:
    """The two ADR-0019 derived reads issue exactly one RPC and no GetProject.

    Dropping the pre-flight is what makes absence return data rather than
    raise; it also removes a ``GetProject`` round-trip from every call. Both
    Android RPCs are notebook-agnostic, and so is web -- a live probe of both
    backends returned a source's guide when asked under an unrelated notebook
    id -- so the guard was a client-side-only divergence, not a boundary the
    protocol enforces.
    """
    guide_transport = _guides_transport(_guide(source_id=SOURCE_A))
    guide_transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_B))
    await _api(guide_transport).get_guide(NOTEBOOK_ID, SOURCE_A)
    assert [method for method, _r, _k in guide_transport.calls] == [GENERATE_DOCUMENT_GUIDES_METHOD]

    fresh_transport = FakeTransport()
    fresh_transport.handlers[GET_PROJECT_METHOD] = _project(_source(SOURCE_B))
    fresh_transport.handlers[CHECK_SOURCE_FRESHNESS_METHOD] = (
        sources_pb2.CheckSourceFreshnessResponse()
    )
    assert await _api(fresh_transport).check_freshness(NOTEBOOK_ID, SOURCE_A) is True
    assert [method for method, _r, _k in fresh_transport.calls] == [CHECK_SOURCE_FRESHNESS_METHOD]


@pytest.mark.asyncio
async def test_fulltext_reports_an_unlabelled_source_as_drift_not_absence() -> None:
    """An absent ``LoadSource`` echo must not masquerade as a missing source.

    The server did return a source; what it withheld is the label. Deferring to
    ``decode_source`` names that precisely instead of claiming the source does
    not exist (issue #2276, "latent twin").
    """
    unlabelled = FakeTransport()
    source = _source(SOURCE_A, title="Document")
    source.ClearField("source_id")
    unlabelled.handlers[LOAD_SOURCE_METHOD] = source_content_pb2.WireLoadSourceResponse(
        source=source,
        plain_text=sources_pb2.PlainTextSourceContent(body="plain body"),
    )

    # Catch broadly and pin the exact type: ``SourceNotFoundError`` is not a
    # ``DecodingError`` subclass, so ``pytest.raises(DecodingError)`` alone
    # would let the pre-fix behaviour escape the test rather than fail it.
    with pytest.raises(Exception) as unlabelled_raised:  # noqa: B017, PT011
        await _api(unlabelled).get_fulltext(NOTEBOOK_ID, SOURCE_A)

    assert type(unlabelled_raised.value) is DecodingError
    assert "did not contain a source id" in str(unlabelled_raised.value)


@pytest.mark.asyncio
async def test_fulltext_rejects_a_populated_mismatched_echo_with_diagnostics() -> None:
    mismatched = FakeTransport()
    mismatched.handlers[LOAD_SOURCE_METHOD] = source_content_pb2.WireLoadSourceResponse(
        source=_source(SOURCE_B, title="Other"),
        plain_text=sources_pb2.PlainTextSourceContent(body="plain body"),
    )

    with pytest.raises(DecodingError) as raised:
        await _api(mismatched).get_fulltext(NOTEBOOK_ID, SOURCE_A)

    assert raised.value.method_id == LOAD_SOURCE_METHOD
    assert raised.value.found_ids == [SOURCE_B]
    assert f"requested={SOURCE_A}, observed={SOURCE_B}" in str(raised.value)


@pytest.mark.asyncio
async def test_fulltext_decodes_current_tailwind_doc_for_text_and_markdown() -> None:
    transport = FakeTransport()
    document = chat_pb2.TailwindDoc(
        body=chat_pb2.Body(
            content=[
                chat_pb2.StructuralElement(
                    start_index=0,
                    end_index=10,
                    paragraph=chat_pb2.Paragraph(
                        elements=[
                            chat_pb2.ParagraphElement(
                                start_index=0,
                                end_index=6,
                                text_run=chat_pb2.TextRun(content="First "),
                            ),
                            chat_pb2.ParagraphElement(
                                start_index=6,
                                end_index=10,
                                text_run=chat_pb2.TextRun(content="line"),
                            ),
                        ],
                        paragraph_style=chat_pb2.ParagraphStyle(
                            named_style_type=chat_pb2.HEADING_1
                        ),
                    ),
                )
            ]
        )
    )
    transport.handlers[LOAD_SOURCE_METHOD] = source_content_pb2.WireLoadSourceResponse(
        source=_source(SOURCE_A, title="Document"),
        tailwind_doc=document,
    )
    api = _api(transport)

    fulltext = await api.get_fulltext(NOTEBOOK_ID, SOURCE_A)
    markdown = await api.get_fulltext(NOTEBOOK_ID, SOURCE_A, output_format="markdown")

    assert fulltext.content == "First \nline"
    assert fulltext.char_count == 11
    assert fulltext.document.text == "First line"
    assert fulltext.rendered_content == "First line"
    assert markdown.content == "# First line"
    assert markdown.document == fulltext.document


@pytest.mark.asyncio
async def test_fulltext_decodes_table_and_code_only_tailwind_doc() -> None:
    transport = FakeTransport()

    def cell(start: int, end: int, text: str) -> Any:
        return chat_pb2.TableCell(
            start_index=start,
            end_index=end,
            content=[
                chat_pb2.StructuralElement(
                    start_index=start,
                    end_index=end,
                    paragraph=chat_pb2.Paragraph(
                        elements=[
                            chat_pb2.ParagraphElement(
                                start_index=start,
                                end_index=end,
                                text_run=chat_pb2.TextRun(content=text),
                            )
                        ]
                    ),
                )
            ],
        )

    document = chat_pb2.TailwindDoc(
        body=chat_pb2.Body(
            content=[
                chat_pb2.StructuralElement(
                    start_index=0,
                    end_index=9,
                    table=chat_pb2.Table(
                        rows=1,
                        columns=2,
                        table_rows=[
                            chat_pb2.TableRow(
                                start_index=0,
                                end_index=9,
                                table_cells=[cell(0, 4, "Head"), cell(4, 9, "Value")],
                            )
                        ],
                    ),
                ),
                chat_pb2.StructuralElement(
                    start_index=9,
                    end_index=17,
                    code_block=chat_pb2.CodeBlock(content="print(1)", language_hint="python"),
                ),
            ]
        )
    )
    transport.handlers[LOAD_SOURCE_METHOD] = source_content_pb2.WireLoadSourceResponse(
        source=_source(SOURCE_A, title="Structured"),
        tailwind_doc=document,
    )
    api = _api(transport)

    fulltext = await api.get_fulltext(NOTEBOOK_ID, SOURCE_A)
    markdown = await api.get_fulltext(NOTEBOOK_ID, SOURCE_A, output_format="markdown")

    assert fulltext.content == "Head\nValue\nprint(1)"
    assert fulltext.rendered_content == "Head\tValue"
    assert [block.kind.value for block in fulltext.document.blocks] == ["table", "code_block"]
    assert markdown.content == ("| Head | Value |\n| --- | --- |\n\n```python\nprint(1)\n```")


def test_plain_renderer_covers_all_text_bearing_variants_and_omits_metadata() -> None:
    nested_text = chat_pb2.StructuralElement(
        paragraph=chat_pb2.Paragraph(
            elements=[
                chat_pb2.ParagraphElement(text_run=chat_pb2.TextRun(content="nested")),
                chat_pb2.ParagraphElement(
                    image=chat_pb2.Image(url="https://example.invalid/inline")
                ),
                chat_pb2.ParagraphElement(resource=chat_pb2.Resource(id="resource-id")),
            ]
        )
    )
    document = chat_pb2.TailwindDoc(
        body=chat_pb2.Body(
            content=[
                nested_text,
                chat_pb2.StructuralElement(
                    table=chat_pb2.Table(
                        table_rows=[
                            chat_pb2.TableRow(
                                table_cells=[chat_pb2.TableCell(content=[nested_text])]
                            )
                        ]
                    )
                ),
                chat_pb2.StructuralElement(code_block=chat_pb2.CodeBlock(content="code")),
                chat_pb2.StructuralElement(a2ui_block=chat_pb2.A2uiBlock(json='{"ok":true}')),
                chat_pb2.StructuralElement(thought=chat_pb2.Thought(elements=[nested_text])),
                chat_pb2.StructuralElement(
                    start_index=1,
                    end_index=2,
                    function_call=agency_pb2.FunctionCall(
                        name="lookup",
                        args=agency_pb2.TailwindStruct(
                            fields=[
                                agency_pb2.TailwindStructEntry(
                                    key="query",
                                    value=agency_pb2.TailwindValue(string_value="term"),
                                )
                            ]
                        ),
                    ),
                ),
                chat_pb2.StructuralElement(
                    start_index=2,
                    end_index=3,
                    function_response=agency_pb2.FunctionResponse(name="lookup"),
                ),
                chat_pb2.StructuralElement(
                    image=chat_pb2.Image(url="https://example.invalid/block")
                ),
                chat_pb2.StructuralElement(horizontal_rule=chat_pb2.HorizontalRule()),
            ]
        )
    )

    assert tailwind_doc_plain_text(document) == 'nested\nnested\ncode\n{"ok":true}\nnested'
    assert [block.kind.value for block in decode_document(document).blocks[-2:]] == [
        "function_call",
        "function_response",
    ]


@pytest.mark.asyncio
async def test_cancellation_between_registration_and_commit_dispatches_no_later_stage() -> None:
    transport = FakeTransport()
    transport.handlers[ADD_TENTATIVE_SOURCES_METHOD] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _api(transport).add_url(NOTEBOOK_ID, URL_A)
    assert [call[0] for call in transport.calls] == [ADD_TENTATIVE_SOURCES_METHOD]


def _ei_item(
    content_id: str,
    *,
    title: str = "The Art of War",
    export_disabled: bool = False,
    export_reason: int = 0,
    field_type: float = 4.5,
    updated_timestamp: Timestamp | None = None,
) -> Any:
    item = sources_pb2.ExpertIntelligenceContentItem(
        content_id=content_id,
        provider=1,
        title=title,
        description="<p>desc</p>",
        thumbnail_image_url=f"https://books/{content_id}",
        export_disabled=export_disabled,
        export_reason=export_reason,
        authors=["Sun Tzu"],
        field_type=field_type,
    )
    if updated_timestamp is not None:
        item.updated_timestamp.CopyFrom(updated_timestamp)
    return item


def _ei_response(*items: Any) -> Any:
    return sources_pb2.ListExpertIntelligenceContentResponse(items=list(items))


class TestPlayBooksAndroid:
    """Play Books (Expert Intelligence) on the Android backend (#2302)."""

    @pytest.mark.asyncio
    async def test_list_play_books_decodes_items(self) -> None:
        transport = FakeTransport()
        transport.handlers[LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD] = _ei_response(
            _ei_item("QhsZEAAAQBAJ", updated_timestamp=Timestamp(seconds=1_700_000_000)),
            _ei_item("BAD", export_disabled=True, export_reason=1, field_type=0.0),
        )
        books = await _api(transport).list_play_books()
        assert [b.content_id for b in books] == ["QhsZEAAAQBAJ", "BAD"]
        first = books[0]
        assert first.title == "The Art of War"
        assert first.authors == ("Sun Tzu",)
        assert first.export_disabled is False
        assert first.reason is None
        assert first.field_type == pytest.approx(4.5)
        assert first.updated_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
        assert books[1].export_disabled is True
        assert books[1].reason is PlayBookExportReason.OPTED_OUT

    @pytest.mark.asyncio
    async def test_list_play_books_sends_source_class(self) -> None:
        transport = FakeTransport()
        transport.handlers[LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD] = _ei_response()
        await _api(transport).list_play_books()
        method, request, kwargs = transport.calls[0]
        assert method == LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD
        assert request.source_class == 1
        assert request.HasField("request_context")
        assert kwargs["replay_safe"] is True

    @pytest.mark.asyncio
    async def test_add_play_book_unknown_content_id_raises(self) -> None:
        transport = FakeTransport()
        transport.handlers[LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD] = _ei_response(
            _ei_item("QhsZEAAAQBAJ")
        )
        with pytest.raises(SourceNotFoundError):
            await _api(transport).add_play_book(NOTEBOOK_ID, "MISSING")
        assert all(m != ADD_SOURCES_METHOD for m, _, _ in transport.calls)

    @pytest.mark.asyncio
    async def test_add_play_book_refuses_non_exportable(self) -> None:
        transport = FakeTransport()
        transport.handlers[LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD] = _ei_response(
            _ei_item("BAD", export_disabled=True, export_reason=1)
        )
        with pytest.raises(PlayBookNotExportableError):
            await _api(transport).add_play_book(NOTEBOOK_ID, "BAD")
        assert all(
            m not in (ADD_TENTATIVE_SOURCES_METHOD, ADD_SOURCES_METHOD)
            for m, _, _ in transport.calls
        )

    @pytest.mark.asyncio
    async def test_add_play_book_commits_expert_intelligence_content(self) -> None:
        transport = _successful_transport()
        phenotype = FakePhenotype()
        transport.handlers[LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD] = _ei_response(
            _ei_item("QhsZEAAAQBAJ")
        )
        await _api(transport, phenotype=phenotype).add_play_book(
            NOTEBOOK_ID,
            "QhsZEAAAQBAJ",
        )
        add = next((req, kw) for method, req, kw in transport.calls if method == ADD_SOURCES_METHOD)
        request, kwargs = add
        content = request.user_content[0].expert_intelligence_content
        assert content.content_id == "QhsZEAAAQBAJ"
        assert content.provider == 1
        assert content.authors == ["Sun Tzu"]
        assert content.field_type == pytest.approx(4.5)
        assert transport.timeline.index("prepare_metadata") < transport.timeline.index(
            ADD_TENTATIVE_SOURCES_METHOD
        )
        assert phenotype.calls == [("fake-bearer", False)]
        # The already-fetched metadata rides the commit without another POST.
        assert callable(kwargs["metadata_augmentor"])
        assert await kwargs["metadata_augmentor"]("new-bearer") == (
            ("x-phenotype-bin", b"metadata"),
        )

    @pytest.mark.asyncio
    async def test_metadata_failure_precedes_tentative_registration(self) -> None:
        transport = FakeTransport()
        phenotype = FakePhenotype(PhenotypeError("fetch failed"))
        transport.handlers[LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD] = _ei_response(
            _ei_item("QhsZEAAAQBAJ")
        )

        with pytest.raises(PhenotypeError, match="fetch failed"):
            await _api(transport, phenotype=phenotype).add_play_book(
                NOTEBOOK_ID,
                "QhsZEAAAQBAJ",
            )

        assert [method for method, _, _ in transport.calls] == [
            LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD
        ]
        assert transport.timeline == [
            LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD,
            "prepare_metadata",
        ]

    @pytest.mark.asyncio
    async def test_internal_refusal_refreshes_after_tentative_readback(self) -> None:
        transport = _successful_transport()
        phenotype = FakePhenotype()
        transport.handlers[LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD] = _ei_response(
            _ei_item("QhsZEAAAQBAJ")
        )
        transport.handlers[ADD_SOURCES_METHOD] = deque(
            [
                ServerError("stale token", method_id=ADD_SOURCES_METHOD, rpc_code=13),
                sources_pb2.AddSourcesResponse(
                    sources=[_source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_PENDING)]
                ),
            ]
        )
        transport.handlers[GET_PROJECT_METHOD] = deque(
            [
                _project(_source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_TENTATIVE)),
                _project(_source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_COMPLETE)),
            ]
        )

        source = await _api(transport, phenotype=phenotype).add_play_book(
            NOTEBOOK_ID,
            "QhsZEAAAQBAJ",
        )

        assert source.id == SOURCE_A
        assert phenotype.calls == [("fake-bearer", False), ("fake-bearer", True)]
        methods = [method for method, _, _ in transport.calls]
        assert methods.count(ADD_TENTATIVE_SOURCES_METHOD) == 1
        assert methods.count(ADD_SOURCES_METHOD) == 2
        assert methods.count(GET_PROJECT_METHOD) == 2

    @pytest.mark.asyncio
    async def test_internal_refusal_does_not_retry_after_commit_proof(self) -> None:
        transport = _successful_transport()
        phenotype = FakePhenotype()
        transport.handlers[LIST_EXPERT_INTELLIGENCE_CONTENT_METHOD] = _ei_response(
            _ei_item("QhsZEAAAQBAJ")
        )
        transport.handlers[ADD_SOURCES_METHOD] = ServerError(
            "ambiguous internal",
            method_id=ADD_SOURCES_METHOD,
            rpc_code=13,
        )
        transport.handlers[GET_PROJECT_METHOD] = _project(
            _source(SOURCE_A, status=source_settings_pb2.SOURCE_STATUS_PENDING)
        )

        source = await _api(transport, phenotype=phenotype).add_play_book(
            NOTEBOOK_ID,
            "QhsZEAAAQBAJ",
        )

        assert source.id == SOURCE_A
        assert phenotype.calls == [("fake-bearer", False)]
        assert [method for method, _, _ in transport.calls].count(ADD_SOURCES_METHOD) == 1
