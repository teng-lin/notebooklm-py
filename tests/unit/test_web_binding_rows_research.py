"""P9.3 research: the four codec rows dispatch exactly as the P6.2 handlers did.

``RESEARCH_START`` is the input-keyed row (fast or deep by ``value.mode``) whose
``map_error`` reproduces the deep-start null-result translation; the other three
are constant rows.  These tests pin the conversion oracles: the identical keyword
set reaches the runtime (including explicit ``False``/``None`` values and the
service-computed ``attempt_timeout``), failure projection is what ``invoke()``
produced for handler rows, and the ``RESEARCH_START_UNAVAILABLE`` error keeps its
message, reason, diagnostics and rejecting cause.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CodecBinding, DeadlineMode, RpcNative
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._semantic.records import (
    RESEARCH_CANCEL_DEF,
    RESEARCH_IMPORT_DEF,
    RESEARCH_POLL_DEF,
    RESEARCH_START_DEF,
    ResearchCancelInput,
    ResearchCancelResult,
    ResearchImportEntry,
    ResearchImportEntryKind,
    ResearchImportInput,
    ResearchMode,
    ResearchPollInput,
    ResearchSearchSource,
    ResearchStartInput,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import research as research_rows
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import RPCError, RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_DEEP_INPUT = ResearchStartInput("nb", "q", ResearchSearchSource.WEB, ResearchMode.DEEP)
_FAST_INPUT = ResearchStartInput("nb", "q", ResearchSearchSource.WEB, ResearchMode.FAST)


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


def _rejected_deep_start() -> RPCError:
    return RPCError(
        "NotebookLM rejected this request",
        method_id=RPCMethod.START_DEEP_RESEARCH.value,
        rpc_code=13,
        found_ids=[RPCMethod.START_DEEP_RESEARCH.value],
    )


def test_research_rows_replace_their_handlers_in_the_registry_and_table() -> None:
    converted = {
        Operation.RESEARCH_START: research_rows.RESEARCH_START,
        Operation.RESEARCH_POLL: research_rows.RESEARCH_POLL,
        Operation.RESEARCH_CANCEL: research_rows.RESEARCH_CANCEL,
        Operation.RESEARCH_IMPORT: research_rows.RESEARCH_IMPORT,
    }
    assert {op: WEB_BINDING_ROWS[op] for op in converted} == converted
    for operation, row in converted.items():
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.forward_disable_internal_retries is False
    # The start row is input-keyed over exactly the ledger's two natives; the
    # other three are constant rows with no semantic error translation.
    start = research_rows.RESEARCH_START
    assert not start.native.is_constant
    assert set(start.native.choices) == {
        RpcNative(RPCMethod.START_FAST_RESEARCH),
        RpcNative(RPCMethod.START_DEEP_RESEARCH),
    }
    assert start.native.select(_FAST_INPUT) == RpcNative(RPCMethod.START_FAST_RESEARCH)
    assert start.native.select(_DEEP_INPUT) == RpcNative(RPCMethod.START_DEEP_RESEARCH)
    assert start.map_error is not None
    for row in (
        research_rows.RESEARCH_POLL,
        research_rows.RESEARCH_CANCEL,
        research_rows.RESEARCH_IMPORT,
    ):
        assert row.native.is_constant
        assert row.map_error is None
    for name in ("_research_start", "_research_poll", "_research_cancel", "_research_import"):
        assert not hasattr(WebRpcBackend, name)
    # The P6.2 mixin is gone and the chain re-links around it.
    # Later slices delete their own chain classes too, so pin only this deletion.
    assert "ResearchWebHandlers" not in {klass.__name__ for klass in WebRpcBackend.__mro__}
    backend = build_web_backend(_RecordingExecutor())
    assert backend._bindings[Operation.RESEARCH_START] is research_rows.RESEARCH_START


@pytest.mark.asyncio
async def test_research_rows_forward_the_identical_keyword_set() -> None:
    executor = _RecordingExecutor(
        ["task_1", "report_1"],
        ["task_2"],
        [],
        None,
        [[["src_1"], "One"]],
    )
    backend = build_web_backend(executor)

    deep = await backend.invoke(RESEARCH_START_DEF, _DEEP_INPUT, deadline=None)
    fast = await backend.invoke(RESEARCH_START_DEF, _FAST_INPUT, deadline=None)
    poll = await backend.invoke(RESEARCH_POLL_DEF, ResearchPollInput("nb"), deadline=None)
    cancel = await backend.invoke(
        RESEARCH_CANCEL_DEF, ResearchCancelInput("nb", "run_1"), deadline=None
    )
    imported = await backend.invoke(
        RESEARCH_IMPORT_DEF,
        ResearchImportInput(
            "nb",
            "task",
            (
                ResearchImportEntry(
                    kind=ResearchImportEntryKind.WEB, url="https://example.com", title="One"
                ),
            ),
            attempt_timeout=12.5,
        ),
        deadline=None,
    )

    assert (deep.task_id, deep.report_id) == ("task_1", "report_1")
    assert (fast.task_id, fast.report_id) == ("task_2", None)
    assert poll.tasks == ()
    assert cancel == ResearchCancelResult()
    assert [(item.id, item.title) for item in imported.imported] == [("src_1", "One")]

    deep_call, fast_call, poll_call, cancel_call, import_call = executor.calls
    assert deep_call.method is RPCMethod.START_DEEP_RESEARCH
    assert deep_call.params == [None, [1], ["q", 1], 5, "nb"]
    assert fast_call.method is RPCMethod.START_FAST_RESEARCH
    assert fast_call.params == [["q", 1], None, 1, "nb"]
    assert poll_call.method is RPCMethod.POLL_RESEARCH
    assert poll_call.params == [None, None, "nb"]
    assert cancel_call.method is RPCMethod.CANCEL_RESEARCH
    assert cancel_call.params == [None, None, "run_1"]
    assert import_call.method is RPCMethod.IMPORT_RESEARCH
    assert import_call.params[:4] == [None, [1], "task", "nb"]
    base_kwargs = {
        "source_path": "/notebook/nb",
        "allow_null": False,
        "_is_retry": False,
        "disable_internal_retries": False,
        "operation_variant": None,
        "read_timeout": None,
        "raise_on_null_status": False,
        "_retry_deadline": None,
    }
    for call in (deep_call, fast_call, poll_call, cancel_call):
        assert call.kwargs == base_kwargs
    # The import row forwards the service-computed attempt window as read_timeout.
    assert import_call.kwargs == {**base_kwargs, "read_timeout": 12.5}


@pytest.mark.asyncio
async def test_import_attempt_timeout_is_clamped_to_the_shared_deadline() -> None:
    executor = _RecordingExecutor([[["src_1"], "One"]])
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await backend.invoke(
        RESEARCH_IMPORT_DEF,
        ResearchImportInput("nb", "task", (), attempt_timeout=30.0),
        deadline=deadline,
    )

    (call,) = executor.calls
    assert call.kwargs["read_timeout"] == pytest.approx(4.0)
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_deep_start_null_result_maps_to_the_unavailable_reason_with_evidence() -> None:
    rejected = _rejected_deep_start()
    backend = build_web_backend(_RecordingExecutor(rejected))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(RESEARCH_START_DEF, _DEEP_INPUT, deadline=None)

    error = caught.value
    assert type(error) is BackendError
    assert error.reason is BackendErrorReason.RESEARCH_START_UNAVAILABLE
    assert error.operation is Operation.RESEARCH_START
    assert error.message == "research start returned no run"
    assert error.outcome_unknown is False
    assert error.__cause__ is rejected
    assert error.diagnostics is not None
    assert error.diagnostics["notebook_id"] == "nb"
    assert error.diagnostics["mode"] == "deep"
    assert error.diagnostics["original_message"] == "NotebookLM rejected this request"
    assert error.diagnostics["original_reason"] == BackendErrorReason.RPC.value
    original = error.diagnostics["original_diagnostics"]
    assert isinstance(original, dict)
    assert original["method_id"] == RPCMethod.START_DEEP_RESEARCH.value
    assert original["rpc_code"] == 13
    assert original["found_ids"] == [RPCMethod.START_DEEP_RESEARCH.value]
    assert "public_error_failure" in original


@pytest.mark.asyncio
async def test_fast_start_null_result_keeps_the_shared_rpc_translation() -> None:
    rejected = RPCError(
        "NotebookLM rejected this request",
        method_id=RPCMethod.START_FAST_RESEARCH.value,
        found_ids=[RPCMethod.START_FAST_RESEARCH.value],
    )
    backend = build_web_backend(_RecordingExecutor(rejected))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(RESEARCH_START_DEF, _FAST_INPUT, deadline=None)

    error = caught.value
    assert error.reason is BackendErrorReason.RPC
    assert error.message == "NotebookLM rejected this request"
    assert error.dispatched is True
    assert error.__cause__ is rejected


@pytest.mark.asyncio
async def test_deep_start_transport_failures_bypass_the_semantic_mapper() -> None:
    server_error = ServerError("boom", method_id=RPCMethod.START_DEEP_RESEARCH.value)
    backend = build_web_backend(_RecordingExecutor(server_error))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(RESEARCH_START_DEF, _DEEP_INPUT, deadline=None)

    error = caught.value
    assert type(error) is BackendError
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.START_DEEP_RESEARCH.value
    assert "public_error_failure" in error.diagnostics
    assert error.__cause__ is server_error


@pytest.mark.asyncio
async def test_codec_row_timeout_after_expiry_becomes_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(RPCTimeoutError("slow", method_id=RPCMethod.POLL_RESEARCH.value))
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(RESEARCH_POLL_DEF, ResearchPollInput("nb"), deadline=deadline)

    error = caught.value
    assert error.operation is Operation.RESEARCH_POLL
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is False  # READ policy
    assert error.dispatched is True
    assert error.diagnostics is not None
    assert error.diagnostics["timeout"] == 5.0
    assert error.diagnostics["method_id"] == RPCMethod.POLL_RESEARCH.value
    assert "public_error_failure" in error.diagnostics
    assert isinstance(error.__cause__, RPCTimeoutError)


@pytest.mark.asyncio
async def test_codec_row_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            RESEARCH_CANCEL_DEF, ResearchCancelInput("nb", "run_1"), deadline=deadline
        )

    assert executor.calls == []
    assert caught.value.operation is Operation.RESEARCH_CANCEL
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False


@pytest.mark.parametrize(
    ("value", "method"),
    [
        (_FAST_INPUT, RPCMethod.START_FAST_RESEARCH),
        (_DEEP_INPUT, RPCMethod.START_DEEP_RESEARCH),
    ],
)
@pytest.mark.asyncio
async def test_keyed_row_pre_dispatch_expiry_names_the_native_the_input_selected(
    value: ResearchStartInput, method: RPCMethod
) -> None:
    """A keyed spec picks the native from the input, so the expiry can name it."""
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(RESEARCH_START_DEF, value, deadline=deadline)

    assert executor.calls == []
    assert caught.value.operation is Operation.RESEARCH_START
    assert caught.value.dispatched is False
    assert caught.value.outcome_unknown is False
    assert caught.value.diagnostics == {
        "timeout": 5.0,
        "remaining": 0.0,
        "timeout_seconds": 5.0,
        "method_id": method.value,
    }
