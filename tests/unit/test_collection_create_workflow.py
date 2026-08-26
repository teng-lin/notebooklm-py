"""P9.2-9: ``collection.create`` is a service-owned workflow over three leaves.

``LabelSetService.create`` owns the account collection baseline, one shared
``label.allocate`` call, mandatory ``collection.list`` readback, exact-id
reconciliation, one workflow deadline and leaf-error rebinding. The tests use
``RecordingBackend`` as the workflow oracle and pin byte-identical primitive
kwargs through the public facade.
"""

from __future__ import annotations

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
from notebooklm._collections import CollectionsAPI
from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._label_service import LabelSetService
from notebooklm._operations import Operation
from notebooklm._records import (
    COLLECTION_CREATE_DEF,
    COLLECTION_LIST_DEF,
    LABEL_ALLOCATE_DEF,
    LabelAllocateInput,
    LabelAllocateResult,
    LabelCreateInput,
    LabelKind,
    LabelListInput,
    LabelListResult,
    LabelRecord,
)
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import CollectionError, RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend, scripted_error
from tests._fixtures.web_backend import build_web_backend

_EXISTING = LabelRecord("c1", "Existing", LabelKind.COLLECTION)
_CREATED = LabelRecord("c2", "New", LabelKind.COLLECTION)
_BASELINE = LabelListResult(labels=(_EXISTING,))
_READBACK = LabelListResult(labels=(_EXISTING, _CREATED))
_ALLOCATED = LabelAllocateResult()


def _service(backend: RecordingBackend, factory: RuntimeDeadlineFactory | None = None):
    return LabelSetService(backend, LabelKind.COLLECTION, deadline_factory=factory)


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_sequence(COLLECTION_LIST_DEF, [_BASELINE, _READBACK])
    backend.set_result(LABEL_ALLOCATE_DEF, _ALLOCATED)
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


def test_collection_create_is_service_owned_with_list_and_allocate_edges() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.COLLECTION_CREATE]
    assert binding.service_owned is True and binding.is_supported is False
    assert binding.row is None
    assert Operation.COLLECTION_CREATE in WEB_SERVICE_OWNED_OPERATIONS
    assert Operation.COLLECTION_CREATE not in WEB_CALL_POLICY_BINDINGS
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.COLLECTION_CREATE]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.COLLECTION_LIST, frozenset({None})),
        (Operation.LABEL_ALLOCATE, frozenset({None})),
    ]
    # One list edge is intentionally called twice by the workflow; native-set
    # parity records reachability, while the RecordingBackend tests pin cardinality.
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
    assert backend.capabilities.supports(Operation.COLLECTION_CREATE) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            COLLECTION_CREATE_DEF,
            LabelCreateInput(LabelKind.COLLECTION, "New"),
            deadline=None,
        )


# --- workflow sequence and reconciliation ------------------------------------------


@pytest.mark.asyncio
async def test_create_baselines_allocates_re_lists_and_reconciles_by_exact_id() -> None:
    backend = _backend()
    created = await _service(backend).create("New")

    assert created is _CREATED
    assert _ops(backend) == [
        Operation.COLLECTION_LIST,
        Operation.LABEL_ALLOCATE,
        Operation.COLLECTION_LIST,
    ]
    assert [invocation.value for invocation in backend.invocations] == [
        LabelListInput(LabelKind.COLLECTION),
        LabelAllocateInput(LabelKind.COLLECTION, "New"),
        LabelListInput(LabelKind.COLLECTION),
    ]


@pytest.mark.asyncio
async def test_duplicate_names_never_replace_id_diff_attribution() -> None:
    same_name = LabelRecord("c3", "Existing", LabelKind.COLLECTION)
    backend = RecordingBackend()
    backend.set_sequence(
        COLLECTION_LIST_DEF,
        [_BASELINE, LabelListResult(labels=(_EXISTING, same_name))],
    )
    backend.set_result(LABEL_ALLOCATE_DEF, _ALLOCATED)

    assert await _service(backend).create("Existing") is same_name


