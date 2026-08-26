"""P9.2-12: ``notebook.create`` is a service-owned reconciliation workflow."""

from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType

import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS
from scripts.audit_operation_catalog import derive_workflow_natives

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
)
from notebooklm._backend_compat import project_backend_error
from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._notebook_mutation_service import NotebookMutationService
from notebooklm._operations import Operation
from notebooklm._records import (
    NOTEBOOK_ALLOCATE_DEF,
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_LIST_DEF,
    SETTINGS_GET_LIMITS_DEF,
    AccountLimitsRecord,
    NotebookAllocateInput,
    NotebookAllocateResult,
    NotebookCreateInput,
    NotebookListInput,
    NotebookListResult,
    NotebookRecord,
    SettingsGetLimitsResult,
)
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import NotebookLimitError, RPCError
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import BackendInvocation, RecordingBackend, scripted_error
from tests._fixtures.web_backend import build_web_backend

_TITLE = "Daily News"
_CREATED = NotebookRecord("nb-new", _TITLE)
_DONE = NotebookAllocateResult(_CREATED)
_EMPTY = NotebookListResult(())
_NO_LIMIT = SettingsGetLimitsResult(AccountLimitsRecord())


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_LIST_DEF, _EMPTY)
    backend.set_result(NOTEBOOK_ALLOCATE_DEF, _DONE)
    backend.set_result(SETTINGS_GET_LIMITS_DEF, _NO_LIMIT)
    return backend


def _service(
    backend: RecordingBackend,
    factory: RuntimeDeadlineFactory | None = None,
) -> NotebookMutationService:
    return NotebookMutationService(backend, deadline_factory=factory)


def _ops(backend: RecordingBackend) -> list[Operation]:
    return [invocation.operation for invocation in backend.invocations]


def _uncertain_allocate() -> BackendError:
    return scripted_error(
        BackendErrorReason.SERVER,
        operation=Operation.NOTEBOOK_ALLOCATE,
        dispatched=True,
    )


def _quota_rejection() -> BackendError:
    return BackendError(
        "The server rejected this request (invalid argument).",
        operation=Operation.NOTEBOOK_ALLOCATE,
        diagnostics=MappingProxyType(
            {
                "quota_rejection": True,
                "method_id": RPCMethod.CREATE_NOTEBOOK.value,
                "rpc_code": 3,
                "raw_response": None,
                "found_ids": None,
            }
        ),
        reason=BackendErrorReason.RPC,
        dispatched=True,
    )


def test_create_is_service_owned_and_declares_all_conditional_leaves() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.NOTEBOOK_CREATE]
    assert binding.service_owned is True
    assert binding.is_supported is False
    assert binding.row is None
    assert Operation.NOTEBOOK_CREATE in WEB_SERVICE_OWNED_OPERATIONS
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.NOTEBOOK_CREATE]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.NOTEBOOK_LIST, frozenset({None})),
        (Operation.NOTEBOOK_ALLOCATE, frozenset({None})),
        (Operation.SETTINGS_GET_LIMITS, frozenset({None})),
    ]
    assert derive_workflow_natives(workflow) == {
        (native.method, native.variant) for native in workflow.native_bindings
    }
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.LIST_NOTEBOOKS, None),
        (RPCMethod.CREATE_NOTEBOOK, None),
        (RPCMethod.GET_USER_SETTINGS, None),
    }


@pytest.mark.asyncio
async def test_backend_refuses_direct_create_invocation() -> None:
    backend = build_web_backend(type("Executor", (), {"rpc_call": None})())
    assert backend.capabilities.supports(Operation.NOTEBOOK_CREATE) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            NOTEBOOK_CREATE_DEF,
            NotebookCreateInput(_TITLE),
            deadline=None,
        )


@pytest.mark.asyncio
async def test_success_snapshots_then_allocates_once() -> None:
    backend = _backend()
    created = await _service(backend).create(_TITLE)
    assert (created.id, created.title) == (_CREATED.id, _TITLE)
    assert backend.invocations == [
        BackendInvocation(Operation.NOTEBOOK_LIST, NotebookListInput(), None),
        BackendInvocation(
            Operation.NOTEBOOK_ALLOCATE,
            NotebookAllocateInput(_TITLE),
            None,
        ),
    ]


@pytest.mark.asyncio
async def test_missing_conditional_leaf_fails_before_the_baseline() -> None:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_LIST_DEF, _EMPTY)
    backend.set_result(NOTEBOOK_ALLOCATE_DEF, _DONE)
    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).create(_TITLE)
    assert caught.value.operation is Operation.SETTINGS_GET_LIMITS
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_one_factory_deadline_covers_baseline_allocate_and_quota_diagnosis() -> None:
    backend = _backend()
    owned = tuple(NotebookRecord(f"nb-{index}", f"N{index}") for index in range(10))
    backend.set_sequence(NOTEBOOK_LIST_DEF, [_EMPTY, NotebookListResult(owned)])
    backend.set_error(NOTEBOOK_ALLOCATE_DEF, _quota_rejection())
    backend.set_result(
        SETTINGS_GET_LIMITS_DEF,
        SettingsGetLimitsResult(AccountLimitsRecord(notebook_limit=10)),
    )
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)

    with pytest.raises(BackendError):
        await _service(backend, factory).create(_TITLE)

    deadlines = [invocation.deadline for invocation in backend.invocations]
    assert len(deadlines) == 4
    assert isinstance(deadlines[0], RuntimeDeadline)
    assert all(deadline is deadlines[0] for deadline in deadlines)


