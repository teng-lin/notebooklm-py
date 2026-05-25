"""Construction-time helpers extracted from :class:`Session.__init__`.

Mechanical decomposition of ``Session.__init__`` (``docs/improvement.md``
§3.1) into three concerns:
:func:`validate_constructor_args` (kwarg validation + normalization),
:func:`build_collaborators` (the 8 collaborators in dependency order),
and :func:`wire_middleware_chain` (the seven-middleware ADR-009 chain).
Behavior is bit-for-bit identical to the pre-extraction ``__init__``;
dependency-ordering and seam-resolution comments are preserved verbatim
inside the helpers so future readers see *why* the order matters.

Builds on the constructor-DI work in #1027 (``36dcc634`` —
"refactor(session): constructor DI for late-bound test seams; drop
http_client.setter"), which eliminated the late-binding wrappers and
the ``Kernel.http_client.setter`` and made ``decode_response`` /
``sleep`` / ``is_auth_error`` / ``async_client_factory`` the canonical
injection seams.

**Seam-resolution boundary**: ``None``-default resolution for ``sleep``
(→ ``asyncio.sleep``) and ``async_client_factory`` (→
``httpx.AsyncClient``) MUST stay in :mod:`notebooklm._session` so the
documented monkeypatch paths ``notebooklm._session.asyncio.sleep`` and
``notebooklm._session.httpx.AsyncClient`` keep steering the seam at
construction time. The helpers here accept already-resolved seam
callables; the caller (``Session.__init__``) owns the
``X if X is not None else <module-attr>`` dance against its own bindings.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ._client_metrics import ClientMetrics
from ._cookie_persistence import CookiePersistence
from ._kernel import Kernel
from ._middleware import Middleware, NextCall, build_chain
from ._middleware_chain import MiddlewareChainBuilder
from ._polling_registry import PollRegistry
from ._reqid_counter import ReqidCounter
from ._session_auth import AuthRefreshCoordinator
from ._session_config import normalize_max_concurrent_uploads
from ._session_helpers import _resolve_keepalive_interval
from ._session_lifecycle import ClientLifecycle, CookieRotator, CookieSaver
from ._transport_drain import TransportDrainTracker
from .auth import AuthTokens

if TYPE_CHECKING:
    # Runtime import of ``ConnectionLimits`` is deferred to
    # :func:`validate_constructor_args` to keep the long-standing
    # defensive guard against the ``types.py`` → session cycle (see the
    # inline comment in the function body).
    from .types import ConnectionLimits, RpcTelemetryEvent


@dataclass(frozen=True)
class ValidatedSessionConfig:
    """Validated + normalized scalar configuration produced by
    :func:`validate_constructor_args`.

    Everything in here is either a value the caller supplied that passed
    validation, a normalized form (e.g. the keepalive interval clamped
    to the minimum-interval floor), or a seam callable resolved through
    the canonical module-attribute lookup that ``None`` defaults trigger
    (where applicable — see the module docstring for which seams are
    resolved here vs. in ``Session.__init__``).
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
class SessionCollaborators:
    """Constructed-collaborator bundle produced by
    :func:`build_collaborators`.

    The construction order inside ``build_collaborators`` mirrors the
    pre-extraction ``Session.__init__`` exactly (see the inline comments
    there for the rationale); this container exists only to give
    ``__init__`` a single hand-off shape after the construction phase.
    """

    metrics: ClientMetrics
    drain_tracker: TransportDrainTracker
    reqid: ReqidCounter
    auth_coord: AuthRefreshCoordinator
    kernel: Kernel
    lifecycle: ClientLifecycle
    cookie_persistence: CookiePersistence
    poll_registry: PollRegistry