@pytest.mark.parametrize(
    ("after", "count"),
    [
        pytest.param((_EXISTING,), 0, id="no-new-id"),
        pytest.param(
            (
                _EXISTING,
                _CREATED,
                LabelRecord("c3", "New", LabelKind.COLLECTION),
            ),
            2,
            id="several-new-ids",
        ),
    ],
)
@pytest.mark.asyncio
async def test_ambiguous_readback_preserves_message_diagnostics_and_projection(
    after: tuple[LabelRecord, ...], count: int
) -> None:
    backend = RecordingBackend()
    backend.set_sequence(
        COLLECTION_LIST_DEF,
        [_BASELINE, LabelListResult(labels=after)],
    )
    backend.set_result(LABEL_ALLOCATE_DEF, _ALLOCATED)

    with pytest.raises(BackendError) as caught:
        await _service(backend).create("New")

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.COLLECTION_CREATE
    assert error.reason is BackendErrorReason.LABEL_AMBIGUOUS_CREATE
    assert error.message == (
        f"create(name='New') expected exactly 1 new collection, found {count} "
        "(a concurrent create, or read-after-write lag on the re-list, can cause this — "
        "retry from a fresh list)"
    )
    assert dict(error.diagnostics or {}) == {
        "label_kind": "collection",
        "candidate_count": count,
        "name": "New",
    }
    projected = project_backend_error(error)
    assert type(projected) is CollectionError
    assert str(projected) == error.message


@pytest.mark.asyncio
async def test_missing_allocate_leaf_is_rejected_before_the_baseline_read() -> None:
    backend = RecordingBackend()
    backend.set_result(COLLECTION_LIST_DEF, _BASELINE)

    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).create("New")

    assert caught.value.operation is Operation.LABEL_ALLOCATE
    assert backend.invocations == []


# --- deadline, uncertainty and cause identity --------------------------------------


@pytest.mark.asyncio
async def test_one_deadline_identity_covers_all_three_leaves() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)
    await _service(backend, factory).create("New")

    deadlines = [invocation.deadline for invocation in backend.invocations]
    assert len(deadlines) == 3
    assert isinstance(deadlines[0], RuntimeDeadline)
    assert all(deadline is deadlines[0] for deadline in deadlines)
    assert deadlines[0].timeout == 30.0


@pytest.mark.asyncio
async def test_explicit_deadline_is_never_replaced() -> None:
    backend = _backend()
    deadline = RuntimeDeadline(timeout=20.0, started_at=50.0, monotonic=lambda: 55.0)
    factory = RuntimeDeadlineFactory(lambda: pytest.fail("factory was called"))
    await _service(backend, factory).create("New", deadline=deadline)
    assert all(invocation.deadline is deadline for invocation in backend.invocations)


@pytest.mark.parametrize(
    ("failed_phase", "dispatched", "leaf_unknown", "workflow_unknown", "expected_ops"),
    [
        pytest.param(
            "baseline",
            False,
            False,
            False,
            [Operation.COLLECTION_LIST],
            id="baseline-pre-dispatch",
        ),
        pytest.param(
            "allocate",
            False,
            False,
            False,
            [Operation.COLLECTION_LIST, Operation.LABEL_ALLOCATE],
            id="allocation-pre-dispatch",
        ),
        pytest.param(
            "allocate",
            True,
            True,
            True,
            [Operation.COLLECTION_LIST, Operation.LABEL_ALLOCATE],
            id="allocation-dispatched",
        ),
        pytest.param(
            "readback",
            False,
            False,
            True,
            [Operation.COLLECTION_LIST, Operation.LABEL_ALLOCATE, Operation.COLLECTION_LIST],
            id="readback-after-write",
        ),
    ],
)
@pytest.mark.asyncio
async def test_expiry_truth_table_preserves_the_handler_era(
    failed_phase: str,
    dispatched: bool,
    leaf_unknown: bool,
    workflow_unknown: bool,
    expected_ops: list[Operation],
) -> None:
    backend = RecordingBackend()
    failed_leaf = (
        Operation.LABEL_ALLOCATE if failed_phase == "allocate" else Operation.COLLECTION_LIST
    )
    deadline_error = BackendDeadlineExceededError(
        failed_leaf,
        outcome_unknown=leaf_unknown,
        diagnostics=MappingProxyType({"method_id": "blocked", "timeout": 1.0}),
        dispatched=dispatched,
    )
    if failed_phase == "baseline":
        backend.set_sequence(COLLECTION_LIST_DEF, [deadline_error])
        backend.set_result(LABEL_ALLOCATE_DEF, _ALLOCATED)
    elif failed_phase == "allocate":
        backend.set_result(COLLECTION_LIST_DEF, _BASELINE)
        backend.set_sequence(LABEL_ALLOCATE_DEF, [deadline_error])
    else:
        backend.set_sequence(COLLECTION_LIST_DEF, [_BASELINE, deadline_error])
        backend.set_result(LABEL_ALLOCATE_DEF, _ALLOCATED)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).create("New")

    error = caught.value
    assert _ops(backend) == expected_ops
    assert error.operation is Operation.COLLECTION_CREATE
    assert error.message == "collection.create exceeded its deadline"
    assert error.dispatched is dispatched
    assert error.outcome_unknown is workflow_unknown
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is failed_leaf
    projected = project_backend_error(error)
    assert isinstance(projected, RPCTimeoutError)
    assert getattr(projected, "unconfirmed", False) is workflow_unknown


