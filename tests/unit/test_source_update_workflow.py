"""P9.2-4: ``source.update`` is a service-owned workflow sequenced from leaves.

``SourceService.update`` owns what the P6.7 web handler owned: one
``source.patch_title`` leaf and, only on a null echo, one ``source.get``
hydration read, under one workflow deadline, with leaf failures rebound to
``source.update`` and the not-found identity (including the legacy
``method_id``) preserved through the compatibility projector.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import Any

import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS
from scripts.audit_operation_catalog import derive_workflow_natives

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
    may_have_committed,
)
from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._operations import Operation
from notebooklm._semantic.compat import project_backend_error
from notebooklm._semantic.records import (
    SOURCE_GET_DEF,
    SOURCE_PATCH_TITLE_DEF,
    SOURCE_UPDATE_DEF,
    SourceGetResult,
    SourcePatchTitleInput,
    SourcePatchTitleResult,
    SourceRecord,
    SourceUpdateInput,
)
from notebooklm._source_service import SourceService
from notebooklm._web.bindings import primitives as primitive_rows
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import (
    NetworkError,
    RateLimitError,
    RPCTimeoutError,
    SourceNotFoundError,
)
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend, scripted_error
from tests._fixtures.web_backend import build_web_backend

_NB = "nb_1"
_SOURCE = SourceRecord(id="src", title="Renamed")
_ECHO = SourcePatchTitleResult(source=_SOURCE)
_NULL = SourcePatchTitleResult(source=None)
_FOUND = SourceGetResult(source=_SOURCE)
_MISSING = SourceGetResult(source=None)


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


def test_source_update_is_service_owned_over_patch_title_and_get() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.SOURCE_UPDATE]
    assert binding.service_owned is True and binding.is_supported is False
    assert Operation.SOURCE_UPDATE in WEB_SERVICE_OWNED_OPERATIONS
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SOURCE_UPDATE]
    assert [leaf.operation for leaf in workflow.leaf_operations] == [
        Operation.SOURCE_PATCH_TITLE,
        Operation.SOURCE_GET,
    ]
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.UPDATE_SOURCE, None),
        (RPCMethod.GET_NOTEBOOK, None),
    }
    primitive = WEB_OPERATION_REGISTRY[Operation.SOURCE_PATCH_TITLE]
    assert primitive.row is primitive_rows.SOURCE_PATCH_TITLE
    assert primitive_rows.SOURCE_PATCH_TITLE.native.select(None).method is RPCMethod.UPDATE_SOURCE


@pytest.mark.asyncio
async def test_backend_refuses_the_workflow_directly() -> None:
    backend = build_web_backend(_RecordingExecutor())
    assert backend.capabilities.supports(Operation.SOURCE_UPDATE) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            SOURCE_UPDATE_DEF, SourceUpdateInput(_NB, "src", "Renamed"), deadline=None
        )


@pytest.mark.asyncio
async def test_echo_short_circuits_and_return_object_selects_the_record() -> None:
    backend = RecordingBackend()
    backend.set_result(SOURCE_PATCH_TITLE_DEF, _ECHO)
    backend.set_result(SOURCE_GET_DEF, _FOUND)
    result = await SourceService(backend).update(_NB, "src", "Renamed", return_object=True)
    assert result.source is _SOURCE
    assert _ops(backend) == [Operation.SOURCE_PATCH_TITLE]
    assert backend.invocations[0].value == SourcePatchTitleInput(_NB, "src", "Renamed")

    backend = RecordingBackend()
    backend.set_result(SOURCE_PATCH_TITLE_DEF, _ECHO)
    backend.set_result(SOURCE_GET_DEF, _FOUND)
    result = await SourceService(backend).update(_NB, "src", "Renamed", return_object=False)
    assert result.source is None
    assert _ops(backend) == [Operation.SOURCE_PATCH_TITLE]


@pytest.mark.asyncio
async def test_null_echo_hydrates_through_source_get_in_both_modes() -> None:
    for return_object in (True, False):
        backend = RecordingBackend()
        backend.set_result(SOURCE_PATCH_TITLE_DEF, _NULL)
        backend.set_result(SOURCE_GET_DEF, _FOUND)
        result = await SourceService(backend).update(
            _NB, "src", "Renamed", return_object=return_object
        )
        assert result.source is (_SOURCE if return_object else None)
        assert _ops(backend) == [Operation.SOURCE_PATCH_TITLE, Operation.SOURCE_GET]


@pytest.mark.asyncio
async def test_null_echo_miss_keeps_the_public_not_found_identity() -> None:
    backend = RecordingBackend()
    backend.set_result(SOURCE_PATCH_TITLE_DEF, _NULL)
    backend.set_result(SOURCE_GET_DEF, _MISSING)
    with pytest.raises(BackendError) as caught:
        await SourceService(backend).update(_NB, "src", "Renamed", return_object=False)
    error = caught.value
    assert error.operation is Operation.SOURCE_UPDATE
    assert error.reason is BackendErrorReason.SOURCE_NOT_FOUND
    assert error.message == "Source not found: src"
    assert error.diagnostics is not None
    assert error.diagnostics["source_id"] == "src"
    assert error.diagnostics["raw_response"] is None
    projected = project_backend_error(error)
    assert isinstance(projected, SourceNotFoundError)
    assert projected.source_id == "src"
    assert projected.method_id == RPCMethod.UPDATE_SOURCE.value
    assert projected.raw_response is None


@pytest.mark.asyncio
async def test_unsupported_leaf_is_rejected_before_any_side_effect() -> None:
    backend = RecordingBackend()
    backend.set_result(SOURCE_PATCH_TITLE_DEF, _ECHO)
    with pytest.raises(UnsupportedOperationError) as caught:
        await SourceService(backend).update(_NB, "src", "Renamed", return_object=True)
    assert caught.value.operation is Operation.SOURCE_GET
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_one_deadline_identity_covers_both_leaves() -> None:
    backend = RecordingBackend()
    backend.set_result(SOURCE_PATCH_TITLE_DEF, _NULL)
    backend.set_result(SOURCE_GET_DEF, _FOUND)
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)
    await SourceService(backend, deadline_factory=factory).update(
        _NB, "src", "Renamed", return_object=True
    )
    deadlines = [invocation.deadline for invocation in backend.invocations]
    assert len(deadlines) == 2
    assert isinstance(deadlines[0], RuntimeDeadline) and deadlines[1] is deadlines[0]
    assert deadlines[0].timeout == 30.0

    backend = RecordingBackend()
    backend.set_result(SOURCE_PATCH_TITLE_DEF, _ECHO)
    backend.set_result(SOURCE_GET_DEF, _FOUND)
    await SourceService(backend).update(_NB, "src", "Renamed", return_object=True)
    assert backend.invocations[0].deadline is None


@pytest.mark.asyncio
async def test_explicit_deadline_remains_authoritative_over_the_factory() -> None:
    backend = RecordingBackend()
    backend.set_result(SOURCE_PATCH_TITLE_DEF, _NULL)
    backend.set_result(SOURCE_GET_DEF, _FOUND)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    factory = RuntimeDeadlineFactory(lambda: pytest.fail("factory was called"))

    await SourceService(backend, deadline_factory=factory).update(
        _NB,
        "src",
        "Renamed",
        return_object=True,
        deadline=deadline,
    )

    assert [invocation.deadline for invocation in backend.invocations] == [deadline, deadline]


def _expiry(operation: Operation, *, dispatched: bool, method_id: str) -> BackendError:
    return BackendDeadlineExceededError(
        operation,
        outcome_unknown=dispatched,  # both leaves are MUTATION rows
        diagnostics=MappingProxyType({"timeout": 1.0, "remaining": 0.0, "method_id": method_id}),
        dispatched=dispatched,
    )


@pytest.mark.parametrize(
    ("patch_sequence", "get_sequence", "expected_ops", "unknown", "blocked"),
    [
        pytest.param(
            [_NULL],
            [_expiry(Operation.SOURCE_GET, dispatched=False, method_id="get")],
            [Operation.SOURCE_PATCH_TITLE, Operation.SOURCE_GET],
            True,
            "get",
            id="source-update-null-readback",
        ),
        pytest.param(
            [_expiry(Operation.SOURCE_PATCH_TITLE, dispatched=False, method_id="patch")],
            [],
            [Operation.SOURCE_PATCH_TITLE],
            False,
            "patch",
            id="source-update-pre-dispatch",
        ),
        pytest.param(
            [_expiry(Operation.SOURCE_PATCH_TITLE, dispatched=True, method_id="patch")],
            [],
            [Operation.SOURCE_PATCH_TITLE],
            True,
            "patch",
            id="source-update-dispatched-then-expired",
        ),
    ],
)
@pytest.mark.asyncio
async def test_expiry_truth_table_matches_the_handler_era(
    patch_sequence: list[object],
    get_sequence: list[object],
    expected_ops: list[Operation],
    unknown: bool,
    blocked: str,
) -> None:
    backend = RecordingBackend()
    backend.set_sequence(SOURCE_PATCH_TITLE_DEF, patch_sequence)
    backend.set_sequence(SOURCE_GET_DEF, get_sequence or [_FOUND])
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await SourceService(backend).update(_NB, "src", "Renamed", return_object=True)
    error = caught.value
    assert _ops(backend) == expected_ops
    assert error.operation is Operation.SOURCE_UPDATE
    assert error.message == "source.update exceeded its deadline"
    assert error.outcome_unknown is unknown
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == blocked
    assert error.diagnostics["leaf_operation"] is expected_ops[-1]
    projected = project_backend_error(error)
    assert isinstance(projected, RPCTimeoutError)
    assert getattr(projected, "unconfirmed", False) is unknown


@pytest.mark.asyncio
async def test_leaf_failures_are_rebound_to_the_workflow() -> None:
    backend = RecordingBackend()
    backend.set_sequence(
        SOURCE_PATCH_TITLE_DEF,
        [
            scripted_error(
                BackendErrorReason.SERVER, operation=Operation.SOURCE_PATCH_TITLE, dispatched=True
            )
        ],
    )
    backend.set_result(SOURCE_GET_DEF, _FOUND)
    with pytest.raises(BackendError) as caught:
        await SourceService(backend).update(_NB, "src", "Renamed", return_object=True)
    assert caught.value.operation is Operation.SOURCE_UPDATE
    assert caught.value.reason is BackendErrorReason.SERVER
    assert caught.value.dispatched is True
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["leaf_operation"] is Operation.SOURCE_PATCH_TITLE


@pytest.mark.parametrize(
    ("reason", "projected_type"),
    [
        (BackendErrorReason.NETWORK, NetworkError),
        (BackendErrorReason.RATE_LIMIT, RateLimitError),
    ],
)
@pytest.mark.asyncio
async def test_dispatched_network_and_rate_limit_failures_preserve_commit_uncertainty(
    reason: BackendErrorReason,
    projected_type: type[Exception],
) -> None:
    backend = RecordingBackend()
    backend.set_sequence(
        SOURCE_PATCH_TITLE_DEF,
        [
            scripted_error(
                reason,
                operation=Operation.SOURCE_PATCH_TITLE,
                dispatched=True,
                diagnostics={"method_id": RPCMethod.UPDATE_SOURCE.value},
            )
        ],
    )
    backend.set_result(SOURCE_GET_DEF, _FOUND)

    with pytest.raises(BackendError) as caught:
        await SourceService(backend).update(_NB, "src", "Renamed", return_object=True)

    error = caught.value
    assert error.operation is Operation.SOURCE_UPDATE
    assert error.reason is reason
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is Operation.SOURCE_PATCH_TITLE
    assert isinstance(project_backend_error(error), projected_type)


@pytest.mark.parametrize("cancel_during_readback", [False, True])
@pytest.mark.asyncio
async def test_cancellation_instance_propagates_without_rebinding(
    cancel_during_readback: bool,
) -> None:
    cancelled = asyncio.CancelledError()
    backend = RecordingBackend()
    backend.set_sequence(
        SOURCE_PATCH_TITLE_DEF,
        [_NULL if cancel_during_readback else cancelled],
    )
    backend.set_sequence(
        SOURCE_GET_DEF,
        [cancelled] if cancel_during_readback else [_FOUND],
    )

    with pytest.raises(asyncio.CancelledError) as caught:
        await SourceService(backend).update(_NB, "src", "Renamed", return_object=True)

    assert caught.value is cancelled
    assert _ops(backend) == (
        [Operation.SOURCE_PATCH_TITLE, Operation.SOURCE_GET]
        if cancel_during_readback
        else [Operation.SOURCE_PATCH_TITLE]
    )


def test_client_composition_threads_its_deadline_factory_into_the_source_facade() -> None:
    client = NotebookLMClient(
        AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session"),
        timeout=17.0,
    )

    factory = client._backend._deadline_factory
    assert isinstance(factory, RuntimeDeadlineFactory)
    assert client.sources._require_source_service()._deadline_factory is factory
    deadline = factory.start()
    assert deadline is not None and deadline.timeout == 17.0


@pytest.mark.asyncio
async def test_web_sequence_and_kwargs_are_byte_identical_to_the_handler_era() -> None:
    hydrated = ["src", ["Renamed", None], None, None, 2, None, None, None, None, None, [2]]
    executor = _RecordingExecutor(None, [["Notebook", [hydrated], _NB]])
    backend = build_web_backend(executor)
    await SourceService(backend).update(_NB, "src", "Renamed", return_object=False)
    methods = [method for method, _params, _kwargs in executor.calls]
    assert methods == [RPCMethod.UPDATE_SOURCE, RPCMethod.GET_NOTEBOOK]
    patch, read = (kwargs for _method, _params, kwargs in executor.calls)
    assert executor.calls[0][1] == [None, ["src"], [[["Renamed"]]]]
    assert patch["source_path"] == f"/notebook/{_NB}"
    assert patch["allow_null"] is True
    assert patch["disable_internal_retries"] is False
    assert read["source_path"] == f"/notebook/{_NB}"
    assert read["allow_null"] is False


@pytest.mark.asyncio
async def test_web_echo_decodes_and_short_circuits_the_hydration_read() -> None:
    echoed = ["src", ["Renamed", None], None, None, 2, None, None, None, None, None, [2]]
    executor = _RecordingExecutor(echoed)

    result = await SourceService(build_web_backend(executor)).update(
        _NB, "src", "Renamed", return_object=True
    )

    assert result.source is not None and result.source.id == "src"
    assert [method for method, _params, _kwargs in executor.calls] == [RPCMethod.UPDATE_SOURCE]
