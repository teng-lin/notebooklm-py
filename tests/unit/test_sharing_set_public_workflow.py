"""P9.2-5 service-owned ``sharing.set_public`` workflow characterization."""

from __future__ import annotations

import asyncio
import inspect
from types import MappingProxyType
from typing import Any

import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS, WEB_CALL_POLICY_BINDINGS
from scripts.audit_operation_catalog import derive_workflow_natives

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
)
from notebooklm._backend_compat import project_backend_error
from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._operations import Operation
from notebooklm._records import (
    SHARING_GET_DEF,
    SHARING_MUTATE_DEF,
    SHARING_SET_PUBLIC_DEF,
    ShareAccessLevel,
    ShareStatusRecord,
    ShareViewScope,
    SharingGetInput,
    SharingGetResult,
    SharingMutateInput,
    SharingMutateResult,
    SharingSetPublicInput,
    SharingVisibility,
)
from notebooklm._sharing import SharingAPI
from notebooklm._sharing_service import SharingService
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import ShareAccess, ShareViewLevel
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
_DONE = SharingMutateResult()


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(SHARING_MUTATE_DEF, _DONE)
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


def test_set_public_is_service_owned_with_the_exact_leaf_conjunction() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.SHARING_SET_PUBLIC]
    assert binding.service_owned is True
    assert binding.is_supported is False
    assert binding.row is None
    assert Operation.SHARING_SET_PUBLIC in WEB_SERVICE_OWNED_OPERATIONS
    assert Operation.SHARING_SET_PUBLIC not in WEB_CALL_POLICY_BINDINGS

    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SHARING_SET_PUBLIC]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.SHARING_MUTATE, frozenset({None})),
        (Operation.SHARING_GET, frozenset({None})),
    ]
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.SHARE_NOTEBOOK, None),
        (RPCMethod.GET_SHARE_STATUS, None),
    }


@pytest.mark.asyncio
async def test_backend_refuses_direct_set_public_invocation() -> None:
    backend = build_web_backend(_RecordingExecutor())
    assert backend.capabilities.supports(Operation.SHARING_SET_PUBLIC) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            SHARING_SET_PUBLIC_DEF,
            SharingSetPublicInput(_NB, True),
            deadline=None,
        )


@pytest.mark.asyncio
async def test_visibility_workflow_sequences_the_closed_mutation_then_readback() -> None:
    backend = _backend()
    status = await _service(backend).set_public(_NB, True)

    assert _ops(backend) == [Operation.SHARING_MUTATE, Operation.SHARING_GET]
    assert backend.invocations[0].value == SharingMutateInput(_NB, SharingVisibility(True))
    assert backend.invocations[1].value == SharingGetInput(_NB)
    assert status.notebook_id == _NB
    assert status.is_public is True
    # Neutral record vocabulary (P10 R6.3 / invariant I1). The public
    # ``ShareAccess`` / ``ShareViewLevel`` projection of the same workflow is
    # asserted below, where ``SharingAPI`` now performs it.
    assert status.access is ShareAccessLevel.ANYONE_WITH_LINK
    assert status.view_level is ShareViewScope.FULL_NOTEBOOK
    assert status.max_individuals_share_limit == 1000
    assert status.is_public_sharing_allowed is True


@pytest.mark.asyncio
async def test_public_facade_preserves_both_native_payloads_and_runtime_kwargs() -> None:
    payload = [[], [True], 1000, True]
    executor = _RecordingExecutor(None, payload)
    api = SharingAPI(_backend=build_web_backend(executor))

    status = await api.set_public(_NB, True)

    assert status.notebook_id == _NB
    assert status.is_public is True
    assert status.access is ShareAccess.ANYONE_WITH_LINK
    assert status.view_level is ShareViewLevel.FULL_NOTEBOOK
    assert status.max_individuals_share_limit == 1000
    assert status.is_public_sharing_allowed is True

    mutate, readback = executor.calls
    assert mutate.method is RPCMethod.SHARE_NOTEBOOK
    assert mutate.params == [[[_NB, None, [1], [1, ""]]], 1, None, [2]]
    from notebooklm._web.codec.sharing import build_share_visibility_params

    assert mutate.params == build_share_visibility_params(_NB, True)
    assert mutate.kwargs["source_path"] == f"/notebook/{_NB}"
    assert mutate.kwargs["allow_null"] is True
    assert mutate.kwargs["raise_on_null_status"] is False
    assert mutate.kwargs["disable_internal_retries"] is False
    assert mutate.kwargs["operation_variant"] is None
    assert readback.method is RPCMethod.GET_SHARE_STATUS
    assert readback.params == [_NB, [2]]
    assert readback.kwargs["source_path"] == f"/notebook/{_NB}"
    assert readback.kwargs["allow_null"] is False
    assert readback.kwargs["raise_on_null_status"] is False
    assert readback.kwargs["disable_internal_retries"] is False
    assert readback.kwargs["operation_variant"] is None


