"""Regression test for the httpx connection-pool tuning via ConnectionLimits.

Audit item #3 (`thread-safety-concurrency-audit.md` §3, also §19):
Pre-fix, `ClientCore.open()` instantiated `httpx.AsyncClient(...)` with
no `limits=` kwarg, defaulting to httpx's `~100 / 20-per-host` pool.
Heavy fan-out workloads (FastAPI services sharing a client across many
concurrent requests, large `wait_for_sources` batches) tripped
`httpx.PoolTimeout`.

Post-fix: a stable `ConnectionLimits` dataclass on
`notebooklm.types` exposes pool tuning, defaults to
`max_connections=100, max_keepalive_connections=50,
keepalive_expiry=30.0`, and is plumbed through `ClientCore.__init__`
and `NotebookLMClient.__init__` / `from_storage`.

The test asserts the limits passed to `httpx.AsyncClient` match the
configured values. A real-pool stress test (200-way fan-out against
a stdlib http.server) is the audit's preferred verification, but the
deterministic constructor-args check is sufficient and avoids the
infrastructure overhead of an in-process server in the test suite.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from notebooklm import NotebookLMClient
from notebooklm.types import ConnectionLimits

# pool-config + patch-based tuning tests; no HTTP, no cassette.
# Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def test_connection_limits_dataclass_defaults() -> None:
    """The defaults match the audit's recommendation for typical fan-out."""
    limits = ConnectionLimits()
    assert limits.max_connections == 100
    assert limits.max_keepalive_connections == 50
    assert limits.keepalive_expiry == 30.0


def test_connection_limits_to_httpx_limits_round_trip() -> None:
    """to_httpx_limits() preserves all three fields."""
    cl = ConnectionLimits(
        max_connections=200,
        max_keepalive_connections=80,
        keepalive_expiry=15.0,
    )
    hl = cl.to_httpx_limits()
    assert isinstance(hl, httpx.Limits)
    assert hl.max_connections == 200
    assert hl.max_keepalive_connections == 80
    assert hl.keepalive_expiry == 15.0


async def test_default_limits_passed_to_async_client(auth_tokens) -> None:
    """No explicit `limits=` -> ClientCore uses ConnectionLimits() defaults."""
    captured: dict[str, httpx.Limits | None] = {"limits": None}
    real_async_client = httpx.AsyncClient

    def _capturing_client(**kwargs: object) -> httpx.AsyncClient:
        # type: ignore[misc] — mock keeps signature flexible
        captured["limits"] = kwargs.get("limits")  # type: ignore[assignment]
        return real_async_client(**kwargs)  # type: ignore[arg-type]

    with patch("notebooklm._core.httpx.AsyncClient", side_effect=_capturing_client):
        async with NotebookLMClient(auth_tokens):
            pass

    captured_limits = captured["limits"]
    assert isinstance(captured_limits, httpx.Limits)
    assert captured_limits.max_connections == 100
    assert captured_limits.max_keepalive_connections == 50
    assert captured_limits.keepalive_expiry == 30.0


async def test_custom_limits_passed_to_async_client(auth_tokens) -> None:
    """Explicit `limits=ConnectionLimits(...)` -> AsyncClient sees those values."""
    custom = ConnectionLimits(
        max_connections=500,
        max_keepalive_connections=100,
        keepalive_expiry=10.0,
    )
    captured: dict[str, httpx.Limits | None] = {"limits": None}
    real_async_client = httpx.AsyncClient

    def _capturing_client(**kwargs: object) -> httpx.AsyncClient:
        captured["limits"] = kwargs.get("limits")  # type: ignore[assignment]
        return real_async_client(**kwargs)  # type: ignore[arg-type]

    with patch("notebooklm._core.httpx.AsyncClient", side_effect=_capturing_client):
        async with NotebookLMClient(auth_tokens, limits=custom):
            pass

    captured_limits = captured["limits"]
    assert isinstance(captured_limits, httpx.Limits)
    assert captured_limits.max_connections == 500
    assert captured_limits.max_keepalive_connections == 100
    assert captured_limits.keepalive_expiry == 10.0


def test_connection_limits_is_frozen() -> None:
    """Frozen dataclass — callers can't accidentally mutate after passing in."""
    import dataclasses

    limits = ConnectionLimits()
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_connections = 999  # type: ignore[misc]
