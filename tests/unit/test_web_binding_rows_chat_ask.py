"""P10 R2.2: ``chat.ask`` is a service-owned workflow over the streamed leaf.

``CHAT_STREAM_ANSWER`` is the one codec row whose ``NativeCallSpec`` selects a
:class:`StreamNative`: it declares no ``RPCMethod`` and dispatches through
``Transport.stream``.  ``ChatWorkflowService.ask`` sequences it and, only when the caller
resolved no id, the ``CHAT_GET_CONVERSATION`` leaf.

These tests are the conversion oracles for that move.  Every observable the
retired ``CustomBinding`` row produced is pinned here against the new path: the
streamed request's parse label and clamped read timeout, the readback's
identical runtime keyword set, the precondition error, the pre-dispatch expiry,
the ``NetworkError``-after-expiry mapping, the post-stream expiry, request-URL
scrubbing, ``dispatched``/commit-uncertainty, and the two ``chat_logger``
diagnostics that precede a failed or empty readback.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS, WEB_CALL_POLICY_BINDINGS

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
    may_have_committed,
)
from notebooklm._binding import CodecBinding, NativeCallSpec, StreamNative, StreamRequestPayload
from notebooklm._chat.workflow import ChatWorkflowService
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation, OperationTier
from notebooklm._semantic.records import (
    CHAT_ASK_DEF,
    CHAT_STREAM_ANSWER_DEF,
    ChatAskInput,
    ChatStreamAnswerInput,
)
from notebooklm._web.backend import ROW_COLLABORATOR_NAMES, WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import primitives as primitive_rows
from notebooklm._web.codec.chat_stream import ChatStreamRequestData
from notebooklm._web.failure_projection import _capture_public_failure
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm._web.transport import WebStreamRequest
from notebooklm.exceptions import ChatError, NetworkError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_ANSWER_ROW: list[Any] = json.loads(
    (_FIXTURES_DIR / "chat_answer_row_with_citations.json").read_text()
)
_NB = "nb-1"

_BASE_KWARGS = {
    "allow_null": False,
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}


def _stream_body(answer_row: list[Any]) -> bytes:
    return (
        ")]}'\n\n"
        + json.dumps([["wrb.fr", None, json.dumps([answer_row]), None, None, None, "generic"]])
    ).encode()


def _response(body: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("POST", "https://example.test/chat"),
        content=body,
    )


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _backend(
    executor: _RecordingExecutor,
    *,
    transport: Any = None,
    body: bytes | None = None,
    reqid: Any = None,
    chat_timeout: float | None = 45.0,
) -> WebRpcBackend:
    if transport is None:
        transport = SimpleNamespace(
            perform_authed_post=AsyncMock(return_value=_response(body or _stream_body(_ANSWER_ROW)))
        )
    return build_web_backend(
        executor,
        chat_transport=transport,
        reqid=reqid or SimpleNamespace(next_reqid=AsyncMock(return_value=100000)),
        chat_timeout=chat_timeout,
    )


class _NoSourceIds:
    """Source-id resolver the workflow never reaches: every ask supplies its own."""

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        raise AssertionError("chat.ask leaf sequencing must not resolve source ids")


def _ask_input(**overrides: Any) -> ChatAskInput:
    values: dict[str, Any] = {
        "notebook_id": _NB,
        "question": "Q?",
        "source_ids": ("source-1",),
        "resolved_conversation_id": None,
    }
    values.update(overrides)
    return ChatAskInput(**values)


async def _ask(backend: WebRpcBackend, **overrides: Any) -> Any:
    deadline = overrides.pop("deadline", None)
    return await ChatWorkflowService(backend, notebooks=_NoSourceIds()).ask(
        _ask_input(**overrides), deadline=deadline
    )


# --- registry partition ------------------------------------------------------


def test_chat_ask_is_service_owned_over_a_streamed_primitive_leaf() -> None:
    workflow = WEB_OPERATION_REGISTRY[Operation.CHAT_ASK]
    assert workflow.row is None and not workflow.is_supported
    assert workflow.service_owned and Operation.CHAT_ASK not in WEB_BINDING_ROWS
    assert "ChatWorkflowService.ask" in (workflow.unsupported_reason or "")

    row = primitive_rows.CHAT_STREAM_ANSWER
    leaf = WEB_OPERATION_REGISTRY[Operation.CHAT_STREAM_ANSWER]
    assert WEB_BINDING_ROWS[Operation.CHAT_STREAM_ANSWER] is row
    assert leaf.is_supported and leaf.row is row
    assert isinstance(row, CodecBinding)
    assert row.definition.tier is OperationTier.PRIMITIVE
    assert row.native == NativeCallSpec(choices=(StreamNative("chat.ask"),))
    assert row.map_error is None
    # A streamed verb is not a method: the leaf reaches the wire without ever
    # naming one, so it declares no collaborator either.
    assert not getattr(row, "collaborators", ())
    assert {"source_uploader"} == ROW_COLLABORATOR_NAMES

    # The ledger keeps the facade's GET_NOTEBOOK recency read as a reviewed
    # divergence, now on the workflow row rather than a direct binding.
    assert Operation.CHAT_ASK not in WEB_CALL_POLICY_BINDINGS
    ledger = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.CHAT_ASK]
    assert ledger.known_divergence is not None
    assert {native.method for native in ledger.native_bindings} == {
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.GET_LAST_CONVERSATION_ID,
    }
    assert [leaf.operation for leaf in ledger.leaf_operations] == [
        Operation.CHAT_STREAM_ANSWER,
        Operation.CHAT_GET_CONVERSATION,
    ]
    streamed = WEB_CALL_POLICY_BINDINGS[Operation.CHAT_STREAM_ANSWER]
    assert streamed.native_bindings == ()
    assert [item.label for item in streamed.streamed_bindings] == ["chat.ask"]

    # The handler, its mixin and the module that held them are all gone.
    assert not hasattr(WebRpcBackend, "_chat_ask")
    assert importlib.util.find_spec("notebooklm._web.chat") is None
    assert "ChatWebHandlers" not in {klass.__name__ for klass in WebRpcBackend.__mro__}


@pytest.mark.asyncio
async def test_the_port_refuses_the_workflow_and_accepts_only_the_leaf() -> None:
    backend = _backend(_RecordingExecutor())

    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=None)

    assert backend.capabilities.available(Operation.CHAT_ASK) is True
    assert backend.capabilities.supports(Operation.CHAT_ASK) is False
    assert backend.capabilities.supports(Operation.CHAT_STREAM_ANSWER) is True


# --- sequence and kwargs ---------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_streams_then_reads_the_conversation_id_with_identical_kwargs() -> None:
    transport = SimpleNamespace(
        perform_authed_post=AsyncMock(return_value=_response(_stream_body(_ANSWER_ROW)))
    )
    executor = _RecordingExecutor([[["conv-9"]]])
    backend = _backend(executor, transport=transport)

    result = await _ask(backend)

    assert result.conversation_id == "conv-9"
    assert len(result.answer) == 536
    assert len(result.raw_response) == 1000
    stream_kwargs = transport.perform_authed_post.await_args.kwargs
    assert stream_kwargs["log_label"] == "chat.ask"
    assert stream_kwargs["read_timeout"] == 45.0
    assert stream_kwargs["retry_deadline"] is None
    assert stream_kwargs["disable_read_timeout_retries"] is True
    (readback,) = executor.calls
    assert readback.method is RPCMethod.GET_LAST_CONVERSATION_ID
    assert readback.kwargs == {**_BASE_KWARGS, "source_path": f"/notebook/{_NB}"}


@pytest.mark.asyncio
async def test_ask_skips_phase_two_when_the_conversation_is_already_resolved() -> None:
    executor = _RecordingExecutor()
    backend = _backend(executor)

    result = await _ask(backend, resolved_conversation_id="conv-1")

    assert result.conversation_id == "conv-1"
    assert executor.calls == []


@pytest.mark.asyncio
async def test_ask_clamps_the_stream_read_timeout_and_threads_the_deadline() -> None:
    transport = SimpleNamespace(
        perform_authed_post=AsyncMock(return_value=_response(_stream_body(_ANSWER_ROW)))
    )
    executor = _RecordingExecutor([[["conv-9"]]])
    backend = _backend(executor, transport=transport)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 12.0)

    await _ask(backend, deadline=deadline)

    stream_kwargs = transport.perform_authed_post.await_args.kwargs
    assert stream_kwargs["read_timeout"] == 3.0
    assert stream_kwargs["retry_deadline"] is deadline
    (readback,) = executor.calls
    assert readback.kwargs["_retry_deadline"] is deadline
    assert readback.kwargs["read_timeout"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_the_streamed_request_id_comes_from_the_one_shared_counter() -> None:
    transport = SimpleNamespace(
        perform_authed_post=AsyncMock(return_value=_response(_stream_body(_ANSWER_ROW)))
    )
    reqid = SimpleNamespace(next_reqid=AsyncMock(return_value=123456))
    backend = _backend(_RecordingExecutor(), transport=transport, reqid=reqid)

    await _ask(backend, resolved_conversation_id="conv-1")

    reqid.next_reqid.assert_awaited_once_with()
    build_request = transport.perform_authed_post.await_args.kwargs["build_request"]
    url, body, headers = build_request(
        SimpleNamespace(csrf_token="tok", session_id="sid", authuser=0, account_email=None)
    )
    assert "_reqid=123456" in url
    assert "f.req=" in body and headers == {}


def test_the_codec_encodes_request_data_the_transport_packages_verbatim() -> None:
    payload = primitive_rows.CHAT_STREAM_ANSWER.encode(
        ChatStreamAnswerInput(
            notebook_id=_NB,
            question="Q?",
            source_ids=("source-1",),
            post_conversation_id="conv-1",
        )
    )

    assert payload == StreamRequestPayload(
        ChatStreamRequestData(
            notebook_id=_NB,
            question="Q?",
            source_ids=("source-1",),
            conversation_history=None,
            conversation_id="conv-1",
        )
    )

    backend = _backend(_RecordingExecutor())
    request = backend._transport.assemble_stream(
        CHAT_STREAM_ANSWER_DEF,
        StreamNative("chat.ask"),
        payload,
        deadline=None,
    )
    assert isinstance(request, WebStreamRequest)
    assert request.operation is Operation.CHAT_STREAM_ANSWER
    assert request.parse_label == "chat.ask"
    assert request.read_timeout == 45.0
    assert request.data is payload.data


def test_the_transport_rejects_a_streamed_payload_it_cannot_materialise() -> None:
    backend = _backend(_RecordingExecutor())

    with pytest.raises(BackendContractError, match="not encoded chat-stream request data"):
        backend._transport.assemble_stream(
            CHAT_STREAM_ANSWER_DEF,
            StreamNative("chat.ask"),
            StreamRequestPayload(data="nonsense"),
            deadline=None,
        )


# --- preconditions and deadline projection -----------------------------------------


@pytest.mark.asyncio
async def test_ask_requires_the_composed_chat_transport_and_reqid_counter() -> None:
    backend = build_web_backend(_RecordingExecutor(), chat_transport=None, reqid=None)

    with pytest.raises(BackendContractError) as caught:
        await _ask(backend)

    assert str(caught.value) == (
        "chat.stream_answer requires the composed chat transport and request-id counter"
    )
    assert caught.value.operation is Operation.CHAT_ASK


@pytest.mark.asyncio
async def test_ask_pre_dispatch_expiry_is_not_dispatched_and_not_unknown() -> None:
    transport = SimpleNamespace(perform_authed_post=AsyncMock())
    executor = _RecordingExecutor()
    backend = _backend(executor, transport=transport)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _ask(backend, deadline=deadline)

    error = caught.value
    assert error.operation is Operation.CHAT_ASK
    assert str(error) == "chat.ask exceeded its deadline"
    assert error.outcome_unknown is False
    assert error.dispatched is False
    assert may_have_committed(error) is False
    # The row streams: it resolves no wire method before dispatch, so — unlike a
    # codec or keyed row — its expiry names no ``method_id``.
    assert error.diagnostics is not None
    assert {key: error.diagnostics[key] for key in ("timeout", "remaining", "timeout_seconds")} == {
        "timeout": 5.0,
        "remaining": 0.0,
        "timeout_seconds": 5.0,
    }
    assert "method_id" not in error.diagnostics
    assert error.diagnostics["leaf_operation"] is Operation.CHAT_STREAM_ANSWER
    transport.perform_authed_post.assert_not_awaited()
    assert executor.calls == []


@pytest.mark.asyncio
async def test_ask_network_timeout_after_expiry_becomes_a_commit_uncertain_deadline_error() -> None:
    clock = [11.0]

    async def perform_authed_post(**kwargs: Any) -> httpx.Response:
        clock[0] = 16.0
        raise NetworkError("slow", original_error=httpx.ReadTimeout("slow"))

    transport = SimpleNamespace(perform_authed_post=perform_authed_post)
    backend = _backend(_RecordingExecutor(), transport=transport)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _ask(backend, deadline=deadline)

    error = caught.value
    assert error.operation is Operation.CHAT_ASK
    assert error.outcome_unknown is True
    assert isinstance(error.__cause__, NetworkError)


@pytest.mark.asyncio
async def test_ask_post_stream_expiry_is_commit_uncertain() -> None:
    clock = [11.0]

    async def perform_authed_post(**kwargs: Any) -> httpx.Response:
        clock[0] = 16.0
        return _response(_stream_body(_ANSWER_ROW))

    transport = SimpleNamespace(perform_authed_post=perform_authed_post)
    executor = _RecordingExecutor()
    backend = _backend(executor, transport=transport)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _ask(backend, deadline=deadline)

    assert caught.value.operation is Operation.CHAT_ASK
    assert caught.value.outcome_unknown is True
    assert caught.value.__cause__ is None
    assert executor.calls == []


@pytest.mark.asyncio
async def test_a_readback_expiry_after_an_accepted_stream_is_commit_uncertain() -> None:
    clock = [11.0]

    async def perform_authed_post(**kwargs: Any) -> httpx.Response:
        return _response(_stream_body(_ANSWER_ROW))

    transport = SimpleNamespace(perform_authed_post=perform_authed_post)
    executor = _RecordingExecutor()
    backend = _backend(executor, transport=transport)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    # The stream lands inside the budget; the readback finds it spent.
    async def expire_then_stream(**kwargs: Any) -> httpx.Response:
        response = await perform_authed_post(**kwargs)
        clock[0] = 16.0
        return response

    transport.perform_authed_post = expire_then_stream
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _ask(backend, deadline=deadline)

    assert caught.value.operation is Operation.CHAT_ASK
    assert caught.value.outcome_unknown is True
    assert executor.calls == []


# --- failure projection ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_failure_is_translated_scrubbed_dispatched_and_tagged_with_the_stream() -> (
    None
):
    request = httpx.Request(
        "POST",
        "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?rpcids=x&f.sid=1",
    )
    raw = NetworkError(
        "connect failed", original_error=httpx.ConnectError("refused", request=request)
    )

    async def perform_authed_post(**kwargs: Any) -> httpx.Response:
        raise raw

    transport = SimpleNamespace(perform_authed_post=perform_authed_post)
    backend = _backend(_RecordingExecutor(), transport=transport)

    with pytest.raises(BackendError) as caught:
        await _ask(backend)

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.CHAT_ASK
    assert error.reason is BackendErrorReason.NETWORK
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    # The chat family projects its public failure with request URLs scrubbed —
    # the same projection the retired custom row's ``TRANSLATE_SCRUBBED`` mode
    # applied, now inherited from the leaf's membership of the chat family.
    assert error.diagnostics["public_error_failure"] == _capture_public_failure(
        raw, operation=Operation.CHAT_STREAM_ANSWER, scrub_request_urls=True
    )
    assert error.diagnostics["public_error_failure"] != _capture_public_failure(
        raw, operation=Operation.CHAT_STREAM_ANSWER, scrub_request_urls=False
    )
    assert error.diagnostics["leaf_operation"] is Operation.CHAT_STREAM_ANSWER
    assert error.__cause__ is raw
    assert raw.binding_native == StreamNative("chat.ask")


@pytest.mark.asyncio
async def test_phase_two_failure_is_translated_dispatched_and_tagged_with_the_native(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = _RecordingExecutor(
        ServerError("boom", method_id=RPCMethod.GET_LAST_CONVERSATION_ID.value)
    )
    backend = _backend(executor)
    caplog.set_level(logging.ERROR, logger="notebooklm._chat.api")

    with pytest.raises(BackendError) as caught:
        await _ask(backend)

    error = caught.value
    assert error.operation is Operation.CHAT_ASK
    assert error.reason is BackendErrorReason.SERVER
    assert error.dispatched is True
    assert error.outcome_unknown is False
    assert isinstance(error.__cause__, ServerError)
    assert "post-ask get_conversation_id failed" in caplog.text


@pytest.mark.asyncio
async def test_missing_conversation_id_after_a_non_empty_answer_is_a_chat_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = _RecordingExecutor([[[None]]])
    backend = _backend(executor)
    caplog.set_level(logging.ERROR, logger="notebooklm._chat.api")

    with pytest.raises(BackendError) as caught:
        await _ask(backend)

    error = caught.value
    assert error.operation is Operation.CHAT_ASK
    assert error.reason is BackendErrorReason.CHAT
    assert error.outcome_unknown is False
    assert error.message == (
        "Server did not register a conversation for this ask (hPTbtc returned no "
        "id). The response may have been empty, or the API shape may have changed. "
        "Please file an issue at https://github.com/teng-lin/notebooklm-py/issues."
    )
    assert "returned no conversation_id" in caplog.text

    # ...and the compatibility projector reproduces the public exception the
    # retired row raised directly, character for character.
    from notebooklm._semantic.compat import project_backend_error

    public = project_backend_error(error)
    assert type(public) is ChatError
    assert str(public) == error.message
    assert getattr(public, "unconfirmed", False) is False
