"""P9.2-10: ``artifact.rename`` is a service-owned two-leaf workflow.

``StudioManagementService.rename`` sequences one ``artifact.patch_title``
mutation and one plain ``artifact.catalog`` readback under a single deadline.
These tests pin the leaf conjunction, call order, error rebinding, not-found
projection, and post-write uncertainty with ``RecordingBackend``.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

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
    ARTIFACT_CATALOG_DEF,
    ARTIFACT_PATCH_TITLE_DEF,
    ARTIFACT_RENAME_DEF,
    ArtifactCatalogInput,
    ArtifactCatalogResult,
    ArtifactPatchTitleInput,
    ArtifactPatchTitleResult,
    ArtifactRecord,
    ArtifactRenameInput,
)
from notebooklm._studio.management import (
    ARTIFACT_NOT_FOUND_PHASE_KEY,
    ARTIFACT_NOT_FOUND_RENAME_READBACK,
    StudioManagementService,
)
from notebooklm._web.policy import (
    SERVICE_OWNED_WORKFLOW_BINDINGS,
    WEB_CALL_POLICY_BINDINGS,
    derive_workflow_natives,
)
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import ArtifactNotFoundError, RPCTimeoutError
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend, scripted_error
from tests._fixtures.web_backend import build_web_backend

_NB = "nb-1"
_ARTIFACT = ArtifactRecord("artifact-1", "Renamed", "report", "completed")
_PATCHED = ArtifactPatchTitleResult()
_CATALOG = ArtifactCatalogResult((_ARTIFACT,))


def _service(
    backend: RecordingBackend,
    factory: RuntimeDeadlineFactory | None = None,
) -> StudioManagementService:
    return StudioManagementService(backend, deadline_factory=factory)


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(ARTIFACT_PATCH_TITLE_DEF, _PATCHED)
    backend.set_result(ARTIFACT_CATALOG_DEF, _CATALOG)
    return backend


def _ops(backend: RecordingBackend) -> list[Operation]:
    return [invocation.operation for invocation in backend.invocations]


def test_artifact_rename_is_service_owned_with_exact_leaf_edges() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.ARTIFACT_RENAME]
    assert binding.service_owned is True and binding.is_supported is False
    assert binding.row is None
    assert Operation.ARTIFACT_RENAME in WEB_SERVICE_OWNED_OPERATIONS
    assert Operation.ARTIFACT_RENAME not in WEB_CALL_POLICY_BINDINGS
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.ARTIFACT_RENAME]
    assert [(leaf.operation, leaf.allowed_variants) for leaf in workflow.leaf_operations] == [
        (Operation.ARTIFACT_PATCH_TITLE, frozenset({None})),
        (Operation.ARTIFACT_CATALOG, frozenset({None})),
    ]
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.RENAME_ARTIFACT, None),
        (RPCMethod.LIST_ARTIFACTS, None),
    }
    assert derive_workflow_natives(workflow) == {
        (native.method, native.variant) for native in workflow.native_bindings
    }


@pytest.mark.asyncio
async def test_backend_refuses_the_workflow_directly() -> None:
    backend = build_web_backend(_NoCallExecutor())
    assert backend.capabilities.supports(Operation.ARTIFACT_RENAME) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            ARTIFACT_RENAME_DEF,
            ArtifactRenameInput(_NB, "artifact-1", "Renamed"),
            deadline=None,
        )


class _NoCallExecutor:
    async def rpc_call(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("service-owned workflow reached the web executor directly")


@pytest.mark.asyncio
async def test_rename_mutates_then_reads_the_plain_catalog() -> None:
    backend = _backend()

    result = await _service(backend).rename(ArtifactRenameInput(_NB, "artifact-1", "Renamed"))

    assert result.artifact is _ARTIFACT
    assert _ops(backend) == [Operation.ARTIFACT_PATCH_TITLE, Operation.ARTIFACT_CATALOG]
    assert [invocation.value for invocation in backend.invocations] == [
        ArtifactPatchTitleInput(_NB, "artifact-1", "Renamed"),
        ArtifactCatalogInput(_NB),
    ]


@pytest.mark.asyncio
async def test_unsupported_catalog_leaf_is_rejected_before_the_title_mutation() -> None:
    backend = RecordingBackend()
    backend.set_result(ARTIFACT_PATCH_TITLE_DEF, _PATCHED)

    with pytest.raises(UnsupportedOperationError) as caught:
        await _service(backend).rename(ArtifactRenameInput(_NB, "artifact-1", "Renamed"))

    assert caught.value.operation is Operation.ARTIFACT_CATALOG
    assert backend.invocations == []


@pytest.mark.asyncio
async def test_one_deadline_identity_covers_mutation_and_readback() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory.fixed(30.0, monotonic=lambda: 100.0)

    await _service(backend, factory).rename(ArtifactRenameInput(_NB, "artifact-1", "Renamed"))

    first, second = (invocation.deadline for invocation in backend.invocations)
    assert isinstance(first, RuntimeDeadline)
    assert second is first
    assert first.timeout == 30.0


@pytest.mark.asyncio
async def test_explicit_deadline_is_never_replaced_by_the_factory() -> None:
    backend = _backend()
    factory = RuntimeDeadlineFactory(lambda: pytest.fail("factory was called"))
    deadline = RuntimeDeadline(timeout=20.0, started_at=50.0, monotonic=lambda: 55.0)

    await _service(backend, factory).rename(
        ArtifactRenameInput(_NB, "artifact-1", "Renamed"),
        deadline=deadline,
    )

    assert all(invocation.deadline is deadline for invocation in backend.invocations)


@pytest.mark.asyncio
async def test_missing_readback_preserves_not_found_evidence_and_public_message() -> None:
    backend = _backend()
    backend.set_result(ARTIFACT_CATALOG_DEF, ArtifactCatalogResult(()))

    with pytest.raises(BackendError) as caught:
        await _service(backend).rename(ArtifactRenameInput(_NB, "missing", "Renamed"))

    error = caught.value
    assert error.operation is Operation.ARTIFACT_RENAME
    assert error.reason is BackendErrorReason.ARTIFACT_NOT_FOUND
    assert error.message == "Artifact not found: missing"
    assert dict(error.diagnostics or {}) == {
        "artifact_id": "missing",
        "artifact_type": None,
        ARTIFACT_NOT_FOUND_PHASE_KEY: ARTIFACT_NOT_FOUND_RENAME_READBACK,
        "raw_response": None,
    }
    projected = project_backend_error(error)
    assert isinstance(projected, ArtifactNotFoundError)
    assert str(projected) == "Artifact not found: missing"
    assert projected.artifact_id == "missing"
    assert projected.method_id == RPCMethod.RENAME_ARTIFACT.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leaf", "method_id", "expected_ops"),
    [
        pytest.param(
            Operation.ARTIFACT_PATCH_TITLE,
            RPCMethod.RENAME_ARTIFACT.value,
            [Operation.ARTIFACT_PATCH_TITLE],
            id="mutation",
        ),
        pytest.param(
            Operation.ARTIFACT_CATALOG,
            RPCMethod.LIST_ARTIFACTS.value,
            [Operation.ARTIFACT_PATCH_TITLE, Operation.ARTIFACT_CATALOG],
            id="readback",
        ),
    ],
)
async def test_leaf_server_errors_are_rebound_to_artifact_rename(
    leaf: Operation,
    method_id: str,
    expected_ops: list[Operation],
) -> None:
    backend = _backend()
    error = scripted_error(
        BackendErrorReason.SERVER,
        operation=leaf,
        dispatched=True,
        message="boom",
        diagnostics={"method_id": method_id},
    )
    if leaf is Operation.ARTIFACT_PATCH_TITLE:
        backend.set_sequence(ARTIFACT_PATCH_TITLE_DEF, [error])
    else:
        backend.set_sequence(ARTIFACT_CATALOG_DEF, [error])

    with pytest.raises(BackendError) as caught:
        await _service(backend).rename(ArtifactRenameInput(_NB, "artifact-1", "Renamed"))

    rebound = caught.value
    assert type(rebound) is BackendError
    assert rebound.operation is Operation.ARTIFACT_RENAME
    assert rebound.reason is BackendErrorReason.SERVER
    assert rebound.message == "boom"
    assert rebound.dispatched is True
    assert rebound.outcome_unknown is False
    assert rebound.diagnostics is not None
    assert rebound.diagnostics["method_id"] == method_id
    assert rebound.diagnostics["leaf_operation"] is leaf
    assert _ops(backend) == expected_ops


def _expiry(
    operation: Operation,
    *,
    dispatched: bool,
    outcome_unknown: bool = False,
) -> BackendDeadlineExceededError:
    method_id = (
        RPCMethod.RENAME_ARTIFACT.value
        if operation is Operation.ARTIFACT_PATCH_TITLE
        else RPCMethod.LIST_ARTIFACTS.value
    )
    return BackendDeadlineExceededError(
        operation,
        outcome_unknown=outcome_unknown,
        diagnostics=MappingProxyType({"timeout": 1.0, "remaining": 0.0, "method_id": method_id}),
        dispatched=dispatched,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "patch_sequence",
        "catalog_sequence",
        "expected_ops",
        "expected_unknown",
        "expected_dispatched",
    ),
    [
        pytest.param(
            [_expiry(Operation.ARTIFACT_PATCH_TITLE, dispatched=False)],
            [_CATALOG],
            [Operation.ARTIFACT_PATCH_TITLE],
            False,
            False,
            id="mutation-pre-dispatch",
        ),
        pytest.param(
            [
                _expiry(
                    Operation.ARTIFACT_PATCH_TITLE,
                    dispatched=True,
                    outcome_unknown=True,
                )
            ],
            [_CATALOG],
            [Operation.ARTIFACT_PATCH_TITLE],
            True,
            True,
            id="mutation-after-dispatch",
        ),
        pytest.param(
            [_PATCHED],
            [_expiry(Operation.ARTIFACT_CATALOG, dispatched=False)],
            [Operation.ARTIFACT_PATCH_TITLE, Operation.ARTIFACT_CATALOG],
            True,
            False,
            id="readback-pre-dispatch-after-write",
        ),
        pytest.param(
            [_PATCHED],
            [_expiry(Operation.ARTIFACT_CATALOG, dispatched=True)],
            [Operation.ARTIFACT_PATCH_TITLE, Operation.ARTIFACT_CATALOG],
            True,
            True,
            id="readback-after-dispatch-after-write",
        ),
    ],
)
async def test_deadline_truth_table_preserves_post_write_uncertainty(
    patch_sequence: list[object],
    catalog_sequence: list[object],
    expected_ops: list[Operation],
    expected_unknown: bool,
    expected_dispatched: bool,
) -> None:
    backend = RecordingBackend()
    backend.set_sequence(ARTIFACT_PATCH_TITLE_DEF, patch_sequence)
    backend.set_sequence(ARTIFACT_CATALOG_DEF, catalog_sequence)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).rename(ArtifactRenameInput(_NB, "artifact-1", "Renamed"))

    error = caught.value
    assert _ops(backend) == expected_ops
    assert error.operation is Operation.ARTIFACT_RENAME
    assert error.message == "artifact.rename exceeded its deadline"
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is expected_unknown
    assert error.dispatched is expected_dispatched
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is expected_ops[-1]
    projected = project_backend_error(error)
    assert isinstance(projected, RPCTimeoutError)
    assert getattr(projected, "unconfirmed", False) is expected_unknown


@pytest.mark.asyncio
async def test_post_write_deadline_rebinding_preserves_the_native_cause() -> None:
    native = RPCTimeoutError("slow", method_id=RPCMethod.LIST_ARTIFACTS.value)
    leaf = _expiry(Operation.ARTIFACT_CATALOG, dispatched=False)
    try:
        raise leaf from native
    except BackendDeadlineExceededError as caused_leaf:
        leaf = caused_leaf
    backend = _backend()
    backend.set_sequence(ARTIFACT_CATALOG_DEF, [leaf])

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _service(backend).rename(ArtifactRenameInput(_NB, "artifact-1", "Renamed"))

    assert caught.value.operation is Operation.ARTIFACT_RENAME
    assert caught.value.outcome_unknown is True
    assert caught.value.__cause__ is native