@dataclass(frozen=True)
class WiredMiddleware:
    """Wired middleware chain produced by :func:`wire_middleware_chain`."""

    chain_builder: MiddlewareChainBuilder
    middlewares: list[Middleware]
    authed_post_chain: NextCall


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
    """Validate and normalize the scalar args to :class:`Session.__init__`.

    Mirrors the original validation/normalization block of
    ``Session.__init__`` one-for-one — same ``ValueError`` messages, same
    order of checks. The seam callables (``decode_response`` / ``sleep`` /
    ``is_auth_error`` / ``async_client_factory``) are already resolved by
    the caller against the ``_session`` module's bindings — see the
    module docstring for why the seam-resolution boundary stops here.
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
        # Lazy import — defensive guard against the ``types.py`` →
        # session cycle (preserved from the pre-extraction
        # ``Session.__init__`` comment "Lazy import to break the
        # types.py -> _core.py cycle").
        from .types import ConnectionLimits

        _resolved_limits = ConnectionLimits()

    if rate_limit_max_retries < 0:
        raise ValueError(f"rate_limit_max_retries must be >= 0, got {rate_limit_max_retries}")
    if server_error_max_retries < 0:
        raise ValueError(f"server_error_max_retries must be >= 0, got {server_error_max_retries}")

    # Fail-fast validation for ``max_concurrent_uploads``. The value is
    # NOT propagated into :class:`ValidatedSessionConfig` because the
    # actual upload semaphore state is owned by
    # ``SourceUploadPipeline`` (not ``Session``); this call exists
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
    # ``Session`` synthesizes its own ``ConnectionLimits()`` when
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
    auth: AuthTokens,
    refresh_callback: Callable[[], Awaitable[AuthTokens]] | None,
    on_rpc_event: Callable[[RpcTelemetryEvent], object] | None,
    cookie_saver: CookieSaver | None,
    cookie_rotator: CookieRotator | None,
) -> SessionCollaborators:
    """Construct the eight extracted collaborators in dependency order.

    The order mirrors the pre-extraction ``Session.__init__`` exactly so
    the load-bearing inter-collaborator wiring stays obvious to future
    readers: metrics is built first because it absorbs the optional
    ``on_rpc_event`` callback AND because the lock-wait metric callback
    captured by ``ReqidCounter`` is its bound method (so ``metrics``
    MUST exist before ``ReqidCounter`` is constructed — otherwise the
    counter would close over an attribute that has not yet been set,
    re-introducing the pre-PR-8 ordering trap); the drain tracker /
    reqid counter / auth coordinator follow because they are leaf
    collaborators with no inter-helper dependencies; ``Kernel`` is
    built next because ``ClientLifecycle`` holds a reference to it;
    ``CookiePersistence`` and ``PollRegistry`` close out the bundle.
    """
    # Observability counters + telemetry callback. ``metrics_snapshot``
    # remains the lock-safe read path; helper-level tests that need
    # implementation state read ``self._metrics_obj`` directly.
    metrics = ClientMetrics(on_rpc_event=on_rpc_event)
    # Transport drain bookkeeping (in-flight posts, drain condition,
    # per-task operation depth, draining flag). The helper's
    # ``__init__`` is event-loop-agnostic; the ``asyncio.Condition`` is
    # created lazily on first ``get_drain_condition`` call.
    drain_tracker = TransportDrainTracker()
    # Request ID counter for chat API (must be unique per request).
    # The :class:`ReqidCounter` helper owns the monotonic ``_value`` and
    # the lazily-allocated ``asyncio.Lock`` that serialises mutation.
    # Access ``self._reqid.value`` / ``self._reqid._lock`` directly.
    # The ``on_lock_wait`` hook keeps the cumulative
    # ``lock_wait_seconds_*`` metrics ticking inside ``metrics`` — we
    # pass the bound method of the metrics object we just built so the
    # counter cannot capture an unbound seam (which is what would happen
    # if we forwarded ``Session._record_lock_wait`` before
    # ``self._metrics_obj`` was assigned in the outer ``__init__``).
    reqid = ReqidCounter(on_lock_wait=metrics.record_lock_wait)
    # Auth refresh coordination — single-flight refresh task, snapshot
    # serialization, and cookie-jar sync. The coordinator owns
    # ``_refresh_lock``, ``_refresh_task``, ``_refresh_callback``, and
    # ``_auth_snapshot_lock``. Tests and internal callers that need
    # implementation state read the coordinator directly. The live auth
    # snapshot lock is reachable via :meth:`_get_auth_snapshot_lock`.
    # The auth snapshot lock is intentionally distinct from
    # ``_refresh_lock`` — mixing them would re-introduce the
    # reentrancy ambiguity that snapshot-side serialization was added
    # to avoid. The attribute name ``_auth_coord`` is part of the
    # inter-helper contract for the upcoming B2/C1 extractions; do not
    # rename.
    auth_coord = AuthRefreshCoordinator(refresh_callback=refresh_callback)
    # HTTP-client lifecycle — owns loop binding, keepalive, and close
    # ordering while delegating the live ``httpx.AsyncClient`` to
    # ``self._kernel``. The ``_resolve_keepalive_interval`` clamp lives
    # in :mod:`notebooklm._session_helpers` and is imported above; we
    # call it directly here. (The historical ``notebooklm._core``
    # re-export was removed in v0.5.0.)
    #
    # Event-loop affinity guard rationale: the lifecycle captures
    # ``asyncio.get_running_loop()`` in ``_bound_loop`` at ``open()`` time
    # and the cross-loop check in ``_perform_authed_post`` does a cheap
    # ``is`` comparison against it. Each client is per-loop — the asyncio primitives we hold
    # (``_reqid_lock``, ``_refresh_lock``, ``_auth_snapshot_lock``,
    # ``_rpc_semaphore``, the ``httpx.AsyncClient``
    # pool, in-flight tasks like ``_refresh_task`` / ``_keepalive_task``)
    # are all bound to the loop that ``open()`` ran on; reusing them
    # under a different loop produces hangs and ``RuntimeError`` deep
    # in httpx instead of an actionable message at the call site.
    kernel = Kernel(async_client_factory=config.async_client_factory)
    lifecycle = ClientLifecycle(
        timeout=config.timeout,
        connect_timeout=config.connect_timeout,
        limits=config.limits,
        keepalive_interval=config.keepalive_interval,
        keepalive_storage_path=config.keepalive_storage_path,
        kernel=kernel,
        # Phase 2 PR 3 injectable seams. ``None`` is forwarded so the
        # lifecycle's ``or _default_*`` resolves to the late-binding
        # wrapper — preserving the existing ``_core`` monkeypatch
        # surface for unchanged callers.
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
    )
    # Owns the in-process save lock and open-time cookie baseline.
    cookie_persistence = CookiePersistence(auth, config.keepalive_storage_path)
    # Session-level :class:`PollRegistry` retained as a legacy attribute
    # for historical tests. The *live* artifact-polling state is owned
    # separately by
    # :class:`ArtifactsAPI` (``src/notebooklm/_artifacts.py``), which
    # constructs its own :class:`PollRegistry` and threads it into
    # :class:`ArtifactPollingService` (``src/notebooklm/_artifact_polling.py``).
    # This ``self.poll_registry`` is currently unused by production code;
    # the tests in ``tests/integration/concurrency/test_artifact_poll_dedupe.py``
    # observe it directly. Migrating those tests to
    # ``client.artifacts._polling.poll_registry.pending`` — and dropping
    # this attribute — is tracked as a follow-up audit.
    poll_registry = PollRegistry()

    return SessionCollaborators(
        metrics=metrics,
        drain_tracker=drain_tracker,
        reqid=reqid,
        auth_coord=auth_coord,
        kernel=kernel,
        lifecycle=lifecycle,
        cookie_persistence=cookie_persistence,
        poll_registry=poll_registry,
    )


if TYPE_CHECKING:
    from ._session import Session


def wire_middleware_chain(
    config: ValidatedSessionConfig,
    collaborators: SessionCollaborators,
    *,
    host: Session,
    authed_post_chain_terminal: Callable[..., Awaitable[Any]],
    rpc_semaphore_factory: Callable[[], AbstractAsyncContextManager[Any]],
) -> WiredMiddleware:
    """Construct the :class:`MiddlewareChainBuilder`, build the seven-middleware
    list, and wire the final chain via :func:`build_chain`.

    The provider lambdas read from ``host`` (the :class:`Session`
    instance) so post-construction mutations on ``Session`` —
    integration tests do ``session._rate_limit_max_retries = 0`` —
    still take effect through the middleware live-binding contract
    documented in :class:`MiddlewareChainBuilder`. The
    ``rpc_semaphore_factory`` is passed in explicitly so the helper does
    not need to know that the live semaphore lives on
    ``Session._get_rpc_semaphore``.
    """
    # ADR-009 chain construction. PR history, leaf exception shape,
    # and ``RpcRequest.context`` contract live in
    # ``_middleware_chain.py`` module docstring.
    chain_builder = MiddlewareChainBuilder(
        drain_tracker=collaborators.drain_tracker,
        metrics=collaborators.metrics,
        rpc_semaphore_factory=rpc_semaphore_factory,
        rate_limit_max_retries_provider=lambda: host._rate_limit_max_retries,
        server_error_max_retries_provider=lambda: host._server_error_max_retries,
        refresh_retry_delay_provider=lambda: host._refresh_retry_delay,
        refresh_callable=host._await_refresh,
        auth_snapshot_provider=host._snapshot,
        is_auth_error=config.is_auth_error,
        refresh_callback_enabled_provider=lambda: collaborators.auth_coord.has_refresh_callback,
    )
    middlewares: list[Middleware] = chain_builder.build()
    authed_post_chain: NextCall = build_chain(
        middlewares,
        authed_post_chain_terminal,
    )
    return WiredMiddleware(
        chain_builder=chain_builder,
        middlewares=middlewares,
        authed_post_chain=authed_post_chain,
    )


__all__ = [
    "SessionCollaborators",
    "ValidatedSessionConfig",
    "WiredMiddleware",
    "build_collaborators",
    "validate_constructor_args",
    "wire_middleware_chain",
]
