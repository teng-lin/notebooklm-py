"""Focused policy and cancellation tests for ``CallSupervisor``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._deadline import RuntimeDeadline
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._transport_drain import TransportDrainTracker, _TransportOperationToken
from notebooklm.types import RpcTelemetryEvent


class _SpyMetrics(ClientMetrics):
    def __init__(
        self,
        events: list[str],
        *,
        emit_error: BaseException | None = None,
        queue_error: BaseException | None = None,
        on_emit: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._events = events
        self._emit_error = emit_error
        self._queue_error = queue_error
        self._on_emit = on_emit

    async def emit_rpc_event(self, event: RpcTelemetryEvent) -> None:
        self._events.append(f"event:{event.status}")
        if self._on_emit is not None:
            self._on_emit()
        if self._emit_error is not None:
            raise self._emit_error

    def record_rpc_queue_wait(self, wait_seconds: float) -> None:
        self._events.append("queue")
        if self._queue_error is not None:
            raise self._queue_error
        super().record_rpc_queue_wait(wait_seconds)


class _SpyDrain(TransportDrainTracker):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    async def begin_transport_post(self, log_label: str) -> _TransportOperationToken:
        self._events.append("drain:begin")
        return await super().begin_transport_post(log_label)

    async def finish_transport_post(self, token: _TransportOperationToken) -> None:
        await super().finish_transport_post(token)
        self._events.append("drain:finish")


class _BlockingFinishDrain(TransportDrainTracker):
    def __init__(self) -> None:
        super().__init__()
        self.finish_started = asyncio.Event()
        self.finish_release = asyncio.Event()

    async def finish_transport_post(self, token: _TransportOperationToken) -> None:
        self.finish_started.set()
        await self.finish_release.wait()
        await super().finish_transport_post(token)


def _supervisor(
    *,
    metrics: ClientMetrics | None = None,
    drain: TransportDrainTracker | None = None,
    max_concurrent_rpcs: int | None = 1,
) -> CallSupervisor:
    supervisor = CallSupervisor(
        metrics=metrics if metrics is not None else ClientMetrics(),
        drain_tracker=drain if drain is not None else TransportDrainTracker(),
        max_concurrent_rpcs=max_concurrent_rpcs,
    )
    supervisor.set_bound_loop(asyncio.get_running_loop())
    supervisor.reset_after_open()
    return supervisor


@pytest.mark.asyncio
async def test_call_scope_preserves_drain_metrics_semaphore_settlement_order() -> None:
    events: list[str] = []
    drain = _SpyDrain(events)
    supervisor: CallSupervisor

    def _assert_released() -> None:
        assert supervisor._rpc_semaphore is not None
        assert supervisor._rpc_semaphore._value == 1

    metrics = _SpyMetrics(events, on_emit=_assert_released)
    supervisor = _supervisor(metrics=metrics, drain=drain)
    assert supervisor._rpc_semaphore is None
    supervisor.record_started("LIST_NOTEBOOKS")

    async with supervisor.call_scope("list", "LIST_NOTEBOOKS", None) as lease:
        events.append("body")
        assert lease.epoch == 1
        assert lease.deadline is None

    assert events == [
        "drain:begin",
        "body",
        "event:success",
        "drain:finish",
        "queue",
    ]
    snapshot = metrics.snapshot()
    assert snapshot.rpc_calls_started == 1
    assert snapshot.rpc_calls_succeeded == 1
    assert snapshot.rpc_calls_failed == 0


@pytest.mark.asyncio
async def test_run_is_lazy_and_deadline_bounds_semaphore_queue() -> None:
    metrics = ClientMetrics()
    supervisor = _supervisor(metrics=metrics)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def _hold(_lease: object) -> None:
        first_entered.set()
        await release_first.wait()

    first = asyncio.create_task(supervisor.run("first", "FIRST", None, _hold))
    await first_entered.wait()
    invoked = False

    async def _must_not_start(_lease: object) -> None:
        nonlocal invoked
        invoked = True

    deadline = RuntimeDeadline.start(0.001)
    with pytest.raises(TimeoutError):
        await supervisor.run("queued", "QUEUED", deadline, _must_not_start)

    assert invoked is False
    assert supervisor.drain_tracker._in_flight_posts == 1
    release_first.set()
    await first
    snapshot = metrics.snapshot()
    assert snapshot.rpc_calls_failed == 1
    assert snapshot.rpc_calls_succeeded == 1


@pytest.mark.asyncio
async def test_cancellation_is_uncounted_but_queued_token_settles() -> None:
    metrics = ClientMetrics()
    supervisor = _supervisor(metrics=metrics)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def _hold(_lease: object) -> None:
        first_entered.set()
        await release_first.wait()

    first = asyncio.create_task(supervisor.run("first", "FIRST", None, _hold))
    await first_entered.wait()
    queued = asyncio.create_task(
        supervisor.run("queued", "QUEUED", None, lambda _lease: asyncio.sleep(0))
    )
    await asyncio.sleep(0)
    assert supervisor.drain_tracker._in_flight_posts == 2
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert supervisor.drain_tracker._in_flight_posts == 1
    release_first.set()
    await first

    snapshot = metrics.snapshot()
    assert snapshot.rpc_calls_failed == 0
    assert snapshot.rpc_calls_succeeded == 1
    assert snapshot.rpc_queue_wait_seconds_total >= 0.0


@pytest.mark.asyncio
async def test_recancellation_cannot_orphan_retained_settlement() -> None:
    drain = _BlockingFinishDrain()
    supervisor = _supervisor(drain=drain, max_concurrent_rpcs=None)
    body_started = asyncio.Event()

    async def _body() -> None:
        async with supervisor.operation_scope("workflow"):
            body_started.set()
            await asyncio.Future()

    caller = asyncio.create_task(_body())
    await body_started.wait()
    caller.cancel()
    await drain.finish_started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert drain._in_flight_posts == 1
    assert supervisor._settlement_tasks
    drain.finish_release.set()
    await asyncio.gather(*tuple(supervisor._settlement_tasks))
    assert drain._in_flight_posts == 0
    assert not supervisor._settlement_tasks


@pytest.mark.asyncio
async def test_recorder_failure_never_skips_drain_and_body_error_wins() -> None:
    events: list[str] = []
    drain = _SpyDrain(events)
    metrics = _SpyMetrics(
        events,
        emit_error=RuntimeError("emit failed"),
        queue_error=RuntimeError("queue failed"),
    )
    supervisor = _supervisor(metrics=metrics, drain=drain)

    with pytest.raises(ValueError, match="body failed"):
        async with supervisor.call_scope("call", "METHOD", None):
            raise ValueError("body failed")

    assert drain._in_flight_posts == 0
    assert events[-2:] == ["drain:finish", "queue"]


@pytest.mark.asyncio
async def test_spawn_child_requires_parent_and_invokes_factory_only_after_admission() -> None:
    supervisor = _supervisor(max_concurrent_rpcs=None)
    invoked = False

    async def _child() -> int:
        nonlocal invoked
        invoked = True
        return 42

    with pytest.raises(RuntimeError, match="same-generation parent token"):
        await supervisor.spawn_child("orphan", _child)
    assert invoked is False

    async with supervisor.operation_scope("parent") as lease:
        child = await supervisor.spawn_child("child", _child)
        assert lease.epoch == 1
        assert await child == 42
    assert supervisor.drain_tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_retired_parent_cannot_enter_reopened_generation() -> None:
    supervisor = _supervisor(max_concurrent_rpcs=None)
    async with supervisor.operation_scope("old") as old:
        supervisor.reset_after_open()
        assert old.epoch == 1
        with pytest.raises(RuntimeError, match="retired resource generation"):
            async with supervisor.operation_scope("old nested"):
                raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_lifecycle_transitions_gate_top_level_nested_and_child_work() -> None:
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=None,
    )
    supervisor.set_bound_loop(asyncio.get_running_loop())
    supervisor.prepare_generation(7)
    with pytest.raises(RuntimeError, match="state=closed"):
        async with supervisor.operation_scope("before commit"):
            raise AssertionError("unreachable")

    supervisor.start_accepting(7)
    async with supervisor.operation_scope("accepted"):
        await supervisor.stop_accepting(7)
        async with supervisor.operation_scope("nested"):
            child = await supervisor.spawn_child("nested child", lambda: asyncio.sleep(0))
            await child
        outsider = asyncio.create_task(_enter_operation(supervisor, "outsider"))
        with pytest.raises(RuntimeError, match="state=draining"):
            await outsider
        await supervisor.begin_closing(7)
        with pytest.raises(RuntimeError, match="state=closing"):
            async with supervisor.operation_scope("closing nested"):
                raise AssertionError("unreachable")
        with pytest.raises(RuntimeError, match="not accepting child work"):
            await supervisor.spawn_child("closing child", lambda: asyncio.sleep(0))
    await supervisor.wait_for_idle(7, 0.1)
    supervisor.mark_closed(7)


async def _enter_operation(supervisor: CallSupervisor, label: str) -> None:
    async with supervisor.operation_scope(label):
        return


@pytest.mark.asyncio
async def test_forced_close_late_settlement_cannot_mutate_new_generation() -> None:
    supervisor = _supervisor(max_concurrent_rpcs=None)
    old_entered = asyncio.Event()
    release_old = asyncio.Event()

    async def _old_work() -> None:
        async with supervisor.operation_scope("old"):
            old_entered.set()
            await release_old.wait()

    old_task = asyncio.create_task(_old_work())
    await old_entered.wait()
    old_generation = supervisor._current
    assert old_generation is not None
    await supervisor.begin_closing(1)
    supervisor.mark_closed(1)
    supervisor.prepare_generation(2)
    supervisor.drain_tracker.reset_after_open()
    supervisor.start_accepting(2)

    async with supervisor.operation_scope("new"):
        new_generation = supervisor._current
        assert new_generation is not None
        assert new_generation.epoch == 2
        assert new_generation.in_flight == 1
        release_old.set()
        await old_task
        assert old_generation.in_flight == 0
        assert new_generation.in_flight == 1
    assert new_generation.in_flight == 0


@pytest.mark.asyncio
async def test_spawn_child_is_safe_with_eager_task_factory() -> None:
    eager_factory = getattr(asyncio, "eager_task_factory", None)
    if eager_factory is None:
        pytest.skip("asyncio eager task factory requires Python 3.12+")
    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(eager_factory)
    try:
        supervisor = _supervisor(max_concurrent_rpcs=None)
        observed_parent_depth: list[int] = []

        async def _child() -> None:
            current = asyncio.current_task()
            observed_parent_depth.append(supervisor._task_depth(current, 1))

        async with supervisor.operation_scope("parent"):
            child = await supervisor.spawn_child("eager-child", _child)
            await child
        assert observed_parent_depth == [1]
    finally:
        loop.set_task_factory(previous)
