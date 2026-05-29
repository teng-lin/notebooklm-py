"""Compatibility shim for probe-then-create recovery metadata.

The active retry taxonomy lives in :mod:`notebooklm._idempotency`. This
module intentionally derives its keys from ``IDEMPOTENCY_REGISTRY`` so it
cannot become a second source of truth for which RPCs suppress blind
transport retries. The only data retained here is the legacy recovery
label used by existing tests and private importers.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

from ._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyEntry, IdempotencyPolicy
from .rpc.types import RPCMethod


class RecoveryKind(str, Enum):
    """Whether a PROBE_THEN_CREATE operation has executable recovery today."""

    EXECUTABLE = "executable"
    DISABLE_ONLY = "disable_only"


@dataclass(frozen=True)
class MutatingOperationPolicy:
    """Recovery inventory row for one ``PROBE_THEN_CREATE`` registry entry."""

    method: RPCMethod
    variant: str | None
    recovery: RecoveryKind
    disable_only_reason: str = ""


_EXECUTABLE_RECOVERY_KEYS: frozenset[tuple[RPCMethod, str | None]] = frozenset(
    {
        (RPCMethod.CREATE_NOTEBOOK, None),
        (RPCMethod.ADD_SOURCE, "url"),
        (RPCMethod.ADD_SOURCE, "drive"),
        (RPCMethod.ADD_SOURCE_FILE, None),
    }
)


def _build_policy(
    method: RPCMethod,
    variant: str | None,
    entry: IdempotencyEntry,
) -> MutatingOperationPolicy:
    key = (method, variant)
    if key in _EXECUTABLE_RECOVERY_KEYS:
        return MutatingOperationPolicy(
            method=method,
            variant=variant,
            recovery=RecoveryKind.EXECUTABLE,
        )
    return MutatingOperationPolicy(
        method=method,
        variant=variant,
        recovery=RecoveryKind.DISABLE_ONLY,
        disable_only_reason=entry.notes,
    )


MUTATING_OPERATION_POLICIES: dict[tuple[RPCMethod, str | None], MutatingOperationPolicy] = {
    (method, variant): _build_policy(method, variant, entry)
    for method, variant, entry in IDEMPOTENCY_REGISTRY.iter_entries()
    if entry.policy is IdempotencyPolicy.PROBE_THEN_CREATE
}


for (method, variant), policy in MUTATING_OPERATION_POLICIES.items():
    if (policy.method, policy.variant) != (method, variant):
        raise ValueError(
            "MUTATING_OPERATION_POLICIES key does not match policy payload: "
            f"key=({method}, {variant!r}), "
            f"payload=({policy.method}, {policy.variant!r})"
        )


def iter_mutating_operation_policies() -> Iterator[MutatingOperationPolicy]:
    """Return an iterator over registered mutating operation recovery policies."""
    return iter(MUTATING_OPERATION_POLICIES.values())


__all__ = [
    "MUTATING_OPERATION_POLICIES",
    "MutatingOperationPolicy",
    "RecoveryKind",
    "iter_mutating_operation_policies",
]
