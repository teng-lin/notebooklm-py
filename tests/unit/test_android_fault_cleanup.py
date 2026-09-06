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


@pytest.mark.parametrize("wrapper", ["core", "resilience"])
@pytest.mark.parametrize("primary_kind", ["error", "cancel", "none"])
@pytest.mark.parametrize("shutdown_kind", ["error", "interrupt"])
async def test_android_grpc_wrapper_preserves_primary_and_shutdown_evidence(
    wrapper: str,
    primary_kind: str,
    shutdown_kind: str,
) -> None:
    from tests._fault_server.android_resilience_scenarios import _run_server as resilience_server
    from tests._fault_server.android_scenarios import _run_server as core_server

    result = ScenarioResult("android", "grpc-cleanup-negative", "cleanup")
    primary = (
        ValueError("private-primary")
        if primary_kind == "error"
        else asyncio.CancelledError("private-primary")
        if primary_kind == "cancel"
        else None
    )
    shutdown = (
        SystemExit("private-shutdown")
        if shutdown_kind == "interrupt"
        else RuntimeError("private-shutdown")
    )
    stopped = False

    async def body(server) -> None:
        original_shutdown = server._shutdown

        async def failed_shutdown() -> None:
            nonlocal stopped
            await original_shutdown()
            stopped = True
            raise shutdown

        # The injected failure belongs to this server instance. Its real gRPC
        # listener is closed before the failure; no global substitution or HTTP.
        server._shutdown = failed_shutdown
        if primary is not None:
            raise primary

    expected = shutdown if isinstance(shutdown, SystemExit) or primary is None else primary
    with pytest.raises(type(expected)) as caught:
        await (core_server if wrapper == "core" else resilience_server)(result, body)
    assert caught.value is expected
    assert stopped
    cleanup = next(event for event in result.events if event["kind"] == "cleanup")
    assert cleanup["primary_error"] == (None if primary is None else type(primary).__name__)
    assert cleanup["cleanup_error_types"] == [type(shutdown).__name__]
    assert not all(result.checks.values())
    assert "private" not in json.dumps(result.events)
