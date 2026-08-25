"""P9.4b: ``chat.ask`` dispatches as the protocol ``CustomBinding`` row exactly as the handler did.

The row declares its one native (``GET_LAST_CONVERSATION_ID`` under ``"conversation"``)
and its one streamed verb (``"ask"``), and reaches the request-id counter and the
configured chat read timeout only through declared collaborators.  These tests pin
the conversion oracles: the streamed request carries the same builder, parse label
and clamped read timeout; the conditional phase-two read forwards the identical
runtime keyword set (including ``outcome_unknown_on_expiry`` on the request);
the precondition error, the pre-dispatch expiry, the ``NetworkError``-after-expiry
mapping and the post-stream expiry are byte-for-byte the handler's; translated
failures are request-URL scrubbed, ``dispatched``, and tagged with the phase that
failed; and the emptied ``ChatWebHandlers`` mixin is gone from the chain.
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

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CustomBinding, ErrorMode, NativeChoice, StreamPayload, StreamSpec
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import CHAT_ASK_DEF, ChatAskInput
from notebooklm._web.backend import ROW_COLLABORATOR_NAMES, WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import chat as chat_rows
from notebooklm._web.failure_projection import _capture_public_failure
from notebooklm._web.policy import WEB_CALL_POLICY_BINDINGS
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
        chat_reqid=reqid or SimpleNamespace(next_reqid=AsyncMock(return_value=100000)),
        chat_timeout=chat_timeout,
    )


def _ask_input(**overrides: Any) -> ChatAskInput:
    values: dict[str, Any] = {
        "notebook_id": _NB,
        "question": "Q?",
        "source_ids": ("source-1",),
        "resolved_conversation_id": None,
    }
    values.update(overrides)
    return ChatAskInput(**values)


# --- registry partition ------------------------------------------------------


def test_chat_ask_is_the_protocol_custom_row_and_the_chat_mixin_is_gone() -> None:
    row = chat_rows.CHAT_ASK
    binding = WEB_OPERATION_REGISTRY[Operation.CHAT_ASK]
    assert WEB_BINDING_ROWS[Operation.CHAT_ASK] is row
    assert binding.is_supported and binding.row is row
    assert isinstance(row, CustomBinding)
    assert row.category == "protocol"
    assert row.error_mode is ErrorMode.TRANSLATE_SCRUBBED
    assert row.map_error is None
    assert [spec.key for spec in row.native] == ["conversation"]
    assert row.native[0].select(None) == NativeChoice(RPCMethod.GET_LAST_CONVERSATION_ID)
    assert row.streams == (StreamSpec(key="ask", label="chat.ask"),)
    assert set(row.collaborators) == {"chat_reqid", "chat_timeout", "chat_transport_composed"}
    assert set(row.collaborators) <= ROW_COLLABORATOR_NAMES
    # The ledger keeps the facade's GET_NOTEBOOK recency read as a reviewed divergence.
    ledger = WEB_CALL_POLICY_BINDINGS[Operation.CHAT_ASK]
    assert ledger.known_divergence is not None
    assert {native.method for native in ledger.native_bindings} == {
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.GET_LAST_CONVERSATION_ID,
    }
    # The handler and its mixin are deleted; the chain shrank by one.
    assert not hasattr(WebRpcBackend, "_chat_ask")
    assert not hasattr(WebRpcBackend, "_chat_conversation_id")
    assert importlib.util.find_spec("notebooklm._web.chat") is None
    assert "ChatWebHandlers" not in {klass.__name__ for klass in WebRpcBackend.__mro__}
    backend = _backend(_RecordingExecutor())
    assert backend._bindings[Operation.CHAT_ASK] is row


# --- sequence and kwargs ---------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_streams_then_reads_the_conversation_id_with_identical_kwargs() -> None:
    transport = SimpleNamespace(
        perform_authed_post=AsyncMock(return_value=_response(_stream_body(_ANSWER_ROW)))
    )
    executor = _RecordingExecutor([[["conv-9"]]])
    backend = _backend(executor, transport=transport)

    result = await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=None)

    assert result.conversation_id == "conv-9"
    assert len(result.answer) == 536
    assert len(result.raw_response) == 1000
    stream_kwargs = transport.perform_authed_post.await_args.kwargs
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

    result = await backend.invoke(
        CHAT_ASK_DEF, _ask_input(resolved_conversation_id="conv-1"), deadline=None
    )

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

    await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=deadline)

    stream_kwargs = transport.perform_authed_post.await_args.kwargs
    assert stream_kwargs["read_timeout"] == 3.0
    assert stream_kwargs["retry_deadline"] is deadline
    (readback,) = executor.calls
    assert readback.kwargs["_retry_deadline"] is deadline
    assert readback.kwargs["read_timeout"] == pytest.approx(3.0)


def test_stream_spec_assembles_the_chat_aware_request() -> None:
    backend = _backend(_RecordingExecutor())
    request = backend._transport.assemble_stream(
        CHAT_ASK_DEF,
        chat_rows.CHAT_ASK.streams[0],
        StreamPayload(build_request=lambda snapshot: snapshot, attempt_timeout=7.0),
        deadline=None,
    )
    assert isinstance(request, WebStreamRequest)
    assert request.operation is Operation.CHAT_ASK
    assert request.parse_label == "chat.ask"
    assert request.read_timeout == 7.0


# --- preconditions and deadline projection -----------------------------------------


@pytest.mark.asyncio
async def test_ask_requires_the_composed_chat_transport_and_reqid_counter() -> None:
    backend = build_web_backend(_RecordingExecutor(), chat_transport=None, chat_reqid=None)

    with pytest.raises(BackendContractError) as caught:
        await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=None)

    assert str(caught.value) == (
        "chat.ask requires the composed chat transport and request-id counter"
    )
    assert caught.value.operation is Operation.CHAT_ASK


@pytest.mark.asyncio
async def test_ask_pre_dispatch_expiry_is_not_dispatched_and_not_unknown() -> None:
    transport = SimpleNamespace(perform_authed_post=AsyncMock())
    executor = _RecordingExecutor()
    backend = _backend(executor, transport=transport)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=deadline)

    error = caught.value
    assert error.operation is Operation.CHAT_ASK
    assert error.outcome_unknown is False
    assert error.dispatched is False
    assert may_have_committed(error) is False
    # The row streams: it resolves no native before dispatch, so — unlike a codec
    # or keyed row — its expiry names no ``method_id``.
    assert error.diagnostics == {
        "timeout": 5.0,
        "remaining": 0.0,
        "timeout_seconds": 5.0,
    }
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
        await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=deadline)

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
        await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=deadline)

    assert caught.value.outcome_unknown is True
    assert caught.value.__cause__ is None
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
        await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=None)

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.CHAT_ASK
    assert error.reason is BackendErrorReason.NETWORK
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    # The chat family projects its public failure with request URLs scrubbed —
    # the same projection ``invoke()`` applied when the handler owned the stream.
    assert error.diagnostics["public_error_failure"] == _capture_public_failure(
        raw, operation=Operation.CHAT_ASK, scrub_request_urls=True
    )
    assert error.diagnostics["public_error_failure"] != _capture_public_failure(
        raw, operation=Operation.CHAT_ASK, scrub_request_urls=False
    )
    assert error.__cause__ is raw
    assert raw.binding_native == StreamSpec(key="ask", label="chat.ask")


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
        await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=None)

    error = caught.value
    assert error.operation is Operation.CHAT_ASK
    assert error.reason is BackendErrorReason.SERVER
    assert error.dispatched is True
    assert isinstance(error.__cause__, ServerError)
    assert error.__cause__.binding_native == NativeChoice(RPCMethod.GET_LAST_CONVERSATION_ID)
    assert "post-ask get_conversation_id failed" in caplog.text


@pytest.mark.asyncio
async def test_missing_conversation_id_after_a_non_empty_answer_is_a_chat_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = _RecordingExecutor([[[None]]])
    backend = _backend(executor)
    caplog.set_level(logging.ERROR, logger="notebooklm._chat.api")

    with pytest.raises(BackendError) as caught:
        await backend.invoke(CHAT_ASK_DEF, _ask_input(), deadline=None)

    assert isinstance(caught.value.__cause__, ChatError)
    assert "hPTbtc returned no id" in str(caught.value.__cause__)
    assert "returned no conversation_id" in caplog.text
