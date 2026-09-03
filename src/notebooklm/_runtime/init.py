"""Transport-neutral construction helpers for the client runtime.

This module validates constructor arguments and builds only the collaborators
shared by every backend.  Web transport composition lives in
:mod:`notebooklm._web.transport.init`; keeping that boundary explicit lets an
Android-only client avoid importing or allocating the web stack once assembly
becomes backend-conditional.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .._client_metrics import ClientMetrics
from .call_supervisor import CallSupervisor
from .config import normalize_max_concurrent_uploads
from .helpers import _resolve_keepalive_interval
from .lifecycle import ClientLifecycle

if TYPE_CHECKING:
    # Runtime import of ``ConnectionLimits`` is deferred to
    # :func:`validate_constructor_args` to keep the long-standing
    # defensive guard against the historical ``types.py`` -> runtime
    # construction cycle (see the inline comment in the function body).
    from ..types import ConnectionLimits, RpcTelemetryEvent


@dataclass(frozen=True)
class ValidatedSessionConfig:
    """Validated + normalized scalar configuration produced by
    :func:`validate_constructor_args`.

    Everything in here is either a value the caller supplied that passed
    validation, a normalized form (e.g. the keepalive interval clamped
    to the minimum-interval floor), or a seam callable resolved through
    the canonical module-attribute lookup that ``None`` defaults trigger.
    Web-only defaults are resolved by
    :func:`notebooklm._web.transport.init.compose_client_internals` before
    this neutral configuration is built.
    """

    timeout: float
    connect_timeout: float
    limits: ConnectionLimits
    refresh_retry_delay: float
    rate_limit_max_retries: int
    server_error_max_retries: int
    max_concurrent_rpcs: int | None
    keepalive_interval: float | None
    keepalive_storage_path: Path | None
    decode_response: Callable[..., Any]
    sleep: Callable[[float], Awaitable[Any]]
    is_auth_error: Callable[[Exception], bool]
    async_client_factory: Callable[..., httpx.AsyncClient]


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
    _lifecycle: ClientLifecycle | None

    @property
    def lifecycle(self) -> ClientLifecycle:
        """Return the root after final client assembly has frozen the graph."""
        if self._lifecycle is None:
            raise RuntimeError("Client lifecycle has not been assembled.")
        return self._lifecycle


# One-release compatibility name for private importers.  The concrete type is
# intentionally the neutral bundle; web-only collaborators live on WebRuntime.
RuntimeCollaborators = SharedRuntime


def validate_constructor_args(
    *,
    timeout: float,
    connect_timeout: float,
    refresh_retry_delay: float,
    rate_limit_max_retries: int,
    server_error_max_retries: int,
    keepalive: float | None,
    keepalive_min_interval: float,
    keepalive_storage_path: Path | None,
    auth_storage_path: Path | None,
    limits: ConnectionLimits | None,
    max_concurrent_uploads: int | None,
    max_concurrent_rpcs: int | None,
    decode_response: Callable[..., Any],
    sleep: Callable[[float], Awaitable[Any]],
    is_auth_error: Callable[[Exception], bool],
    async_client_factory: Callable[..., httpx.AsyncClient],
) -> ValidatedSessionConfig:
    """Validate and normalize the scalar args for client internals.

    Mirrors the original validation/normalization behavior one-for-one:
    same ``ValueError`` messages, same order of checks. The seam callables
    (``decode_response`` / ``sleep`` / ``is_auth_error`` /
    ``async_client_factory``) are already resolved by the web composition
    caller against the final client-side seam bindings; this neutral helper
    only validates and normalizes the resulting values.
    The returned :class:`ValidatedSessionConfig` is consumed by
    :func:`build_collaborators` and :func:`wire_middleware_chain`.

    Raises:
        ValueError: If ``rate_limit_max_retries`` / ``server_error_max_retries``
            is negative, if ``max_concurrent_uploads`` /
            ``max_concurrent_rpcs`` is a non-positive integer, or if
            ``keepalive`` / ``keepalive_min_interval`` is not a positive
            finite number.
    """
    if limits is not None:
        _resolved_limits = limits
    else:
        # Lazy import — defensive guard against the ``types.py`` ->
        # runtime-construction import cycle.
        from ..types import ConnectionLimits

        _resolved_limits = ConnectionLimits()

    if rate_limit_max_retries < 0:
        raise ValueError(f"rate_limit_max_retries must be >= 0, got {rate_limit_max_retries}")
    if server_error_max_retries < 0:
        raise ValueError(f"server_error_max_retries must be >= 0, got {server_error_max_retries}")

    # Fail-fast validation for ``max_concurrent_uploads``. The value is
    # NOT propagated into :class:`ValidatedSessionConfig` because the
    # actual upload semaphore state is owned by
    # ``SourceUploadPipeline`` (not the client-runtime composition
    # helpers); this call exists
    # solely for the ``ValueError``-raising side effect on the
    # constructor's behalf — same shape as the inline check it
    # replaced.
    normalize_max_concurrent_uploads(max_concurrent_uploads)

    # RPC-fanout throttle. ``None`` means "no
    # gate" (caller has an external rate-limiter, or this is a
    # single-shot CLI invocation). Default ``DEFAULT_MAX_CONCURRENT_RPCS``
    # (16) sits well below the default ``ConnectionLimits.max_connections``
    # so helper GET/POSTs outside the RPC pipeline still have pool
    # headroom. Cross-validation with ``limits.max_connections`` is
    # enforced one layer up at ``NotebookLMClient.__init__`` because
    # this helper synthesizes its own ``ConnectionLimits()`` when
    # ``limits=None``, masking the relationship at this layer.
    resolved_max_concurrent_rpcs: int | None
    if max_concurrent_rpcs is None:
        resolved_max_concurrent_rpcs = None
    else:
        if max_concurrent_rpcs < 1:
            raise ValueError(f"max_concurrent_rpcs must be >= 1, got {max_concurrent_rpcs!r}")
        resolved_max_concurrent_rpcs = max_concurrent_rpcs

    # Prefer the explicit storage_path if provided (e.g.
    # ``NotebookLMClient(storage_path=...)`` with a manually-built
    # ``AuthTokens``), otherwise fall back to ``auth.storage_path``.
    resolved_storage_path: Path | None = (
        keepalive_storage_path if keepalive_storage_path is not None else auth_storage_path
    )

    return ValidatedSessionConfig(
        timeout=timeout,
        connect_timeout=connect_timeout,
        limits=_resolved_limits,
        refresh_retry_delay=refresh_retry_delay,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        max_concurrent_rpcs=resolved_max_concurrent_rpcs,
        keepalive_interval=_resolve_keepalive_interval(keepalive, keepalive_min_interval),
        keepalive_storage_path=resolved_storage_path,
        decode_response=decode_response,
        sleep=sleep,
        is_auth_error=is_auth_error,
        async_client_factory=async_client_factory,
    )


def build_collaborators(
    config: ValidatedSessionConfig,
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
    )
    return SharedRuntime(
        metrics=metrics,
        call_supervisor=call_supervisor,
        _lifecycle=None,
    )


__all__ = [
    "RuntimeCollaborators",
    "SharedRuntime",
    "ValidatedSessionConfig",
    "build_collaborators",
    "validate_constructor_args",
]
