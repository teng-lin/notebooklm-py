"""Composes the ADR-0009 middleware chain.

The chain wraps ``Kernel.post`` through
``MiddlewareChainHost._authed_post_chain_terminal`` (the shared seam
covering ``RuntimeTransport.perform_authed_post`` and
``RpcExecutor._execute_once``'s dispatch into the transport).

The ADR-0009 ordering is ``[Drain, Metrics, Semaphore, Retry, AuthRefresh,
ErrorInjection, Tracing]`` (outermost → innermost). ``build_chain``
composes the leftmost entry as the outermost wrapper, so keeping
``TracingMiddleware`` at the RIGHT end of the list preserves Tracing as
the innermost wrapper.

The 429 / 5xx retry loops and the auth-refresh-once retry live in
``RetryMiddleware`` and ``AuthRefreshMiddleware`` respectively.
The leaf is a *pure* POST — every retry decision happens in the chain. The
terminal maps raw ``Kernel.post`` errors to ``TransportRateLimited`` /
``TransportServerError`` for 429 / 5xx so ``RetryMiddleware`` can catch; raw
``httpx.HTTPStatusError`` (400/401/403) propagates so
``AuthRefreshMiddleware`` can catch via ``is_auth_error`` and drive
refresh-then-retry.

The terminal reads ``RpcRequest.url`` / ``headers`` / ``body`` and
delegates to ``Kernel.post``. Middlewares and the terminal read/write the
centralized ``RPC_CONTEXT_*`` keys from ``_middleware.context``; the
allowed vocabulary is mirrored in ADR-0009 §"Per-request behavior".

The order is pinned at two levels:
* facade-level by ``tests/unit/test_chain_wiring.py::test_chain_seeded_with_final_adr_009_ordering``
* builder-level by ``tests/unit/test_middleware_chain_builder.py``
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from .auth_refresh import AuthRefreshMiddleware
from .core import Middleware
from .drain import DrainMiddleware
from .error_injection import ErrorInjectionMiddleware
from .metrics import MetricsMiddleware
from .retry import RetryMiddleware
from .semaphore import SemaphoreMiddleware
from .tracing import TracingMiddleware


class MiddlewareChainBuilder:
    """Builds the seven-middleware ADR-0009 chain.

    Provider callables (``rate_limit_max_retries_provider`` etc.) are
    used by ``RetryMiddleware`` / ``AuthRefreshMiddleware`` so
    post-construction mutations on ``MiddlewareChainHost`` still take
    effect — the integration-test idiom of poking
    ``core._composed.chain_host._rate_limit_max_retries = 0`` must keep working.
    """

    def __init__(
        self,
        *,
        drain_tracker: Any,
        metrics: Any,
        rpc_semaphore_factory: Callable[[], AbstractAsyncContextManager[Any]],
        rate_limit_max_retries_provider: Callable[[], int],
        server_error_max_retries_provider: Callable[[], int],
        retry_timeout_provider: Callable[[], float | None],
        refresh_retry_delay_provider: Callable[[], float],
        refresh_callable: Callable[..., Awaitable[Any]],
        auth_snapshot_provider: Callable[[], Awaitable[Any]],
        is_auth_error: Callable[[Exception], bool],
        refresh_callback_enabled_provider: Callable[[], bool],
    ) -> None:
        # NOTE: do NOT accept an ``auth_coord`` param — the
        # coordinator's only chain-relevant outputs are wrapped behind
        # ``refresh_callable`` and ``refresh_callback_enabled_provider``
        # already. Passing the coordinator object directly would create
        # a redundant reference that lints can't easily follow.
        self._drain_tracker = drain_tracker
        self._metrics = metrics
        self._rpc_semaphore_factory = rpc_semaphore_factory
        self._rate_limit_max_retries_provider = rate_limit_max_retries_provider
        self._server_error_max_retries_provider = server_error_max_retries_provider
        self._retry_timeout_provider = retry_timeout_provider
        self._refresh_retry_delay_provider = refresh_retry_delay_provider
        self._refresh_callable = refresh_callable
        self._auth_snapshot_provider = auth_snapshot_provider
        self._is_auth_error = is_auth_error
        self._refresh_callback_enabled_provider = refresh_callback_enabled_provider

    def build(self) -> list[Middleware]:
        return [
            DrainMiddleware(self._drain_tracker),
            MetricsMiddleware(self._metrics),
            # Acquire the ``max_concurrent_rpcs`` slot AFTER Drain admits
            # the call (so queued tasks count toward shutdown drain) and
            # AFTER Metrics starts timing (so latency includes queue
            # wait), but BEFORE Retry can re-enter the inner chain — that
            # way ``RetryMiddleware``'s retry attempts stay in the same
            # slot rather than racing to claim another, preserving the
            # "one slot per logical RPC" contract.
            # ``rpc_semaphore_factory`` returns ``contextlib.nullcontext``
            # when ``max_concurrent_rpcs is None`` (unbounded), so the
            # ``async with`` collapses to a no-op for opted-out clients.
            SemaphoreMiddleware(self._rpc_semaphore_factory),
            # Pass callable budgets so post-construction mutation of
            # ``chain_host._rate_limit_max_retries`` /
            # ``chain_host._server_error_max_retries`` (an integration-test
            # idiom; production never mutates these) still takes effect —
            # preserving the live-binding contract where the retry loop
            # reads these attrs LIVE.
            RetryMiddleware(
                rate_limit_max_retries=self._rate_limit_max_retries_provider,
                server_error_max_retries=self._server_error_max_retries_provider,
                retry_timeout=self._retry_timeout_provider,
                metrics=self._metrics,
            ),
            # AuthRefresh callbacks: ``refresh_callable`` invokes
            # ``MiddlewareChainHost.await_refresh``, which dynamically
            # delegates to ``AuthRefreshCoordinator.await_refresh``, so
            # the coalesced single-flight refresh contract from the
            # coordinator is preserved end-to-end.
            # ``refresh_callback_enabled_provider`` reads the coordinator's
            # ``has_refresh_callback`` property to skip refresh when no
            # callback was configured.
            # ``refresh_retry_delay_provider`` is callable for
            # live-binding parity with retry budgets.
            # ``is_auth_error`` is supplied as a live-binding callable from
            # ``ClientSeams`` so test rebinds can still steer the chain.
            # ``auth_snapshot_provider`` gives AuthRefreshMiddleware a
            # fresh post-refresh snapshot so it can replace the
            # populated request envelope before retrying the Kernel.post
            # terminal.
            AuthRefreshMiddleware(
                refresh_callable=self._refresh_callable,
                is_auth_error=self._is_auth_error,
                refresh_callback_enabled=self._refresh_callback_enabled_provider,
                refresh_retry_delay=self._refresh_retry_delay_provider,
                snapshot_provider=self._auth_snapshot_provider,
                metrics=self._metrics,
            ),
            ErrorInjectionMiddleware(),
            TracingMiddleware(),
        ]
