"""Unit tests for MiddlewareChainBuilder — pins ADR-0009 ordering at builder level."""

from __future__ import annotations

from unittest.mock import MagicMock

from notebooklm._web.transport.middleware.auth_refresh import AuthRefreshMiddleware
from notebooklm._web.transport.middleware.error_injection import ErrorInjectionMiddleware
from notebooklm._web.transport.middleware.retry import RetryMiddleware
from notebooklm._web.transport.middleware.tracing import TracingMiddleware


def _builder_kwargs():
    """Return kwargs sufficient to instantiate MiddlewareChainBuilder."""

    async def _snapshot():
        return MagicMock()

    return {
        "metrics": MagicMock(),
        "rate_limit_max_retries_provider": lambda: 3,
        "server_error_max_retries_provider": lambda: 3,
        "retry_timeout_provider": lambda: 30.0,
        "refresh_retry_delay_provider": lambda: 0.0,
        "refresh_callable": lambda: None,
        "auth_snapshot_provider": _snapshot,
        "is_auth_error": lambda exc: False,
        "refresh_callback_enabled_provider": lambda: True,
    }


def test_builder_returns_adr_009_order():
    from notebooklm._web.transport.middleware.chain import MiddlewareChainBuilder

    chain = MiddlewareChainBuilder(**_builder_kwargs()).build()

    assert len(chain) == 4
    assert isinstance(chain[0], RetryMiddleware)
    assert isinstance(chain[1], AuthRefreshMiddleware)
    assert isinstance(chain[2], ErrorInjectionMiddleware)
    assert isinstance(chain[3], TracingMiddleware)
