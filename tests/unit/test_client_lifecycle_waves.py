"""B0b characterization for root-owned phased lifecycle waves."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from notebooklm._runtime.lifecycle import ClientLifecycle
from notebooklm.types import ConnectionLimits


@dataclass
class _Supervisor:
    events: list[str] = field(default_factory=list)
    wait_gate: asyncio.Event | None = None
    wait_error: BaseException | None = None

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.events.append("bind")

    def reset_after_open(self) -> None:
        self.events.append("reset")

    def prepare_generation(self, epoch: int) -> None:
        self.events.append(f"prepare-generation:{epoch}")

    def start_accepting(self, epoch: int) -> None:
        self.events.append(f"accept:{epoch}")

    async def stop_accepting(self, epoch: int) -> None:
        self.events.append(f"drain:{epoch}")

    async def wait_for_idle(self, epoch: int, timeout: float | None) -> None:
        self.events.append(f"idle:{epoch}:{timeout}")
        if self.wait_error is not None:
            raise self.wait_error
        if self.wait_gate is not None:
            await self.wait_gate.wait()

    async def begin_closing(self, epoch: int) -> None:
        self.events.append(f"closing:{epoch}")

    def mark_closed(self, epoch: int) -> None:
        self.events.append(f"closed:{epoch}")

    async def run_drain_hooks(self) -> None:
        self.events.append("hooks")


@dataclass
class _Participant:
    events: list[str]
    name: str

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.events.append(f"bind:{self.name}")

    def reset_after_open(self) -> None:
        self.events.append(f"reset:{self.name}")


@dataclass
class _Transport:
    name: str
    events: list[str]
    open_gate: asyncio.Event | None = None
    open_error: BaseException | None = None
    prepare_error: BaseException | None = None
    close_error: BaseException | None = None
    opens: list[int] = field(default_factory=list)

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        self.opens.append(epoch)
        self.events.append(f"open:{self.name}:{epoch}")
        if self.open_gate is not None:
            await self.open_gate.wait()
        if self.open_error is not None:
            raise self.open_error

    async def prepare_close(self) -> None:
        self.events.append(f"prepare:{self.name}")
        if self.prepare_error is not None:
            raise self.prepare_error

    async def close_resources(self) -> None:
        self.events.append(f"close:{self.name}")
        if self.close_error is not None:
            raise self.close_error


def _lifecycle(
    supervisor: _Supervisor,
    *transports: _Transport,
    participants: tuple[_Participant, ...] = (),
) -> ClientLifecycle:
    return ClientLifecycle(
        timeout=1,
        connect_timeout=1,
        limits=ConnectionLimits(),
        keepalive_interval=None,
        keepalive_storage_path=None,
        supervisor=supervisor,
        transports=transports,
        loop_participants=participants,
    )


@pytest.mark.asyncio
async def test_open_is_transactional_and_concurrent_callers_coalesce() -> None:
    events: list[str] = []
    gate = asyncio.Event()
    supervisor = _Supervisor(events=events)
    transport = _Transport("web", events, open_gate=gate)
    lifecycle = _lifecycle(supervisor, transport, participants=(_Participant(events, "reqid"),))

    owner = asyncio.create_task(lifecycle.open())
    await asyncio.sleep(0)
    joiner = asyncio.create_task(lifecycle.open())
    await asyncio.sleep(0)

    assert not lifecycle.is_open()
    assert transport.opens == [1]
    gate.set()
    await asyncio.gather(owner, joiner)

    assert lifecycle.is_open()
    assert transport.opens == [1]
    assert events.index("prepare-generation:1") < events.index("open:web:1")
    assert events.index("open:web:1") < events.index("accept:1")


@pytest.mark.asyncio
async def test_open_failure_rolls_back_every_transport_and_preserves_original() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    original = LookupError("open failed")
    first = _Transport("first", events)
    second = _Transport(
        "second",
        events,
        open_error=original,
        prepare_error=RuntimeError("rollback prepare"),
        close_error=RuntimeError("rollback close"),
    )
    lifecycle = _lifecycle(supervisor, first, second)

    with pytest.raises(LookupError, match="open failed") as raised:
        await lifecycle.open()

    assert raised.value is original
    assert not lifecycle.is_open()
    for expected in ("prepare:first", "prepare:second", "close:first", "close:second"):
        assert expected in events
    assert "closed:1" in events


@pytest.mark.asyncio
async def test_manual_drain_keeps_resources_open_and_close_waves_coalesce() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    first = _Transport("first", events)
    second = _Transport("second", events)
    lifecycle = _lifecycle(supervisor, first, second)
    await lifecycle.open()

    await lifecycle.drain(timeout=0.5)
    assert lifecycle.is_open()

    await asyncio.gather(lifecycle.close(), lifecycle.close(drain=False))
    assert not lifecycle.is_open()
    assert events.count("hooks") == 2
    assert events.count("prepare:first") == 1
    assert events.count("prepare:second") == 1
    assert events.count("close:first") == 1
    assert events.count("close:second") == 1
    assert events.index("closing:1") < events.index("prepare:first")


@pytest.mark.asyncio
async def test_drain_timeout_precedes_ordered_transport_failures() -> None:
    events: list[str] = []
    timeout = TimeoutError("idle timed out")
    supervisor = _Supervisor(events=events, wait_error=timeout)
    first_error = ValueError("first prepare")
    lifecycle = _lifecycle(
        supervisor,
        _Transport("first", events, prepare_error=first_error),
        _Transport("second", events, close_error=RuntimeError("second close")),
    )
    await lifecycle.open()

    with pytest.raises(TimeoutError, match="idle timed out") as raised:
        await lifecycle.close(drain_timeout=0.01)

    assert raised.value is timeout
    assert raised.value.__cause__ is first_error
    assert not lifecycle.is_open()
    assert "close:second" in events


@pytest.mark.asyncio
async def test_cancelled_close_aborts_hung_graceful_wait_but_finishes_teardown() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events, wait_gate=asyncio.Event())
    lifecycle = _lifecycle(supervisor, _Transport("web", events))
    await lifecycle.open()

    closing = asyncio.create_task(lifecycle.close())
    while not any(event.startswith("idle:") for event in events):
        await asyncio.sleep(0)
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not lifecycle.is_open()
    assert "prepare:web" in events
    assert "close:web" in events


@pytest.mark.asyncio
async def test_close_reopen_allocates_a_new_resource_epoch() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    transport = _Transport("web", events)
    lifecycle = _lifecycle(supervisor, transport)

    await lifecycle.open()
    await lifecycle.close(drain=False, drain_timeout=-1)
    await lifecycle.open()

    assert transport.opens == [1, 2]
    assert lifecycle.current_epoch == 2


@pytest.mark.asyncio
async def test_closed_noops_and_timeout_validation_are_resource_independent() -> None:
    lifecycle = _lifecycle(_Supervisor(), _Transport("web", []))

    await lifecycle.drain()
    await lifecycle.close()
    await lifecycle.close(drain=False, drain_timeout=-1)
    with pytest.raises(ValueError, match="timeout must be"):
        await lifecycle.drain(-1)
    with pytest.raises(ValueError, match="timeout must be"):
        await lifecycle.close(drain_timeout=-1)
