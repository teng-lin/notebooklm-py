"""Audit inventory for mutating operations that suppress blind retries."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

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


MUTATING_OPERATION_POLICIES: dict[tuple[RPCMethod, str | None], MutatingOperationPolicy] = {
    (RPCMethod.CREATE_NOTEBOOK, None): MutatingOperationPolicy(
        method=RPCMethod.CREATE_NOTEBOOK,
        variant=None,
        recovery=RecoveryKind.EXECUTABLE,
    ),
    (RPCMethod.ADD_SOURCE, "url"): MutatingOperationPolicy(
        method=RPCMethod.ADD_SOURCE,
        variant="url",
        recovery=RecoveryKind.EXECUTABLE,
    ),
    (RPCMethod.ADD_SOURCE, "drive"): MutatingOperationPolicy(
        method=RPCMethod.ADD_SOURCE,
        variant="drive",
        recovery=RecoveryKind.EXECUTABLE,
    ),
    (RPCMethod.ADD_SOURCE_FILE, None): MutatingOperationPolicy(
        method=RPCMethod.ADD_SOURCE_FILE,
        variant=None,
        recovery=RecoveryKind.EXECUTABLE,
    ),
    (RPCMethod.CREATE_ARTIFACT, None): MutatingOperationPolicy(
        method=RPCMethod.CREATE_ARTIFACT,
        variant=None,
        recovery=RecoveryKind.DISABLE_ONLY,
        disable_only_reason=(
            "artifact generation disables blind retries today; list-based "
            "probe recovery is not implemented yet"
        ),
    ),
    (RPCMethod.GENERATE_MIND_MAP, None): MutatingOperationPolicy(
        method=RPCMethod.GENERATE_MIND_MAP,
        variant=None,
        recovery=RecoveryKind.DISABLE_ONLY,
        disable_only_reason=(
            "mind-map generation disables blind retries today; no executable "
            "probe wrapper binds a lost generation response to a later result"
        ),
    ),
    (RPCMethod.SHARE_NOTEBOOK, None): MutatingOperationPolicy(
        method=RPCMethod.SHARE_NOTEBOOK,
        variant=None,
        recovery=RecoveryKind.DISABLE_ONLY,
        disable_only_reason=(
            "sharing disables blind retries today; GET_SHARE_STATUS-based "
            "probe recovery is not implemented yet"
        ),
    ),
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