@pytest.mark.asyncio
async def test_all_leaves_are_required_before_the_first_side_effect() -> None:
    backend = RecordingBackend()
    backend.set_result(SHARING_MUTATE_DEF, _DONE)
    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).set_public(_NB, True)
    assert caught.value.operation is Operation.SHARING_GET
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_one_minted_deadline_identity_covers_both_leaves() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)
    await _service(backend, factory).set_public(_NB, True)
    first, second = [invocation.deadline for invocation in backend.invocations]
    assert isinstance(first, RuntimeDeadline)
    assert second is first
    assert first.timeout == 30.0


@pytest.mark.asyncio
async def test_explicit_deadline_is_not_replaced_and_no_factory_keeps_none() -> None:
    backend = _backend()
    deadline = RuntimeDeadline(timeout=20.0, started_at=10.0, monotonic=lambda: 11.0)
    factory = RuntimeDeadlineFactory(lambda: pytest.fail("factory called"))
    await _service(backend, factory).set_public(_NB, True, deadline=deadline)
    assert all(invocation.deadline is deadline for invocation in backend.invocations)

    backend = _backend()
    await _service(backend).set_public(_NB, False)
    assert all(invocation.deadline is None for invocation in backend.invocations)


@pytest.mark.parametrize(
    "reason",
    [BackendErrorReason.SERVER, BackendErrorReason.NETWORK, BackendErrorReason.RATE_LIMIT],
)
@pytest.mark.asyncio
async def test_mutation_failures_rebind_to_the_workflow_without_losing_evidence(
    reason: BackendErrorReason,
) -> None:
    backend = RecordingBackend()
    backend.set_sequence(
        SHARING_MUTATE_DEF,
        [
            scripted_error(
                reason,
                operation=Operation.SHARING_MUTATE,
                dispatched=True,
                diagnostics={"method_id": RPCMethod.SHARE_NOTEBOOK.value},
            )
        ],
    )
    backend.set_result(SHARING_GET_DEF, _READ)
    with pytest.raises(BackendError) as caught:
        await _service(backend).set_public(_NB, True)

    error = caught.value
    assert error.operation is Operation.SHARING_SET_PUBLIC
    assert error.reason is reason
    assert error.dispatched is True
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.SHARE_NOTEBOOK.value
    assert error.diagnostics["leaf_operation"] is Operation.SHARING_MUTATE
    assert _ops(backend) == [Operation.SHARING_MUTATE]


@pytest.mark.asyncio
async def test_first_write_pre_dispatch_expiry_stays_confirmed() -> None:
    backend = RecordingBackend()
    backend.set_sequence(
        SHARING_MUTATE_DEF,
        [
            BackendDeadlineExceededError(
                Operation.SHARING_MUTATE,
                diagnostics=MappingProxyType({"method_id": RPCMethod.SHARE_NOTEBOOK.value}),
                dispatched=False,
            )
        ],
    )
    backend.set_result(SHARING_GET_DEF, _READ)
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).set_public(_NB, True)
    error = caught.value
    assert error.operation is Operation.SHARING_SET_PUBLIC
    assert error.message == "sharing.set_public exceeded its deadline"
    assert error.outcome_unknown is False
    assert error.dispatched is False


