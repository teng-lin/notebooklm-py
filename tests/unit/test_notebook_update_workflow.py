"""P9.2-11: ``notebook.update`` is sequenced from PATCH and required GET leaves."""

from __future__ import annotations

import asyncio
from types import MappingProxyType

import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS
from scripts.audit_operation_catalog import derive_workflow_natives

from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._semantic.backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
)
from notebooklm._semantic.compat import project_backend_error
from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import (
    NOTEBOOK_GET_DEF,
    NOTEBOOK_PATCH_DEF,
    NOTEBOOK_UPDATE_DEF,
    NotebookGetInput,
    NotebookGetResult,
    NotebookPatchInput,
    NotebookPatchResult,
    NotebookRecord,
    NotebookUpdateInput,
)
from notebooklm._semantic.services.notebook_mutation import NotebookMutationService
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import ClientError, NotebookNotFoundError, RPCTimeoutError
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import BackendInvocation, RecordingBackend, scripted_error
from tests._fixtures.web_backend import build_web_backend

_ID = "nb-1"
_UPDATED = NotebookRecord(_ID, "Renamed", emoji="📖")


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_PATCH_DEF, NotebookPatchResult())
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(_UPDATED))
    return backend


def _service(
    backend: RecordingBackend,
    factory: RuntimeDeadlineFactory | None = None,
) -> NotebookMutationService:
    return NotebookMutationService(backend, deadline_factory=factory)


def _ops(backend: RecordingBackend) -> list[Operation]:
    return [invocation.operation for invocation in backend.invocations]


def test_update_is_service_owned_and_declares_the_exact_leaf_conjunction() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.NOTEBOOK_UPDATE]
    assert binding.service_owned is True
    assert binding.is_supported is False
    assert binding.row is None
    assert Operation.NOTEBOOK_UPDATE in WEB_SERVICE_OWNED_OPERATIONS
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.NOTEBOOK_UPDATE]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.NOTEBOOK_PATCH, frozenset({None})),
        (Operation.NOTEBOOK_GET, frozenset({None})),
    ]
    assert derive_workflow_natives(workflow) == {
        (native.method, native.variant) for native in workflow.native_bindings
    }
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.RENAME_NOTEBOOK, None),
        (RPCMethod.GET_NOTEBOOK, None),
    }


@pytest.mark.asyncio
async def test_backend_refuses_direct_update_invocation() -> None:
    backend = build_web_backend(type("Executor", (), {"rpc_call": None})())
    assert backend.capabilities.supports(Operation.NOTEBOOK_UPDATE) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            NOTEBOOK_UPDATE_DEF,
            NotebookUpdateInput(_ID, title="Renamed"),
            deadline=None,
        )


@pytest.mark.asyncio
async def test_update_records_patch_then_required_readback_with_identical_deadline() -> None:
    backend = _backend()
    deadline = RuntimeDeadline(timeout=10.0, started_at=20.0, monotonic=lambda: 21.0)

    updated = await _service(backend).update(
        _ID,
        title="Renamed",
        emoji="📖",
        deadline=deadline,
    )

    assert (updated.id, updated.title, updated.emoji) == (_ID, "Renamed", "📖")
    assert backend.invocations == [
        BackendInvocation(
            Operation.NOTEBOOK_PATCH,
            NotebookPatchInput(_ID, title="Renamed", emoji="📖"),
            deadline,
        ),
        BackendInvocation(
            Operation.NOTEBOOK_GET,
            NotebookGetInput(_ID, require_notebook=True),
            deadline,
        ),
    ]


@pytest.mark.asyncio
async def test_unsupported_readback_leaf_fails_before_the_patch() -> None:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_PATCH_DEF, NotebookPatchResult())

    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).update(_ID, title="Renamed")

    assert caught.value.operation is Operation.NOTEBOOK_GET
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_factory_mints_exactly_one_deadline_for_both_leaves() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)

    await _service(backend, factory).update(_ID, title="Renamed")

    first, second = (invocation.deadline for invocation in backend.invocations)
    assert isinstance(first, RuntimeDeadline)
    assert second is first
    assert first.timeout == 30.0


@pytest.mark.asyncio
async def test_explicit_deadline_is_not_replaced_and_no_factory_keeps_none() -> None:
    explicit = RuntimeDeadline(timeout=20.0, started_at=50.0, monotonic=lambda: 55.0)
    backend = _backend()
    factory = RuntimeDeadlineFactory(lambda: pytest.fail("factory was called"))
    await _service(backend, factory).update(_ID, title="Renamed", deadline=explicit)
    assert all(call.deadline is explicit for call in backend.invocations)

    backend = _backend()
    await _service(backend).update(_ID, title="Renamed")
    assert all(call.deadline is None for call in backend.invocations)


@pytest.mark.asyncio
async def test_empty_required_readback_maps_to_legacy_not_found_diagnostics() -> None:
    backend = _backend()
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(None))

    with pytest.raises(BackendError) as caught:
        await _service(backend).update(_ID, title="Renamed")

    error = caught.value
    assert error.operation is Operation.NOTEBOOK_UPDATE
    assert error.reason is BackendErrorReason.NOTEBOOK_NOT_FOUND
    assert error.message == f"Notebook not found: {_ID}"
    assert dict(error.diagnostics or {}) == {
        "notebook_id": _ID,
        "leaf_operation": Operation.NOTEBOOK_GET,
    }
    projected = project_backend_error(error)
    assert isinstance(projected, NotebookNotFoundError)
    assert projected.notebook_id == _ID
    assert projected.method_id == RPCMethod.GET_NOTEBOOK.value


