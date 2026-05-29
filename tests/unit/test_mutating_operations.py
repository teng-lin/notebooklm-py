"""Audit tests for the mutating operation compatibility shim."""

from __future__ import annotations

from notebooklm._idempotency import (
    IDEMPOTENCY_REGISTRY,
    IdempotencyPolicy,
    resolve_effective_disable_internal_retries,
)
from notebooklm._mutating_operations import (
    MUTATING_OPERATION_POLICIES,
    RecoveryKind,
    iter_mutating_operation_policies,
)
from notebooklm.rpc import RPCMethod


def _probe_then_create_registry_keys() -> set[tuple[RPCMethod, str | None]]:
    return {
        (method, variant)
        for method, variant, entry in IDEMPOTENCY_REGISTRY.iter_entries()
        if entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE
    }


def test_mutating_operation_shim_does_not_add_policy_data() -> None:
    """The shim must derive retry-policy membership from the active registry."""
    assert set(MUTATING_OPERATION_POLICIES) == _probe_then_create_registry_keys()


def test_every_mutating_operation_policy_matches_registry_probe_then_create() -> None:
    for policy in iter_mutating_operation_policies():
        entry = IDEMPOTENCY_REGISTRY.get_entry(
            policy.method,
            operation_variant=policy.variant,
        )

        assert entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE


def test_disable_only_mutating_operations_document_reason() -> None:
    for policy in iter_mutating_operation_policies():
        if policy.recovery is RecoveryKind.DISABLE_ONLY:
            assert policy.disable_only_reason.strip()


def test_create_notebook_is_probe_then_create_and_forces_retry_disable() -> None:
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.CREATE_NOTEBOOK)

    assert entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE
    assert (
        resolve_effective_disable_internal_retries(
            IDEMPOTENCY_REGISTRY,
            RPCMethod.CREATE_NOTEBOOK,
            caller_disable_internal_retries=False,
            operation_variant=None,
        )
        is True
    )
    assert (
        MUTATING_OPERATION_POLICIES[(RPCMethod.CREATE_NOTEBOOK, None)].recovery
        is RecoveryKind.EXECUTABLE
    )
