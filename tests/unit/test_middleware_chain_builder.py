"""Unit tests for MiddlewareChainBuilder — pins ADR-009 ordering at builder level."""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock

from notebooklm._middleware_auth_refresh import AuthRefreshMiddleware
from notebooklm._middleware_drain import DrainMiddleware
from notebooklm._middleware_error_injection import ErrorInjectionMiddleware
from notebooklm._middleware_metrics import MetricsMiddleware
from notebooklm._middleware_retry import RetryMiddleware
from notebooklm._middleware_semaphore import SemaphoreMiddleware
from notebooklm._middleware_tracing import TracingMiddleware


def _builder_kwargs():
    """Return kwargs sufficient to instantiate MiddlewareChainBuilder."""

    async def _snapshot():
        return MagicMock()

    return {
        "drain_tracker": MagicMock(),
        "metrics": MagicMock(),
        "rpc_semaphore_factory": lambda: nullcontext(),
        "rate_limit_max_retries_provider": lambda: 3,
        "server_error_max_retries_provider": lambda: 3,
        "refresh_retry_delay_provider": lambda: 0.0,
        "refresh_callable": lambda: None,
        "auth_snapshot_provider": _snapshot,
        "is_auth_error": lambda exc: False,
        "refresh_callback_enabled_provider": lambda: True,
    }


def test_builder_returns_adr_009_order():
    from notebooklm._middleware_chain import MiddlewareChainBuilder

    chain = MiddlewareChainBuilder(**_builder_kwargs()).build()

    assert len(chain) == 7
    assert isinstance(chain[0], DrainMiddleware)
    assert isinstance(chain[1], MetricsMiddleware)
    assert isinstance(chain[2], SemaphoreMiddleware)
    assert isinstance(chain[3], RetryMiddleware)
    assert isinstance(chain[4], AuthRefreshMiddleware)
    assert isinstance(chain[5], ErrorInjectionMiddleware)
    assert isinstance(chain[6], TracingMiddleware)
