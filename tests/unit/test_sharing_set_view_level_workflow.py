"""P9.2-7 service-owned ``sharing.set_view_level`` workflow characterization."""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import Any

import pytest

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
)
from notebooklm._backend_compat import project_backend_error
from notebooklm._binding import CodecPayload
from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._operations import Operation
from notebooklm._records import (
    SHARING_GET_DEF,
    SHARING_PATCH_VIEW_LEVEL_DEF,
    SHARING_SET_VIEW_LEVEL_DEF,
    ShareAccessLevel,
    ShareStatusRecord,
    ShareViewScope,
    SharingGetInput,
    SharingGetResult,
    SharingPatchViewLevelInput,
    SharingPatchViewLevelResult,
    SharingSetViewLevelInput,
)
from notebooklm._sharing import SharingAPI
from notebooklm._sharing_service import SharingService
from notebooklm._web.bindings import primitives
from notebooklm._web.codec import sharing as sharing_codec
from notebooklm._web.policy import (
    SERVICE_OWNED_WORKFLOW_BINDINGS,
    WEB_CALL_POLICY_BINDINGS,
    derive_workflow_natives,
)
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import RPCTimeoutError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import ShareViewLevel
from tests._fixtures.recording_backend import RecordingBackend, scripted_error
from tests._fixtures.web_backend import build_web_backend

_NB = "nb_123"
_STATUS = ShareStatusRecord(
    notebook_id=_NB,
    is_public=True,
    access=ShareAccessLevel.ANYONE_WITH_LINK,
    view_level=ShareViewScope.FULL_NOTEBOOK,
    max_individuals_share_limit=1000,
    is_public_sharing_allowed=True,
)
_READ = SharingGetResult(_STATUS)
_DONE = SharingPatchViewLevelResult()


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(SHARING_PATCH_VIEW_LEVEL_DEF, _DONE)
    backend.set_result(SHARING_GET_DEF, _READ)
    return backend


def _service(
    backend: RecordingBackend,
    factory: RuntimeDeadlineFactory | None = None,
) -> SharingService:
    return SharingService(backend, deadline_factory=factory)


def _ops(backend: RecordingBackend) -> list[Operation]:
    return [invocation.operation for invocation in backend.invocations]


class _Call:
    def __init__(self, method: RPCMethod, params: list[Any], kwargs: dict[str, Any]) -> None:
        self.method = method
        self.params = params
        self.kwargs = kwargs


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method, params, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_view_workflow_is_service_owned_with_the_exact_leaf_conjunction() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.SHARING_SET_VIEW_LEVEL]
    assert binding.service_owned is True
    assert binding.is_supported is False
    assert binding.row is None
    assert Operation.SHARING_SET_VIEW_LEVEL in WEB_SERVICE_OWNED_OPERATIONS
    assert Operation.SHARING_SET_VIEW_LEVEL not in WEB_CALL_POLICY_BINDINGS

    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SHARING_SET_VIEW_LEVEL]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.SHARING_PATCH_VIEW_LEVEL, frozenset({None})),
        (Operation.SHARING_GET, frozenset({None})),
    ]
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.RENAME_NOTEBOOK, None),
        (RPCMethod.GET_SHARE_STATUS, None),
    }


def test_patch_view_level_is_a_constant_single_native_codec_row() -> None:
    row = primitives.SHARING_PATCH_VIEW_LEVEL
    assert row.definition is SHARING_PATCH_VIEW_LEVEL_DEF
    assert row.native.is_constant
    assert row.native.select(None).method is RPCMethod.RENAME_NOTEBOOK
    assert WEB_OPERATION_REGISTRY[Operation.SHARING_PATCH_VIEW_LEVEL].row is row
    value = SharingPatchViewLevelInput(_NB, ShareViewScope.CHAT_ONLY)
    assert row.encode(value) == CodecPayload(
        params=sharing_codec.build_share_view_level_params(_NB, ShareViewScope.CHAT_ONLY),
        source_path=f"/notebook/{_NB}",
        allow_null=True,
    )
    assert row.decode(value, None) == SharingPatchViewLevelResult()


