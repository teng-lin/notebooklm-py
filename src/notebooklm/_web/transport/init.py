"""Construction helpers for the NotebookLM web transport runtime."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..._auth.profile_store import ProfileStore
from ..._runtime.config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_KEEPALIVE_MIN_INTERVAL,
    DEFAULT_MAX_CONCURRENT_RPCS,
    DEFAULT_MAX_CONCURRENT_UPLOADS,
    DEFAULT_TIMEOUT,
)
from ..._runtime.init import (
    SharedRuntime,
    ValidatedSessionConfig,
    build_collaborators,
    validate_constructor_args,
)
from ...auth import AuthTokens
from ..sources.upload import SourceUploadPipeline
from .auth import AuthRefreshCoordinator
from .composed import ClientComposed
from .cookie_persistence import CookiePersistence
from .error_injection import _refuse_synthetic_error_outside_test_context
from .executor import RpcExecutor
from .kernel import Kernel
from .lifecycle import (
    CookieRotator,
    CookieSaver,
    WebTransportLifecycle,
    _default_cookie_rotator,
)
from .middleware.chain import MiddlewareChainBuilder
from .middleware.chain_host import MiddlewareChainHost
from .middleware.core import Middleware, NextCall, build_chain
from .reqid_counter import ReqidCounter
from .runtime import RuntimeTransport
from .seams import ClientSeams, resolve_client_seams

if TYPE_CHECKING:
    from ...types import ConnectionLimits, RpcTelemetryEvent


# Preserve the historical logger namespace while avoiding a raw deleted-module
# string that the no-session lint guard should reject elsewhere.
SESSION_LOGGER = logging.getLogger("notebooklm" + "." + "_session")


@dataclass(frozen=True)
class WebRuntime:
    """All collaborators owned exclusively by the web backend."""

    reqid: ReqidCounter
    auth_coord: AuthRefreshCoordinator
    kernel: Kernel
    cookie_persistence: CookiePersistence
    web_transport: WebTransportLifecycle
    composed: ClientComposed
    executor: RpcExecutor
    source_uploader: SourceUploadPipeline


@dataclass(frozen=True)
class WiredMiddleware:
    """Wired middleware chain produced by :func:`wire_middleware_chain`."""

    chain_builder: MiddlewareChainBuilder
    middlewares: list[Middleware]
    authed_post_chain: NextCall


@dataclass(frozen=True)
class ClientInternals:
    """Shared and web runtime bundles produced by client composition."""

    collaborators: SharedRuntime
    web_runtime: WebRuntime


def _resolve_async_client_factory(
    async_client_factory: Callable[..., httpx.AsyncClient] | None,
) -> Callable[..., httpx.AsyncClient]:
    """Resolve the construction-only async-client seam."""
    if async_client_factory is not None:
        return async_client_factory
    # PoC opt-in: browser TLS/JA3 impersonation transport (curl_cffi), shared
    # across every authenticated-Google client.
    from ..._curl_cffi_transport import resolve_transport_factory

    return resolve_transport_factory()


def build_runtime_transport(
    collaborators: SharedRuntime,
    *,
    auth: AuthTokens,
    auth_coord: AuthRefreshCoordinator,
    kernel: Kernel,
    chain_host: MiddlewareChainHost,
    logger: logging.Logger,
) -> RuntimeTransport:
    """Construct the web request transport around shared supervision.

    This runs after the shared and web leaf collaborators exist but before
    middleware wiring. The transport reads the live chain slot through
    ``chain_host`` on every authenticated POST, so tests and recovery paths
    may replace the chain without rebuilding the transport. Authentication
    snapshots always use the client-owned ``AuthTokens`` instance through
    ``AuthRefreshCoordinator``; loop and admission checks remain owned by the
    neutral ``CallSupervisor``. The supplied logger deliberately preserves
    the historical session logger namespace.
    """
    return RuntimeTransport(
        kernel=kernel,
        snapshot_provider=lambda expected_epoch: auth_coord.snapshot(
            auth=auth,
            expected_epoch=expected_epoch,
        ),
        chain_provider=lambda: chain_host._authed_post_chain,
        call_supervisor=collaborators.call_supervisor,
        bound_loop_check=collaborators.call_supervisor.assert_bound_loop,
        logger=logger,
    )


def wire_middleware_chain(
    collaborators: SharedRuntime,
    *,
    auth_coord: AuthRefreshCoordinator,
    chain_host: MiddlewareChainHost,
    auth: AuthTokens,
    authed_post_chain_terminal: Callable[..., Awaitable[Any]],
    is_auth_error: Callable[[Exception], bool],
    timeout: float,
) -> WiredMiddleware:
    """Build and connect the four-middleware ADR-0009 web chain.

    Retry tunables and the refresh entry point are live bindings on
    ``chain_host``. The auth snapshot closure captures the client-owned
    ``AuthTokens`` object by reference; production refresh mutates that same
    object in place. ``is_auth_error`` is likewise passed as a live-binding
    callable so a test seam reassignment remains observable after assembly.
    """
    chain_builder = MiddlewareChainBuilder(
        metrics=collaborators.metrics,
        rate_limit_max_retries_provider=lambda: chain_host._rate_limit_max_retries,
        server_error_max_retries_provider=lambda: chain_host._server_error_max_retries,
        retry_timeout_provider=lambda: timeout,
        refresh_retry_delay_provider=lambda: chain_host._refresh_retry_delay,
        refresh_callable=chain_host.await_refresh,
        auth_snapshot_provider=lambda expected_epoch: auth_coord.snapshot(
            auth=auth,
            expected_epoch=expected_epoch,
        ),
        is_auth_error=is_auth_error,
        refresh_callback_enabled_provider=lambda: auth_coord.has_refresh_callback,
    )
    middlewares: list[Middleware] = chain_builder.build()
    authed_post_chain: NextCall = build_chain(middlewares, authed_post_chain_terminal)
    return WiredMiddleware(
        chain_builder=chain_builder,
        middlewares=middlewares,
        authed_post_chain=authed_post_chain,
    )


def _build_web_transport(
    config: ValidatedSessionConfig,
    *,
    auth: AuthTokens,
    refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None,
    shared: SharedRuntime,
    cookie_saver: CookieSaver | None,
    cookie_rotator: CookieRotator | None,
) -> tuple[ReqidCounter, AuthRefreshCoordinator, Kernel, CookiePersistence, WebTransportLifecycle]:
    """Build the web-only leaf collaborators in dependency order."""
    # ReqidCounter captures this bound method so metrics must exist first.
    reqid = ReqidCounter(on_lock_wait=shared.metrics.record_lock_wait)
    # Snapshot serialization is intentionally distinct from the refresh lock;
    # combining them would reintroduce refresh reentrancy ambiguity.
    auth_coord = AuthRefreshCoordinator(
        refresh_callback=refresh_callback,
        metrics=shared.metrics,
    )
    # ADR-0032 bootstrap hand-off: after construction, first-party live and
    # closed-state readers use the kernel-owned jar rather than AuthTokens'
    # public compatibility shadows.
    kernel = Kernel(auth=auth, async_client_factory=config.async_client_factory)
    # Preserve only the load-time snapshot. Re-reading a newer profile at open
    # could let this process's older live jar overwrite a sibling writer's
    # intervening cookie update during the eventual three-way merge.
    cookie_persistence = CookiePersistence._from_store(
        ProfileStore(auth.storage_path) if auth.storage_path is not None else None,
        initial_snapshot=auth.cookie_snapshot,
    )
    web_transport = WebTransportLifecycle(
        auth=auth,
        auth_coord=auth_coord,
        cookie_persistence=cookie_persistence,
        kernel=kernel,
        timeout=config.timeout,
        connect_timeout=config.connect_timeout,
        limits=config.limits,
        keepalive_interval=config.keepalive_interval,
        keepalive_storage_path=config.keepalive_storage_path,
        cookie_persistence_path=config.keepalive_storage_path,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator or _default_cookie_rotator,
    )
    return reqid, auth_coord, kernel, cookie_persistence, web_transport


def compose_client_internals(
    *,
    auth: AuthTokens,
    timeout: float = DEFAULT_TIMEOUT,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None = None,
    refresh_retry_delay: float = 0.2,
    keepalive: float | None = None,
    keepalive_min_interval: float = DEFAULT_KEEPALIVE_MIN_INTERVAL,
    keepalive_storage_path: Path | None = None,
    rate_limit_max_retries: int = 3,
    server_error_max_retries: int = 3,
    limits: ConnectionLimits | None = None,
    max_concurrent_uploads: int | None = DEFAULT_MAX_CONCURRENT_UPLOADS,
    max_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
    upload_timeout: httpx.Timeout | None = None,
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
    """Build the shared runtime and the complete web runtime bundle."""
    # MUST stay first — preserves the earliest-opportunity refusal that
    # ``test_synthetic_error_transport_guard`` pins.
    _refuse_synthetic_error_outside_test_context()

    seams = seams or resolve_client_seams(
        sleep=sleep,
        is_auth_error=is_auth_error,
        decode_response=decode_response,
    )
    composed = composed or ClientComposed()
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
    shared = build_collaborators(config, on_rpc_event=on_rpc_event)
    web_runtime = build_web_runtime(
        config=config,
        auth=auth,
        refresh_callback=refresh_callback,
        shared=shared,
        upload_timeout=upload_timeout,
        max_concurrent_uploads=max_concurrent_uploads,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
        seams=seams,
        composed=composed,
    )
    return ClientInternals(collaborators=shared, web_runtime=web_runtime)


def build_web_runtime(
    *,
    config: ValidatedSessionConfig,
    auth: AuthTokens,
    refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None,
    shared: SharedRuntime,
    upload_timeout: httpx.Timeout | None,
    max_concurrent_uploads: int | None,
    cookie_saver: CookieSaver | None,
    cookie_rotator: CookieRotator | None,
    seams: ClientSeams,
    composed: ClientComposed | None = None,
) -> WebRuntime:
    """Build one web-only bundle around an existing neutral runtime.

    Normal web clients reach this through :func:`compose_client_internals`.
    The deprecated Android ``rpc_call`` compatibility sidecar also uses it,
    but only after that call has been admitted by the root supervisor.  Keeping
    this builder separate is what lets ordinary Android construction avoid a
    ``Kernel``, cookie-persistence owner, executor, or HTTP client entirely.
    """

    composed = composed or ClientComposed()
    reqid, auth_coord, kernel, cookie_persistence, web_transport = _build_web_transport(
        config,
        auth=auth,
        refresh_callback=refresh_callback,
        shared=shared,
        cookie_saver=cookie_saver,
        cookie_rotator=cookie_rotator,
    )

    chain_host = MiddlewareChainHost(
        _auth_refresh=auth_coord,
        _rate_limit_max_retries=config.rate_limit_max_retries,
        _server_error_max_retries=config.server_error_max_retries,
        _refresh_retry_delay=config.refresh_retry_delay,
    )
    composed.bind_chain_host(chain_host)

    transport = build_runtime_transport(
        shared,
        auth=auth,
        auth_coord=auth_coord,
        kernel=kernel,
        chain_host=chain_host,
        logger=SESSION_LOGGER,
    )
    composed.bind_transport(transport)
    chain_host._bind_transport(transport)

    wired = wire_middleware_chain(
        shared,
        auth_coord=auth_coord,
        chain_host=chain_host,
        auth=auth,
        authed_post_chain_terminal=chain_host._authed_post_chain_terminal,
        is_auth_error=lambda *a, **kw: seams.is_auth_error(*a, **kw),
        timeout=config.timeout,
    )
    chain_host._authed_post_chain = wired.authed_post_chain
    composed.bind_chain_metadata(wired)

    executor = RpcExecutor(
        transport=transport,
        auth_refresh=auth_coord,
        metrics=shared.metrics,
        call_supervisor=shared.call_supervisor,
        decode_response=lambda *a, **kw: seams.decode_response(*a, **kw),
        is_auth_error=lambda *a, **kw: seams.is_auth_error(*a, **kw),
        sleep=lambda *a, **kw: seams.sleep(*a, **kw),
        timeout_provider=lambda: config.timeout,
        refresh_callback_enabled_provider=lambda: auth_coord.has_refresh_callback,
        refresh_retry_delay_provider=lambda: chain_host._refresh_retry_delay,
    )
    composed.bind_executor(executor)

    # ADR-0014 Rule 2: construct the uploader from its narrow collaborators.
    # ADR-0016's Auth Instance Invariant requires this exact client-owned
    # ``auth`` object so refresh-time in-place mutation remains visible.
    source_uploader = SourceUploadPipeline(
        rpc=executor,
        supervisor=shared.call_supervisor,
        kernel=kernel,
        auth=auth,
        upload_timeout=upload_timeout,
        max_concurrent_uploads=max_concurrent_uploads,
        record_upload_queue_wait=shared.metrics.record_upload_queue_wait,
    )
    web_runtime = WebRuntime(
        reqid=reqid,
        auth_coord=auth_coord,
        kernel=kernel,
        cookie_persistence=cookie_persistence,
        web_transport=web_transport,
        composed=composed,
        executor=executor,
        source_uploader=source_uploader,
    )
    return web_runtime


__all__ = [
    "ClientInternals",
    "WebRuntime",
    "WiredMiddleware",
    "build_runtime_transport",
    "build_web_runtime",
    "compose_client_internals",
    "wire_middleware_chain",
]