@pytest.mark.asyncio
async def test_explicit_deadline_is_not_replaced_and_no_factory_keeps_none() -> None:
    explicit = RuntimeDeadline(timeout=20.0, started_at=50.0, monotonic=lambda: 55.0)
    backend = _backend()
    factory = RuntimeDeadlineFactory(lambda: pytest.fail("factory was called"))
    await _service(backend, factory).create(_TITLE, deadline=explicit)
    assert all(invocation.deadline is explicit for invocation in backend.invocations)

    backend = _backend()
    await _service(backend).create(_TITLE)
    assert all(invocation.deadline is None for invocation in backend.invocations)


@pytest.mark.asyncio
async def test_uncertain_allocate_adopts_one_new_baseline_diff_without_repost() -> None:
    old = NotebookRecord("nb-old", _TITLE)
    landed = NotebookRecord("nb-landed", _TITLE)
    backend = _backend()
    backend.set_sequence(
        NOTEBOOK_LIST_DEF,
        [NotebookListResult((old,)), NotebookListResult((old, landed))],
    )
    backend.set_error(NOTEBOOK_ALLOCATE_DEF, _uncertain_allocate())

    recovered = await _service(backend).create(_TITLE)

    assert recovered.id == landed.id
    assert _ops(backend) == [
        Operation.NOTEBOOK_LIST,
        Operation.NOTEBOOK_ALLOCATE,
        Operation.NOTEBOOK_LIST,
    ]


@pytest.mark.asyncio
async def test_empty_probe_retries_allocate_once() -> None:
    backend = _backend()
    backend.set_sequence(NOTEBOOK_LIST_DEF, [_EMPTY, _EMPTY])
    backend.set_sequence(NOTEBOOK_ALLOCATE_DEF, [_uncertain_allocate(), _DONE])

    created = await _service(backend).create(_TITLE)

    assert created.id == _CREATED.id
    assert _ops(backend) == [
        Operation.NOTEBOOK_LIST,
        Operation.NOTEBOOK_ALLOCATE,
        Operation.NOTEBOOK_LIST,
        Operation.NOTEBOOK_ALLOCATE,
    ]


@pytest.mark.asyncio
async def test_predispatch_allocate_failure_is_rebound_without_probe_or_retry() -> None:
    backend = _backend()
    backend.set_error(
        NOTEBOOK_ALLOCATE_DEF,
        scripted_error(
            BackendErrorReason.SERVER,
            operation=Operation.NOTEBOOK_ALLOCATE,
            dispatched=False,
        ),
    )

    with pytest.raises(BackendError) as caught:
        await _service(backend).create(_TITLE)

    assert _ops(backend) == [Operation.NOTEBOOK_LIST, Operation.NOTEBOOK_ALLOCATE]
    assert caught.value.operation is Operation.NOTEBOOK_CREATE
    assert caught.value.reason is BackendErrorReason.SERVER
    assert caught.value.outcome_unknown is False
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["leaf_operation"] is Operation.NOTEBOOK_ALLOCATE


