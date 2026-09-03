"""Deterministic root-lifecycle coverage for supervised Web note children."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._runtime.lifecycle import ClientLifecycle
from notebooklm._web.note_tasks import NoteTaskRegistry
from notebooklm._web.notes import NoteService
from notebooklm.rpc import RPCMethod


class _Transport:
    name = "web"

    def __init__(self) -> None:
        self.events: list[str] = []

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        assert loop is asyncio.get_running_loop()
        self.events.append(f"open:{epoch}")

    async def prepare_close(self) -> None:
        self.events.append("prepare")

    async def close_resources(self) -> None:
        self.events.append("close")


class _RpcHarness:
    """Run fake Web RPC bodies through real supervisor admission."""

    def __init__(self, supervisor: CallSupervisor) -> None:
        self._supervisor = supervisor
        self.calls: list[RPCMethod] = []
        self.update_started = asyncio.Event()
        self.update_release = asyncio.Event()
        self.update_cancelled = asyncio.Event()

    async def rpc_call(
        self,
        method: RPCMethod,
        *_args: object,
        **_kwargs: object,
    ) -> Any:
        async with self._supervisor.operation_scope(f"test-rpc:{method.value}"):
            # Record only after admission. In particular, a cleanup DELETE
            # attempted after CLOSING must fail before this transport-I/O seam.
            self.calls.append(method)
            if method is RPCMethod.CREATE_NOTE:
                return [["note-lifecycle"]]
            if method is RPCMethod.UPDATE_NOTE:
                self.update_started.set()
                try:
                    await self.update_release.wait()
                except asyncio.CancelledError:
                    self.update_cancelled.set()
                    raise
                return None
            if method is RPCMethod.DELETE_NOTE:
                return None
            raise AssertionError(f"unexpected RPC: {method}")


def _runtime() -> tuple[CallSupervisor, ClientLifecycle, _Transport, _RpcHarness, NoteService]:
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=None,
    )
    transport = _Transport()
    lifecycle = ClientLifecycle(
        supervisor=supervisor,
        transports=(transport,),
        loop_participants=(supervisor,),
    )
    rpc = _RpcHarness(supervisor)
    notes = NoteService(rpc, supervisor=supervisor)
    return supervisor, lifecycle, transport, rpc, notes


async def _forced_close(lifecycle: ClientLifecycle, mode: str) -> None:
    if mode == "no-drain":
        await lifecycle.close(drain=False)
        return
    if mode == "drain-timeout":
        with pytest.raises(TimeoutError):
            await lifecycle.close(drain_timeout=0.0)
        return
    raise AssertionError(f"unknown close mode: {mode}")


async def _wait_for_published_child(registry: NoteTaskRegistry, name: str) -> None:
    async def _wait() -> None:
        while not any(task.get_name() == name for task in registry._tasks):
            await asyncio.sleep(0)

    await asyncio.wait_for(_wait(), timeout=1)


@pytest.mark.asyncio
async def test_graceful_close_allows_admitted_finalize_to_return_without_delete() -> None:
    supervisor, lifecycle, transport, rpc, notes = _runtime()
    graceful_hook_ran = asyncio.Event()

    async def _observe_graceful_hook() -> None:
        graceful_hook_ran.set()

    # Registration order puts the note hook first. Because the update holds
    # wait_for_idle, this event can only be from the DRAINING hook pass.
    supervisor.register_drain_hook("test.graceful-observer", _observe_graceful_hook)
    await lifecycle.open()
    create = asyncio.create_task(notes.create_note("nb", "Title", "Body"))
    await rpc.update_started.wait()
    await _wait_for_published_child(notes._task_registry, "note-update-nb-note-lifecycle")

    closing = asyncio.create_task(lifecycle.close())
    await graceful_hook_ran.wait()
    await asyncio.sleep(0)

    assert supervisor.is_closing() is False
    assert not create.done()
    assert not rpc.update_cancelled.is_set()
    assert RPCMethod.DELETE_NOTE not in rpc.calls

    rpc.update_release.set()
    note = await create
    await closing

    assert note.id == "note-lifecycle"
    assert rpc.calls == [RPCMethod.CREATE_NOTE, RPCMethod.UPDATE_NOTE]
    assert notes._task_registry.active_tasks() == []
    assert supervisor._retired == {}
    assert transport.events == ["open:1", "prepare", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no-drain", "drain-timeout"])
async def test_forced_close_cancels_and_settles_finalize_child(mode: str) -> None:
    supervisor, lifecycle, transport, rpc, notes = _runtime()
    await lifecycle.open()
    create = asyncio.create_task(notes.create_note("nb", "Title", "Body"))
    await rpc.update_started.wait()
    await _wait_for_published_child(notes._task_registry, "note-update-nb-note-lifecycle")

    await _forced_close(lifecycle, mode)

    with pytest.raises(asyncio.CancelledError):
        await create
    assert rpc.update_cancelled.is_set()
    assert RPCMethod.DELETE_NOTE not in rpc.calls
    assert notes._task_registry.active_tasks() == []
    assert supervisor._retired == {}
    assert transport.events == ["open:1", "prepare", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no-drain", "drain-timeout"])
async def test_forced_close_cancels_and_settles_finalize_and_cleanup_children(mode: str) -> None:
    supervisor, lifecycle, _transport, rpc, notes = _runtime()
    await lifecycle.open()
    create = asyncio.create_task(notes.create_note("nb", "Title", "Body"))
    await rpc.update_started.wait()
    await _wait_for_published_child(notes._task_registry, "note-update-nb-note-lifecycle")

    create.cancel()
    with pytest.raises(asyncio.CancelledError):
        await create

    active_names = {task.get_name() for task in notes._task_registry.active_tasks()}
    assert active_names == {
        "note-update-nb-note-lifecycle",
        "note-cleanup-nb-note-lifecycle",
    }

    await _forced_close(lifecycle, mode)

    assert rpc.update_cancelled.is_set()
    # The cleanup child's finally block may try DELETE_NOTE, but CLOSING
    # rejects its nested operation before the fake transport records I/O.
    assert RPCMethod.DELETE_NOTE not in rpc.calls
    assert notes._task_registry.active_tasks() == []
    assert supervisor._retired == {}
