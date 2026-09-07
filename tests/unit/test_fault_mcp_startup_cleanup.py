"""Negative ownership checks for the MCP fault lane's cleanup supervisor."""

import asyncio
import json
from pathlib import Path

import pytest

from tests._fault_server import mcp_startup_cleanup as cleanup


class StuckProcess:
    returncode = None

    def __init__(self):
        self.events = []

    async def wait(self):
        self.events.append("wait")
        if self.returncode is None:
            raise TimeoutError("graceful stop timed out")
        return self.returncode

    def kill(self):
        self.events.append("kill")
        self.returncode = -9


@pytest.mark.parametrize("has_primary", [True, False])
async def test_http_cleanup_kills_after_stop_and_wait_failures(tmp_path, monkeypatch, has_primary):
    process = StuckProcess()
    failure = ValueError("primary operation failure") if has_primary else None
    original_touch = Path.touch

    def fail_stop(path, *args, **kwargs):
        if path.name == "stop":
            raise PermissionError("stop notification refused")
        return original_touch(path, *args, **kwargs)

    monkeypatch.setattr(Path, "touch", fail_stop)
    if failure is not None:
        with pytest.raises(ValueError) as caught:
            try:
                raise failure
            finally:
                await cleanup.settle_http_worker(process, tmp_path, failure)
        assert caught.value is failure
    else:
        with pytest.raises(PermissionError):
            await cleanup.settle_http_worker(process, tmp_path, None)
    assert process.events == ["wait", "kill", "wait"]
    assert process.returncode == -9
    report = json.loads((tmp_path / "worker-cleanup.json").read_text())
    assert [item["step"] for item in report["failures"]] == ["stop", "graceful_wait"]
    assert report["exit_code"] == -9


async def test_report_failure_does_not_mask_primary_after_process_settlement(tmp_path, monkeypatch):
    process = StuckProcess()
    primary = RuntimeError("primary")

    def fail_report(*args, **kwargs):
        raise OSError("report write failed")

    monkeypatch.setattr(cleanup, "atomic_write_json", fail_report)
    await cleanup.settle_http_worker(process, tmp_path, primary)
    assert process.returncode == -9
    assert process.events == ["wait", "kill", "wait"]


async def test_failed_gate_release_still_closes_server_and_settles_callers(tmp_path):
    events = []
    released = asyncio.Event()
    started = asyncio.Event()

    class Upstream:
        active_handlers = 1

        def release(self, gate):
            events.append("release")
            raise OSError("gate release failed")

        async def aclose(self):
            events.append("close")
            released.set()
            self.active_handlers = 0
            raise TimeoutError("secondary close failure")

    async def caller():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Finalization depends on the server settling, as real opening does.
            await released.wait()
            events.append("caller_settled")
            raise

    task = asyncio.create_task(caller())
    await started.wait()
    primary = ValueError("original operation error")
    with pytest.raises(ValueError) as caught:
        try:
            raise primary
        finally:
            await cleanup.settle_calls_and_upstream(
                [task], Upstream(), tmp_path, primary, timeout=0.1
            )
    assert caught.value is primary
    assert task.cancelled()
    assert events == ["release", "close", "caller_settled"]
    report = json.loads((tmp_path / "upstream-cleanup.json").read_text())
    assert [failure["step"] for failure in report["failures"]] == ["release", "upstream_close"]
    assert report["active_handlers"] == 0
    assert report["pending_callers"] == 0


async def test_http_session_retains_body_error_when_worker_wait_fails(tmp_path, monkeypatch):
    """Exercise the context-manager wiring, not just its cleanup collaborator."""
    from contextlib import asynccontextmanager

    pytest.importorskip("fastmcp")
    from tests.integration.faults import test_mcp_auth_startup as startup

    process = StuckProcess()

    async def spawn(*args, **kwargs):
        return process

    @asynccontextmanager
    async def connection(*args, **kwargs):
        yield None, None, None

    @asynccontextmanager
    async def session(*args, **kwargs):
        yield object()

    monkeypatch.setattr(startup.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(startup, "streamable_http_client", connection)
    monkeypatch.setattr(startup, "ClientSession", session)
    (tmp_path / "ready").write_text(json.dumps({"url": "http://127.0.0.1:1/mcp"}))
    primary = ValueError("original MCP assertion")
    with pytest.raises(ValueError) as caught:
        async with startup._mcp_connection(tmp_path, 1, "http"):
            raise primary
    assert caught.value is primary
    assert process.events == ["wait", "kill", "wait"]
    report = json.loads((tmp_path / "worker-cleanup.json").read_text())
    assert report["primary_error"] == "ValueError"
    assert report["failures"] == [{"step": "graceful_wait", "error_type": "TimeoutError"}]


async def test_interrupted_caller_wait_reports_actual_pending_tasks(tmp_path, monkeypatch):
    released = asyncio.Event()
    entered = asyncio.Event()

    async def caller():
        entered.set()
        try:
            await released.wait()
        except asyncio.CancelledError:
            await released.wait()

    class Upstream:
        active_handlers = 0

        def release(self, name):
            pass

        async def aclose(self):
            pass

    async def interrupted_wait(*args, **kwargs):
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    task = asyncio.create_task(caller())
    await entered.wait()
    monkeypatch.setattr(cleanup.asyncio, "wait", interrupted_wait)
    try:
        await cleanup.settle_calls_and_upstream([task], Upstream(), tmp_path, ValueError("primary"))
        report = json.loads((tmp_path / "upstream-cleanup.json").read_text())
        assert report["pending_callers"] == 1
        assert not task.done()
        assert report["failures"] == [{"step": "caller_settlement", "error_type": "CancelledError"}]
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)
