"""P9.2-2: ``label.update`` is a service-owned workflow sequenced from leaves.

``LabelSetService.update`` (source-label dialect) owns what the P6.4 web
handler owned: the ``label.get`` preflight/readback, one ``label.mutate`` per
member, one deadline for the whole workflow, and every leaf failure re-raised
as ``label.update`` with the leaf retained in the diagnostics. These tests
replace the backend-level ``LABEL_UPDATE`` oracles (the outcome-unknown truth
table, the registry pins) with per-workflow sequence tests against
``RecordingBackend.set_sequence`` and pin the public error text through the
facade.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import Any

import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
    may_have_committed,
)
from notebooklm._backend_compat import project_backend_error
from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._label_service import (
    NOT_FOUND_FIELD_READBACK,
    NOT_FOUND_MEMBERSHIP_READBACK,
    NOT_FOUND_PHASE_KEY,
    NOT_FOUND_PREFLIGHT,
    LabelSetService,
)
from notebooklm._labels import LabelsAPI
from notebooklm._operations import Operation
from notebooklm._records import (
    LABEL_GET_DEF,
    LABEL_MUTATE_DEF,
    LABEL_UPDATE_DEF,
    LabelGetResult,
    LabelKind,
    LabelMutateInput,
    LabelMutateResult,
    LabelRecord,
    LabelUpdateInput,
)
from notebooklm._web.policy import (
    SERVICE_OWNED_WORKFLOW_BINDINGS,
    WEB_CALL_POLICY_BINDINGS,
    derive_workflow_natives,
)
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import LabelNotFoundError, RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend, scripted_error
from tests._fixtures.web_backend import build_web_backend

_NB = "nb_1"
_LABEL = LabelRecord(
    "l1", "Old", LabelKind.SOURCE_LABEL, _NB, emoji="\U0001f4c4", member_ids=("s1",)
)
_FOUND = LabelGetResult(label=_LABEL)
_MISSING = LabelGetResult(label=None)
_DONE = LabelMutateResult()


def _service(backend: RecordingBackend, factory: RuntimeDeadlineFactory | None = None):
    return LabelSetService(backend, LabelKind.SOURCE_LABEL, deadline_factory=factory)


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(LABEL_GET_DEF, _FOUND)
    backend.set_result(LABEL_MUTATE_DEF, _DONE)
    return backend


def _ops(backend: RecordingBackend) -> list[Operation]:
    return [invocation.operation for invocation in backend.invocations]


# --- disposition and ledger --------------------------------------------------------


def test_label_update_is_service_owned_and_not_invokable() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.LABEL_UPDATE]
    assert binding.service_owned is True
    assert binding.is_supported is False
    assert binding.row is None
    assert Operation.LABEL_UPDATE in WEB_SERVICE_OWNED_OPERATIONS
    assert Operation.LABEL_UPDATE not in WEB_CALL_POLICY_BINDINGS
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.LABEL_UPDATE]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.LABEL_GET, frozenset({None})),
        (Operation.LABEL_MUTATE, frozenset({None, "add_sources", "remove_sources"})),
    ]
    # The hand-reviewed natives equal the leaf-derived set (the P4 parity audit survives).
    assert derive_workflow_natives(workflow) == {
        (native.method, native.variant) for native in workflow.native_bindings
    }
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.LIST_LABELS, None),
        (RPCMethod.UPDATE_LABEL, None),
        (RPCMethod.UPDATE_LABEL, "add_sources"),
        (RPCMethod.UPDATE_LABEL, "remove_sources"),
    }


@pytest.mark.asyncio
async def test_backend_refuses_the_workflow_directly() -> None:
    backend = build_web_backend(_RecordingExecutor())
    assert backend.capabilities.supports(Operation.LABEL_UPDATE) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            LABEL_UPDATE_DEF,
            LabelUpdateInput(LabelKind.SOURCE_LABEL, "l1", _NB, name="X"),
            deadline=None,
        )


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


# --- sequences ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_membership_update_issues_one_mutate_per_member_then_reads_back() -> None:
    backend = _backend()
    record = await _service(backend).update(
        "l1", _NB, add_member_ids=("s2", "s3"), remove_member_ids=("s1",)
    )
    assert record is _LABEL
    assert _ops(backend) == [
        Operation.LABEL_MUTATE,
        Operation.LABEL_MUTATE,
        Operation.LABEL_MUTATE,
        Operation.LABEL_GET,
    ]
    assert [invocation.value for invocation in backend.invocations[:3]] == [
        LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB, add_member_id="s2"),
        LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB, add_member_id="s3"),
        LabelMutateInput(LabelKind.SOURCE_LABEL, "l1", _NB, remove_member_id="s1"),
    ]


@pytest.mark.asyncio
async def test_membership_readback_is_mandatory_even_without_return_object() -> None:
    backend = _backend()
    assert (
        await _service(backend).update("l1", _NB, add_member_ids=("s2",), return_object=False)
        is None
    )
    assert _ops(backend) == [Operation.LABEL_MUTATE, Operation.LABEL_GET]


@pytest.mark.asyncio
async def test_field_update_preflights_carries_the_emoji_and_reads_back_only_when_asked() -> None:
    backend = _backend()
    record = await _service(backend).update("l1", _NB, name="New")
    assert record is _LABEL
    assert _ops(backend) == [Operation.LABEL_GET, Operation.LABEL_MUTATE, Operation.LABEL_GET]
    assert backend.invocations[1].value == LabelMutateInput(
        LabelKind.SOURCE_LABEL, "l1", _NB, name="New", emoji="\U0001f4c4"
    )

    backend = _backend()
    assert await _service(backend).update("l1", _NB, name="New", return_object=False) is None
    assert _ops(backend) == [Operation.LABEL_GET, Operation.LABEL_MUTATE]


@pytest.mark.asyncio
async def test_emoji_only_update_sends_the_explicit_emoji() -> None:
    backend = _backend()
    await _service(backend).update("l1", _NB, emoji="\U0001f525", return_object=False)
    assert backend.invocations[1].value == LabelMutateInput(
        LabelKind.SOURCE_LABEL, "l1", _NB, emoji="\U0001f525"
    )


@pytest.mark.parametrize(
    ("kwargs", "phase", "expected_ops", "method_id"),
    [
        pytest.param(
            {"name": "New"},
            NOT_FOUND_PREFLIGHT,
            [Operation.LABEL_GET],
            RPCMethod.UPDATE_LABEL.value,
            id="preflight",
        ),
        pytest.param(
            {"add_member_ids": ("s2",), "return_object": False},
            NOT_FOUND_MEMBERSHIP_READBACK,
            [Operation.LABEL_MUTATE, Operation.LABEL_GET],
            RPCMethod.UPDATE_LABEL.value,
            id="membership-readback",
        ),
    ],
)
@pytest.mark.asyncio
async def test_not_found_keeps_identity_and_the_legacy_method_id(
    kwargs: dict[str, Any], phase: str, expected_ops: list[Operation], method_id: str
) -> None:
    backend = _backend()
    backend.set_result(LABEL_GET_DEF, _MISSING)
    with pytest.raises(BackendError) as caught:
        await _service(backend).update("l1", _NB, **kwargs)
    error = caught.value
    assert error.operation is Operation.LABEL_UPDATE
    assert error.reason is BackendErrorReason.LABEL_NOT_FOUND
    assert error.message == "Label not found: l1"
    assert dict(error.diagnostics or {}) == {
        "label_kind": "source_label",
        "label_id": "l1",
        "notebook_id": _NB,
        NOT_FOUND_PHASE_KEY: phase,
    }
    assert _ops(backend) == expected_ops
    projected = project_backend_error(error)
    assert isinstance(projected, LabelNotFoundError)
    assert projected.label_id == "l1"
    assert projected.method_id == method_id


@pytest.mark.asyncio
async def test_field_readback_miss_reports_the_read_that_proved_absence() -> None:
    backend = RecordingBackend()
    backend.set_sequence(LABEL_GET_DEF, [_FOUND, _MISSING])
    backend.set_result(LABEL_MUTATE_DEF, _DONE)
    with pytest.raises(BackendError) as caught:
        await _service(backend).update("l1", _NB, name="New")
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics[NOT_FOUND_PHASE_KEY] == NOT_FOUND_FIELD_READBACK
    projected = project_backend_error(caught.value)
    assert isinstance(projected, LabelNotFoundError)
    assert projected.method_id == RPCMethod.LIST_LABELS.value


# --- leaf conjunction and deadline identity -------------------------------------------


@pytest.mark.asyncio
async def test_unsupported_leaf_is_rejected_before_any_side_effect() -> None:
    backend = RecordingBackend()
    backend.set_result(LABEL_GET_DEF, _FOUND)  # LABEL_MUTATE deliberately unregistered
    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).update("l1", _NB, add_member_ids=("s2",))
    assert caught.value.operation is Operation.LABEL_MUTATE
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_one_deadline_identity_covers_every_leaf() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)
    await _service(backend, factory).update("l1", _NB, add_member_ids=("s2", "s3"))
    deadlines = [invocation.deadline for invocation in backend.invocations]
    assert len(deadlines) == 3
    assert isinstance(deadlines[0], RuntimeDeadline)
    assert all(deadline is deadlines[0] for deadline in deadlines)
    assert deadlines[0].timeout == 30.0


@pytest.mark.asyncio
async def test_explicit_deadline_is_never_replaced_by_the_factory() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory(lambda: pytest.fail("factory was called"))
    deadline = RuntimeDeadline(timeout=20.0, started_at=50.0, monotonic=lambda: 55.0)
    await _service(backend, factory).update("l1", _NB, name="New", deadline=deadline)
    assert all(invocation.deadline is deadline for invocation in backend.invocations)


@pytest.mark.asyncio
async def test_no_factory_means_no_deadline_exactly_as_before() -> None:
    backend = _backend()
    await _service(backend).update("l1", _NB, name="New")
    assert all(invocation.deadline is None for invocation in backend.invocations)


# --- failure projection: the outcome-unknown truth table, case for case -----------------


def _deadline_error(operation: Operation, *, dispatched: bool, method_id: str) -> BackendError:
    return BackendDeadlineExceededError(
        operation,
        outcome_unknown=dispatched and operation is Operation.LABEL_MUTATE,
        diagnostics=MappingProxyType({"timeout": 1.0, "remaining": 0.0, "method_id": method_id}),
        dispatched=dispatched,
    )


@pytest.mark.parametrize(
    ("kwargs", "get_sequence", "mutate_sequence", "expected_ops", "unknown", "blocked"),
    [
        pytest.param(
            {"name": "Renamed"},
            [_FOUND, _deadline_error(Operation.LABEL_GET, dispatched=False, method_id="list")],
            [_DONE],
            [Operation.LABEL_GET, Operation.LABEL_MUTATE, Operation.LABEL_GET],
            True,
            "list",
            id="label-field-readback",
        ),
        pytest.param(
            {"add_member_ids": ("source-1",)},
            [_deadline_error(Operation.LABEL_GET, dispatched=False, method_id="list")],
            [_DONE],
            [Operation.LABEL_MUTATE, Operation.LABEL_GET],
            True,
            "list",
            id="label-membership-readback",
        ),
        pytest.param(
            {"add_member_ids": ("source-1", "source-2")},
            [],
            [_DONE, _deadline_error(Operation.LABEL_MUTATE, dispatched=False, method_id="mut")],
            [Operation.LABEL_MUTATE, Operation.LABEL_MUTATE],
            True,
            "mut",
            id="label-second-membership-write",
        ),
        pytest.param(
            {"name": "Renamed"},
            [_deadline_error(Operation.LABEL_GET, dispatched=False, method_id="list")],
            [],
            [Operation.LABEL_GET],
            False,
            "list",
            id="label-preflight-read-only",
        ),
        pytest.param(
            {"add_member_ids": ("source-1",)},
            [],
            [_deadline_error(Operation.LABEL_MUTATE, dispatched=False, method_id="mut")],
            [Operation.LABEL_MUTATE],
            False,
            "mut",
            id="label-first-write-not-dispatched",
        ),
        pytest.param(
            {"add_member_ids": ("source-1",)},
            [],
            [_deadline_error(Operation.LABEL_MUTATE, dispatched=True, method_id="mut")],
            [Operation.LABEL_MUTATE],
            True,
            "mut",
            id="label-first-write-dispatched-then-expired",
        ),
    ],
)
@pytest.mark.asyncio
async def test_expiry_truth_table_matches_the_handler_era(
    kwargs: dict[str, Any],
    get_sequence: list[object],
    mutate_sequence: list[object],
    expected_ops: list[Operation],
    unknown: bool,
    blocked: str,
) -> None:
    backend = RecordingBackend()
    backend.set_sequence(LABEL_GET_DEF, get_sequence or [_FOUND])
    backend.set_sequence(LABEL_MUTATE_DEF, mutate_sequence or [_DONE])
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).update("l1", _NB, **kwargs)
    error = caught.value
    assert _ops(backend) == expected_ops
    assert error.operation is Operation.LABEL_UPDATE
    assert error.message == "label.update exceeded its deadline"
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is unknown
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == blocked
    assert error.diagnostics["leaf_operation"] is expected_ops[-1]
    projected = project_backend_error(error)
    assert isinstance(projected, RPCTimeoutError)
    assert getattr(projected, "unconfirmed", False) is unknown


@pytest.mark.parametrize(
    "reason",
    [BackendErrorReason.SERVER, BackendErrorReason.NETWORK, BackendErrorReason.RATE_LIMIT],
)
@pytest.mark.asyncio
async def test_dispatched_write_failures_keep_their_commit_uncertainty(
    reason: BackendErrorReason,
) -> None:
    backend = RecordingBackend()
    backend.set_result(LABEL_GET_DEF, _FOUND)
    backend.set_sequence(
        LABEL_MUTATE_DEF,
        [_DONE, scripted_error(reason, operation=Operation.LABEL_MUTATE, dispatched=True)],
    )
    with pytest.raises(BackendError) as caught:
        await _service(backend).update("l1", _NB, add_member_ids=("s2", "s3"))
    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.LABEL_UPDATE
    assert error.reason is reason
    assert error.dispatched is True
    assert may_have_committed(error) is True
    # A non-deadline failure keeps the leaf's own outcome verdict (never re-marked).
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is Operation.LABEL_MUTATE
    assert _ops(backend) == [Operation.LABEL_MUTATE, Operation.LABEL_MUTATE]


@pytest.mark.asyncio
async def test_readback_failure_after_a_write_is_rebound_without_re_marking() -> None:
    backend = RecordingBackend()
    backend.set_sequence(
        LABEL_GET_DEF, [scripted_error(BackendErrorReason.SERVER, operation=Operation.LABEL_GET)]
    )
    backend.set_result(LABEL_MUTATE_DEF, _DONE)
    with pytest.raises(BackendError) as caught:
        await _service(backend).update("l1", _NB, add_member_ids=("s2",))
    assert caught.value.operation is Operation.LABEL_UPDATE
    assert caught.value.outcome_unknown is False
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["leaf_operation"] is Operation.LABEL_GET


@pytest.mark.asyncio
async def test_unknown_readback_keeps_the_leaf_native_cause_across_error_copies() -> None:
    native = RPCTimeoutError("slow", method_id=RPCMethod.LIST_LABELS.value)
    leaf = _deadline_error(Operation.LABEL_GET, dispatched=False, method_id="list")
    try:
        raise leaf from native
    except BackendDeadlineExceededError as caused_leaf:
        leaf = caused_leaf
    backend = RecordingBackend()
    backend.set_sequence(LABEL_GET_DEF, [leaf])
    backend.set_result(LABEL_MUTATE_DEF, _DONE)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).update("l1", _NB, add_member_ids=("s2",))

    assert caught.value.operation is Operation.LABEL_UPDATE
    assert caught.value.outcome_unknown is True
    assert caught.value.__cause__ is native


@pytest.mark.asyncio
async def test_cancellation_propagates_without_rebinding() -> None:
    class _Cancelling(RecordingBackend):
        async def invoke(self, *_args: object, **_kwargs: object) -> object:  # type: ignore[override]
            raise asyncio.CancelledError

    backend = _Cancelling()
    backend.set_result(LABEL_GET_DEF, _FOUND)
    backend.set_result(LABEL_MUTATE_DEF, _DONE)
    with pytest.raises(asyncio.CancelledError):
        await _service(backend).update("l1", _NB, add_member_ids=("s2",))


@pytest.mark.asyncio
async def test_leaf_contract_errors_are_rebound_to_the_workflow() -> None:
    backend = RecordingBackend()
    backend.set_result(LABEL_GET_DEF, _FOUND)
    backend.set_sequence(
        LABEL_MUTATE_DEF,
        [BackendContractError("bad leaf input", operation=Operation.LABEL_MUTATE)],
    )
    with pytest.raises(BackendContractError) as caught:
        await _service(backend).update("l1", _NB, add_member_ids=("s2",))
    assert caught.value.operation is Operation.LABEL_UPDATE
    assert caught.value.message == "bad leaf input"


# --- end to end through the web backend and facade -----------------------------------------


_LABEL_ROW = ["Old", [["s1"]], "l1", "\U0001f4c4"]


@pytest.mark.asyncio
async def test_facade_sequence_and_kwargs_are_byte_identical_to_the_handler_era() -> None:
    executor = _RecordingExecutor([], [], [[_LABEL_ROW]])
    api = LabelsAPI(build_web_backend(executor), list_sources=None)  # type: ignore[arg-type]
    assert await api.add_sources(_NB, "l1", ["s2", "s3"], return_object=False) is None
    methods = [method for method, _params, _kwargs in executor.calls]
    assert methods == [RPCMethod.UPDATE_LABEL, RPCMethod.UPDATE_LABEL, RPCMethod.LIST_LABELS]
    first, second, readback = (kwargs for _method, _params, kwargs in executor.calls)
    assert first["operation_variant"] == "add_sources"
    assert second["operation_variant"] == "add_sources"
    assert first["source_path"] == f"/notebook/{_NB}"
    assert first["allow_null"] is True
    assert readback["operation_variant"] is None
    assert readback["allow_null"] is False
    assert executor.calls[0][1][3] == [[None, [["s2"]]]]
    assert executor.calls[1][1][3] == [[None, [["s3"]]]]


@pytest.mark.asyncio
async def test_facade_public_error_text_is_unchanged() -> None:
    executor = _RecordingExecutor([[]])
    api = LabelsAPI(build_web_backend(executor), list_sources=None)  # type: ignore[arg-type]
    with pytest.raises(LabelNotFoundError) as missing:
        await api.rename(_NB, "missing", "X", return_object=False)
    assert missing.value.label_id == "missing"
    assert missing.value.method_id == RPCMethod.UPDATE_LABEL.value
    assert "missing" in str(missing.value)

    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.UPDATE_LABEL.value))
    api = LabelsAPI(build_web_backend(executor), list_sources=None)  # type: ignore[arg-type]
    with pytest.raises(ServerError) as boom:
        await api.add_sources(_NB, "l1", ["s2"], return_object=False)
    assert str(boom.value) == "boom"
    assert boom.value.method_id == RPCMethod.UPDATE_LABEL.value


@pytest.mark.asyncio
async def test_facade_pre_dispatch_expiry_after_a_write_projects_unconfirmed() -> None:
    clock = [0.0]

    class _Expiring(_RecordingExecutor):
        async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
            result = await super().rpc_call(method, params, **kwargs)
            clock[0] = 2.0
            return result

    executor = _Expiring([])
    factory = RuntimeDeadlineFactory.fixed(1.0, monotonic=lambda: clock[0])
    api = LabelsAPI(
        build_web_backend(executor),
        list_sources=None,  # type: ignore[arg-type]
        deadline_factory=factory,
    )
    with pytest.raises(RPCTimeoutError) as caught:
        await api.add_sources(_NB, "l1", ["s2"], return_object=False)
    assert caught.value.method_id == RPCMethod.LIST_LABELS.value
    assert getattr(caught.value, "unconfirmed", False) is True
    assert [method for method, _params, _kwargs in executor.calls] == [RPCMethod.UPDATE_LABEL]