@pytest.mark.asyncio
async def test_neutral_required_get_miss_preserves_rpc_evidence_and_public_cause() -> None:
    native = ClientError(
        "not found",
        status_code=404,
        method_id=RPCMethod.GET_NOTEBOOK.value,
        raw_response="scrubbed response",
        rpc_code=5,
    )
    leaf = BackendError(
        "not found",
        operation=Operation.NOTEBOOK_GET,
        reason=BackendErrorReason.NOT_FOUND,
        diagnostics=MappingProxyType(
            {
                "notebook_id": _ID,
                "status_code": 404,
                "method_id": RPCMethod.GET_NOTEBOOK.value,
                "raw_response": "scrubbed response",
                "rpc_code": 5,
                "found_ids": None,
                "detail": "not found",
                "original_message": "not found",
            }
        ),
    )
    try:
        raise leaf from native
    except BackendError as caused:
        leaf = caused
    backend = _backend()
    backend.set_error(NOTEBOOK_GET_DEF, leaf)

    with pytest.raises(BackendError) as caught:
        await _service(backend).update(_ID, title="Renamed")

    error = caught.value
    assert error.operation is Operation.NOTEBOOK_UPDATE
    assert error.reason is BackendErrorReason.NOTEBOOK_NOT_FOUND
    assert error.__cause__ is native
    projected = project_backend_error(error)
    assert isinstance(projected, NotebookNotFoundError)
    assert isinstance(projected.__cause__, ClientError)
    assert projected.__cause__.rpc_code == 5
    assert projected.__cause__.raw_response == "scrubbed response"


@pytest.mark.parametrize(
    ("failing_leaf", "expected_ops"),
    [
        (Operation.NOTEBOOK_PATCH, [Operation.NOTEBOOK_PATCH]),
        (Operation.NOTEBOOK_GET, [Operation.NOTEBOOK_PATCH, Operation.NOTEBOOK_GET]),
    ],
)
@pytest.mark.asyncio
async def test_leaf_errors_are_rebound_to_update(
    failing_leaf: Operation,
    expected_ops: list[Operation],
) -> None:
    backend = _backend()
    error = scripted_error(BackendErrorReason.SERVER, operation=failing_leaf)
    if failing_leaf is Operation.NOTEBOOK_PATCH:
        backend.set_error(NOTEBOOK_PATCH_DEF, error)
    else:
        backend.set_error(NOTEBOOK_GET_DEF, error)

    with pytest.raises(BackendError) as caught:
        await _service(backend).update(_ID, title="Renamed")

    assert _ops(backend) == expected_ops
    assert caught.value.operation is Operation.NOTEBOOK_UPDATE
    assert caught.value.reason is BackendErrorReason.SERVER
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["leaf_operation"] is failing_leaf


@pytest.mark.parametrize(
    ("failing_leaf", "dispatched", "leaf_unknown", "workflow_unknown"),
    [
        (Operation.NOTEBOOK_PATCH, False, False, False),
        (Operation.NOTEBOOK_PATCH, True, True, True),
        (Operation.NOTEBOOK_GET, False, False, True),
        (Operation.NOTEBOOK_GET, True, True, True),
    ],
)
@pytest.mark.asyncio
async def test_deadline_commit_uncertainty_truth_table(
    failing_leaf: Operation,
    dispatched: bool,
    leaf_unknown: bool,
    workflow_unknown: bool,
) -> None:
    backend = _backend()
    error = scripted_error(
        BackendErrorReason.TIMEOUT,
        operation=failing_leaf,
        dispatched=dispatched,
        outcome_unknown=leaf_unknown,
        diagnostics={"method_id": "blocked", "timeout": 1.0, "remaining": 0.0},
    )
    if failing_leaf is Operation.NOTEBOOK_PATCH:
        backend.set_error(NOTEBOOK_PATCH_DEF, error)
    else:
        backend.set_error(NOTEBOOK_GET_DEF, error)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).update(_ID, title="Renamed")

    assert caught.value.operation is Operation.NOTEBOOK_UPDATE
    assert caught.value.outcome_unknown is workflow_unknown
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["leaf_operation"] is failing_leaf
    projected = project_backend_error(caught.value)
    assert isinstance(projected, RPCTimeoutError)
    assert getattr(projected, "unconfirmed", False) is workflow_unknown


@pytest.mark.asyncio
async def test_contract_error_is_rebound_and_cancellation_passes_through() -> None:
    backend = _backend()
    backend.set_error(
        NOTEBOOK_PATCH_DEF,
        BackendContractError("bad patch", operation=Operation.NOTEBOOK_PATCH),
    )
    with pytest.raises(BackendContractError) as caught:
        await _service(backend).update(_ID, title="Renamed")
    assert caught.value.operation is Operation.NOTEBOOK_UPDATE
    assert caught.value.message == "bad patch"

    class CancellingBackend(RecordingBackend):
        async def invoke(self, *_args: object, **_kwargs: object) -> object:  # type: ignore[override]
            raise asyncio.CancelledError

    cancelling = CancellingBackend()
    cancelling.set_result(NOTEBOOK_PATCH_DEF, NotebookPatchResult())
    cancelling.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(_UPDATED))
    with pytest.raises(asyncio.CancelledError):
        await _service(cancelling).update(_ID, title="Renamed")
