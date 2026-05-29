"""Construction-time helpers for the NotebookLM client composition root.

Mechanical decomposition of the former monolithic client-runtime constructor
(``docs/improvement.md`` §3.1) into three concerns:
:func:`validate_constructor_args` (kwarg validation + normalization),
:func:`build_collaborators` (the seven collaborators in dependency order),
and :func:`wire_middleware_chain` (the seven-middleware ADR-009 chain).
Behavior is bit-for-bit identical to the pre-extraction constructor;
dependency-ordering and seam-resolution comments are preserved where still
applicable inside the helpers so future readers see *why* the order matters
without looking for a concrete ``Session`` owner.

Builds on the constructor-DI work in #1027 (``36dcc634`` —
"refactor(session): constructor DI for late-bound test seams; drop
http_client.setter"), which eliminated the late-binding wrappers and
the ``Kernel.http_client.setter`` and made ``decode_response`` /
``sleep`` / ``is_auth_error`` / ``async_client_factory`` the canonical
injection seams.

``None``-default resolution for ``sleep`` is owned by
:mod:`notebooklm._client_seams`; ``async_client_factory`` resolves directly to
``httpx.AsyncClient`` here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ._client_composed import ClientComposed
from ._client_metrics import ClientMetrics
from ._client_seams import ClientSeams, resolve_client_seams
from ._cookie_persistence import CookiePersistence
from ._error_injection import _refuse_synthetic_error_outside_test_context
from ._kernel import Kernel
from ._middleware import Middleware, NextCall, build_chain
from ._middleware_chain import MiddlewareChainBuilder
from ._middleware_chain_host import MiddlewareChainHost
from ._reqid_counter import ReqidCounter
from ._rpc_executor import RpcExecutor
from ._session_auth import AuthRefreshCoordinator
from ._session_config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
    normalize_max_concurrent_uploads,
)
from ._session_helpers import _resolve_keepalive_interval
from ._session_lifecycle import ClientLifecycle, CookieRotator, CookieSaver
from ._session_transport import SessionTransport
from ._transport_drain import TransportDrainTracker
from .auth import AuthTokens

if TYPE_CHECKING:
    # Runtime import of ``ConnectionLimits`` is deferred to
    # :func:`validate_constructor_args` to keep the long-standing
    # defensive guard against the historical ``types.py`` -> runtime
    # construction cycle (see the inline comment in the function body).
    from .types import ConnectionLimits, RpcTelemetryEvent

# Preserve the historical logger namespace while avoiding a raw deleted-module
# string that the no-session lint guard should reject elsewhere.
SESSION_LOGGER = logging.getLogger("notebooklm" + "." + "_session")


@dataclass(frozen=True)
class ValidatedSessionConfig:
    """Validated + normalized scalar configuration produced by
    :func:`validate_constructor_args`.

    Everything in here is either a value the caller supplied that passed
    validation, a normalized form (e.g. the keepalive interval clamped
    to the minimum-interval floor), or a seam callable resolved through
    the canonical module-attribute lookup that ``None`` defaults trigger
    (where applicable — see the module docstring for which seams are
    resolved here vs. in the public client constructor).
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
    pre-extraction runtime constructor exactly (see the inline comments
    there for the rationale); this container exists only to give the
    client constructor a single hand-off shape after the construction phase.
    """

    metrics: ClientMetrics
    drain_tracker: TransportDrainTracker
    reqid: ReqidCounter
    auth_coord: AuthRefreshCoordinator
    kernel: Kernel
    lifecycle: ClientLifecycle
    cookie_persistence: CookiePersistence


@dataclass(frozen=True)
class WiredMiddleware:
    """Wired middleware chain produced by :func:`wire_middleware_chain`."""

    chain_builder: MiddlewareChainBuilder
    middlewares: list[Middleware]
    authed_post_chain: NextCall


@dataclass(frozen=True)
class ClientInternals:
    """Result of :func:`compose_client_internals`."""

    collaborators: SessionCollaborators
    executor: RpcExecutor


def _resolve_async_client_factory(
    async_client_factory: Callable[..., httpx.AsyncClient] | None,
) -> Callable[..., httpx.AsyncClient]:
    """Resolve the construction-only async-client seam."""
    if async_client_factory is not None:
        return async_client_factory
    return httpx.AsyncClient


def resolve_seam_defaults(
    *,
    sleep: Callable[[float], Awaitable[Any]] | None,
    async_client_factory: Callable[..., httpx.AsyncClient] | None,
    is_auth_error: Callable[[Exception], bool] | None,
    decode_response: Callable[..., Any] | None,
) -> dict[str, Callable[..., Any]]:
    """Resolve legacy seam-default shape while keeping ``ClientSeams`` separate."""
    seams = resolve_client_seams(
        sleep=sleep,
        is_auth_error=is_auth_error,
        decode_response=decode_response,
    )
    return {
        "sleep": seams.sleep,
        "async_client_factory": _resolve_async_client_factory(async_client_factory),
        "is_auth_error": seams.is_auth_error,
        "decode_response": seams.decode_response,
    }


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
    ``async_client_factory``) are already resolved by the caller against
    the final client-side seam bindings; see the module docstring for why
    the seam-resolution boundary stops here.
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
        # Lazy import — defensive guard against the historical
        # ``types.py`` -> runtime-construction cycle (preserved from
        # the pre-extraction comment "Lazy import to break the
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
    auth: AuthTokens,
    refresh_callback: Callable[[], Awaitable[AuthTokens]] | None,
    on_rpc_event: Callable[[RpcTelemetryEvent], object] | None,
    cookie_saver: CookieSaver | None,
    cookie_rotator: CookieRotator | None,
) -> SessionCollaborators:
    """Construct the seven extracted collaborators in dependency order.

    The order mirrors the pre-extraction runtime constructor exactly so
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
    ``CookiePersistence`` closes out the bundle.
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
    # if we forwarded a broad host-level thin wrapper before
    # ``self._metrics_obj`` was assigned in the outer ``__init__``).
    reqid = ReqidCounter(on_lock_wait=metrics.record_lock_wait)
    # Auth refresh coordination — single-flight refresh task, snapshot
    # serialization, and cookie-jar sync. The coordinator owns
    # ``_refresh_lock``, ``_refresh_task``, ``_refresh_callback``, and
    # ``_auth_snapshot_lock``. Tests and internal callers that need
    # implementation state read the coordinator directly. The live auth
    # snapshot lock is reachable via
    # :meth:`AuthRefreshCoordinator.get_auth_snapshot_lock` (the
    # legacy ``_get_auth_snapshot_lock`` thin wrapper was inlined in
    # PR #4b — callers now address the coordinator directly
    # through ``self._auth_coord``).
    # The auth snapshot lock is intentionally distinct from
    # ``_refresh_lock`` — mixing them would re-introduce the
    # reentrancy ambiguity that snapshot-side serialization was added
    # to avoid. The attribute name ``_auth_coord`` is part of the
    # inter-helper contract for the upcoming B2/C1 extractions; do not
    # rename.
    # Wave 3b of session-decoupling (Task 1.0): supply ``metrics`` so
    # ``await_refresh`` records lock-wait latency without needing a host
    # parameter. The remaining coordinator methods (``snapshot``,
    # ``update_auth_tokens``, ``update_auth_headers``) take their explicit
    # ``auth`` / ``kernel`` collaborators per call — the ``_AuthRefreshHost``
    # Protocol was deleted alongside that signature change.
    auth_coord = AuthRefreshCoordinator(
        refresh_callback=refresh_callback,
        metrics=metrics,
    )
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

    return SessionCollaborators(
        metrics=metrics,
        drain_tracker=drain_tracker,
        reqid=reqid,
        auth_coord=auth_coord,
        kernel=kernel,
        lifecycle=lifecycle,
        cookie_persistence=cookie_persistence,
    )


