"""B0b characterization for root-owned phased lifecycle waves."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field

import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._runtime.lifecycle import ClientLifecycle, _ResourceState
from notebooklm._transport_drain import TransportDrainTracker


def _assert_republished_cancel_message(error: asyncio.CancelledError, expected: str) -> None:
    """Assert first-cancel precedence across supported asyncio versions.

    Python 3.10 drops the optional cancellation message when cancellation is
    caught and republished through shielded lifecycle cleanup. Python 3.11+
    preserves it; neither version may replace it with a later message.
    """
    assert error.args == ((expected,) if sys.version_info >= (3, 11) else ())


@dataclass
class _Supervisor:
    events: list[str] = field(default_factory=list)
    wait_gate: asyncio.Event | None = None
    stop_error: BaseException | None = None
    stop_errors: list[BaseException] = field(default_factory=list)
    wait_error: BaseException | None = None
    hook_errors: list[BaseException] = field(default_factory=list)
    start_error: BaseException | None = None
    closing_error: BaseException | None = None
    mark_error: BaseException | None = None

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.events.append("bind")

    def reset_after_open(self) -> None:
        self.events.append("reset")

    def prepare_generation(self, epoch: int) -> None:
        self.events.append(f"prepare-generation:{epoch}")

    def start_accepting(self, epoch: int) -> None:
        self.events.append(f"accept:{epoch}")
        if self.start_error is not None:
            raise self.start_error

    async def stop_accepting(self, epoch: int) -> None:
        self.events.append(f"drain:{epoch}")
        # ``stop_errors`` fails a bounded number of calls; ``close()`` also
        # calls this, so a persistent ``stop_error`` would break teardown too.
        if self.stop_errors:
            raise self.stop_errors.pop(0)
        if self.stop_error is not None:
            raise self.stop_error

    async def wait_for_idle(self, epoch: int, timeout: float | None) -> None:
        self.events.append(f"idle:{epoch}:{timeout}")
        if self.wait_gate is not None:
            await self.wait_gate.wait()
        if self.wait_error is not None:
            raise self.wait_error

    async def begin_closing(self, epoch: int) -> None:
        self.events.append(f"closing:{epoch}")
        if self.closing_error is not None:
            raise self.closing_error

    def mark_closed(self, epoch: int) -> None:
        self.events.append(f"closed:{epoch}")
        if self.mark_error is not None:
            raise self.mark_error

    async def run_drain_hooks(self) -> None:
        self.events.append("hooks")
        if self.hook_errors:
            raise self.hook_errors.pop(0)


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
    prepare_gate: asyncio.Event | None = None
    close_gate: asyncio.Event | None = None
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
        if self.prepare_gate is not None:
            await self.prepare_gate.wait()
        if self.prepare_error is not None:
            raise self.prepare_error

    async def close_resources(self) -> None:
        self.events.append(f"close:{self.name}")
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error is not None:
            raise self.close_error


def _lifecycle(
    supervisor: _Supervisor,
    *transports: _Transport,
    participants: tuple[_Participant, ...] = (),
) -> ClientLifecycle:
    return ClientLifecycle(
        supervisor=supervisor,
        transports=transports,
        loop_participants=(supervisor, *participants),
    )


async def _wait_for_event(events: list[str], prefix: str) -> None:
    """Yield until a lifecycle phase records ``prefix`` or fail deterministically."""
    for _ in range(100):
        if any(event.startswith(prefix) for event in events):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"lifecycle event {prefix!r} was not observed: {events!r}")


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
async def test_open_commit_failure_rolls_back_releases_joiners_and_allows_reopen() -> None:
    events: list[str] = []
    gate = asyncio.Event()
    original = LookupError("commit failed")
    supervisor = _Supervisor(events=events, start_error=original)
    first = _Transport("first", events)
    second = _Transport(
        "second",
        events,
        open_gate=gate,
        prepare_error=RuntimeError("rollback prepare"),
        close_error=RuntimeError("rollback close"),
    )
    lifecycle = _lifecycle(supervisor, first, second)

    owner = asyncio.create_task(lifecycle.open())
    await asyncio.sleep(0)
    joiners = [
        asyncio.create_task(lifecycle.open()),
        asyncio.create_task(lifecycle.close(drain=False)),
        asyncio.create_task(lifecycle.drain()),
    ]
    await asyncio.sleep(0)
    gate.set()

    results = await asyncio.gather(owner, *joiners, return_exceptions=True)

    assert all(result is original for result in results)
    assert not lifecycle.is_open()
    assert lifecycle._state is _ResourceState.CLOSED
    assert lifecycle._open_wave is None
    for expected in ("prepare:first", "prepare:second", "close:first", "close:second"):
        assert events.count(expected) == 1
    assert events.count("closed:1") == 1

    supervisor.start_error = None
    second.prepare_error = None
    second.close_error = None
    await lifecycle.open()
    assert lifecycle.is_open()
    assert first.opens == [1, 2]
    assert second.opens == [1, 2]
    await lifecycle.close(drain=False)
    assert not lifecycle.is_open()


@pytest.mark.asyncio
async def test_first_cancel_during_failed_open_waits_for_rollback_and_wins() -> None:
    events: list[str] = []
    rollback_gate = asyncio.Event()
    original = LookupError("open failed")
    lifecycle = _lifecycle(
        _Supervisor(events=events),
        _Transport(
            "web",
            events,
            open_error=original,
            prepare_gate=rollback_gate,
        ),
    )

    owner = asyncio.create_task(lifecycle.open())
    while "prepare:web" not in events:
        await asyncio.sleep(0)
    owner.cancel("first cancellation")
    await asyncio.sleep(0)

    assert not owner.done()
    rollback_gate.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await owner

    _assert_republished_cancel_message(raised.value, "first cancellation")
    assert not lifecycle.is_open()
    assert "close:web" in events


@pytest.mark.asyncio
@pytest.mark.parametrize("recancel", [False, True])
async def test_process_exit_from_failed_open_beats_cancellation_during_rollback(
    recancel: bool,
) -> None:
    events: list[str] = []
    rollback_gate = asyncio.Event()
    process_exit = SystemExit("open shutdown")
    supervisor = _Supervisor(events=events, start_error=process_exit)
    lifecycle = _lifecycle(
        supervisor,
        _Transport("web", events, prepare_gate=rollback_gate),
    )

    async def capture_open_outcome() -> BaseException | None:
        try:
            await lifecycle.open()
        except BaseException as exc:
            return exc
        return None

    owner = asyncio.create_task(capture_open_outcome())
    await _wait_for_event(events, "prepare:web")
    owner.cancel("caller cancellation")
    await asyncio.sleep(0)

    assert not owner.done()
    if recancel:
        owner.cancel("caller recancellation")
    else:
        rollback_gate.set()
    assert await owner is process_exit

    rollback_gate.set()
    for _ in range(100):
        if not lifecycle.is_open() and lifecycle._open_wave is None:
            break
        await asyncio.sleep(0)
    assert not lifecycle.is_open()


@pytest.mark.asyncio
async def test_recancel_during_failed_open_detaches_rollback_with_first_cancel() -> None:
    events: list[str] = []
    rollback_gate = asyncio.Event()
    lifecycle = _lifecycle(
        _Supervisor(events=events),
        _Transport(
            "web",
            events,
            open_error=LookupError("open failed"),
            prepare_gate=rollback_gate,
        ),
    )

    owner = asyncio.create_task(lifecycle.open())
    while "prepare:web" not in events:
        await asyncio.sleep(0)
    owner.cancel("first cancellation")
    await asyncio.sleep(0)
    owner.cancel("second cancellation")

    with pytest.raises(asyncio.CancelledError) as raised:
        await owner
    _assert_republished_cancel_message(raised.value, "first cancellation")

    rollback_gate.set()
    for _ in range(100):
        if "close:web" in events:
            break
        await asyncio.sleep(0)
    assert "close:web" in events
    assert not lifecycle.is_open()


@pytest.mark.asyncio
async def test_cancel_immediately_after_prepare_before_commit_rolls_back() -> None:
    events: list[str] = []
    rollback_gate = asyncio.Event()
    transport = _Transport("web", events, prepare_gate=rollback_gate)
    lifecycle = _lifecycle(_Supervisor(events=events), transport)
    owner: asyncio.Task[None] | None = None

    async def cancel_before_commit(loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        await _Transport.open(transport, loop, epoch)
        assert owner is not None
        loop.call_soon(owner.cancel, "between prepare and commit")

    transport.open = cancel_before_commit  # type: ignore[method-assign]
    owner = asyncio.create_task(lifecycle.open())
    while "prepare:web" not in events:
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not owner.done()
    rollback_gate.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await owner

    _assert_republished_cancel_message(raised.value, "between prepare and commit")
    assert "accept:1" not in events
    assert "close:web" in events
    assert not lifecycle.is_open()


@pytest.mark.asyncio
@pytest.mark.parametrize("waiter_action", ["close", "drain"])
async def test_failed_open_is_re_raised_to_close_and_drain_waiters(waiter_action: str) -> None:
    events: list[str] = []
    gate = asyncio.Event()
    original = LookupError("open failed")
    lifecycle = _lifecycle(
        _Supervisor(events=events),
        _Transport("web", events, open_gate=gate, open_error=original),
    )

    owner = asyncio.create_task(lifecycle.open())
    await asyncio.sleep(0)
    if waiter_action == "close":
        waiter = asyncio.create_task(lifecycle.close())
    else:
        waiter = asyncio.create_task(lifecycle.drain())
    await asyncio.sleep(0)
    gate.set()

    owner_result, waiter_result = await asyncio.gather(owner, waiter, return_exceptions=True)
    assert owner_result is original
    assert waiter_result is original
    assert not lifecycle.is_open()


@pytest.mark.asyncio
async def test_cancelling_non_owner_open_does_not_abort_owner() -> None:
    events: list[str] = []
    gate = asyncio.Event()
    transport = _Transport("web", events, open_gate=gate)
    lifecycle = _lifecycle(_Supervisor(events=events), transport)

    owner = asyncio.create_task(lifecycle.open())
    await asyncio.sleep(0)
    joiner = asyncio.create_task(lifecycle.open())
    await asyncio.sleep(0)
    joiner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await joiner
    assert not owner.done()
    gate.set()
    await owner
    assert lifecycle.is_open()
    assert transport.opens == [1]


@pytest.mark.asyncio
async def test_open_joiner_retries_after_owner_cancellation_rollback() -> None:
    events: list[str] = []
    gate = asyncio.Event()
    transport = _Transport("web", events, open_gate=gate)
    lifecycle = _lifecycle(_Supervisor(events=events), transport)

    owner = asyncio.create_task(lifecycle.open())
    while transport.opens != [1]:
        await asyncio.sleep(0)
    joiner = asyncio.create_task(lifecycle.open())
    await asyncio.sleep(0)

    owner.cancel("owner-aborted")
    with pytest.raises(asyncio.CancelledError) as raised:
        await owner
    _assert_republished_cancel_message(raised.value, "owner-aborted")

    # The aborted wave releases the non-owner only after rollback. It then
    # claims a fresh generation instead of inheriting the owner's cancellation.
    gate.set()
    await joiner
    assert lifecycle.is_open()
    assert transport.opens == [1, 2]
    assert events.count("closed:1") == 1
    assert events.count("accept:2") == 1

    await lifecycle.close(drain=False)


@pytest.mark.asyncio
async def test_open_owner_recancellation_leaves_retained_rollback_running() -> None:
    events: list[str] = []
    open_gate = asyncio.Event()
    rollback_gate = asyncio.Event()
    transport = _Transport(
        "web",
        events,
        open_gate=open_gate,
        prepare_gate=rollback_gate,
    )
    lifecycle = _lifecycle(_Supervisor(events=events), transport)

    owner = asyncio.create_task(lifecycle.open())
    await asyncio.sleep(0)
    owner.cancel()
    while "prepare:web" not in events:
        await asyncio.sleep(0)
    owner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await owner
    assert not lifecycle.is_open()
    rollback_gate.set()
    for _ in range(100):
        if "close:web" in events:
            break
        await asyncio.sleep(0)
    assert "close:web" in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "closing_error", [RuntimeError("closing failed"), asyncio.CancelledError()]
)
async def test_begin_closing_failure_restores_non_stranded_resource_state(
    closing_error: BaseException,
) -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events, closing_error=closing_error)
    lifecycle = _lifecycle(supervisor, _Transport("web", events))
    await lifecycle.open()

    with pytest.raises(type(closing_error), match=str(closing_error) or None):
        await lifecycle.close(drain=False)

    assert lifecycle.is_open()
    assert "prepare:web" not in events
    supervisor.closing_error = None
    await lifecycle.close(drain=False)
    assert not lifecycle.is_open()
    assert "close:web" in events


@pytest.mark.asyncio
async def test_graceful_prephase_cancellation_releases_joiners_and_allows_retry() -> None:
    events: list[str] = []
    gate = asyncio.Event()
    supervisor = _Supervisor(
        events=events,
        wait_gate=gate,
        wait_error=asyncio.CancelledError("idle wait cancelled"),
    )
    lifecycle = _lifecycle(supervisor, _Transport("web", events))
    await lifecycle.open()

    owner = asyncio.create_task(lifecycle.close())
    while not any(event.startswith("idle:") for event in events):
        await asyncio.sleep(0)
    joiner = asyncio.create_task(lifecycle.close())
    await asyncio.sleep(0)
    gate.set()

    results = await asyncio.gather(owner, joiner, return_exceptions=True)

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert lifecycle.is_open()
    assert lifecycle._state is _ResourceState.OPEN
    assert lifecycle._close_wave is None
    assert "prepare:web" not in events
    assert "close:web" not in events

    supervisor.wait_gate = None
    supervisor.wait_error = None
    await lifecycle.drain(timeout=0.5)
    assert lifecycle.is_open()
    await lifecycle.close(drain=False)
    assert not lifecycle.is_open()
    assert events.count("prepare:web") == 1
    assert events.count("close:web") == 1


@pytest.mark.asyncio
async def test_rollback_process_exit_beats_original_open_failure() -> None:
    from notebooklm._runtime.lifecycle import _capture, _OpenOutcome, _OpenWave

    process_exit = SystemExit("shutdown")
    supervisor = _Supervisor(mark_error=process_exit)
    lifecycle = _lifecycle(supervisor, _Transport("web", []))
    loop = asyncio.get_running_loop()
    prepare_task = asyncio.create_task(_capture(asyncio.sleep(0)))
    await prepare_task
    result = loop.create_future()
    wave = _OpenWave(loop, asyncio.current_task(), 1, prepare_task, result)

    with pytest.raises(SystemExit, match="shutdown") as raised:
        await lifecycle._rollback_open(wave, _OpenOutcome.FAILED, LookupError("open failed"))

    assert raised.value is process_exit
    outcome = result.result()
    assert outcome.outcome is _OpenOutcome.FAILED
    assert outcome.error is process_exit


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
async def test_drain_racing_opening_waits_for_commit_then_drains_generation() -> None:
    events: list[str] = []
    open_gate = asyncio.Event()
    supervisor = _Supervisor(events=events)
    lifecycle = _lifecycle(
        supervisor,
        _Transport("web", events, open_gate=open_gate),
    )

    opening = asyncio.create_task(lifecycle.open())
    await _wait_for_event(events, "open:web:1")
    draining = asyncio.create_task(lifecycle.drain(timeout=0.5))
    await asyncio.sleep(0)

    assert lifecycle._state is _ResourceState.OPENING
    assert not lifecycle.is_open()
    assert not draining.done()
    assert "drain:1" not in events

    open_gate.set()
    await asyncio.gather(opening, draining)

    assert lifecycle._state is _ResourceState.OPEN
    assert lifecycle.is_open()
    assert events.index("accept:1") < events.index("drain:1")
    assert events.index("drain:1") < events.index("idle:1:0.5")
    await lifecycle.close(drain=False)


@pytest.mark.asyncio
async def test_drain_racing_manual_drain_waits_on_same_open_generation() -> None:
    events: list[str] = []
    idle_gate = asyncio.Event()
    supervisor = _Supervisor(events=events, wait_gate=idle_gate)
    lifecycle = _lifecycle(supervisor, _Transport("web", events))
    await lifecycle.open()

    first = asyncio.create_task(lifecycle.drain(timeout=0.5))
    await _wait_for_event(events, "idle:1:0.5")
    second = asyncio.create_task(lifecycle.drain(timeout=1.0))
    await _wait_for_event(events, "idle:1:1.0")

    assert lifecycle._state is _ResourceState.OPEN
    assert lifecycle.is_open()
    assert not first.done()
    assert not second.done()

    idle_gate.set()
    await asyncio.gather(first, second)

    assert lifecycle._state is _ResourceState.OPEN
    assert lifecycle.is_open()
    assert events.count("drain:1") == 2
    await lifecycle.close(drain=False)


@pytest.mark.asyncio
async def test_drain_racing_closing_joins_close_wave_without_redraining() -> None:
    events: list[str] = []
    prepare_gate = asyncio.Event()
    supervisor = _Supervisor(events=events)
    lifecycle = _lifecycle(
        supervisor,
        _Transport("web", events, prepare_gate=prepare_gate),
    )
    await lifecycle.open()

    closing = asyncio.create_task(lifecycle.close(drain=False))
    await _wait_for_event(events, "prepare:web")
    draining = asyncio.create_task(lifecycle.drain(timeout=0.5))
    await asyncio.sleep(0)

    assert lifecycle._state is _ResourceState.CLOSING
    assert lifecycle.is_open()
    assert not draining.done()
    assert "drain:1" not in events
    assert not any(event.startswith("idle:1:") for event in events)

    prepare_gate.set()
    await asyncio.gather(closing, draining)

    assert lifecycle._state is _ResourceState.CLOSED
    assert not lifecycle.is_open()
    assert events.count("prepare:web") == 1
    assert events.count("close:web") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("close_finishes_before_drain_resumes", [False, True])
async def test_drain_that_snapshotted_open_joins_or_observes_racing_close(
    close_finishes_before_drain_resumes: bool,
) -> None:
    events: list[str] = []
    prepare_gate = asyncio.Event()
    transport = _Transport("web", events, prepare_gate=prepare_gate)
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=None,
    )
    lifecycle = ClientLifecycle(
        supervisor=supervisor,
        transports=(transport,),
        loop_participants=(supervisor,),
    )
    await lifecycle.open()

    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()
    real_stop_accepting = supervisor.stop_accepting

    async def delayed_stop_accepting(epoch: int) -> None:
        stop_entered.set()
        await release_stop.wait()
        await real_stop_accepting(epoch)

    supervisor.stop_accepting = delayed_stop_accepting  # type: ignore[method-assign]
    draining = asyncio.create_task(lifecycle.drain())
    await stop_entered.wait()
    closing = asyncio.create_task(lifecycle.close(drain=False))
    await _wait_for_event(events, "prepare:web")

    if close_finishes_before_drain_resumes:
        prepare_gate.set()
        await closing
        release_stop.set()
    else:
        release_stop.set()
        await asyncio.sleep(0)
        assert not draining.done()
        prepare_gate.set()

    await asyncio.gather(closing, draining)
    assert not lifecycle.is_open()
    assert events.count("prepare:web") == 1
    assert events.count("close:web") == 1


@pytest.mark.asyncio
async def test_resource_state_and_is_open_transitions_cover_every_phase() -> None:
    events: list[str] = []
    open_gate = asyncio.Event()
    prepare_gate = asyncio.Event()
    supervisor = _Supervisor(events=events)
    transport = _Transport(
        "web",
        events,
        open_gate=open_gate,
        prepare_gate=prepare_gate,
    )
    lifecycle = _lifecycle(supervisor, transport)

    assert lifecycle._state is _ResourceState.CLOSED
    assert not lifecycle.is_open()

    opening = asyncio.create_task(lifecycle.open())
    await _wait_for_event(events, "open:web:1")
    assert lifecycle._state is _ResourceState.OPENING
    assert not lifecycle.is_open()

    open_gate.set()
    await opening
    assert lifecycle._state is _ResourceState.OPEN
    assert lifecycle.is_open()

    closing = asyncio.create_task(lifecycle.close(drain=False))
    await _wait_for_event(events, "prepare:web")
    assert lifecycle._state is _ResourceState.CLOSING
    assert lifecycle.is_open()

    prepare_gate.set()
    await closing
    assert lifecycle._state is _ResourceState.CLOSED
    assert not lifecycle.is_open()


@pytest.mark.asyncio
async def test_second_close_waiter_cancellation_aborts_hung_first_graceful_prephase() -> None:
    events: list[str] = []
    idle_gate = asyncio.Event()
    supervisor = _Supervisor(events=events, wait_gate=idle_gate)
    lifecycle = _lifecycle(supervisor, _Transport("web", events))
    await lifecycle.open()

    first = asyncio.create_task(lifecycle.close(drain_timeout=30.0))
    await _wait_for_event(events, "idle:1:30.0")
    second = asyncio.create_task(lifecycle.close(drain=False))
    await asyncio.sleep(0)
    second.cancel("second waiter cancelled")

    with pytest.raises(asyncio.CancelledError) as raised:
        await second
    await first

    _assert_republished_cancel_message(raised.value, "second waiter cancelled")
    assert not idle_gate.is_set()
    assert lifecycle._state is _ResourceState.CLOSED
    assert not lifecycle.is_open()
    assert events.count("prepare:web") == 1
    assert events.count("close:web") == 1
    assert events.count("closed:1") == 1


@pytest.mark.asyncio
async def test_invalid_graceful_timeout_precedes_hooks_and_forced_close_ignores_it() -> None:
    events: list[str] = []
    lifecycle = _lifecycle(_Supervisor(events=events), _Transport("web", events))
    await lifecycle.open()

    with pytest.raises(ValueError, match="timeout must be"):
        await lifecycle.close(drain_timeout=-1)

    assert lifecycle._state is _ResourceState.OPEN
    assert lifecycle.is_open()
    assert "drain:1" not in events
    assert "hooks" not in events
    assert "prepare:web" not in events

    await lifecycle.close(drain=False, drain_timeout=-1)

    assert lifecycle._state is _ResourceState.CLOSED
    assert not lifecycle.is_open()
    assert "drain:1" not in events
    assert not any(event.startswith("idle:1:") for event in events)
    assert events.count("hooks") == 1


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
async def test_process_exit_precedes_graceful_timeout_and_teardown_failures() -> None:
    events: list[str] = []
    timeout = TimeoutError("idle timed out")
    process_exit = SystemExit("shutdown")
    lifecycle = _lifecycle(
        _Supervisor(events=events, wait_error=timeout),
        _Transport(
            "web",
            events,
            prepare_error=ValueError("prepare failed"),
            close_error=process_exit,
        ),
    )
    await lifecycle.open()

    with pytest.raises(SystemExit, match="shutdown") as raised:
        await lifecycle.close(drain_timeout=0.01)

    assert raised.value is process_exit
    assert lifecycle._state is _ResourceState.CLOSED
    assert not lifecycle.is_open()
    assert "prepare:web" in events
    assert "close:web" in events
    assert "closed:1" in events


@pytest.mark.asyncio
async def test_close_phase_process_exit_waits_for_siblings_and_marks_closed() -> None:
    events: list[str] = []
    process_exit = KeyboardInterrupt("shutdown")
    lifecycle = _lifecycle(
        _Supervisor(events=events),
        _Transport("first", events, prepare_error=process_exit),
        _Transport("second", events),
    )
    await lifecycle.open()

    with pytest.raises(KeyboardInterrupt, match="shutdown") as raised:
        await lifecycle.close(drain=False)

    assert raised.value is process_exit
    assert not lifecycle.is_open()
    assert "prepare:second" in events
    assert "close:first" in events
    assert "close:second" in events
    assert "closed:1" in events


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["stop_accepting", "pre_drain_hook", "wait_for_idle"])
@pytest.mark.parametrize("exit_type", [KeyboardInterrupt, SystemExit])
async def test_graceful_prephase_process_exit_finishes_every_close_phase(
    phase: str,
    exit_type: type[BaseException],
) -> None:
    events: list[str] = []
    process_exit = exit_type(f"{phase} exit")
    supervisor = _Supervisor(events=events)
    if phase == "stop_accepting":
        supervisor.stop_error = process_exit
    elif phase == "pre_drain_hook":
        supervisor.hook_errors.append(process_exit)
    else:
        supervisor.wait_error = process_exit
    lifecycle = _lifecycle(supervisor, _Transport("web", events))
    await lifecycle.open()

    with pytest.raises(exit_type, match=f"{phase} exit") as raised:
        await lifecycle.close()

    assert raised.value is process_exit
    assert not lifecycle.is_open()
    assert events.count("hooks") == 2
    assert any(event.startswith("idle:1:") for event in events)
    assert events.index("drain:1") < events.index("hooks")
    assert events.index("hooks") < next(
        index for index, event in enumerate(events) if event.startswith("idle:1:")
    )
    assert next(
        index for index, event in enumerate(events) if event.startswith("idle:1:")
    ) < events.index("closing:1")
    assert events.index("closing:1") < events.index("prepare:web")
    assert events.index("prepare:web") < events.index("hooks", events.index("hooks") + 1)
    assert events.index("hooks", events.index("hooks") + 1) < events.index("close:web")
    assert events.index("close:web") < events.index("closed:1")


@pytest.mark.asyncio
async def test_observed_retained_process_exit_is_not_forwarded_to_loop_handler() -> None:
    events: list[str] = []
    process_exit = SystemExit("observed exit")
    lifecycle = _lifecycle(
        _Supervisor(events=events),
        _Transport("web", events, prepare_error=process_exit),
    )
    await lifecycle.open()
    loop = asyncio.get_running_loop()
    contexts: list[dict[str, object]] = []
    prior_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        with pytest.raises(SystemExit, match="observed exit") as raised:
            await lifecycle.close(drain=False)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(prior_handler)

    assert raised.value is process_exit
    assert contexts == []


@pytest.mark.asyncio
async def test_detached_retained_process_exit_is_forwarded_to_loop_handler_once() -> None:
    events: list[str] = []
    prepare_gate = asyncio.Event()
    process_exit = SystemExit("detached exit")
    lifecycle = _lifecycle(
        _Supervisor(events=events),
        _Transport(
            "web",
            events,
            prepare_gate=prepare_gate,
            prepare_error=process_exit,
        ),
    )
    await lifecycle.open()
    loop = asyncio.get_running_loop()
    contexts: list[dict[str, object]] = []
    prior_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        closing = asyncio.create_task(lifecycle.close(drain=False))
        while "prepare:web" not in events:
            await asyncio.sleep(0)
        closing.cancel("first cancellation")
        await asyncio.sleep(0)
        closing.cancel("second cancellation")
        with pytest.raises(asyncio.CancelledError) as raised:
            await closing
        _assert_republished_cancel_message(raised.value, "first cancellation")

        prepare_gate.set()
        for _ in range(100):
            if not lifecycle.is_open() and contexts:
                break
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(prior_handler)

    assert not lifecycle.is_open()
    assert len(contexts) == 1
    assert contexts[0]["exception"] is process_exit


@pytest.mark.asyncio
async def test_observer_suppresses_handler_after_another_waiter_detaches() -> None:
    events: list[str] = []
    prepare_gate = asyncio.Event()
    process_exit = SystemExit("shared exit")
    lifecycle = _lifecycle(
        _Supervisor(events=events),
        _Transport(
            "web",
            events,
            prepare_gate=prepare_gate,
            prepare_error=process_exit,
        ),
    )
    await lifecycle.open()
    loop = asyncio.get_running_loop()
    contexts: list[dict[str, object]] = []
    prior_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        detached = asyncio.create_task(lifecycle.close(drain=False))
        while "prepare:web" not in events:
            await asyncio.sleep(0)
        detached.cancel("first cancellation")
        await asyncio.sleep(0)
        detached.cancel("second cancellation")
        with pytest.raises(asyncio.CancelledError):
            await detached

        loop.call_soon(prepare_gate.set)
        with pytest.raises(SystemExit, match="shared exit") as raised:
            await lifecycle.close(drain=False)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(prior_handler)

    assert raised.value is process_exit
    assert contexts == []


@pytest.mark.asyncio
async def test_eager_task_factory_never_runs_wave_code_under_state_lock() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    transport = _Transport("web", events)
    lifecycle = _lifecycle(supervisor, transport)

    original_open = transport.open
    original_prepare = transport.prepare_close

    async def checked_open(loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        assert not lifecycle._state_lock.locked()
        await original_open(loop, epoch)

    async def checked_prepare() -> None:
        assert not lifecycle._state_lock.locked()
        await original_prepare()

    transport.open = checked_open  # type: ignore[method-assign]
    transport.prepare_close = checked_prepare  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    eager_factory = getattr(asyncio, "eager_task_factory", None)
    if eager_factory is None:
        pytest.skip("asyncio eager task factory requires Python 3.12+")
    prior_factory = loop.get_task_factory()
    loop.set_task_factory(eager_factory)
    try:
        await lifecycle.open()
        await lifecycle.close(drain=False)
    finally:
        loop.set_task_factory(prior_factory)

    assert not lifecycle.is_open()


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
async def test_close_recancellation_leaves_retained_teardown_running() -> None:
    events: list[str] = []
    prepare_gate = asyncio.Event()
    supervisor = _Supervisor(events=events, wait_gate=asyncio.Event())
    lifecycle = _lifecycle(
        supervisor,
        _Transport("web", events, prepare_gate=prepare_gate),
    )
    await lifecycle.open()

    closing = asyncio.create_task(lifecycle.close())
    while not any(event.startswith("idle:") for event in events):
        await asyncio.sleep(0)
    closing.cancel()
    while "prepare:web" not in events:
        await asyncio.sleep(0)
    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert lifecycle.is_open()
    prepare_gate.set()
    for _ in range(100):
        if not lifecycle.is_open():
            break
        await asyncio.sleep(0)
    assert not lifecycle.is_open()
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
    assert lifecycle._epoch == 2


@pytest.mark.parametrize("resource_state", ["open", "opening", "closing"])
@pytest.mark.parametrize("action", ["open", "drain", "close"])
def test_foreign_loop_public_lifecycle_calls_are_rejected(
    resource_state: str,
    action: str,
) -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    transport = _Transport("web", events)
    lifecycle = _lifecycle(supervisor, transport)
    owner_loop = asyncio.new_event_loop()
    foreign_loop = asyncio.new_event_loop()
    gate: asyncio.Event | None = None
    active_wave: asyncio.Task[None] | None = None

    async def invoke() -> None:
        if action == "open":
            await lifecycle.open()
        elif action == "drain":
            await lifecycle.drain()
        else:
            await lifecycle.close(drain=False)

    try:
        if resource_state == "open":
            owner_loop.run_until_complete(lifecycle.open())
            assert lifecycle._state is _ResourceState.OPEN
        elif resource_state == "opening":
            gate = asyncio.Event()
            transport.open_gate = gate
            active_wave = owner_loop.create_task(lifecycle.open())
            owner_loop.run_until_complete(_wait_for_event(events, "open:web:1"))
            assert lifecycle._state is _ResourceState.OPENING
        else:
            owner_loop.run_until_complete(lifecycle.open())
            gate = asyncio.Event()
            transport.prepare_gate = gate
            active_wave = owner_loop.create_task(lifecycle.close(drain=False))
            owner_loop.run_until_complete(_wait_for_event(events, "prepare:web"))
            assert lifecycle._state is _ResourceState.CLOSING

        with pytest.raises(RuntimeError, match="different event loop"):
            foreign_loop.run_until_complete(invoke())
    finally:
        if gate is not None:
            gate.set()
        if active_wave is not None:
            owner_loop.run_until_complete(active_wave)
        if lifecycle.is_open():
            owner_loop.run_until_complete(lifecycle.close(drain=False))
        foreign_loop.close()
        owner_loop.close()


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


@pytest.mark.parametrize("waiter_action", ["close", "drain"])
def test_same_new_loop_waiter_can_join_cross_loop_reopen(waiter_action: str) -> None:
    events: list[str] = []
    lifecycle = _lifecycle(_Supervisor(events=events), _Transport("web", events))

    async def first_generation() -> None:
        await lifecycle.open()
        await lifecycle.close(drain=False)

    async def second_generation() -> None:
        opening = asyncio.create_task(lifecycle.open())
        if waiter_action == "close":
            waiter = asyncio.create_task(lifecycle.close(drain=False))
        else:
            waiter = asyncio.create_task(lifecycle.drain())
        await asyncio.gather(opening, waiter)
        if waiter_action == "drain":
            assert lifecycle.is_open()
            await lifecycle.close(drain=False)

    asyncio.run(first_generation())
    asyncio.run(second_generation())

    assert not lifecycle.is_open()


# ===========================================================================
# drain(): admission fencing without closing resources
# ===========================================================================


@pytest.mark.asyncio
async def test_draining_a_closed_lifecycle_is_a_no_op() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    lifecycle = _lifecycle(supervisor, _Transport("t", events))

    await lifecycle.drain()

    assert events == []


@pytest.mark.asyncio
async def test_drain_rejects_a_negative_timeout() -> None:
    events: list[str] = []
    lifecycle = _lifecycle(_Supervisor(events=events), _Transport("t", events))

    with pytest.raises(ValueError, match="timeout must be >= 0 or None"):
        await lifecycle.drain(timeout=-1.0)

    assert events == []


@pytest.mark.asyncio
async def test_drain_stops_admission_and_waits_for_idle() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    lifecycle = _lifecycle(supervisor, _Transport("t", events))
    await lifecycle.open()
    events.clear()

    await lifecycle.drain(timeout=5.0)

    assert events == ["drain:1", "idle:1:5.0"]
    # Resources stay open: drain fences admission only, it does not close.
    assert lifecycle._state is _ResourceState.OPEN
    assert not any(event.startswith("close:") for event in events)


@pytest.mark.asyncio
async def test_a_drain_racing_a_close_joins_that_close_instead_of_failing() -> None:
    """``stop_accepting`` raises once ``close()`` has claimed the epoch."""
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    close_gate = asyncio.Event()
    transport = _Transport("t", events, close_gate=close_gate)
    lifecycle = _lifecycle(supervisor, transport)
    await lifecycle.open()

    close_task = asyncio.create_task(lifecycle.close())
    await _wait_for_event(events, "close:t")
    # The epoch now belongs to the close wave, so a drain's ``stop_accepting``
    # is the call that must be suppressed — not a real fault.
    supervisor.stop_errors.append(RuntimeError("generation is not current"))

    drain_task = asyncio.create_task(lifecycle.drain())
    close_gate.set()
    await asyncio.gather(drain_task, close_task)

    assert lifecycle._state is _ResourceState.CLOSED


@pytest.mark.asyncio
async def test_an_unrelated_supervisor_failure_during_drain_propagates() -> None:
    """Only the close race is suppressed — a real fault must stay loud."""
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    supervisor.stop_errors.append(RuntimeError("supervisor is broken"))
    lifecycle = _lifecycle(supervisor, _Transport("t", events))
    await lifecycle.open()

    with pytest.raises(RuntimeError, match="supervisor is broken"):
        await lifecycle.drain()

    await lifecycle.close()


@pytest.mark.asyncio
async def test_a_drain_that_times_out_waiting_for_idle_propagates() -> None:
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    supervisor.wait_error = TimeoutError("still busy")
    lifecycle = _lifecycle(supervisor, _Transport("t", events))
    await lifecycle.open()

    with pytest.raises(TimeoutError):
        await lifecycle.drain(timeout=0.0)

    supervisor.wait_error = None
    await lifecycle.close()


# ===========================================================================
# open(): state guards
# ===========================================================================


@pytest.mark.asyncio
async def test_reopening_an_open_lifecycle_is_a_no_op() -> None:
    events: list[str] = []
    lifecycle = _lifecycle(_Supervisor(events=events), _Transport("t", events))
    await lifecycle.open()
    events.clear()

    await lifecycle.open()

    assert events == []
    await lifecycle.close()


@pytest.mark.asyncio
async def test_opening_while_closing_is_refused() -> None:
    events: list[str] = []
    close_gate = asyncio.Event()
    transport = _Transport("t", events, close_gate=close_gate)
    lifecycle = _lifecycle(_Supervisor(events=events), transport)
    await lifecycle.open()

    close_task = asyncio.create_task(lifecycle.close())
    await _wait_for_event(events, "close:t")

    with pytest.raises(RuntimeError, match="closing; wait for close"):
        await lifecycle.open()

    close_gate.set()
    await close_task


@pytest.mark.asyncio
async def test_an_admission_rollback_failure_does_not_mask_the_open_failure() -> None:
    """A failed open rolls back; a rollback fault must not replace the cause."""
    events: list[str] = []
    supervisor = _Supervisor(events=events)
    supervisor.mark_error = RuntimeError("rollback refused")
    transport = _Transport("t", events, open_error=RuntimeError("transport open failed"))
    lifecycle = _lifecycle(supervisor, transport)

    with pytest.raises(RuntimeError, match="transport open failed"):
        await lifecycle.open()

    assert lifecycle._state is _ResourceState.CLOSED