@pytest.mark.asyncio
async def test_dispatched_write_timeout_keeps_the_leaf_uncertainty() -> None:
    backend = RecordingBackend()
    backend.set_sequence(
        SHARING_MUTATE_DEF,
        [
            BackendDeadlineExceededError(
                Operation.SHARING_MUTATE,
                outcome_unknown=True,
                diagnostics=MappingProxyType({"method_id": RPCMethod.SHARE_NOTEBOOK.value}),
                dispatched=True,
            )
        ],
    )
    backend.set_result(SHARING_GET_DEF, _READ)
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).set_public(_NB, True)
    assert caught.value.operation is Operation.SHARING_SET_PUBLIC
    assert caught.value.outcome_unknown is True
    assert caught.value.dispatched is True


@pytest.mark.asyncio
async def test_readback_deadline_after_a_successful_write_is_unconfirmed() -> None:
    backend = RecordingBackend()
    backend.set_result(SHARING_MUTATE_DEF, _DONE)
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
        await _service(backend).set_public(_NB, True)

    error = caught.value
    assert _ops(backend) == [Operation.SHARING_MUTATE, Operation.SHARING_GET]
    assert error.operation is Operation.SHARING_SET_PUBLIC
    assert error.message == "sharing.set_public exceeded its deadline"
    assert error.outcome_unknown is True
    assert error.dispatched is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.GET_SHARE_STATUS.value
    assert error.diagnostics["leaf_operation"] is Operation.SHARING_GET
    public = project_backend_error(error)
    assert isinstance(public, RPCTimeoutError)
    assert public.unconfirmed is True


@pytest.mark.asyncio
async def test_readback_server_error_rebinds_without_inventing_uncertainty() -> None:
    backend = RecordingBackend()
    backend.set_result(SHARING_MUTATE_DEF, _DONE)
    backend.set_sequence(
        SHARING_GET_DEF,
        [
            scripted_error(
                BackendErrorReason.SERVER,
                operation=Operation.SHARING_GET,
                dispatched=True,
                diagnostics={"method_id": RPCMethod.GET_SHARE_STATUS.value},
            )
        ],
    )
    with pytest.raises(BackendError) as caught:
        await _service(backend).set_public(_NB, True)
    assert caught.value.operation is Operation.SHARING_SET_PUBLIC
    assert caught.value.reason is BackendErrorReason.SERVER
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
    backend.set_result(SHARING_MUTATE_DEF, _DONE)
    backend.set_result(SHARING_GET_DEF, _READ)
    with pytest.raises(asyncio.CancelledError):
        await _service(backend).set_public(_NB, True)


def test_label_allocate_decoder_gets_method_id_from_its_binding_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The codec body has no primitive-specific ``RPCMethod`` authority."""
    from notebooklm._records import LabelAllocateInput, LabelKind
    from notebooklm._web.bindings import primitives
    from notebooklm._web.codec import labels as label_codec

    source = inspect.getsource(label_codec.decode_label_allocate)
    assert "RPCMethod" not in source
    captured: dict[str, object] = {}

    def decode_echo(data: object, *, notebook_id: str, method_id: str):
        captured.update(data=data, notebook_id=notebook_id, method_id=method_id)
        return ()

    monkeypatch.setattr(label_codec, "decode_label_create_echo", decode_echo)
    result = primitives.LABEL_ALLOCATE.decode(
        LabelAllocateInput(LabelKind.SOURCE_LABEL, "Name", notebook_id=_NB),
        [["row"]],
    )
    assert result.labels == ()
    assert captured == {
        "data": [["row"]],
        "notebook_id": _NB,
        "method_id": RPCMethod.CREATE_LABEL.value,
    }


def test_native_failure_projector_still_preserves_the_public_server_error_text() -> None:
    error = BackendError(
        "server failed",
        Operation.SHARING_SET_PUBLIC,
        diagnostics=MappingProxyType(
            {
                "method_id": RPCMethod.SHARE_NOTEBOOK.value,
                "rpc_code": 13,
                "original_message": "server failed",
            }
        ),
        reason=BackendErrorReason.SERVER,
    )
    public = project_backend_error(error)
    assert isinstance(public, ServerError)
    assert str(public) == "server failed"
    assert public.method_id == RPCMethod.SHARE_NOTEBOOK.value