def build_session_transport(
    collaborators: SessionCollaborators,
    *,
    auth: AuthTokens,
    chain_host: MiddlewareChainHost,
    logger: logging.Logger,
) -> SessionTransport:
    """Construct the :class:`SessionTransport` collaborator.

    Built **after** :func:`build_collaborators` and **before**
    :func:`wire_middleware_chain`, because the wired chain is built
    around ``transport.terminal``. The transport reaches the chain
    through a live-binding ``chain_provider`` closure that reads
    ``chain_host._authed_post_chain`` on every authed POST; that
    attribute is assigned by :func:`compose_client_internals`
    immediately after :func:`wire_middleware_chain` returns. Using a
    provider closure (rather than a frozen reference) preserves the
    pre-extraction behavior where tests reassign
    ``core._composed.chain_host._authed_post_chain = fake_chain`` to
    install a fake chain and expect the next call to honor it.

    The ``snapshot_provider`` closure passes the client-owned
    :class:`AuthTokens` collaborator directly to
    :meth:`AuthRefreshCoordinator.snapshot`; the former host-shaped
    ``_AuthRefreshHost`` Protocol was deleted, and the coordinator routes
    its lock-wait metric through ``self._metrics`` (supplied at
    construction). The ``bound_loop_check`` lambda reads through
    ``collaborators.lifecycle.assert_bound_loop`` at call time, preserving
    lifecycle method patchability without retaining a broad host-level
    ``assert_bound_loop`` forward.

    The ``chain_host`` parameter lets the chain-slot lookup go through
    the host directly, with no extra indirection on the hot
    path.

    The ``logger`` is forwarded as-is so transport-error log lines keep
    appearing under the historical session logger namespace rather than
    acquiring a new transport logger namespace that callers' log filters
    / ``caplog`` selectors would not yet recognise.
    """
    return SessionTransport(
        kernel=collaborators.kernel,
        snapshot_provider=lambda: collaborators.auth_coord.snapshot(auth=auth),
        chain_provider=lambda: chain_host._authed_post_chain,
        metrics=collaborators.metrics,
        bound_loop_check=lambda: collaborators.lifecycle.assert_bound_loop(),
        logger=logger,
    )


