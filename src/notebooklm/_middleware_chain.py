"""Composes the ADR-009 middleware chain.

Tier-12 PR 12.2 wired an empty middleware chain around
``AuthedTransport.perform_authed_post`` (the shared seam covering
``Session._perform_authed_post`` and ``RpcExecutor.execute``'s call to
``self._owner._perform_authed_post`` at ``_rpc_executor.py:245``).

PR 12.3 added ``TracingMiddleware`` (innermost), PR 12.4 prepended
``MetricsMiddleware``, PR 12.5 prepended ``DrainMiddleware`` outermost,
PR 12.6 inserted ``ErrorInjectionMiddleware`` between
``MetricsMiddleware`` and ``TracingMiddleware``, PR 12.7 inserted
``RetryMiddleware`` between ``MetricsMiddleware`` and
``ErrorInjectionMiddleware``, and PR 12.8 inserts
``AuthRefreshMiddleware`` BETWEEN ``RetryMiddleware`` and
``ErrorInjectionMiddleware`` so the list now reads the **final** ADR-009
ordering ``[Drain, Metrics, Semaphore, Retry, AuthRefresh,
ErrorInjection, Tracing]`` (outermost → innermost). ``build_chain``
composes the leftmost entry as the outermost wrapper, so keeping
``TracingMiddleware`` at the RIGHT end of the list preserves Tracing as
the innermost wrapper.

PR 12.7 lifted the 429 / 5xx retry loops out of the leaf into
``RetryMiddleware``; PR 12.8 lifts the auth-refresh-once retry too.
After PR 12.8 the leaf is a *pure* POST — every retry decision happens
in the chain. The leaf still raises ``TransportRateLimited`` /
``TransportServerError`` for 429 / 5xx so ``RetryMiddleware`` can
catch; raw ``httpx.HTTPStatusError`` (400/401/403) propagates so
``AuthRefreshMiddleware`` can catch via ``is_auth_error`` and drive
refresh-then-retry.

The terminal adapter reads ``build_request`` / ``log_label`` /
``disable_internal_retries`` from ``RpcRequest.context`` and delegates
to ``self._get_authed_transport().perform_authed_post``.
``RetryMiddleware`` reads ``log_label`` / ``disable_internal_retries``
from the same ``context`` dict. ``AuthRefreshMiddleware`` reads
``log_label``. See ADR-009 §"Per-request behavior" and
``.sisyphus/plans/tier-12-13-greenfield-migration.md`` line 160.

The order is pinned at two levels:
* facade-level by ``tests/unit/test_chain_wiring.py::test_chain_seeded_with_final_adr_009_ordering``
* builder-level by ``tests/unit/test_middleware_chain_builder.py``
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from ._middleware import Middleware
from ._middleware_auth_refresh import AuthRefreshMiddleware
from ._middleware_drain import DrainMiddleware
from ._middleware_error_injection import ErrorInjectionMiddleware
from ._middleware_metrics import MetricsMiddleware
from ._middleware_retry import RetryMiddleware
from ._middleware_semaphore import SemaphoreMiddleware
from ._middleware_tracing import TracingMiddleware


class MiddlewareChainBuilder:
    """Builds the seven-middleware ADR-009 chain.

    Provider callables (``rate_limit_max_retries_provider`` etc.) are
    used by ``RetryMiddleware`` / ``AuthRefreshMiddleware`` so
    post-construction mutations on ``Session`` still take effect — the
    integration-test idiom of poking
    ``session._rate_limit_max_retries = 0`` must keep working.
    """

    def __init__(
        self,
        *,
        drain_tracker: Any,
        metrics: Any,
        rpc_semaphore_factory: Callable[[], AbstractAsyncContextManager[Any]],
        rate_limit_max_retries_provider: Callable[[], int],
        server_error_max_retries_provider: Callable[[], int],
        refresh_retry_delay_provider: Callable[[], float],
        refresh_callable: Callable[..., Awaitable[Any]],
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
        self._refresh_retry_delay_provider = refresh_retry_delay_provider
        self._refresh_callable = refresh_callable
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
            # pre-Tier-12 "one slot per logical RPC" contract.
            # ``rpc_semaphore_factory`` returns ``contextlib.nullcontext``
            # when ``max_concurrent_rpcs is None`` (unbounded), so the
            # ``async with`` collapses to a no-op for opted-out clients.
            SemaphoreMiddleware(self._rpc_semaphore_factory),
            # Pass callable budgets so post-construction mutation of
            # ``self._rate_limit_max_retries`` /
            # ``self._server_error_max_retries`` (an integration-test
            # idiom; production never mutates these) still takes effect —
            # bit-for-bit preserving the pre-PR-12.7 live-binding
            # contract where ``AuthedTransport`` read these attrs LIVE
            # inside its retry loop.
            RetryMiddleware(
                rate_limit_max_retries=self._rate_limit_max_retries_provider,
                server_error_max_retries=self._server_error_max_retries_provider,
                metrics=self._metrics,
            ),
            # AuthRefresh callbacks: ``refresh_callable`` invokes the
            # same ``_await_refresh`` path the leaf used pre-PR-12.8, so
            # the coalesced single-flight refresh contract from
            # ``AuthRefreshCoordinator`` is preserved end-to-end.
            # ``refresh_callback_enabled_provider`` reads the
            # coordinator's internal callback slot to skip refresh when
            # no callback was configured (matches the legacy
            # ``host._refresh_callback is not None`` gate in the leaf).
            # ``refresh_retry_delay_provider`` is callable for
            # live-binding parity with retry budgets.
            # ``is_auth_error`` is bound late (see ``_live_is_auth_error``
            # in ``_session.py``) so test monkeypatches of
            # ``notebooklm._core.is_auth_error`` reach the chain.
            AuthRefreshMiddleware(
                refresh_callable=self._refresh_callable,
                is_auth_error=self._is_auth_error,
                refresh_callback_enabled=self._refresh_callback_enabled_provider,
                refresh_retry_delay=self._refresh_retry_delay_provider,
                metrics=self._metrics,
            ),
            ErrorInjectionMiddleware(),
            TracingMiddleware(),
        ]
