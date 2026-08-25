"""P9.2-3: ``collection.update`` is a service-owned workflow sequenced from leaves.

The account-level dialect of the same ``LabelSetService.update`` workflow:
``collection.get`` preflight/readback plus one ``label.mutate`` per notebook
member (``add_notebooks``/``remove_notebooks`` variants), the dialect guards
raised before any leaf, one workflow deadline, and leaf failures rebound to
``collection.update``. These replace the backend-level ``COLLECTION_UPDATE``
oracles (outcome-unknown truth table, registry pins).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
)
from notebooklm._backend_compat import project_backend_error
from notebooklm._collections import CollectionsAPI
from notebooklm._deadline import RuntimeDeadlineFactory
from notebooklm._label_service import NOT_FOUND_PHASE_KEY, NOT_FOUND_PREFLIGHT, LabelSetService
from notebooklm._operations import Operation
from notebooklm._records import (
    COLLECTION_GET_DEF,
    COLLECTION_UPDATE_DEF,
    LABEL_MUTATE_DEF,
    LabelGetResult,
    LabelKind,
    LabelMutateInput,
    LabelMutateResult,
    LabelRecord,
    LabelUpdateInput,
)
from notebooklm._web.policy import SERVICE_OWNED_WORKFLOW_BINDINGS, derive_workflow_natives
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import CollectionNotFoundError, RPCTimeoutError
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend
from tests._fixtures.web_backend import build_web_backend

_COLLECTION = LabelRecord("c1", "Old", LabelKind.COLLECTION, None, emoji="", member_ids=("n1",))
_FOUND = LabelGetResult(label=_COLLECTION)
_MISSING = LabelGetResult(label=None)
_DONE = LabelMutateResult()


def _service(backend: RecordingBackend, factory: RuntimeDeadlineFactory | None = None):
    return LabelSetService(backend, LabelKind.COLLECTION, deadline_factory=factory)


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(COLLECTION_GET_DEF, _FOUND)
    backend.set_result(LABEL_MUTATE_DEF, _DONE)
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


def test_collection_update_is_service_owned_with_the_collection_edges() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.COLLECTION_UPDATE]
    assert binding.service_owned is True and binding.is_supported is False
    assert Operation.COLLECTION_UPDATE in WEB_SERVICE_OWNED_OPERATIONS
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.COLLECTION_UPDATE]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.COLLECTION_GET, frozenset({None})),
        (Operation.LABEL_MUTATE, frozenset({None, "add_notebooks", "remove_notebooks"})),
    ]
    # The shared primitive exposes different variants to each family; the edge
    # form keeps the parity audit from attributing both families to both.
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.LIST_LABELS, None),
        (RPCMethod.UPDATE_LABEL, None),
        (RPCMethod.UPDATE_LABEL, "add_notebooks"),
        (RPCMethod.UPDATE_LABEL, "remove_notebooks"),
    }
    assert derive_workflow_natives(workflow) == {
        (native.method, native.variant) for native in workflow.native_bindings
    }


@pytest.mark.asyncio
async def test_backend_refuses_the_workflow_directly() -> None:
    backend = build_web_backend(_RecordingExecutor())
    assert backend.capabilities.supports(Operation.COLLECTION_UPDATE) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            COLLECTION_UPDATE_DEF,
            LabelUpdateInput(LabelKind.COLLECTION, "c1", name="X"),
            deadline=None,
        )


@pytest.mark.asyncio
async def test_membership_and_rename_sequences() -> None:
    backend = _backend()
    await _service(backend).update("c1", add_member_ids=("n2",), remove_member_ids=("n1",))
    assert _ops(backend) == [
        Operation.LABEL_MUTATE,
        Operation.LABEL_MUTATE,
        Operation.COLLECTION_GET,
    ]
    assert [invocation.value for invocation in backend.invocations[:2]] == [
        LabelMutateInput(LabelKind.COLLECTION, "c1", None, add_member_id="n2"),
        LabelMutateInput(LabelKind.COLLECTION, "c1", None, remove_member_id="n1"),
    ]

    backend = _backend()
    assert await _service(backend).update("c1", name="New", return_object=False) is None
    assert _ops(backend) == [Operation.COLLECTION_GET, Operation.LABEL_MUTATE]
    assert backend.invocations[1].value == LabelMutateInput(
        LabelKind.COLLECTION, "c1", None, name="New", emoji=""
    )


@pytest.mark.asyncio
async def test_emoji_only_mask_is_rejected_before_any_leaf() -> None:
    backend = _backend()
    with pytest.raises(BackendContractError, match="a name is required") as caught:
        await _service(backend).update("c1", emoji="\U0001f525")
    assert caught.value.operation is Operation.COLLECTION_UPDATE
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_unsupported_leaf_is_rejected_before_any_side_effect() -> None:
    backend = RecordingBackend()
    backend.set_result(COLLECTION_GET_DEF, _FOUND)
    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).update("c1", name="New")
    assert caught.value.operation is Operation.LABEL_MUTATE
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_not_found_projects_to_the_collection_class_with_the_legacy_method_id() -> None:
    backend = _backend()
    backend.set_result(COLLECTION_GET_DEF, _MISSING)
    with pytest.raises(BackendError) as caught:
        await _service(backend).update("c1", name="New", return_object=False)
    error = caught.value
    assert error.operation is Operation.COLLECTION_UPDATE
    assert error.reason is BackendErrorReason.LABEL_NOT_FOUND
    assert error.message == "Collection not found: c1"
    assert error.diagnostics is not None
    assert error.diagnostics["label_kind"] == "collection"
    assert error.diagnostics[NOT_FOUND_PHASE_KEY] == NOT_FOUND_PREFLIGHT
    projected = project_backend_error(error)
    assert isinstance(projected, CollectionNotFoundError)
    assert projected.collection_id == "c1"
    assert projected.method_id == RPCMethod.UPDATE_LABEL.value


def _expiry(operation: Operation, *, dispatched: bool, method_id: str) -> BackendError:
    return BackendDeadlineExceededError(
        operation,
        outcome_unknown=dispatched and operation is Operation.LABEL_MUTATE,
        diagnostics=MappingProxyType({"timeout": 1.0, "remaining": 0.0, "method_id": method_id}),
        dispatched=dispatched,
    )


@pytest.mark.parametrize(
    ("kwargs", "get_sequence", "mutate_sequence", "expected_ops", "unknown"),
    [
        pytest.param(
            {"name": "Renamed"},
            [_FOUND, _expiry(Operation.COLLECTION_GET, dispatched=False, method_id="list")],
            [_DONE],
            [Operation.COLLECTION_GET, Operation.LABEL_MUTATE, Operation.COLLECTION_GET],
            True,
            id="collection-field-readback",
        ),
        pytest.param(
            {"add_member_ids": ("nb-member-1",)},
            [_expiry(Operation.COLLECTION_GET, dispatched=False, method_id="list")],
            [_DONE],
            [Operation.LABEL_MUTATE, Operation.COLLECTION_GET],
            True,
            id="collection-membership-readback",
        ),
        pytest.param(
            {"add_member_ids": ("nb-member-1", "nb-member-2")},
            [],
            [_DONE, _expiry(Operation.LABEL_MUTATE, dispatched=False, method_id="mut")],
            [Operation.LABEL_MUTATE, Operation.LABEL_MUTATE],
            True,
            id="collection-second-membership-write",
        ),
        pytest.param(
            {"name": "Renamed"},
            [_expiry(Operation.COLLECTION_GET, dispatched=False, method_id="list")],
            [],
            [Operation.COLLECTION_GET],
            False,
            id="collection-preflight-read-only",
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
) -> None:
    backend = RecordingBackend()
    backend.set_sequence(COLLECTION_GET_DEF, get_sequence or [_FOUND])
    backend.set_sequence(LABEL_MUTATE_DEF, mutate_sequence or [_DONE])
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).update("c1", **kwargs)
    error = caught.value
    assert _ops(backend) == expected_ops
    assert error.operation is Operation.COLLECTION_UPDATE
    assert error.message == "collection.update exceeded its deadline"
    assert error.outcome_unknown is unknown
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is expected_ops[-1]
    projected = project_backend_error(error)
    assert isinstance(projected, RPCTimeoutError)
    assert getattr(projected, "unconfirmed", False) is unknown


@pytest.mark.asyncio
async def test_one_deadline_identity_covers_every_leaf() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)
    await _service(backend, factory).update("c1", name="New")
    deadlines = [invocation.deadline for invocation in backend.invocations]
    assert len(deadlines) == 3 and all(d is deadlines[0] for d in deadlines)
    assert deadlines[0] is not None and deadlines[0].timeout == 30.0


_COLLECTION_ROW = ["Old", ["n1"], "c1", ""]


@pytest.mark.asyncio
async def test_facade_sequence_and_kwargs_are_byte_identical_to_the_handler_era() -> None:
    executor = _RecordingExecutor([], [None, [_COLLECTION_ROW]])
    api = CollectionsAPI(build_web_backend(executor), list_notebooks=None)  # type: ignore[arg-type]
    await api.remove_notebooks("c1", ["n1"])
    methods = [method for method, _params, _kwargs in executor.calls]
    assert methods == [RPCMethod.UPDATE_LABEL, RPCMethod.LIST_LABELS]
    write, readback = (kwargs for _method, _params, kwargs in executor.calls)
    assert write["operation_variant"] == "remove_notebooks"
    assert write["source_path"] == "/"
    assert write["allow_null"] is True
    assert executor.calls[0][1][3] == [[None, None, None, None, [["n1"]]], []]
    assert readback["source_path"] == "/"
    assert readback["allow_null"] is True
    assert readback["operation_variant"] is None


@pytest.mark.asyncio
async def test_facade_rename_carries_the_preflight_emoji_and_public_not_found_text() -> None:
    executor = _RecordingExecutor([None, [["Old", None, "c1", "\U0001f525"]]], [], [None, []])
    api = CollectionsAPI(build_web_backend(executor), list_notebooks=None)  # type: ignore[arg-type]
    await api.rename("c1", "New", return_object=False)
    assert executor.calls[1][1][3] == [[["New", "\U0001f525"]]]

    executor = _RecordingExecutor([None, []])
    api = CollectionsAPI(build_web_backend(executor), list_notebooks=None)  # type: ignore[arg-type]
    with pytest.raises(CollectionNotFoundError) as missing:
        await api.rename("missing", "X", return_object=False)
    assert missing.value.collection_id == "missing"
    assert missing.value.method_id == RPCMethod.UPDATE_LABEL.value