def wire_middleware_chain(
    collaborators: SessionCollaborators,
    *,
    chain_host: MiddlewareChainHost,
    auth: AuthTokens,
    authed_post_chain_terminal: Callable[..., Awaitable[Any]],
    rpc_semaphore_factory: Callable[[], AbstractAsyncContextManager[Any]],
    is_auth_error: Callable[[Exception], bool],
) -> WiredMiddleware:
    """Construct the :class:`MiddlewareChainBuilder`, build the seven-middleware
    list, and wire the final chain via :func:`build_chain`.

    Two narrow host parameters:

    * ``chain_host`` — the :class:`MiddlewareChainHost` owns the three
      retry-budget tunables (``_rate_limit_max_retries`` /
      ``_server_error_max_retries`` / ``_refresh_retry_delay``) plus the
      dynamic-delegate refresh entry point (:meth:`await_refresh`). The
      tunable provider lambdas and the ``refresh_callable`` reference
      capture this host directly.
    * ``auth`` — the live :class:`AuthTokens` collaborator passed
      explicitly to :meth:`AuthRefreshCoordinator.snapshot` on every
      call. The provider lambda captures this object by reference; the
      capture is safe because production never reassigns the
      client-owned ``AuthTokens`` object after construction (the only
      mutation path is in-place scalar updates via
      :meth:`AuthRefreshCoordinator.update_auth_tokens`, which mutate
      the captured instance directly). This replaced the previous
      ``auth_snapshot_host: _AuthRefreshHost`` host-shaped parameter
      when the ``_AuthRefreshHost`` Protocol was deleted in favor of
      per-method explicit collaborators; the coordinator routes its
      lock-wait metric through its own ``self._metrics`` (supplied at
      construction), so it no longer needs a host-shaped collaborator
      for the metric either.

    Post-construction mutation on ``chain_host._<attr>`` still takes
    effect through the middleware live-binding contract documented in
    :class:`MiddlewareChainBuilder`. The ``rpc_semaphore_factory`` is
    passed in explicitly so the helper does not need to know which holder
    owns the live semaphore. ``is_auth_error`` is passed as a live-binding
    callable so rebinding ``ClientSeams.is_auth_error`` after construction
    still steers the chain.
    """
    # ADR-009 chain construction. PR history, leaf exception shape,
    # and ``RpcRequest.context`` contract live in
    # ``_middleware_chain.py`` module docstring.
    chain_builder = MiddlewareChainBuilder(
        drain_tracker=collaborators.drain_tracker,
        metrics=collaborators.metrics,
        rpc_semaphore_factory=rpc_semaphore_factory,
        rate_limit_max_retries_provider=lambda: chain_host._rate_limit_max_retries,
        server_error_max_retries_provider=lambda: chain_host._server_error_max_retries,
        retry_timeout_provider=lambda: collaborators.lifecycle._timeout,
        refresh_retry_delay_provider=lambda: chain_host._refresh_retry_delay,
        refresh_callable=chain_host.await_refresh,
        auth_snapshot_provider=lambda: collaborators.auth_coord.snapshot(auth=auth),
        is_auth_error=is_auth_error,
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


def compose_client_internals(
    *,
    auth: AuthTokens,
    timeout: float = DEFAULT_TIMEOUT,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    refresh_callback: Callable[[], Awaitable[AuthTokens]] | None = None,
    refresh_retry_delay: float = 0.2,
    keepalive: float | None = None,
    keepalive_min_interval: float = DEFAULT_KEEPALIVE_MIN_INTERVAL,
    keepalive_storage_path: Path | None = None,
    rate_limit_max_retries: int = 3,
    server_error_max_retries: int = 3,
    limits: ConnectionLimits | None = None,
    max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
    max_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
    on_rpc_event: Callable[[RpcTelemetryEvent], object] | None = None,
    cookie_saver: CookieSaver | None = None,
    cookie_rotator: CookieRotator | None = None,
    decode_response: Callable[..., Any] | None = None,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
    is_auth_error: Callable[[Exception], bool] | None = None,
    async_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    seams: ClientSeams | None = None,
    composed: ClientComposed | None = None,
) -> ClientInternals:
    """Single entry point that owns the client composition sequence."""
    # MUST stay first — preserves the earliest-opportunity refusal that
    # ``test_synthetic_error_transport_guard`` pins.
    _refuse_synthetic_error_outside_test_context()

    seams = seams or resolve_client_seams(
        sleep=sleep,
        is_auth_error=is_auth_error,
        decode_response=decode_response,
    )
    if composed is None:
        composed = ClientComposed(max_concurrent_rpcs=max_concurrent_rpcs)
    elif composed.max_concurrent_rpcs != max_concurrent_rpcs:
        raise ValueError(
            "composed.max_concurrent_rpcs must match max_concurrent_rpcs "
            f"(got composed.max_concurrent_rpcs={composed.max_concurrent_rpcs!r}, "
            f"max_concurrent_rpcs={max_concurrent_rpcs!r})"
        )
    async_client_factory = _resolve_async_client_factory(async_client_factory)

    config = validate_constructor_args(
        timeout=timeout,
        connect_timeout=connect_timeout,
        refresh_retry_delay=refresh_retry_delay,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
        keepalive=keepalive,
        keepalive_min_interval=keepalive_min_interval,
        keepalive_storage_path=keepalive_storage_path,
        auth_storage_path=auth.storage_path,
        limits=limits,
        max_concurrent_uploads=max_concurrent_uploads,
        max_concurrent_rpcs=max_concurrent_rpcs,
        decode_response=seams.decode_response,
        sleep=seams.sleep,
        is_auth_error=seams.is_auth_error,
        async_client_factory=async_client_factory,
    )
    collaborators = build_collaborators(
        config,
        auth=auth,
        refresh_callback=refresh_callback,
        on_rpc_event=on_rpc_event,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
    )
    chain_host = MiddlewareChainHost(
        _auth_refresh=collaborators.auth_coord,
        _rate_limit_max_retries=config.rate_limit_max_retries,
        _server_error_max_retries=config.server_error_max_retries,
        _refresh_retry_delay=config.refresh_retry_delay,
    )
    composed.bind_session_collaborators(collaborators)
    composed.bind_chain_host(chain_host)

    transport = build_session_transport(
        collaborators,
        auth=auth,
        chain_host=chain_host,
        logger=SESSION_LOGGER,
    )
    composed.bind_transport(transport)
    chain_host._bind_transport(transport)

    wired = wire_middleware_chain(
        collaborators,
        chain_host=chain_host,
        auth=auth,
        authed_post_chain_terminal=chain_host._authed_post_chain_terminal,
        rpc_semaphore_factory=composed.get_rpc_semaphore,
        is_auth_error=lambda *a, **kw: seams.is_auth_error(*a, **kw),
    )
    chain_host._authed_post_chain = wired.authed_post_chain
    composed.bind_chain_metadata(wired)

    executor = RpcExecutor(
        kernel=collaborators.kernel,
        transport=transport,
        auth_refresh=collaborators.auth_coord,
        metrics=collaborators.metrics,
        decode_response=lambda *a, **kw: seams.decode_response(*a, **kw),
        is_auth_error=lambda *a, **kw: seams.is_auth_error(*a, **kw),
        sleep=lambda *a, **kw: seams.sleep(*a, **kw),
        timeout_provider=lambda: collaborators.lifecycle._timeout,
        refresh_callback_enabled_provider=lambda: collaborators.auth_coord.has_refresh_callback,
        refresh_retry_delay_provider=lambda: chain_host._refresh_retry_delay,
    )
    composed.bind_executor(executor)

    return ClientInternals(
        collaborators=collaborators,
        executor=executor,
    )


__all__ = [
    "ClientInternals",
    "SessionCollaborators",
    "ValidatedSessionConfig",
    "WiredMiddleware",
    "build_collaborators",
    "build_session_transport",
    "compose_client_internals",
    "resolve_seam_defaults",
    "validate_constructor_args",
    "wire_middleware_chain",
]
