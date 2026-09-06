"""Android cohort cleanup preserves errors while settling independent owners."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests._fault_server.android_cleanup import finish_cleanup, settle_actions
from tests._fault_server.common import ScenarioResult


@pytest.mark.parametrize("cancel", [False, True], ids=["primary-error", "primary-cancel"])
async def test_android_cleanup_keeps_primary_and_reports_sanitized_close_failure(
    cancel: bool,
) -> None:
    primary = asyncio.CancelledError("private-primary") if cancel else ValueError("private-primary")
    failure = RuntimeError("private-close")
    closed: list[str] = []

    async def close_client() -> None:
        closed.append("client")
        raise failure

    async def close_server() -> None:
        closed.append("server")

    result = ScenarioResult("android", "cleanup-negative", "cleanup")
    with pytest.raises(type(primary)) as caught:
        try:
            raise primary
        except BaseException:
            errors = await settle_actions([close_client, close_server])
            finish_cleanup(result, primary, errors, clean=True, handlers=0)
            raise
    assert caught.value is primary
    assert closed == ["client", "server"]
    assert result.checks["cleanup"] is False
    assert result.events[0]["cleanup_error_types"] == ["RuntimeError"]
    assert result.events[0]["primary_error"] == type(primary).__name__
    assert "private" not in json.dumps(result.events)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
async def test_android_cleanup_keeps_interpreter_exit_priority(
    interrupt: type[BaseException],
) -> None:
    original = interrupt("private-exit")
    later_closed = False

    async def interrupted_close() -> None:
        raise original

    async def later_close() -> None:
        nonlocal later_closed
        later_closed = True

    errors = await settle_actions([interrupted_close, later_close])
    result = ScenarioResult("android", "cleanup-interpreter", "cleanup")
    with pytest.raises(interrupt) as caught:
        finish_cleanup(result, asyncio.CancelledError(), errors, clean=True)
    assert caught.value is original
    assert later_closed
    assert "private" not in json.dumps(result.events)


async def test_android_cleanup_repeated_cancellation_does_not_abandon_close() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def close() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    task = asyncio.create_task(settle_actions([close]))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    errors = await asyncio.wait_for(task, 1)
    assert calls == 1
    assert len(errors) == 2
    assert all(isinstance(error, asyncio.CancelledError) for error in errors)
