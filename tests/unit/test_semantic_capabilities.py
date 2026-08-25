"""The two capability views of the semantic backend port (P10 invariant I5).

``BackendCapabilities`` answers two different questions and must keep answering
them differently:

* ``supports(op)`` — "may dispatch hand ``op`` to ``invoke``?" This is what
  ``WebRpcBackend.invoke`` and ``require_leaves`` gate on. A service-owned
  workflow is deliberately *false*: the backend refuses it so a semantic
  service has to sequence the leaves.
* ``available(op)`` — "can the client perform ``op`` at all?" This is the union
  with ``workflows``, and it is what defect N2 was about: before it, a
  service-owned workflow was indistinguishable from a feature the backend does
  not have.

The regression these tests guard is a well-meaning widening of ``supports()``
to mean ``available()``, which would silently let ``invoke`` accept a workflow
it cannot execute.
"""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._backend import (
    BackendCapabilities,
    UnsupportedOperationError,
    require_leaves,
)
from notebooklm._binding import OperationDisposition
from notebooklm._notebook_records import NotebookCreateInput
from notebooklm._operations import Operation, OperationTier
from notebooklm._records import NOTEBOOK_CREATE_DEF
from notebooklm._web.registry import (
    WEB_OPERATION_REGISTRY,
    WEB_SERVICE_OWNED_OPERATIONS,
    WEB_SUPPORTED_OPERATIONS,
)
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend
from tests._fixtures.web_backend import build_web_backend


class _RefusingExecutor:
    """An rpc owner that fails loudly if any capability check reaches the wire."""

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        raise AssertionError(f"capability inspection must not dispatch {method}")


def _unsupported_operations() -> frozenset[Operation]:
    return frozenset(
        operation
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.disposition is OperationDisposition.UNSUPPORTED
    )


def test_service_owned_workflow_is_available_but_not_directly_supported() -> None:
    """The N2 acceptance case: ``notebook.create`` runs, but not through invoke."""
    capabilities = build_web_backend(_RefusingExecutor()).capabilities

    assert capabilities.available(Operation.NOTEBOOK_CREATE) is True
    assert capabilities.supports(Operation.NOTEBOOK_CREATE) is False


def test_research_wait_stays_unavailable_until_it_gains_a_typed_definition() -> None:
    """``research.wait`` has an enum member but no def; R6.4 flips it."""
    capabilities = build_web_backend(_RefusingExecutor()).capabilities

    assert capabilities.available(Operation.RESEARCH_WAIT) is False
    assert capabilities.supports(Operation.RESEARCH_WAIT) is False


def test_product_operations_are_the_vocabulary_minus_the_primitives() -> None:
    """97 members are 87 product operations plus the ten decomposition leaves."""
    primitives = {
        operation
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.definition is not None and binding.definition.tier is OperationTier.PRIMITIVE
    }

    assert len(Operation) == 97
    assert len(primitives) == 10
    assert len(Operation) - len(primitives) == 87


def test_every_disposition_maps_to_exactly_one_pair_of_capability_answers() -> None:
    """Direct rows: both true. Workflows: available only. Unsupported: neither."""
    capabilities = build_web_backend(_RefusingExecutor()).capabilities

    for operation in WEB_SUPPORTED_OPERATIONS:
        assert capabilities.supports(operation) is True
        assert capabilities.available(operation) is True
    for operation in WEB_SERVICE_OWNED_OPERATIONS:
        assert capabilities.supports(operation) is False
        assert capabilities.available(operation) is True
    for operation in _unsupported_operations():
        assert capabilities.supports(operation) is False
        assert capabilities.available(operation) is False

    assert (
        WEB_SUPPORTED_OPERATIONS | WEB_SERVICE_OWNED_OPERATIONS | _unsupported_operations()
        == frozenset(Operation)
    )


@pytest.mark.asyncio
async def test_invoke_and_require_leaves_still_gate_on_supports_not_available() -> None:
    """Widening ``supports()`` would break both gates; they must stay on it."""
    backend = build_web_backend(_RefusingExecutor())

    assert backend.capabilities.available(Operation.NOTEBOOK_CREATE) is True
    with pytest.raises(UnsupportedOperationError) as invoked:
        await backend.invoke(NOTEBOOK_CREATE_DEF, NotebookCreateInput("Title"), deadline=None)
    assert invoked.value.operation is Operation.NOTEBOOK_CREATE

    with pytest.raises(UnsupportedOperationError) as required:
        require_leaves(backend, Operation.NOTEBOOK_CREATE)
    assert required.value.operation is Operation.NOTEBOOK_CREATE


def test_default_capabilities_declare_no_workflows_and_collapse_the_two_views() -> None:
    """Without ``workflows`` the union is a no-op, so existing backends are unchanged."""
    empty = BackendCapabilities()
    direct = BackendCapabilities(frozenset({Operation.NOTEBOOK_LIST}))

    assert empty.workflows == frozenset()
    assert empty.available(Operation.NOTEBOOK_LIST) is False
    assert direct.available(Operation.NOTEBOOK_LIST) == direct.supports(Operation.NOTEBOOK_LIST)


def test_capabilities_stay_a_frozen_hashable_value_with_both_views() -> None:
    """``workflows`` participates in equality; it is data, not a live view."""
    one = BackendCapabilities(
        frozenset({Operation.NOTEBOOK_LIST}), workflows=frozenset({Operation.NOTEBOOK_CREATE})
    )
    same = BackendCapabilities(
        frozenset({Operation.NOTEBOOK_LIST}), workflows=frozenset({Operation.NOTEBOOK_CREATE})
    )
    other = BackendCapabilities(frozenset({Operation.NOTEBOOK_LIST}))

    assert one == same
    assert hash(one) == hash(same)
    assert one != other


@pytest.mark.asyncio
async def test_recording_backend_mirrors_the_workflow_view_without_accepting_it() -> None:
    """The service-test fake reproduces the web backend's two-view split."""
    backend = RecordingBackend()
    backend.set_workflows(Operation.NOTEBOOK_CREATE)

    assert backend.capabilities.available(Operation.NOTEBOOK_CREATE) is True
    assert backend.capabilities.supports(Operation.NOTEBOOK_CREATE) is False
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(NOTEBOOK_CREATE_DEF, NotebookCreateInput("Title"), deadline=None)


def test_every_unsupported_operation_states_why_rather_than_a_generic_string() -> None:
    """N2: each closed disposition names what runs the operation today."""
    reasons = {
        operation.value: WEB_OPERATION_REGISTRY[operation].unsupported_reason
        for operation in _unsupported_operations()
    }

    assert set(reasons) == {
        "notebook.metadata",
        "label.sources",
        "collection.notebooks",
        "research.wait",
        "research.import_verify",
    }
    assert len(set(reasons.values())) == len(reasons)
    for operation_value, reason in reasons.items():
        assert reason is not None
        assert "without a typed def" in reason, operation_value
    assert "R6.4" in reasons["research.wait"]
    assert "R6.4" in reasons["research.import_verify"]
    assert "R6.2" in reasons["notebook.metadata"]