@pytest.mark.asyncio
async def test_readback_failure_keeps_reason_message_dispatch_state_and_native_cause() -> None:
    backend = RecordingBackend()
    cause = ServerError("boom", method_id=RPCMethod.LIST_LABELS.value)
    leaf = scripted_error(
        BackendErrorReason.SERVER,
        operation=Operation.COLLECTION_LIST,
        dispatched=True,
        message="boom",
        diagnostics={"method_id": RPCMethod.LIST_LABELS.value},
    )
    try:
        raise leaf from cause
    except BackendError as chained:
        backend.set_sequence(COLLECTION_LIST_DEF, [_BASELINE, chained])
    backend.set_result(LABEL_ALLOCATE_DEF, _ALLOCATED)

    with pytest.raises(BackendError) as caught:
        await _service(backend).create("New")

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.COLLECTION_CREATE
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.dispatched is True
    assert error.outcome_unknown is False
    assert error.__cause__ is cause
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is Operation.COLLECTION_LIST


# --- web/facade compatibility -------------------------------------------------------


_COLLECTION_OPTS = [
    2,
    None,
    None,
    [1, None, None, None, None, None, None, None, None, None, [1, 3]],
]
_COLLECTION_CREATE_OPTS = [
    2,
    None,
    [1],
    [1, None, None, None, None, None, None, None, None, None, [1, 3]],
]
_BASE_KWARGS = {
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}
_COLLECTION_ROW = ["Existing", None, "c1", ""]
_CREATED_ROW = ["New", None, "c2", ""]


@pytest.mark.asyncio
async def test_facade_sequence_and_primitive_kwargs_are_byte_identical() -> None:
    executor = _RecordingExecutor(
        [None, [_COLLECTION_ROW]],
        None,
        [None, [_COLLECTION_ROW, _CREATED_ROW]],
    )
    api = CollectionsAPI(build_web_backend(executor), list_notebooks=None)  # type: ignore[arg-type]

    created = await api.create("New")

    assert created.id == "c2"
    assert [method for method, _params, _kwargs in executor.calls] == [
        RPCMethod.LIST_LABELS,
        RPCMethod.CREATE_LABEL,
        RPCMethod.LIST_LABELS,
    ]
    baseline, allocate, readback = executor.calls
    assert baseline[1] == [_COLLECTION_OPTS, None, 3]
    assert allocate[1] == [
        _COLLECTION_CREATE_OPTS,
        None,
        None,
        None,
        None,
        [["New"]],
        3,
    ]
    assert readback[1] == [_COLLECTION_OPTS, None, 3]
    for call in (baseline, allocate, readback):
        assert call[2] == {"source_path": "/", "allow_null": True, **_BASE_KWARGS}


@pytest.mark.asyncio
async def test_facade_preserves_readback_server_error_text_and_method() -> None:
    native = ServerError("boom", method_id=RPCMethod.LIST_LABELS.value)
    executor = _RecordingExecutor([None, [_COLLECTION_ROW]], None, native)
    api = CollectionsAPI(build_web_backend(executor), list_notebooks=None)  # type: ignore[arg-type]

    with pytest.raises(ServerError) as caught:
        await api.create("New")

    assert str(caught.value) == "boom"
    assert caught.value.method_id == RPCMethod.LIST_LABELS.value
