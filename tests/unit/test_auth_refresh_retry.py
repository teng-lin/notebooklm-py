"""Unit tests for the shared auth refresh-and-retry core (issue #1205).

Pins the contract documented in ``src/notebooklm/_auth_refresh_retry.py``:

- :class:`RefreshBudget` is a single-consume once-per-logical-call token.
- :func:`refresh_and_count` owns the common refresh body shared by the
  HTTP-status layer (``AuthRefreshMiddleware``) and the decoded-RPC layer
  (``RpcExecutor``): log → refresh → on-failure raise (caller-shaped) →
  optional sleep → log → ``rpc_auth_retries`` metric increment.
"""

from __future__ import annotations

import logging

import pytest

from notebooklm._auth_refresh_retry import RefreshBudget, refresh_and_count
from notebooklm._client_metrics import ClientMetrics

# ---------------------------------------------------------------------------
# RefreshBudget
# ---------------------------------------------------------------------------


def test_refresh_budget_consumes_exactly_once() -> None:
    budget = RefreshBudget()
    assert budget.available is True
    assert budget.consume() is True
    assert budget.available is False
    # Every subsequent consume returns False — the single allowance is spent.
    assert budget.consume() is False
    assert budget.consume() is False
    assert budget.available is False


# ---------------------------------------------------------------------------
# refresh_and_count — success path
# ---------------------------------------------------------------------------


async def _noop_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_refresh_and_count_success_logs_sleeps_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refresh_calls: list[None] = []
    sleeps: list[float] = []

    async def refresh() -> None:
        refresh_calls.append(None)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    metrics = ClientMetrics()

    def _should_not_fail(_error: Exception) -> BaseException:  # pragma: no cover
        raise AssertionError("on_refresh_failure must not be called on success")

    with caplog.at_level(logging.INFO, logger="notebooklm.test_arr"):
        await refresh_and_count(
            refresh=refresh,
            on_refresh_failure=_should_not_fail,
            sleep=sleep,
            refresh_retry_delay=0.25,
            log_label="RPC LIST_NOTEBOOKS",
            logger=logging.getLogger("notebooklm.test_arr"),
            metrics=metrics,
        )

    assert refresh_calls == [None]
    assert sleeps == [0.25]
    assert metrics.snapshot().rpc_auth_retries == 1
    info_msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any(
        "RPC LIST_NOTEBOOKS auth error detected, attempting token refresh" in m for m in info_msgs
    )
    assert any("Token refresh successful, retrying RPC LIST_NOTEBOOKS" in m for m in info_msgs)


@pytest.mark.asyncio
async def test_refresh_and_count_zero_delay_skips_sleep() -> None:
    sleeps: list[float] = []

    async def refresh() -> None:
        return None

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    await refresh_and_count(
        refresh=refresh,
        on_refresh_failure=lambda _e: AssertionError("unreachable"),
        sleep=sleep,
        refresh_retry_delay=0.0,
        log_label="RPC X",
        logger=logging.getLogger("notebooklm.test_arr2"),
        metrics=None,
    )

    assert sleeps == []


# ---------------------------------------------------------------------------
# refresh_and_count — failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_and_count_failure_raises_caller_shape_chained(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refresh_error = RuntimeError("login expired")
    sentinel = ValueError("caller-shaped failure")

    async def refresh() -> None:
        raise refresh_error

    metrics = ClientMetrics()

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm.test_arr3"),
        pytest.raises(ValueError) as excinfo,
    ):
        await refresh_and_count(
            refresh=refresh,
            on_refresh_failure=lambda _e: sentinel,
            sleep=_noop_sleep,
            refresh_retry_delay=0.25,
            log_label="RPC LIST_NOTEBOOKS",
            logger=logging.getLogger("notebooklm.test_arr3"),
            metrics=metrics,
        )

    # The caller-supplied exception is raised, chained from the refresh error.
    assert excinfo.value is sentinel
    assert excinfo.value.__cause__ is refresh_error
    # No metric increment and no sleep happen on a refresh failure.
    assert metrics.snapshot().rpc_auth_retries == 0
    warn_msgs = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("Token refresh failed: login expired" in m for m in warn_msgs)


@pytest.mark.asyncio
async def test_refresh_and_count_failure_receives_refresh_error() -> None:
    refresh_error = RuntimeError("boom")
    received: list[Exception] = []

    async def refresh() -> None:
        raise refresh_error

    def on_failure(error: Exception) -> BaseException:
        received.append(error)
        return KeyError("mapped")

    with pytest.raises(KeyError):
        await refresh_and_count(
            refresh=refresh,
            on_refresh_failure=on_failure,
            sleep=_noop_sleep,
            refresh_retry_delay=0.0,
            log_label="RPC X",
            logger=logging.getLogger("notebooklm.test_arr4"),
            metrics=None,
        )

    assert received == [refresh_error]
