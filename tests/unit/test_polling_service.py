"""Unit tests for shared CLI polling helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from notebooklm.cli.services import polling
from notebooklm.cli.services.polling import poll_until, status_with_elapsed


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

    with patch.object(polling.console, "status") as status:
        async with status_with_elapsed("Waiting for work...", json_output=True):
            entered = True

    assert entered is True
    status.assert_not_called()
