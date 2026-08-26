"""P9.2-8: ``label.create`` is a service-owned workflow sequenced from leaves.

``LabelSetService.create`` owns the source-label baseline, one
``label.allocate`` call, exact-id reconciliation of the allocation echo, one
workflow deadline and leaf-error rebinding. These tests replace the deleted
web handler with a comprehensive ``RecordingBackend`` workflow oracle and pin
the unchanged web primitive kwargs through the public facade.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS, WEB_CALL_POLICY_BINDINGS
from scripts.audit_operation_catalog import derive_workflow_natives

from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._labels import LabelsAPI
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
    LABEL_ALLOCATE_DEF,
    LABEL_CREATE_DEF,
    LABEL_LIST_DEF,
    LabelAllocateInput,
    LabelAllocateResult,
    LabelCreateInput,
    LabelKind,
    LabelListInput,
    LabelListResult,
    LabelRecord,
)
from notebooklm._semantic.services.label import LabelSetService
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import LabelError, RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend, scripted_error
from tests._fixtures.web_backend import build_web_backend

_NB = "nb_1"
_EXISTING = LabelRecord("l1", "Existing", LabelKind.SOURCE_LABEL, _NB)
_CREATED = LabelRecord("l2", "New", LabelKind.SOURCE_LABEL, _NB, emoji="U0001f4c4")
_BASELINE = LabelListResult(labels=(_EXISTING,))
_ECHO = LabelAllocateResult(labels=(_EXISTING, _CREATED))


def _service(backend: RecordingBackend, factory: RuntimeDeadlineFactory | None = None):
    return LabelSetService(backend, LabelKind.SOURCE_LABEL, deadline_factory=factory)


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(LABEL_LIST_DEF, _BASELINE)
    backend.set_result(LABEL_ALLOCATE_DEF, _ECHO)
    return backend


def _ops(backend: RecordingBackend) -> list[Operation]:
    return [invocation.operation for invocation in backend.invocations]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[RPCMethod, list[Any], dict[str, Any]]] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append((method, params, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


# --- disposition and policy ledger -------------------------------------------------


def test_label_create_is_service_owned_with_list_and_allocate_edges() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.LABEL_CREATE]
    assert binding.service_owned is True and binding.is_supported is False
    assert binding.row is None
    assert Operation.LABEL_CREATE in WEB_SERVICE_OWNED_OPERATIONS
    assert Operation.LABEL_CREATE not in WEB_CALL_POLICY_BINDINGS
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.LABEL_CREATE]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.LABEL_LIST, frozenset({None})),
        (Operation.LABEL_ALLOCATE, frozenset({None})),
    ]
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.LIST_LABELS, None),
        (RPCMethod.CREATE_LABEL, None),
    }
    assert derive_workflow_natives(workflow) == {
        (native.method, native.variant) for native in workflow.native_bindings
    }


@pytest.mark.asyncio
async def test_backend_refuses_the_workflow_directly() -> None:
    backend = build_web_backend(_RecordingExecutor())
    assert backend.capabilities.supports(Operation.LABEL_CREATE) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            LABEL_CREATE_DEF,
            LabelCreateInput(LabelKind.SOURCE_LABEL, "New", _NB),
            deadline=None,
        )


# --- workflow sequence and reconciliation ------------------------------------------


@pytest.mark.asyncio
async def test_create_snapshots_then_allocates_and_reconciles_by_exact_id() -> None:
    backend = _backend()
    created = await _service(backend).create("New", _NB, emoji="U0001f4c4")

    assert created is _CREATED
    assert _ops(backend) == [Operation.LABEL_LIST, Operation.LABEL_ALLOCATE]
    assert [invocation.value for invocation in backend.invocations] == [
        LabelListInput(LabelKind.SOURCE_LABEL, _NB),
        LabelAllocateInput(LabelKind.SOURCE_LABEL, "New", _NB, "U0001f4c4"),
    ]


@pytest.mark.asyncio
async def test_duplicate_names_never_replace_id_diff_attribution() -> None:
    same_name = LabelRecord("l3", "Existing", LabelKind.SOURCE_LABEL, _NB)
    backend = RecordingBackend()
    backend.set_result(LABEL_LIST_DEF, _BASELINE)
    backend.set_result(
        LABEL_ALLOCATE_DEF,
        LabelAllocateResult(labels=(_EXISTING, same_name)),
    )

    assert await _service(backend).create("Existing", _NB) is same_name


@pytest.mark.parametrize(
    ("after", "count"),
    [
        pytest.param((_EXISTING,), 0, id="no-new-id"),
        pytest.param(
            (
                _EXISTING,
                _CREATED,
                LabelRecord("l3", "New", LabelKind.SOURCE_LABEL, _NB),
            ),
            2,
            id="several-new-ids",
        ),
    ],
)
@pytest.mark.asyncio
async def test_ambiguous_echo_preserves_message_diagnostics_and_public_projection(
    after: tuple[LabelRecord, ...], count: int
) -> None:
    backend = RecordingBackend()
    backend.set_result(LABEL_LIST_DEF, _BASELINE)
    backend.set_result(LABEL_ALLOCATE_DEF, LabelAllocateResult(labels=after))

    with pytest.raises(BackendError) as caught:
        await _service(backend).create("New", _NB)

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.LABEL_CREATE
    assert error.reason is BackendErrorReason.LABEL_AMBIGUOUS_CREATE
    assert error.message == (
        f"create(name='New') expected exactly 1 new label, found {count} "
        "(concurrent label creation can cause this — retry from a fresh list)"
    )
    assert dict(error.diagnostics or {}) == {
        "label_kind": "source_label",
        "candidate_count": count,
        "name": "New",
    }
    projected = project_backend_error(error)
    assert type(projected) is LabelError
    assert str(projected) == error.message


@pytest.mark.asyncio
async def test_scope_contract_and_missing_leaf_fail_before_any_invocation() -> None:
    backend = _backend()
    with pytest.raises(BackendContractError, match="label.create requires a notebook scope"):
        await _service(backend).create("New")
    assert backend.invocations == []

    backend = RecordingBackend()
    backend.set_result(LABEL_LIST_DEF, _BASELINE)  # allocate deliberately unsupported
    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).create("New", _NB)
    assert caught.value.operation is Operation.LABEL_ALLOCATE
    assert backend.invocations == []


# --- deadline, uncertainty and cause identity --------------------------------------


@pytest.mark.asyncio
async def test_one_deadline_identity_covers_baseline_and_allocation() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)
    await _service(backend, factory).create("New", _NB)

    deadlines = [invocation.deadline for invocation in backend.invocations]
    assert len(deadlines) == 2
    assert isinstance(deadlines[0], RuntimeDeadline)
    assert all(deadline is deadlines[0] for deadline in deadlines)
    assert deadlines[0].timeout == 30.0


@pytest.mark.asyncio
async def test_explicit_deadline_is_never_replaced() -> None:
    backend = _backend()
    deadline = RuntimeDeadline(timeout=20.0, started_at=50.0, monotonic=lambda: 55.0)
    factory = RuntimeDeadlineFactory(lambda: pytest.fail("factory was called"))
    await _service(backend, factory).create("New", _NB, deadline=deadline)
    assert all(invocation.deadline is deadline for invocation in backend.invocations)


@pytest.mark.parametrize(
    ("failed_leaf", "dispatched", "unknown", "expected_ops"),
    [
        pytest.param(
            Operation.LABEL_LIST,
            False,
            False,
            [Operation.LABEL_LIST],
            id="baseline-pre-dispatch",
        ),
        pytest.param(
            Operation.LABEL_ALLOCATE,
            False,
            False,
            [Operation.LABEL_LIST, Operation.LABEL_ALLOCATE],
            id="allocation-pre-dispatch",
        ),
        pytest.param(
            Operation.LABEL_ALLOCATE,
            True,
            True,
            [Operation.LABEL_LIST, Operation.LABEL_ALLOCATE],
            id="allocation-dispatched",
        ),
    ],
)
@pytest.mark.asyncio
async def test_expiry_state_is_rebound_without_losing_dispatch_truth(
    failed_leaf: Operation,
    dispatched: bool,
    unknown: bool,
    expected_ops: list[Operation],
) -> None:
    backend = RecordingBackend()
    deadline_error = BackendDeadlineExceededError(
        failed_leaf,
        outcome_unknown=unknown,
        diagnostics=MappingProxyType({"method_id": "blocked", "timeout": 1.0}),
        dispatched=dispatched,
    )
    if failed_leaf is Operation.LABEL_LIST:
        backend.set_sequence(LABEL_LIST_DEF, [deadline_error])
        backend.set_result(LABEL_ALLOCATE_DEF, _ECHO)
    else:
        backend.set_result(LABEL_LIST_DEF, _BASELINE)
        backend.set_sequence(LABEL_ALLOCATE_DEF, [deadline_error])

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).create("New", _NB)

    error = caught.value
    assert _ops(backend) == expected_ops
    assert error.operation is Operation.LABEL_CREATE
    assert error.message == "label.create exceeded its deadline"
    assert error.dispatched is dispatched
    assert error.outcome_unknown is unknown
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is failed_leaf
    projected = project_backend_error(error)
    assert isinstance(projected, RPCTimeoutError)
    assert getattr(projected, "unconfirmed", False) is unknown


@pytest.mark.asyncio
async def test_leaf_failure_preserves_reason_message_dispatch_and_native_cause() -> None:
    backend = RecordingBackend()
    backend.set_result(LABEL_LIST_DEF, _BASELINE)
    cause = ServerError("boom", method_id=RPCMethod.CREATE_LABEL.value)
    leaf = scripted_error(
        BackendErrorReason.SERVER,
        operation=Operation.LABEL_ALLOCATE,
        dispatched=True,
        message="boom",
        diagnostics={"method_id": RPCMethod.CREATE_LABEL.value},
    )
    try:
        raise leaf from cause
    except BackendError as chained:
        backend.set_error(LABEL_ALLOCATE_DEF, chained)

    with pytest.raises(BackendError) as caught:
        await _service(backend).create("New", _NB)

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.LABEL_CREATE
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.dispatched is True
    assert error.outcome_unknown is False
    assert error.__cause__ is cause
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is Operation.LABEL_ALLOCATE


# --- web/facade compatibility -------------------------------------------------------


_OPTS = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]
_BASE_KWARGS = {
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}
_LABEL_ROW = ["Existing", None, "l1", ""]
_CREATED_ROW = ["New", None, "l2", "U0001f4c4"]


@pytest.mark.asyncio
async def test_facade_sequence_and_primitive_kwargs_are_byte_identical() -> None:
    executor = _RecordingExecutor([[_LABEL_ROW]], [None, [_LABEL_ROW, _CREATED_ROW]])
    api = LabelsAPI(build_web_backend(executor), list_sources=None)  # type: ignore[arg-type]

    created = await api.create(_NB, "New", "U0001f4c4")

    assert created.id == "l2"
    assert [method for method, _params, _kwargs in executor.calls] == [
        RPCMethod.LIST_LABELS,
        RPCMethod.CREATE_LABEL,
    ]
    baseline, allocate = executor.calls
    assert baseline[1] == [_OPTS, _NB]
    assert allocate[1] == [_OPTS, _NB, None, None, None, [["New", "U0001f4c4"]]]
    assert baseline[2] == {
        "source_path": f"/notebook/{_NB}",
        "allow_null": False,
        **_BASE_KWARGS,
    }
    assert allocate[2] == {
        "source_path": f"/notebook/{_NB}",
        "allow_null": True,
        **_BASE_KWARGS,
    }


@pytest.mark.asyncio
async def test_facade_preserves_server_error_text_method_and_cause() -> None:
    native = ServerError("boom", method_id=RPCMethod.CREATE_LABEL.value)
    executor = _RecordingExecutor([[_LABEL_ROW]], native)
    api = LabelsAPI(build_web_backend(executor), list_sources=None)  # type: ignore[arg-type]

    with pytest.raises(ServerError) as caught:
        await api.create(_NB, "New")

    assert str(caught.value) == "boom"
    assert caught.value.method_id == RPCMethod.CREATE_LABEL.value
