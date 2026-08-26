"""P9.2-6 service-owned ``sharing.update_users`` workflow characterization."""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import Any

import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS, WEB_CALL_POLICY_BINDINGS
from scripts.audit_operation_catalog import derive_workflow_natives

from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._semantic.backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
)
from notebooklm._semantic.compat import project_backend_error
from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import (
    SHARING_GET_DEF,
    SHARING_MUTATE_DEF,
    SHARING_UPDATE_USERS_DEF,
    ShareAccessLevel,
    SharePermissionLevel,
    ShareStatusRecord,
    ShareViewScope,
    SharingGetInput,
    SharingGetResult,
    SharingGrants,
    SharingMutateInput,
    SharingMutateResult,
    SharingUpdateUsersInput,
    SharingUserGrant,
)
from notebooklm._semantic.services.sharing import SharingService
from notebooklm._sharing import SharingAPI
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import RPCTimeoutError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import SharePermission
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


def test_update_users_is_service_owned_with_the_exact_leaf_conjunction() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.SHARING_UPDATE_USERS]
    assert binding.service_owned is True
    assert binding.is_supported is False
    assert binding.row is None
    assert Operation.SHARING_UPDATE_USERS in WEB_SERVICE_OWNED_OPERATIONS
    assert Operation.SHARING_UPDATE_USERS not in WEB_CALL_POLICY_BINDINGS

    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SHARING_UPDATE_USERS]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.SHARING_MUTATE, frozenset({None})),
        (Operation.SHARING_GET, frozenset({None})),
    ]
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.SHARE_NOTEBOOK, None),
        (RPCMethod.GET_SHARE_STATUS, None),
    }


@pytest.mark.asyncio
async def test_backend_refuses_direct_update_users_invocation() -> None:
    backend = build_web_backend(_RecordingExecutor())
    assert backend.capabilities.supports(Operation.SHARING_UPDATE_USERS) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            SHARING_UPDATE_USERS_DEF,
            SharingUpdateUsersInput(
                _NB,
                (SharingUserGrant("reader@example.com", SharePermissionLevel.VIEWER),),
            ),
            deadline=None,
        )


@pytest.mark.asyncio
async def test_grant_workflow_sequences_the_closed_mutation_then_readback() -> None:
    backend = _backend()
    status = await _service(backend).set_users(
        _NB,
        [
            ("viewer@example.com", SharePermissionLevel.VIEWER),
            ("editor@example.com", SharePermissionLevel.EDITOR),
        ],
        notify=False,
        welcome_message="Welcome",
    )

    grants = (
        SharingUserGrant("viewer@example.com", SharePermissionLevel.VIEWER),
        SharingUserGrant("editor@example.com", SharePermissionLevel.EDITOR),
    )
    assert _ops(backend) == [Operation.SHARING_MUTATE, Operation.SHARING_GET]
    assert backend.invocations[0].value == SharingMutateInput(
        _NB,
        SharingGrants(grants, notify=False, welcome_message="Welcome"),
    )
    assert backend.invocations[1].value == SharingGetInput(_NB)
    assert status.notebook_id == _NB
    assert status.max_individuals_share_limit == 1000
    assert status.is_public_sharing_allowed is True


@pytest.mark.asyncio
async def test_remove_and_update_user_keep_their_exact_grant_contracts() -> None:
    backend = _backend()
    await _service(backend).remove_user(_NB, "gone@example.com")
    assert backend.invocations[0].value == SharingMutateInput(
        _NB,
        SharingGrants(
            (SharingUserGrant("gone@example.com", SharePermissionLevel.REMOVE),),
            notify=False,
        ),
    )

    backend = _backend()
    await _service(backend).update_user(
        _NB,
        "user@example.com",
        SharePermissionLevel.EDITOR,
    )
    assert backend.invocations[0].value == SharingMutateInput(
        _NB,
        SharingGrants(
            (SharingUserGrant("user@example.com", SharePermissionLevel.EDITOR),),
            notify=False,
        ),
    )


@pytest.mark.asyncio
async def test_public_facade_preserves_both_native_payloads_and_runtime_kwargs() -> None:
    executor = _RecordingExecutor(None, [[], [True], 1000, True])
    api = SharingAPI(_backend=build_web_backend(executor))

    await api.set_users(
        _NB,
        [("reader@example.com", SharePermission.VIEWER)],
        notify=False,
        welcome_message="Welcome",
    )

    mutate, readback = executor.calls
    assert mutate.method is RPCMethod.SHARE_NOTEBOOK
    assert mutate.params == [
        [[_NB, [["reader@example.com", None, SharePermission.VIEWER.value]], None, [0, "Welcome"]]],
        0,
        None,
        [2],
    ]
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
async def test_validation_and_leaf_support_fail_before_the_first_side_effect() -> None:
    backend = _backend()
    with pytest.raises(ValueError, match="at least one"):
        await _service(backend).set_users(_NB, [])
    assert backend.invocations == []

    backend = RecordingBackend()
    backend.set_result(SHARING_MUTATE_DEF, _DONE)
    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).set_users(
            _NB,
            [("reader@example.com", SharePermissionLevel.VIEWER)],
        )
    assert caught.value.operation is Operation.SHARING_GET
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_one_deadline_identity_covers_both_leaves() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)
    await _service(backend, factory).set_users(
        _NB,
        [("reader@example.com", SharePermissionLevel.VIEWER)],
    )
    first, second = [invocation.deadline for invocation in backend.invocations]
    assert isinstance(first, RuntimeDeadline)
    assert second is first
    assert first.timeout == 30.0

    backend = _backend()
    explicit = RuntimeDeadline(timeout=20.0, started_at=10.0, monotonic=lambda: 11.0)
    await _service(backend, factory).remove_user(_NB, "gone@example.com", deadline=explicit)
    assert all(invocation.deadline is explicit for invocation in backend.invocations)


@pytest.mark.parametrize(
    "reason",
    [BackendErrorReason.SERVER, BackendErrorReason.NETWORK, BackendErrorReason.RATE_LIMIT],
)
@pytest.mark.asyncio
async def test_write_failures_rebind_without_losing_leaf_evidence(
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
        await _service(backend).set_users(
            _NB,
            [("reader@example.com", SharePermissionLevel.VIEWER)],
        )
    error = caught.value
    assert error.operation is Operation.SHARING_UPDATE_USERS
    assert error.reason is reason
    assert error.dispatched is True
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.SHARE_NOTEBOOK.value
    assert error.diagnostics["leaf_operation"] is Operation.SHARING_MUTATE
    assert _ops(backend) == [Operation.SHARING_MUTATE]


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
        await _service(backend).remove_user(_NB, "gone@example.com")

    error = caught.value
    assert error.operation is Operation.SHARING_UPDATE_USERS
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
    backend.set_result(SHARING_MUTATE_DEF, _DONE)
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
        await _service(backend).remove_user(_NB, "gone@example.com")
    assert caught.value.operation is Operation.SHARING_UPDATE_USERS
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
        await _service(backend).remove_user(_NB, "gone@example.com")
