"""Transport-neutral construction helpers for the client runtime."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .._client_metrics import ClientMetrics
from .call_supervisor import CallSupervisor

if TYPE_CHECKING:
    from ..types import RpcTelemetryEvent


@dataclass(frozen=True)
class SharedRuntimeConfig:
    """Resolved backend-neutral runtime options retained by the shared owner."""

    max_concurrent_rpcs: int | None
    operation_timeout: float | None = None


@dataclass(frozen=True)
class SharedRuntime:
    """Backend-neutral collaborators produced by :func:`build_collaborators`.

    The construction order inside ``build_collaborators`` is dependency-driven
    (see the inline comments there for the rationale); this container exists
    only to give the client constructor a single hand-off shape after the
    construction phase.
    """

    metrics: ClientMetrics
    call_supervisor: CallSupervisor
    config: SharedRuntimeConfig


# One-release compatibility name for private importers.  The concrete type is
# intentionally the neutral bundle; web-only collaborators live on WebRuntime.
RuntimeCollaborators = SharedRuntime


def validate_shared_runtime_config(
    *,
    max_concurrent_rpcs: int | None,
    operation_timeout: float | None = None,
) -> SharedRuntimeConfig:
    """Validate and return the backend-neutral admission input."""

    if max_concurrent_rpcs is not None and max_concurrent_rpcs < 1:
        raise ValueError(f"max_concurrent_rpcs must be >= 1, got {max_concurrent_rpcs!r}")
    if operation_timeout is not None and (
        isinstance(operation_timeout, bool)
        or not isinstance(operation_timeout, (int, float))
        or not math.isfinite(operation_timeout)
        or operation_timeout <= 0
    ):
        raise ValueError(
            "operation_timeout must be a positive, finite number or None "
            f"(got {operation_timeout!r})"
        )
    return SharedRuntimeConfig(
        max_concurrent_rpcs=max_concurrent_rpcs,
        operation_timeout=operation_timeout,
    )


def build_collaborators(
    config: SharedRuntimeConfig,
    *,
    on_rpc_event: Callable[[RpcTelemetryEvent], object] | None,
) -> SharedRuntime:
    """Construct the extracted runtime collaborators in dependency order.

    The order is dependency-driven so the load-bearing inter-collaborator
    wiring stays obvious to future readers: metrics is built first because
    it absorbs the optional ``on_rpc_event`` callback. The call supervisor
    follows; web request ids,
    auth coordination, kernels, and cookie persistence are constructed by the
    web composition module.
    """
    # Observability counters + telemetry callback. ``metrics_snapshot``
    # remains the lock-safe read path; helper-level tests that need
    # implementation state read ``self._metrics_obj`` directly.
    metrics = ClientMetrics(on_rpc_event=on_rpc_event)
    call_supervisor = CallSupervisor(
        metrics=metrics,
        max_concurrent_rpcs=config.max_concurrent_rpcs,
        operation_timeout=config.operation_timeout,
    )
    return SharedRuntime(
        metrics=metrics,
        call_supervisor=call_supervisor,
        config=config,
    )


__all__ = [
    "RuntimeCollaborators",
    "SharedRuntimeConfig",
    "SharedRuntime",
    "build_collaborators",
    "validate_shared_runtime_config",
]