@pytest.mark.asyncio
async def test_backend_refuses_direct_view_workflow_but_executes_the_patch_leaf() -> None:
    backend = build_web_backend(_RecordingExecutor(None))
    assert backend.capabilities.supports(Operation.SHARING_SET_VIEW_LEVEL) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            SHARING_SET_VIEW_LEVEL_DEF,
            SharingSetViewLevelInput(_NB, ShareViewScope.CHAT_ONLY),
            deadline=None,
        )
    result = await backend.invoke(
        SHARING_PATCH_VIEW_LEVEL_DEF,
        SharingPatchViewLevelInput(_NB, ShareViewScope.CHAT_ONLY),
        deadline=None,
    )
    assert result == SharingPatchViewLevelResult()


@pytest.mark.asyncio
async def test_workflow_sequences_patch_then_read_and_replaces_the_missing_view_level() -> None:
    backend = _backend()
    status = await _service(backend).set_view_level(_NB, ShareViewScope.CHAT_ONLY)

    assert _ops(backend) == [Operation.SHARING_PATCH_VIEW_LEVEL, Operation.SHARING_GET]
    assert backend.invocations[0].value == SharingPatchViewLevelInput(
        _NB,
        ShareViewScope.CHAT_ONLY,
    )
    assert backend.invocations[1].value == SharingGetInput(_NB)
    assert status.view_level is ShareViewLevel.CHAT_ONLY
    assert status.max_individuals_share_limit == 1000
    assert status.is_public_sharing_allowed is True


@pytest.mark.asyncio
async def test_public_facade_preserves_both_native_payloads_and_runtime_kwargs() -> None:
    executor = _RecordingExecutor(None, [[], [True], 1000, True])
    api = SharingAPI(_backend=build_web_backend(executor))

    status = await api.set_view_level(_NB, ShareViewLevel.CHAT_ONLY)

    patch, readback = executor.calls
    assert patch.method is RPCMethod.RENAME_NOTEBOOK
    assert patch.params == [
        _NB,
        [[None, None, None, None, None, None, None, None, [[ShareViewLevel.CHAT_ONLY.value]]]],
    ]
    assert patch.kwargs["source_path"] == f"/notebook/{_NB}"
    assert patch.kwargs["allow_null"] is True
    assert patch.kwargs["raise_on_null_status"] is False
    assert patch.kwargs["disable_internal_retries"] is False
    assert patch.kwargs["operation_variant"] is None
    assert readback.method is RPCMethod.GET_SHARE_STATUS
    assert readback.params == [_NB, [2]]
    assert readback.kwargs["source_path"] == f"/notebook/{_NB}"
    assert readback.kwargs["allow_null"] is False
    assert readback.kwargs["raise_on_null_status"] is False
    assert readback.kwargs["disable_internal_retries"] is False
    assert readback.kwargs["operation_variant"] is None
    assert status.view_level is ShareViewLevel.CHAT_ONLY


@pytest.mark.asyncio
async def test_all_leaves_are_required_before_the_first_side_effect() -> None:
    backend = RecordingBackend()
    backend.set_result(SHARING_PATCH_VIEW_LEVEL_DEF, _DONE)
    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).set_view_level(_NB, ShareViewScope.CHAT_ONLY)
    assert caught.value.operation is Operation.SHARING_GET
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_one_deadline_identity_covers_both_leaves() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)
    await _service(backend, factory).set_view_level(_NB, ShareViewScope.CHAT_ONLY)
    first, second = [invocation.deadline for invocation in backend.invocations]
    assert isinstance(first, RuntimeDeadline)
    assert second is first
    assert first.timeout == 30.0

    backend = _backend()
    explicit = RuntimeDeadline(timeout=20.0, started_at=10.0, monotonic=lambda: 11.0)
    await _service(backend, factory).set_view_level(
        _NB,
        ShareViewScope.CHAT_ONLY,
        deadline=explicit,
    )
    assert all(invocation.deadline is explicit for invocation in backend.invocations)