@pytest.mark.asyncio
async def test_ambiguous_probe_is_rpc_outcome_unknown_and_aborts_retry() -> None:
    first = NotebookRecord("nb-a", _TITLE)
    second = NotebookRecord("nb-b", _TITLE)
    backend = _backend()
    backend.set_sequence(
        NOTEBOOK_LIST_DEF,
        [_EMPTY, NotebookListResult((first, second))],
    )
    backend.set_error(NOTEBOOK_ALLOCATE_DEF, _uncertain_allocate())

    with pytest.raises(BackendError) as caught:
        await _service(backend).create(_TITLE)

    assert caught.value.operation is Operation.NOTEBOOK_CREATE
    assert caught.value.reason is BackendErrorReason.RPC
    assert caught.value.outcome_unknown is True
    assert "Cannot disambiguate" in caught.value.message
    assert _ops(backend).count(Operation.NOTEBOOK_ALLOCATE) == 1
    projected = project_backend_error(caught.value)
    assert isinstance(projected, RPCError)
    assert projected.method_id == RPCMethod.CREATE_NOTEBOOK.value
    assert getattr(projected, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_baseline_failure_warns_but_a_successful_allocate_still_returns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = _backend()
    baseline_error = scripted_error(
        BackendErrorReason.DECODING,
        operation=Operation.NOTEBOOK_LIST,
    )
    backend.set_error(NOTEBOOK_LIST_DEF, baseline_error)

    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        created = await _service(backend).create(_TITLE)

    assert created.id == _CREATED.id
    assert "baseline list() failed" in caplog.text
    assert "surface as an ambiguity error" in caplog.text


@pytest.mark.parametrize(
    "reason",
    [
        BackendErrorReason.AUTH,
        BackendErrorReason.NETWORK,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.SERVER,
        BackendErrorReason.TIMEOUT,
    ],
)
@pytest.mark.asyncio
async def test_probe_transport_failures_are_rebound_and_marked_unknown(
    reason: BackendErrorReason,
) -> None:
    backend = _backend()
    backend.set_sequence(
        NOTEBOOK_LIST_DEF,
        [
            _EMPTY,
            scripted_error(reason, operation=Operation.NOTEBOOK_LIST, dispatched=False),
        ],
    )
    backend.set_error(NOTEBOOK_ALLOCATE_DEF, _uncertain_allocate())

    expected = (
        BackendDeadlineExceededError if reason is BackendErrorReason.TIMEOUT else BackendError
    )
    with pytest.raises(expected) as caught:
        await _service(backend).create(_TITLE)

    assert caught.value.operation is Operation.NOTEBOOK_CREATE
    assert caught.value.reason is reason
    assert caught.value.outcome_unknown is True
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["leaf_operation"] is Operation.NOTEBOOK_LIST


@pytest.mark.asyncio
async def test_probe_decode_failure_wraps_rpc_unknown_and_never_reposts() -> None:
    backend = _backend()
    backend.set_sequence(
        NOTEBOOK_LIST_DEF,
        [
            _EMPTY,
            scripted_error(
                BackendErrorReason.DECODING,
                operation=Operation.NOTEBOOK_LIST,
            ),
        ],
    )
    backend.set_error(NOTEBOOK_ALLOCATE_DEF, _uncertain_allocate())

    with pytest.raises(BackendError) as caught:
        await _service(backend).create(_TITLE)

    assert caught.value.reason is BackendErrorReason.RPC
    assert caught.value.outcome_unknown is True
    assert "UNRESOLVED" in caught.value.message
    assert _ops(backend).count(Operation.NOTEBOOK_ALLOCATE) == 1


@pytest.mark.asyncio
async def test_quota_rejection_near_limit_projects_notebook_limit_with_original_rpc() -> None:
    owned = tuple(NotebookRecord(f"nb-{index}", f"N{index}") for index in range(10))
    backend = _backend()
    backend.set_sequence(NOTEBOOK_LIST_DEF, [_EMPTY, NotebookListResult(owned)])
    backend.set_error(NOTEBOOK_ALLOCATE_DEF, _quota_rejection())
    backend.set_result(
        SETTINGS_GET_LIMITS_DEF,
        SettingsGetLimitsResult(AccountLimitsRecord(notebook_limit=10)),
    )

    with pytest.raises(BackendError) as caught:
        await _service(backend).create(_TITLE)

    error = caught.value
    assert error.operation is Operation.NOTEBOOK_CREATE
    assert error.reason is BackendErrorReason.NOTEBOOK_LIMIT
    assert dict(error.diagnostics or {})["current_count"] == 10
    projected = project_backend_error(error)
    assert isinstance(projected, NotebookLimitError)
    assert projected.current_count == 10 and projected.limit == 10
    assert isinstance(projected.original_error, RPCError)
    assert projected.original_error.rpc_code == 3
    assert projected.__cause__ is projected.original_error


@pytest.mark.asyncio
async def test_quota_rejection_away_from_limit_preserves_original_rpc_without_retry() -> None:
    owned = tuple(NotebookRecord(f"nb-{index}", f"N{index}") for index in range(3))
    backend = _backend()
    backend.set_sequence(NOTEBOOK_LIST_DEF, [_EMPTY, NotebookListResult(owned)])
    backend.set_error(NOTEBOOK_ALLOCATE_DEF, _quota_rejection())
    backend.set_result(
        SETTINGS_GET_LIMITS_DEF,
        SettingsGetLimitsResult(AccountLimitsRecord(notebook_limit=10)),
    )

    with pytest.raises(BackendError) as caught:
        await _service(backend).create(_TITLE)

    assert caught.value.operation is Operation.NOTEBOOK_CREATE
    assert caught.value.reason is BackendErrorReason.RPC
    assert _ops(backend) == [
        Operation.NOTEBOOK_LIST,
        Operation.NOTEBOOK_ALLOCATE,
        Operation.SETTINGS_GET_LIMITS,
        Operation.NOTEBOOK_LIST,
    ]


@pytest.mark.asyncio
async def test_cancellation_propagates_without_probe_or_rebinding() -> None:
    class CancellingBackend(RecordingBackend):
        async def invoke(self, operation, value, *, deadline):  # type: ignore[no-untyped-def]
            if operation.key is Operation.NOTEBOOK_ALLOCATE:
                raise asyncio.CancelledError
            return await super().invoke(operation, value, deadline=deadline)

    backend = CancellingBackend()
    backend.set_result(NOTEBOOK_LIST_DEF, _EMPTY)
    backend.set_result(NOTEBOOK_ALLOCATE_DEF, _DONE)
    backend.set_result(SETTINGS_GET_LIMITS_DEF, _NO_LIMIT)
    with pytest.raises(asyncio.CancelledError):
        await _service(backend).create(_TITLE)
    assert _ops(backend) == [Operation.NOTEBOOK_LIST]
