"""Real Web/Android producer and adapter agreement for P2 commit evidence."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from notebooklm._android.auth import BearerCredential
from notebooklm._android.chat import GENERATE_FREE_FORM_STREAMED_METHOD
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.v1 import source_settings_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._android.source_transfers import (
    ADD_SOURCES_METHOD,
    ADD_TENTATIVE_SOURCES_METHOD,
)
from notebooklm._android.sources import GET_PROJECT_METHOD, AndroidSourcesAPI
from notebooklm._android.upload import AndroidUploadPipeline
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._client_metrics import ClientMetrics
from notebooklm._idempotency import (
    OperationJournal,
    SendIdentity,
    bind_operation_journal_entries,
)
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._web.sources.batch import SourceBatchAddService
from notebooklm.auth import AuthTokens
from notebooklm.cli.error_handler import handle_errors
from notebooklm.exceptions import AuthError, NetworkError
from notebooklm.mcp._errors import to_tool_error, tool_error_payload
from notebooklm.outcomes import CommitState, RecoveryAction
from notebooklm.rpc import RPCMethod
from notebooklm.server._errors import error_response
from tests._fixtures.rpc_error_frames import raw_batchexecute_body
from tests._helpers.client_factory import build_client_shell_for_tests

_URLS = ("https://a.example.test", "https://b.example.test")
_SOURCE_IDS = (
    "00000000-0000-4000-8000-000000000101",
    "00000000-0000-4000-8000-000000000102",
)


@dataclass(frozen=True)
class _Expected:
    state: CommitState
    recovery: RecoveryAction
    category: ErrorCategory
    retriable: bool
    unconfirmed: bool
    cli_code: str
    mcp_code: str
    rest_category: str


_UNKNOWN = _Expected(
    CommitState.UNKNOWN,
    RecoveryAction.INSPECT_AND_RECONCILE,
    ErrorCategory.RPC,
    False,
    True,
    "UNCONFIRMED_WRITE",
    "RPC",
    "rpc",
)
_REJECTED = _Expected(
    CommitState.REJECTED,
    RecoveryAction.NONE,
    ErrorCategory.SOURCE_ADD,
    False,
    False,
    "NOTEBOOKLM_ERROR",
    "SOURCE_ADD",
    "source_add",
)
_NOT_SENT = _Expected(
    CommitState.NOT_SENT,
    RecoveryAction.RETRY,
    ErrorCategory.NETWORK,
    True,
    False,
    "NETWORK_ERROR",
    "NETWORK",
    "network",
)
_NOT_SENT_SOURCE = _Expected(
    CommitState.NOT_SENT,
    RecoveryAction.RETRY,
    ErrorCategory.SOURCE_ADD,
    False,
    False,
    "NOTEBOOKLM_ERROR",
    "SOURCE_ADD",
    "source_add",
)


def _web_response(payload: Any) -> str:
    return raw_batchexecute_body(
        [["wrb.fr", RPCMethod.ADD_SOURCE.value, json.dumps(payload), None, None]]
    )


def _web_client(case: str, requests: list[httpx.Request]):
    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if case == "pre_dispatch":
            raise httpx.ConnectError("connect failed", request=request)
        if case == "decoder_failure_after_acceptance":
            return httpx.Response(200, text="malformed accepted response", request=request)
        if case == "lost_response":
            raise httpx.ReadError("response lost", request=request)
        if case == "transport_read_timeout":
            # P2 has a per-request HTTP read deadline, not the P6 workflow
            # deadline. Exercise the real Kernel -> RuntimeTransport mapping
            # for the transport reporting that deadline as expired.
            raise httpx.ReadTimeout("read deadline expired", request=request)
        if case == "rejected":
            body = raw_batchexecute_body(
                [["wrb.fr", RPCMethod.ADD_SOURCE.value, None, None, None, [9], "generic"]]
            )
            return httpx.Response(200, text=body, request=request)
        rows = [
            [["source-a"], _URLS[0], [None, None, None, None, 5, None, None, [_URLS[0]]]],
            [["source-b"], _URLS[1], [None, None, None, None, 5, None, None, [_URLS[1]]]],
        ]
        return httpx.Response(200, text=_web_response(rows), request=request)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return build_client_shell_for_tests(
        AuthTokens(
            csrf_token="csrf",
            session_id="session",
            cookies={"SID": "sid"},
        ),
        rate_limit_max_retries=0,
        server_error_max_retries=0,
        async_client_factory=factory,
    )


class _Status(Enum):
    UNKNOWN = (2, "unknown")
    UNAUTHENTICATED = (16, "unauthenticated")


class _RawRpcError(Exception):
    def __init__(self, status: _Status = _Status.UNKNOWN) -> None:
        self._status = status

    def code(self) -> _Status:
        return self._status


def _android_source(
    source_id: str,
    *,
    title: str,
    url: str,
) -> read_pb2.Source:
    return read_pb2.Source(
        source_id=read_pb2.SourceId(id=source_id),
        title=title,
        metadata=read_pb2.SourceMetadata(
            original_source_content_type=read_pb2.SOURCE_CONTENT_TYPE_URL,
            webpage_metadata=read_pb2.WebpageMetadata(url=url),
        ),
        settings=source_settings_pb2.SourceSettings(
            status=source_settings_pb2.SOURCE_STATUS_PENDING
        ),
    )


class _Bearer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def activate_for_epoch(self, epoch: int) -> None:
        assert epoch == 1

    async def get(self, expected_epoch: int) -> BearerCredential:
        assert expected_epoch == 1
        if self.fail:
            raise NetworkError("credential acquisition failed")
        return BearerCredential("bearer", 1)

    def invalidate(self, generation: int) -> None:
        del generation

    async def prepare_close(self) -> None:
        return None


class _Channel:
    def __init__(self, case: str) -> None:
        self.case = case
        self.invocations = 0
        self.methods: list[str] = []
        self.readback_cancellation: asyncio.CancelledError | None = None

    def unary_unary(self, method: str, *, request_serializer: Any, response_deserializer: Any):
        async def invoke(request: Any, *, metadata: Any, timeout: float | None) -> Any:
            del metadata
            self.invocations += 1
            self.methods.append(method)
            request_serializer(request)
            if self.case == "lost_response":
                raise _RawRpcError()
            if self.case == "whole_request_rejected":
                raise _RawRpcError(_Status.UNAUTHENTICATED)
            if self.case == "decoder_failure_after_acceptance":
                return response_deserializer(b"\x80")
            if self.case == "transport_deadline":
                assert timeout is not None and timeout > 0
                await asyncio.Event().wait()
                raise AssertionError("the AndroidSession deadline must cancel the wire await")
            if self.case == "readback_cancellation" and method == GET_PROJECT_METHOD:
                assert self.readback_cancellation is not None
                raise self.readback_cancellation
            if method == ADD_TENTATIVE_SOURCES_METHOD:
                response = sources_pb2.AddTentativeSourcesResponse()
                if self.case != "rejected":
                    response.tentative_sources.extend(
                        _android_source(
                            source_id,
                            title=registration.name,
                            url=url,
                        )
                        for registration, source_id, url in zip(
                            request.tentative_sources_metadata,
                            _SOURCE_IDS,
                            _URLS,
                            strict=True,
                        )
                    )
            elif method == ADD_SOURCES_METHOD:
                response = sources_pb2.AddSourcesResponse(
                    sources=[
                        _android_source(
                            item.tentative_source_id.id,
                            title=f"Source {index}",
                            url=(
                                item.web_content.url
                                if item.HasField("web_content")
                                else item.video_content.youtube_url
                            ),
                        )
                        for index, item in enumerate(request.user_content)
                    ]
                )
            elif method == GET_PROJECT_METHOD:
                response = read_pb2.GetProjectResponse(
                    project=read_pb2.Project(
                        id=request.project_id,
                        title="Notebook",
                        sources=[
                            _android_source(
                                source_id,
                                title=f"Source {index}",
                                url=url,
                            )
                            for index, (source_id, url) in enumerate(
                                zip(_SOURCE_IDS, _URLS, strict=True)
                            )
                        ],
                    )
                )
            else:  # pragma: no cover - matrix fixture contract
                raise AssertionError(f"unexpected Android method {method}")
            return response_deserializer(response.SerializeToString())

        return invoke

    def unary_stream(self, method: str, *, request_serializer: Any, response_deserializer: Any):
        del method, response_deserializer

        def invoke(request: Any, *, metadata: Any, timeout: float | None) -> _BlockingStream:
            del metadata, timeout
            self.invocations += 1
            request_serializer(request)
            return _BlockingStream()

        return invoke

    async def close(self) -> None:
        return None


class _Grpc:
    def __init__(self, channel: _Channel) -> None:
        self.aio = SimpleNamespace(secure_channel=lambda *_args, **_kwargs: channel)

    def ssl_channel_credentials(self) -> object:
        return object()


class _BlockingStream:
    entered = asyncio.Event()

    def __aiter__(self) -> _BlockingStream:
        return self

    async def __anext__(self) -> Any:
        self.entered.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    def cancel(self) -> None:
        return None


async def _android_session(case: str) -> tuple[AndroidSession, _Channel]:
    channel = _Channel(case)
    supervisor = CallSupervisor(metrics=ClientMetrics(), max_concurrent_rpcs=None)
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    session = AndroidSession(
        _Bearer(fail=case == "pre_dispatch"),
        supervisor,
        # Android already owns a real aggregate *per-RPC* deadline. P6 will add
        # the wider workflow deadline; do not synthesize that later contract.
        timeout=0.01 if case == "transport_deadline" else 1.0,
        rate_limit_max_retries=0,
        server_error_max_retries=0,
        grpc_loader=lambda: _Grpc(channel),
    )
    session.set_bound_loop(loop)
    session.reset_after_open()
    await session.open(loop, 1)
    return session, channel


async def _produce(backend: str, case: str) -> tuple[BaseException, _Expected]:
    if backend == "web":
        requests: list[httpx.Request] = []
        client = _web_client(case, requests)

        async def no_error_rows(*args: Any, **kwargs: Any) -> list[Any]:
            del args, kwargs
            return []

        async with client:
            try:
                outcomes = await SourceBatchAddService().add_urls(
                    "notebook-1",
                    _URLS,
                    rpc=client._web_runtime.executor,
                    list_sources=no_error_rows,
                    extract_youtube_video_id=lambda _url: None,
                    logger=logging.getLogger(__name__),
                )
            except BaseException as error:
                assert len(requests) == 1
                return error, _NOT_SENT if case == "pre_dispatch" else _UNKNOWN
    else:
        session, channel = await _android_session(case)
        try:
            api = AndroidSourcesAPI(session, object.__new__(AndroidUploadPipeline))
            try:
                outcomes = await api._add_urls_batch("notebook-1", list(_URLS))
            except BaseException as error:
                assert channel.invocations == 1
                return error, _UNKNOWN
        finally:
            await session.close_resources()
        assert channel.invocations == (0 if case == "pre_dispatch" else 1)

    error = outcomes[0].error
    assert error is not None
    expected = (
        _REJECTED
        if case == "rejected"
        else _NOT_SENT_SOURCE
        if case == "pre_dispatch"
        else _UNKNOWN
    )
    return error, expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "case"),
    [
        ("web", "rejected"),
        ("android", "rejected"),
        ("web", "decoder_failure_after_acceptance"),
        ("android", "decoder_failure_after_acceptance"),
        ("web", "pre_dispatch"),
        ("android", "pre_dispatch"),
        ("web", "lost_response"),
        ("android", "lost_response"),
        ("web", "transport_read_timeout"),
        ("android", "transport_deadline"),
    ],
)
async def test_commit_evidence_producer_consumer_matrix(
    backend: str,
    case: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error, expected = await _produce(backend, case)

    metadata = getattr(error, "operation_metadata", None)
    assert metadata is not None
    assert metadata.commit_state is expected.state
    assert metadata.recovery_action is expected.recovery
    assert getattr(error, "unconfirmed", False) is expected.unconfirmed
    classified = classify(error)
    assert (classified.category, classified.retriable) == (
        expected.category,
        expected.retriable,
    )

    batch = metadata.batch_outcome
    assert batch is not None
    assert [item.member for item in batch.items] == [0, 1]
    assert [item.commit_state for item in batch.items] == [expected.state, expected.state]
    if expected.state is CommitState.UNKNOWN:
        assert all(item.reconciliation is not None for item in batch.items)

    with pytest.raises(SystemExit) as cli_exit, handle_errors(json_output=True):
        raise error
    cli_payload = json.loads(capsys.readouterr().out)
    assert cli_exit.value.code == 1
    assert cli_payload["code"] == expected.cli_code
    assert cli_payload["commit_state"] == expected.state.value
    assert cli_payload["batch_outcome"]["items"][1]["member"] == 1

    mcp_payload = tool_error_payload(error)
    assert mcp_payload["code"] == expected.mcp_code
    assert mcp_payload["retriable"] is expected.retriable
    assert mcp_payload["batch_outcome"] == cli_payload["batch_outcome"]
    mcp_wire = str(to_tool_error(error))
    assert mcp_wire.startswith(f"{expected.mcp_code}:")
    assert "batch_outcome" in mcp_wire

    rest_payload = json.loads(error_response(error).body)["error"]
    assert rest_payload["category"] == expected.rest_category
    assert rest_payload["retriable"] is expected.retriable
    assert rest_payload["batch_outcome"] == cli_payload["batch_outcome"]


@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.asyncio
async def test_real_transport_and_decoder_confirm_batch_success(backend: str) -> None:
    if backend == "web":
        requests: list[httpx.Request] = []
        client = _web_client("success", requests)
        async with client:
            outcomes = await SourceBatchAddService().add_urls(
                "notebook-1",
                _URLS,
                rpc=client._web_runtime.executor,
                list_sources=lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
                extract_youtube_video_id=lambda _url: None,
                logger=logging.getLogger(__name__),
            )
        assert len(requests) == 1
    else:
        session, channel = await _android_session("success")
        try:
            api = AndroidSourcesAPI(session, object.__new__(AndroidUploadPipeline))
            outcomes = await api._add_urls_batch("notebook-1", list(_URLS))
        finally:
            await session.close_resources()
        assert channel.methods == [
            ADD_TENTATIVE_SOURCES_METHOD,
            ADD_SOURCES_METHOD,
            GET_PROJECT_METHOD,
        ]

    assert [item.source.id for item in outcomes if item.source is not None] == [
        "source-a" if backend == "web" else _SOURCE_IDS[0],
        "source-b" if backend == "web" else _SOURCE_IDS[1],
    ]
    assert [item.outcome.commit_state for item in outcomes if item.outcome is not None] == [
        CommitState.CONFIRMED,
        CommitState.CONFIRMED,
    ]


@pytest.mark.asyncio
async def test_real_web_auth_refresh_reposts_replay_safe_read_once() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(401, request=request)
        body = raw_batchexecute_body(
            [["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, json.dumps([]), None, None]]
        )
        return httpx.Response(200, text=body, request=request)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    async def refresh(_epoch: int) -> AuthTokens:
        client.auth.csrf_token = "csrf-new"
        client.auth.session_id = "session-new"
        return client.auth

    client = build_client_shell_for_tests(
        AuthTokens(csrf_token="csrf-old", session_id="session-old", cookies={"SID": "sid-old"}),
        refresh_callback=refresh,
        refresh_retry_delay=0,
        async_client_factory=factory,
    )
    journal = OperationJournal("notebooks.list")
    entry = journal.new_entry(
        method=RPCMethod.LIST_NOTEBOOKS.value,
        invocation_id=journal.invocation_id(),
    )
    identity = entry.identity
    assert isinstance(identity, SendIdentity)
    async with client:
        with bind_operation_journal_entries(entry):
            assert (
                await client._web_runtime.executor.rpc_call(
                    RPCMethod.LIST_NOTEBOOKS,
                    [],
                )
                == []
            )

    assert len(requests) == 2
    assert requests[0].content != requests[1].content
    assert entry.identity is identity
    assert journal.entries == (entry,)
    assert [attempt.ordinal for attempt in entry.attempts] == [1, 2]
    assert entry.attempts[0] is not entry.attempts[1]


@pytest.mark.asyncio
async def test_real_android_stream_cancellation_retains_unknown_attempt() -> None:
    _BlockingStream.entered = asyncio.Event()
    session, _channel = await _android_session("stream")
    journal = OperationJournal("chat")
    entry = journal.new_entry(method=GENERATE_FREE_FORM_STREAMED_METHOD)

    async def consume() -> None:
        with bind_operation_journal_entries(entry):
            async for _ in session.stream(
                GENERATE_FREE_FORM_STREAMED_METHOD,
                sources_pb2.AddTentativeSourcesRequest(),
                response_type=sources_pb2.AddTentativeSourcesResponse,
            ):
                pass

    task = asyncio.create_task(consume())
    await _BlockingStream.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await session.close_resources()

    assert entry.commit_state is CommitState.UNKNOWN
    assert len(entry.attempts) == 1


@pytest.mark.asyncio
async def test_real_android_whole_request_rejection_settles_every_batch_member() -> None:
    session, channel = await _android_session("whole_request_rejected")
    try:
        api = AndroidSourcesAPI(session, object.__new__(AndroidUploadPipeline))
        with pytest.raises(AuthError) as raised:
            await api._add_urls_batch("notebook-1", list(_URLS))
    finally:
        await session.close_resources()

    error = raised.value
    assert channel.methods == [ADD_TENTATIVE_SOURCES_METHOD]
    assert getattr(error, "commit_state", None) is CommitState.REJECTED
    metadata = getattr(error, "operation_metadata", None)
    assert metadata is not None
    assert metadata.commit_state is CommitState.REJECTED
    assert metadata.recovery_action is RecoveryAction.NONE
    assert [entry.commit_state for entry in metadata.entries] == [
        CommitState.REJECTED,
        CommitState.REJECTED,
        CommitState.NOT_SENT,
        CommitState.NOT_SENT,
    ]
    assert [attempt.commit_state for attempt in metadata.attempts] == [
        CommitState.REJECTED,
        CommitState.REJECTED,
    ]
    assert metadata.batch_outcome is not None
    assert [item.commit_state for item in metadata.batch_outcome.items] == [
        CommitState.REJECTED,
        CommitState.REJECTED,
    ]


@pytest.mark.asyncio
async def test_real_android_readback_cancellation_preserves_decoded_commit_batch() -> None:
    cancellation = asyncio.CancelledError("cancel Android source readback")
    session, channel = await _android_session("readback_cancellation")
    channel.readback_cancellation = cancellation
    try:
        api = AndroidSourcesAPI(session, object.__new__(AndroidUploadPipeline))
        with pytest.raises(asyncio.CancelledError) as raised:
            await api._add_urls_batch("notebook-1", list(_URLS))
    finally:
        await session.close_resources()

    propagated = raised.value
    # Python 3.10 recreates CancelledError when it crosses the child Task used
    # by asyncio.wait_for; 3.11+ preserves the raised exception instance.
    if sys.version_info >= (3, 11):
        assert propagated is cancellation
    metadata = propagated._operation_metadata  # type: ignore[attr-defined]
    assert metadata.commit_state is CommitState.CONFIRMED
    assert metadata.known_resource_ids == _SOURCE_IDS
    assert [entry.commit_state for entry in metadata.entries] == [
        CommitState.CONFIRMED,
        CommitState.CONFIRMED,
        CommitState.CONFIRMED,
        CommitState.CONFIRMED,
    ]
    assert metadata.batch_outcome is not None
    assert [item.commit_state for item in metadata.batch_outcome.items] == [
        CommitState.CONFIRMED,
        CommitState.CONFIRMED,
    ]
