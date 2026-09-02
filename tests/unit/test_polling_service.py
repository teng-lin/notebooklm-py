"""Unit tests for shared CLI polling helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from notebooklm.cli import polling_ui
from notebooklm.cli.polling_ui import status_with_elapsed
from notebooklm.cli.services import polling
from notebooklm.cli.services.polling import poll_until


@pytest.mark.asyncio
async def test_poll_until_returns_timeout_result_with_last_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeouts return the last fetched value without hiding the timeout state."""
    clock = 0.0
    calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        nonlocal clock
        clock += delay

    async def fetch() -> dict[str, int | str]:
        calls.append(clock)
        return {"status": "pending", "attempt": len(calls)}

    monkeypatch.setattr(polling.time, "monotonic", lambda: clock)
    monkeypatch.setattr(polling.asyncio, "sleep", fake_sleep)

    result = await poll_until(
        fetch,
        lambda value: value["status"] == "completed",
        timeout=0.5,
        interval=0.25,
    )

    assert result.timed_out is True
    assert result.value == {"status": "pending", "attempt": 3}
    assert result.attempts == 3
    assert result.elapsed == 0.5
    assert calls == [0.0, 0.25, 0.5]


@pytest.mark.asyncio
async def test_poll_until_propagates_cancellation() -> None:
    """Cancellation is not converted into a timeout result."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch() -> str:
        started.set()
        await release.wait()
        return "pending"

    task = asyncio.create_task(
        poll_until(fetch, lambda value: value == "completed", timeout=60.0, interval=1.0)
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_status_with_elapsed_json_output_is_no_op() -> None:
    """JSON mode must not start a Rich spinner that could pollute stdout."""
    entered = False

    with patch.object(polling_ui.console, "status") as status:
        async with status_with_elapsed("Waiting for work...", json_output=True):
            entered = True

    assert entered is True
    status.assert_not_called()


@pytest.mark.asyncio
async def test_poll_until_returns_immediately_when_the_first_fetch_is_terminal() -> None:
    """The happy path costs exactly one fetch and never sleeps."""
    sleeps: list[float] = []

    async def fetch() -> str:
        return "completed"

    with patch.object(polling.asyncio, "sleep", side_effect=lambda d: sleeps.append(d)):
        result = await poll_until(
            fetch, lambda value: value == "completed", timeout=10.0, interval=1.0
        )

    assert (result.value, result.attempts, result.timed_out) == ("completed", 1, False)
    assert result.elapsed >= 0.0
    assert sleeps == []


@pytest.mark.asyncio
async def test_poll_until_still_fetches_once_with_a_zero_timeout() -> None:
    """``timeout=0`` is a single-shot poll, not a refusal."""
    attempts = 0

    async def fetch() -> str:
        nonlocal attempts
        attempts += 1
        return "pending"

    result = await poll_until(fetch, lambda value: value == "done", timeout=0.0, interval=1.0)

    assert (attempts, result.attempts, result.timed_out) == (1, 1, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param({"timeout": -1.0, "interval": 1.0}, "non-negative", id="negative-timeout"),
        pytest.param({"timeout": 1.0, "interval": 0.0}, "must be positive", id="zero-interval"),
        pytest.param(
            {"timeout": 1.0, "interval": -1.0}, "must be positive", id="negative-interval"
        ),
    ],
)
async def test_poll_until_rejects_unusable_budgets(kwargs: dict, message: str) -> None:
    """Rejection happens before the first fetch."""
    called = False

    async def fetch() -> str:
        nonlocal called
        called = True
        return "pending"

    with pytest.raises(ValueError, match=message):
        await poll_until(fetch, lambda _value: True, **kwargs)

    assert called is False
