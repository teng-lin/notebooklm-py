"""P9.1: ``WebTransport`` is pure motion of ``_rpc_call`` plus the ``dispatched`` marker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import BackendContractError, BackendDeadlineExceededError
from notebooklm._binding import CodecPayload, NativeChoice
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import NOTEBOOK_LIST_DEF
from notebooklm._web.transport import WebRequest, WebStreamRequest, WebTransport
from notebooklm.exceptions import NetworkError, ServerError
from notebooklm.rpc import RPCMethod


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingRuntime:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.calls: list[_Call] = []
        self._error = error

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        if self._error is not None:
            raise self._error
        return ["ok"]


def _transport(runtime: object) -> WebTransport:
    return WebTransport(
        runtime_provider=lambda: runtime,  # type: ignore[return-value]
        chat_transport=None,
        chat_response_max_bytes=None,
    )


def _expired_deadline() -> RuntimeDeadline:
    return RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)


@pytest.mark.asyncio
async def test_call_forwards_the_identical_keyword_set_including_explicit_defaults() -> None:
    runtime = _RecordingRuntime()
    request = WebRequest(
        operation=Operation.NOTEBOOK_LIST,
        method=RPCMethod.LIST_NOTEBOOKS,
        params=[None, 1],
    )

    result = await _transport(runtime).call(request, deadline=None)

    assert result == ["ok"]
    (call,) = runtime.calls
    assert call.method is RPCMethod.LIST_NOTEBOOKS
    assert call.params == [None, 1]
    assert call.kwargs == {
        "source_path": "/",
        "allow_null": False,
        "_is_retry": False,
        "disable_internal_retries": False,
        "operation_variant": None,
        "read_timeout": None,
        "raise_on_null_status": False,
        "_retry_deadline": None,
    }


@pytest.mark.asyncio
async def test_call_clamps_the_attempt_timeout_to_the_remaining_deadline() -> None:
    runtime = _RecordingRuntime()
    deadline = RuntimeDeadline(timeout=1000.0, started_at=10.0, monotonic=lambda: 11.0)
    request = WebRequest(
        operation=Operation.NOTEBOOK_LIST,
        method=RPCMethod.LIST_NOTEBOOKS,
        params=[],
        source_path="/notebook/n1",
        operation_variant="v",
        allow_null=True,
        raise_on_null_status=True,
        disable_internal_retries=True,
        attempt_timeout=5.0,
    )

    await _transport(runtime).call(request, deadline=deadline)

    (call,) = runtime.calls
    assert call.kwargs["source_path"] == "/notebook/n1"
    assert call.kwargs["operation_variant"] == "v"
    assert call.kwargs["allow_null"] is True
    assert call.kwargs["raise_on_null_status"] is True
    assert call.kwargs["disable_internal_retries"] is True
    assert call.kwargs["read_timeout"] == 5.0
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_pre_dispatch_expiry_raises_without_entering_the_runtime() -> None:
    runtime = _RecordingRuntime()
    request = WebRequest(
        operation=Operation.NOTEBOOK_CREATE,
        method=RPCMethod.CREATE_NOTEBOOK,
        params=[],
        outcome_unknown_on_expiry=True,
    )

    with pytest.raises(BackendDeadlineExceededError) as info:
        await _transport(runtime).call(request, deadline=_expired_deadline())

    assert runtime.calls == []
    assert info.value.operation is Operation.NOTEBOOK_CREATE
    assert info.value.outcome_unknown is True
    assert info.value.diagnostics["method_id"] == RPCMethod.CREATE_NOTEBOOK.value
    assert set(info.value.diagnostics) == {"timeout", "remaining", "timeout_seconds", "method_id"}
    assert getattr(info.value, "dispatched", False) is False


@pytest.mark.asyncio
async def test_pre_dispatch_expiry_defaults_outcome_unknown_to_false() -> None:
    runtime = _RecordingRuntime()
    request = WebRequest(
        operation=Operation.NOTEBOOK_LIST, method=RPCMethod.LIST_NOTEBOOKS, params=[]
    )

    with pytest.raises(BackendDeadlineExceededError) as info:
        await _transport(runtime).call(request, deadline=_expired_deadline())

    assert info.value.outcome_unknown is False


@pytest.mark.asyncio
async def test_escaped_native_error_is_tagged_dispatched_with_its_original() -> None:
    original = ConnectionError("reset")
    error = NetworkError("boom", original_error=original)
    runtime = _RecordingRuntime(error=error)
    request = WebRequest(
        operation=Operation.NOTEBOOK_LIST, method=RPCMethod.LIST_NOTEBOOKS, params=[]
    )

    with pytest.raises(NetworkError) as info:
        await _transport(runtime).call(request, deadline=None)

    assert info.value is error
    assert info.value.dispatched is True  # type: ignore[attr-defined]
    assert getattr(error.original_error, "dispatched", None) is True


@pytest.mark.asyncio
async def test_escaped_error_without_original_is_still_tagged() -> None:
    error = ServerError("500")
    runtime = _RecordingRuntime(error=error)
    request = WebRequest(
        operation=Operation.NOTEBOOK_LIST, method=RPCMethod.LIST_NOTEBOOKS, params=[]
    )

    with pytest.raises(ServerError):
        await _transport(runtime).call(request, deadline=None)

    assert getattr(error, "dispatched", None) is True


@pytest.mark.asyncio
async def test_call_reads_the_runtime_through_the_provider_on_every_call() -> None:
    first = _RecordingRuntime()
    second = _RecordingRuntime()
    holder = {"runtime": first}
    transport = WebTransport(
        runtime_provider=lambda: holder["runtime"],  # type: ignore[return-value]
        chat_transport=None,
        chat_response_max_bytes=None,
    )
    request = WebRequest(
        operation=Operation.NOTEBOOK_LIST, method=RPCMethod.LIST_NOTEBOOKS, params=[]
    )

    await transport.call(request, deadline=None)
    holder["runtime"] = second
    await transport.call(request, deadline=None)

    assert len(first.calls) == 1
    assert len(second.calls) == 1


def test_assemble_builds_the_request_from_the_native_choice_and_payload() -> None:
    transport = _transport(_RecordingRuntime())
    payload = CodecPayload(
        params=[1, 2],
        source_path="/notebook/n1",
        allow_null=True,
        raise_on_null_status=True,
        attempt_timeout=3.0,
    )

    request = transport.assemble(
        NOTEBOOK_LIST_DEF,
        NativeChoice(RPCMethod.LIST_NOTEBOOKS, "recent"),
        payload,
        retry_flag=True,
        deadline=None,
    )

    assert request == WebRequest(
        operation=Operation.NOTEBOOK_LIST,
        method=RPCMethod.LIST_NOTEBOOKS,
        params=[1, 2],
        source_path="/notebook/n1",
        operation_variant="recent",
        allow_null=True,
        raise_on_null_status=True,
        disable_internal_retries=True,
        outcome_unknown_on_expiry=False,
        attempt_timeout=3.0,
    )


def test_request_and_transport_reprs_do_not_leak_params() -> None:
    request = WebRequest(
        operation=Operation.NOTEBOOK_LIST,
        method=RPCMethod.LIST_NOTEBOOKS,
        params=["cookie-old", "csrf-old"],
    )
    stream = WebStreamRequest(
        operation=Operation.CHAT_ASK,
        build_request=lambda snapshot: ("cookie-old", "", {}),  # type: ignore[arg-type,return-value]
        parse_label="chat.ask",
    )

    assert "cookie-old" not in repr(request)
    assert "params" not in repr(request)
    assert "cookie-old" not in repr(stream)
    assert repr(_transport(_RecordingRuntime())) == "WebTransport(chat=False)"


@pytest.mark.asyncio
async def test_stream_rejects_batchexecute_requests_and_missing_chat_transport() -> None:
    transport = _transport(_RecordingRuntime())
    request = WebRequest(operation=Operation.CHAT_ASK, method=RPCMethod.LIST_NOTEBOOKS, params=[])
    with pytest.raises(BackendContractError):
        await transport.stream(request, deadline=None)

    stream = WebStreamRequest(
        operation=Operation.CHAT_ASK,
        build_request=lambda snapshot: ("", "", {}),  # type: ignore[arg-type,return-value]
        parse_label="chat.ask",
    )
    with pytest.raises(BackendContractError):
        await transport.stream(stream, deadline=None)