@pytest.mark.parametrize(
    "reason",
    [BackendErrorReason.SERVER, BackendErrorReason.NETWORK, BackendErrorReason.RATE_LIMIT],
)
@pytest.mark.asyncio
async def test_patch_failures_rebind_without_losing_leaf_evidence(
    reason: BackendErrorReason,
) -> None:
    backend = RecordingBackend()
    backend.set_sequence(
        SHARING_PATCH_VIEW_LEVEL_DEF,
        [
            scripted_error(
                reason,
                operation=Operation.SHARING_PATCH_VIEW_LEVEL,
                dispatched=True,
                diagnostics={"method_id": RPCMethod.RENAME_NOTEBOOK.value},
            )
        ],
    )
    backend.set_result(SHARING_GET_DEF, _READ)
    with pytest.raises(BackendError) as caught:
        await _service(backend).set_view_level(_NB, ShareViewScope.CHAT_ONLY)

    error = caught.value
    assert error.operation is Operation.SHARING_SET_VIEW_LEVEL
    assert error.reason is reason
    assert error.dispatched is True
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.RENAME_NOTEBOOK.value
    assert error.diagnostics["leaf_operation"] is Operation.SHARING_PATCH_VIEW_LEVEL
    assert _ops(backend) == [Operation.SHARING_PATCH_VIEW_LEVEL]


@pytest.mark.asyncio
async def test_first_patch_pre_dispatch_expiry_stays_confirmed() -> None:
    backend = RecordingBackend()
    backend.set_sequence(
        SHARING_PATCH_VIEW_LEVEL_DEF,
        [
            BackendDeadlineExceededError(
                Operation.SHARING_PATCH_VIEW_LEVEL,
                diagnostics=MappingProxyType({"method_id": RPCMethod.RENAME_NOTEBOOK.value}),
                dispatched=False,
            )
        ],
    )
    backend.set_result(SHARING_GET_DEF, _READ)
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).set_view_level(_NB, ShareViewScope.CHAT_ONLY)
    assert caught.value.operation is Operation.SHARING_SET_VIEW_LEVEL
    assert caught.value.outcome_unknown is False
    assert caught.value.dispatched is False


@pytest.mark.asyncio
async def test_readback_deadline_after_a_successful_patch_is_unconfirmed() -> None:
    backend = RecordingBackend()
    backend.set_result(SHARING_PATCH_VIEW_LEVEL_DEF, _DONE)
    backend.set_sequence(
        SHARING_GET_DEF,
        [
            BackendDeadlineExceededError(
                Operation.SHARING_GET,
                diagnostics=MappingProxyType({"method_id": RPCMethod.GET_SHARE_STATUS.value}),
                dispatched=False,
            )
        ],
    )
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).set_view_level(_NB, ShareViewScope.CHAT_ONLY)

    error = caught.value
    assert error.operation is Operation.SHARING_SET_VIEW_LEVEL
    assert error.outcome_unknown is True
    assert error.dispatched is False
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is Operation.SHARING_GET
    public = project_backend_error(error)
    assert isinstance(public, RPCTimeoutError)
    assert public.unconfirmed is True


@pytest.mark.asyncio
async def test_readback_server_error_does_not_invent_uncertainty() -> None:
    backend = RecordingBackend()
    backend.set_result(SHARING_PATCH_VIEW_LEVEL_DEF, _DONE)
    backend.set_sequence(
        SHARING_GET_DEF,
        [
            scripted_error(
                BackendErrorReason.SERVER,
                operation=Operation.SHARING_GET,
                dispatched=True,
            )
        ],
    )
    with pytest.raises(BackendError) as caught:
        await _service(backend).set_view_level(_NB, ShareViewScope.CHAT_ONLY)
    assert caught.value.operation is Operation.SHARING_SET_VIEW_LEVEL
    assert caught.value.outcome_unknown is False
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["leaf_operation"] is Operation.SHARING_GET


@pytest.mark.asyncio
async def test_cancellation_propagates_without_error_rebinding() -> None:
    class _Cancelling(RecordingBackend):
        async def invoke(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
            del args, kwargs
            raise asyncio.CancelledError

    backend = _Cancelling()
    backend.set_result(SHARING_PATCH_VIEW_LEVEL_DEF, _DONE)
    backend.set_result(SHARING_GET_DEF, _READ)
    with pytest.raises(asyncio.CancelledError):
        await _service(backend).set_view_level(_NB, ShareViewScope.CHAT_ONLY)
