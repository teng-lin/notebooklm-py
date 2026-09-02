"""Focused policy and cancellation tests for ``CallSupervisor``."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._deadline import RuntimeDeadline
from notebooklm._runtime.call_supervisor import AdmissionGeneration, CallSupervisor
from notebooklm._transport_drain import TransportDrainTracker, _TransportOperationToken
from notebooklm.types import RpcTelemetryEvent


def _assert_republished_cancel_message(error: asyncio.CancelledError, expected: str) -> None:
    """Pin first-cancel precedence without overclaiming Python 3.10 metadata.

    Python 3.10 republishes a cancellation that crosses multiple shield
    boundaries as a fresh ``CancelledError`` without the optional message.
    Python 3.11+ preserves it. Both retain the cancellation and discard later
    cancellation messages, which is the runtime invariant under test.
    """
    assert error.args == ((expected,) if sys.version_info >= (3, 11) else ())


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


class _BlockingBeginDrain(TransportDrainTracker):
    def __init__(self) -> None:
        super().__init__()
        self.begin_started = asyncio.Event()

    async def begin_transport_post(self, log_label: str) -> _TransportOperationToken:
        del log_label
        self.begin_started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


class _BlockingChildBeginDrain(TransportDrainTracker):
    def __init__(self) -> None:
        super().__init__()
        self.child_begin_started = asyncio.Event()

    async def begin_transport_task(
        self,
        task: asyncio.Task[object],
        log_label: str,
    ) -> _TransportOperationToken:
        del task, log_label
        self.child_begin_started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


class _CancelAfterChildAdmissionDrain(TransportDrainTracker):
    """Cancel the spawning parent immediately after both child tokens exist."""

    async def begin_transport_task(
        self,
        task: asyncio.Task[object],
        log_label: str,
    ) -> _TransportOperationToken:
        # Let the gated wrapper reach ``await gate`` first, then make the
        # cancellation pending before spawn_child publishes the Task.
        await asyncio.sleep(0)
        token = await super().begin_transport_task(task, log_label)
        parent = asyncio.current_task()
        assert parent is not None
        parent.cancel("after-child-admission")
        return token


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
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    return supervisor


def _active_drain(supervisor: CallSupervisor) -> TransportDrainTracker:
    generation = supervisor._current
    assert generation is not None
    return generation.drain


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
    assert _active_drain(supervisor)._in_flight_posts == 1
    release_first.set()
    await first
    snapshot = metrics.snapshot()
    assert snapshot.rpc_calls_failed == 1
    assert snapshot.rpc_calls_succeeded == 1


@pytest.mark.asyncio
async def test_web_queue_is_unbounded_while_transport_deadline_bounds_queue() -> None:
    supervisor = _supervisor()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    web_invoked = False
    bounded_invoked = False

    async def _hold(_lease: object) -> None:
        first_entered.set()
        await release_first.wait()

    async def _web(_lease: object) -> None:
        nonlocal web_invoked
        web_invoked = True

    async def _bounded(_lease: object) -> None:
        nonlocal bounded_invoked
        bounded_invoked = True

    first = asyncio.create_task(supervisor.run("first", "FIRST", None, _hold))
    await first_entered.wait()
    web = asyncio.create_task(supervisor.run("web", "WEB", None, _web))
    while _active_drain(supervisor)._in_flight_posts < 2:
        await asyncio.sleep(0)
    assert web.done() is False
    assert web_invoked is False

    deadline = RuntimeDeadline.start(0.001)
    with pytest.raises(TimeoutError):
        await supervisor.run("bounded", "BOUNDED", deadline, _bounded)
    assert bounded_invoked is False
    assert web.done() is False

    release_first.set()
    await asyncio.gather(first, web)
    assert web_invoked is True


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
    assert _active_drain(supervisor)._in_flight_posts == 2
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert _active_drain(supervisor)._in_flight_posts == 1
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
async def test_recancellation_during_partial_admission_cannot_orphan_generation() -> None:
    drain = _BlockingBeginDrain()
    supervisor = _supervisor(drain=drain, max_concurrent_rpcs=None)

    async def _enter() -> None:
        async with supervisor.operation_scope("partial"):
            raise AssertionError("unreachable")

    caller = asyncio.create_task(_enter())
    await drain.begin_started.wait()
    generation = supervisor._current
    assert generation is not None
    condition_held = asyncio.Event()
    release_condition = asyncio.Event()

    async def _hold_condition() -> None:
        async with generation.condition:
            condition_held.set()
            await release_condition.wait()

    holder = asyncio.create_task(_hold_condition())
    await condition_held.wait()
    caller.cancel("first")
    while not supervisor._settlement_tasks:
        await asyncio.sleep(0)
    caller.cancel("second")

    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    _assert_republished_cancel_message(raised.value, "first")
    assert generation.in_flight == 1

    release_condition.set()
    await holder
    await asyncio.gather(*tuple(supervisor._settlement_tasks))
    await asyncio.sleep(0)
    assert generation.in_flight == 0
    assert not supervisor._settlement_tasks


@pytest.mark.asyncio
async def test_recancellation_during_child_admission_retains_both_settlements() -> None:
    drain = _BlockingChildBeginDrain()
    supervisor = _supervisor(drain=drain, max_concurrent_rpcs=None)
    factory_invoked = False

    async def _child() -> None:
        nonlocal factory_invoked
        factory_invoked = True

    async def _parent() -> None:
        async with supervisor.operation_scope("parent"):
            await supervisor.spawn_child("partial-child", _child)

    caller = asyncio.create_task(_parent())
    await drain.child_begin_started.wait()
    generation = supervisor._current
    assert generation is not None
    condition_held = asyncio.Event()
    release_condition = asyncio.Event()

    async def _hold_condition() -> None:
        async with generation.condition:
            condition_held.set()
            await release_condition.wait()

    holder = asyncio.create_task(_hold_condition())
    await condition_held.wait()
    caller.cancel("first")
    while len(supervisor._settlement_tasks) < 1:
        await asyncio.sleep(0)
    caller.cancel("second")
    while len(supervisor._settlement_tasks) < 2:
        await asyncio.sleep(0)
    caller.cancel("third")

    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    _assert_republished_cancel_message(raised.value, "first")
    assert factory_invoked is False
    assert generation.in_flight == 2

    release_condition.set()
    await holder
    await asyncio.gather(*tuple(supervisor._settlement_tasks))
    await asyncio.sleep(0)
    assert generation.in_flight == 0
    assert drain._in_flight_posts == 0


@pytest.mark.asyncio
async def test_parent_cancel_after_child_admission_settles_each_token_once() -> None:
    drain = _CancelAfterChildAdmissionDrain()
    supervisor = _supervisor(drain=drain, max_concurrent_rpcs=None)
    factory_invoked = False

    async def _child() -> None:
        nonlocal factory_invoked
        factory_invoked = True

    async def _parent() -> None:
        async with supervisor.operation_scope("parent"):
            await supervisor.spawn_child("gated-child", _child)

    caller = asyncio.create_task(_parent())
    with pytest.raises(asyncio.CancelledError) as raised:
        await caller

    _assert_republished_cancel_message(raised.value, "after-child-admission")
    assert factory_invoked is True
    await asyncio.gather(*tuple(supervisor._settlement_tasks))
    await asyncio.sleep(0)
    generation = supervisor._current
    assert generation is not None
    assert generation.in_flight == 0
    assert drain._in_flight_posts == 0


@pytest.mark.asyncio
async def test_recancellation_after_normal_body_preserves_first_cancelled_error() -> None:
    drain = _BlockingFinishDrain()
    supervisor = _supervisor(drain=drain, max_concurrent_rpcs=None)

    async def _body() -> None:
        async with supervisor.operation_scope("completed"):
            return

    caller = asyncio.create_task(_body())
    await drain.finish_started.wait()
    caller.cancel("first")
    await asyncio.sleep(0)
    caller.cancel("second")

    with pytest.raises(asyncio.CancelledError) as raised:
        await caller
    _assert_republished_cancel_message(raised.value, "first")

    drain.finish_release.set()
    await asyncio.gather(*tuple(supervisor._settlement_tasks))


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
async def test_terminal_event_failure_on_success_wins_after_settlement() -> None:
    events: list[str] = []
    terminal_error = RuntimeError("terminal event failed")
    metrics = _SpyMetrics(
        events,
        emit_error=terminal_error,
        queue_error=RuntimeError("queue recorder failed"),
    )
    supervisor = _supervisor(metrics=metrics, drain=_SpyDrain(events))

    with pytest.raises(RuntimeError, match="terminal event failed") as raised:
        async with supervisor.call_scope("call", "METHOD", None):
            events.append("body")

    assert raised.value is terminal_error
    assert events == ["drain:begin", "body", "event:success", "drain:finish", "queue"]
    generation = supervisor._current
    assert generation is not None
    assert generation.in_flight == 0


@pytest.mark.asyncio
async def test_queue_recorder_failure_on_success_is_observed_after_settlement() -> None:
    events: list[str] = []
    queue_error = RuntimeError("queue recorder failed")
    metrics = _SpyMetrics(events, queue_error=queue_error)
    supervisor = _supervisor(metrics=metrics, drain=_SpyDrain(events))

    with pytest.raises(RuntimeError, match="queue recorder failed") as raised:
        async with supervisor.call_scope("call", "METHOD", None):
            events.append("body")

    assert raised.value is queue_error
    assert events == ["drain:begin", "body", "event:success", "drain:finish", "queue"]
    generation = supervisor._current
    assert generation is not None
    assert generation.in_flight == 0


@pytest.mark.asyncio
async def test_settlement_process_exit_is_captured_then_observed_by_caller() -> None:
    events: list[str] = []
    process_exit = SystemExit("shutdown")
    metrics = _SpyMetrics(events, queue_error=process_exit)
    supervisor = _supervisor(metrics=metrics, drain=_SpyDrain(events))

    with pytest.raises(SystemExit, match="shutdown") as raised:
        async with supervisor.call_scope("call", "METHOD", None):
            pass

    assert raised.value is process_exit
    generation = supervisor._current
    assert generation is not None
    assert generation.in_flight == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recorder_error",
    [RuntimeError("queue failed"), KeyboardInterrupt("interrupt"), SystemExit("shutdown")],
    ids=["ordinary", "keyboard-interrupt", "system-exit"],
)
async def test_detached_recorder_failure_reaches_loop_handler_once(
    recorder_error: BaseException,
) -> None:
    events: list[str] = []
    drain = _BlockingFinishDrain()
    metrics = _SpyMetrics(events, queue_error=recorder_error)
    supervisor = _supervisor(metrics=metrics, drain=drain, max_concurrent_rpcs=None)
    body_started = asyncio.Event()
    loop = asyncio.get_running_loop()
    handled: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: handled.append(context))

    async def _invoke(_lease: object) -> None:
        body_started.set()
        await asyncio.Future()

    try:
        caller = asyncio.create_task(supervisor.run("call", None, None, _invoke))
        await body_started.wait()
        caller.cancel("first")
        await drain.finish_started.wait()
        caller.cancel("second")
        with pytest.raises(asyncio.CancelledError) as raised:
            await caller
        _assert_republished_cancel_message(raised.value, "first")

        retained = tuple(supervisor._settlement_tasks)
        assert len(retained) == 1
        drain.finish_release.set()
        await asyncio.gather(*retained)
        await asyncio.sleep(0)

        assert len(handled) == 1
        assert handled[0]["exception"] is recorder_error
        assert handled[0]["task"] is retained[0]
        assert not supervisor._settlement_tasks
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_method_none_applies_admission_and_queue_without_rpc_accounting() -> None:
    events: list[str] = []
    metrics = _SpyMetrics(events)
    supervisor = _supervisor(metrics=metrics, drain=_SpyDrain(events))

    supervisor.record_started(None)
    async with supervisor.call_scope("chat", None, None):
        events.append("body")

    assert events == ["drain:begin", "body", "drain:finish", "queue"]
    snapshot = metrics.snapshot()
    assert snapshot.rpc_calls_started == 0
    assert snapshot.rpc_calls_succeeded == 0
    assert snapshot.rpc_calls_failed == 0
    assert snapshot.rpc_queue_wait_seconds_total >= 0.0


@pytest.mark.asyncio
async def test_record_started_preserves_preopen_error_and_zero_metrics() -> None:
    metrics = ClientMetrics()
    supervisor = CallSupervisor(
        metrics=metrics,
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=None,
    )

    with pytest.raises(RuntimeError) as raised:
        supervisor.record_started("LIST_NOTEBOOKS")

    assert str(raised.value) == "Client not initialized. Use 'async with' context."
    assert metrics.snapshot().rpc_calls_started == 0


@pytest.mark.asyncio
async def test_record_started_keeps_phase_a_counting_before_drain_rejection() -> None:
    metrics = ClientMetrics()
    supervisor = _supervisor(metrics=metrics, max_concurrent_rpcs=None)

    async with supervisor.operation_scope("workflow"):
        await supervisor.stop_accepting(1)
        supervisor.record_started("NESTED")

        async def _outsider() -> None:
            supervisor.record_started("OUTSIDER")
            async with supervisor.call_scope("outsider", "OUTSIDER", None):
                raise AssertionError("unreachable")

        with pytest.raises(RuntimeError, match="state=draining"):
            await asyncio.create_task(_outsider())

    assert metrics.snapshot().rpc_calls_started == 2


@pytest.mark.asyncio
async def test_expected_epoch_rejects_before_legacy_admission_or_invocation() -> None:
    events: list[str] = []
    supervisor = _supervisor(drain=_SpyDrain(events), max_concurrent_rpcs=None)
    invoked = False

    async def _invoke(_lease: object) -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(RuntimeError, match=r"expected=2, active=1"):
        await supervisor.run("retired", "METHOD", None, _invoke, expected_epoch=2)

    assert invoked is False
    assert events == []
    generation = supervisor._current
    assert generation is not None
    assert generation.in_flight == 0


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
    assert _active_drain(supervisor)._in_flight_posts == 0


@pytest.mark.asyncio
async def test_immediately_cancelled_child_settles_both_admission_tokens() -> None:
    supervisor = _supervisor(max_concurrent_rpcs=None)

    async def _child() -> None:
        await asyncio.sleep(10)

    async with supervisor.operation_scope("parent"):
        child = await supervisor.spawn_child("cancel-before-first-step", _child)
        child.cancel()
        with pytest.raises(asyncio.CancelledError):
            await child
        generation = supervisor._current
        assert generation is not None
        assert generation.in_flight == 1  # parent only
        assert _active_drain(supervisor)._in_flight_posts == 1

    await supervisor.wait_for_idle(1, 0.1)
    assert _active_drain(supervisor)._in_flight_posts == 0


@pytest.mark.asyncio
async def test_retired_parent_cannot_enter_reopened_generation() -> None:
    supervisor = _supervisor(max_concurrent_rpcs=None)
    async with supervisor.operation_scope("old") as old:
        await supervisor.begin_closing(old.epoch)
        supervisor.mark_closed(old.epoch)
        supervisor.reset_after_open()
        supervisor.prepare_generation(2)
        supervisor.start_accepting(2)
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
    assert supervisor.is_closing() is False
    with pytest.raises(RuntimeError, match="state=closed"):
        async with supervisor.operation_scope("before commit"):
            raise AssertionError("unreachable")

    supervisor.start_accepting(7)
    assert supervisor.is_closing() is False
    async with supervisor.operation_scope("accepted"):
        await supervisor.stop_accepting(7)
        assert supervisor.is_closing() is False
        async with supervisor.operation_scope("nested"):
            child = await supervisor.spawn_child("nested child", lambda: asyncio.sleep(0))
            await child
        outsider = asyncio.create_task(_enter_operation(supervisor, "outsider"))
        with pytest.raises(RuntimeError, match="state=draining"):
            await outsider
        await supervisor.begin_closing(7)
        assert supervisor.is_closing() is True
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
    assert supervisor._retired == {1: old_generation}
    supervisor.prepare_generation(2)
    supervisor.start_accepting(2)
    new_generation = supervisor._current
    assert new_generation is not None
    assert new_generation.drain is not old_generation.drain
    assert new_generation.drain._in_flight_posts == 0
    assert old_generation.drain._in_flight_posts == 1

    async with supervisor.operation_scope("new"):
        assert new_generation.epoch == 2
        assert new_generation.in_flight == 1
        release_old.set()
        await old_task
        assert old_generation.in_flight == 0
        assert 1 not in supervisor._retired
        assert new_generation.in_flight == 1
    assert new_generation.in_flight == 0


def test_reopen_on_new_loop_allocates_generation_local_drain_state() -> None:
    """A retired epoch cannot carry its loop-bound tracker into a later open."""
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=None,
    )

    async def retire_with_an_outstanding_token() -> tuple[object, AdmissionGeneration]:
        loop = asyncio.get_running_loop()
        supervisor.set_bound_loop(loop)
        supervisor.reset_after_open()
        supervisor.prepare_generation(1)
        supervisor.start_accepting(1)
        token = await supervisor._admit("retained epoch-1 operation")
        generation = supervisor._current
        assert generation is not None
        assert generation.drain._bound_loop is loop
        await supervisor.begin_closing(1)
        supervisor.mark_closed(1)
        return token, generation

    retained_token, old_generation = asyncio.run(retire_with_an_outstanding_token())

    async def reopen_and_use_a_fresh_tracker() -> None:
        loop = asyncio.get_running_loop()
        supervisor.set_bound_loop(loop)
        supervisor.reset_after_open()
        supervisor.prepare_generation(2)
        supervisor.start_accepting(2)
        generation = supervisor._current
        assert generation is not None
        assert generation.drain is not old_generation.drain
        assert generation.drain._bound_loop is loop
        assert generation.drain._in_flight_posts == 0
        assert old_generation.drain._in_flight_posts == 1

        async with supervisor.operation_scope("epoch-2 operation"):
            assert generation.in_flight == 1
            assert generation.drain._in_flight_posts == 1

        await supervisor.begin_closing(2)
        supervisor.mark_closed(2)

    asyncio.run(reopen_and_use_a_fresh_tracker())
    assert retained_token is not None
    assert old_generation.epoch == 1
    assert old_generation.drain._bound_loop is old_generation.loop


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


# ---------------------------------------------------------------------------
# Admission-generation state machine
#
# These guards keep a retired resource generation from admitting work. They
# raise rather than returning a sentinel, so an invalid transition fails closed
# and loudly — and an unexercised guard is one nothing proves still fires.
# ---------------------------------------------------------------------------


def test_a_concurrency_limit_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_concurrent_rpcs must be >= 1"):
        CallSupervisor(
            metrics=ClientMetrics(),
            drain_tracker=TransportDrainTracker(),
            max_concurrent_rpcs=0,
        )


@pytest.mark.asyncio
async def test_bound_loop_is_reported_once_assigned() -> None:
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=1,
    )
    loop = asyncio.get_running_loop()

    assert supervisor.bound_loop is None
    supervisor.set_bound_loop(loop)
    assert supervisor.bound_loop is loop


@pytest.mark.asyncio
async def test_preparing_a_second_generation_while_one_is_current_is_refused() -> None:
    supervisor = _supervisor()

    with pytest.raises(RuntimeError, match="is still current"):
        supervisor.prepare_generation(2)


@pytest.mark.asyncio
async def test_a_generation_epoch_must_move_forward() -> None:
    """Reusing an epoch would let retired work rejoin admission."""
    supervisor = _supervisor()
    supervisor.mark_closed(1)

    with pytest.raises(RuntimeError, match="not newer than prior epochs"):
        supervisor.prepare_generation(1)


@pytest.mark.asyncio
async def test_a_generation_can_only_start_from_closed() -> None:
    supervisor = _supervisor()

    with pytest.raises(RuntimeError, match="cannot start from accepting"):
        supervisor.start_accepting(1)


@pytest.mark.asyncio
async def test_operations_naming_a_non_current_generation_are_refused() -> None:
    supervisor = _supervisor()

    with pytest.raises(RuntimeError, match="generation 99 is not current"):
        supervisor.start_accepting(99)


@pytest.mark.asyncio
async def test_closing_is_idempotent_but_cannot_reopen_a_closed_generation() -> None:
    supervisor = _supervisor()

    await supervisor.begin_closing(1)
    # Repeating the transition is a no-op rather than an error.
    await supervisor.begin_closing(1)

    supervisor.mark_closed(1)
    supervisor.prepare_generation(2)
    with pytest.raises(RuntimeError, match="cannot close from closed"):
        await supervisor.begin_closing(2)


@pytest.mark.asyncio
async def test_a_negative_idle_timeout_is_rejected() -> None:
    supervisor = _supervisor()

    with pytest.raises(ValueError, match="timeout must be >= 0 or None"):
        await supervisor.wait_for_idle(1, -1.0)


@pytest.mark.asyncio
async def test_waiting_for_idle_on_an_already_idle_generation_returns_at_once() -> None:
    supervisor = _supervisor()

    await supervisor.wait_for_idle(1, None)


@pytest.mark.asyncio
async def test_an_unknown_generation_is_named_in_the_error() -> None:
    supervisor = _supervisor()

    with pytest.raises(RuntimeError, match="generation 42 is unknown"):
        supervisor._find_generation(42)


@pytest.mark.asyncio
async def test_task_depth_is_zero_outside_a_known_generation() -> None:
    """Depth lookups happen on teardown paths where the generation may be gone."""
    supervisor = _supervisor()
    task = asyncio.current_task()

    assert supervisor._task_depth(None, 1) == 0
    assert supervisor._task_depth(task, 999) == 0
    assert supervisor._task_depth(task, 1) == 0


@pytest.mark.asyncio
async def test_no_other_generation_depth_without_a_task() -> None:
    supervisor = _supervisor()

    assert supervisor._has_other_generation_depth(None, 1) is False


@pytest.mark.asyncio
async def test_spawning_a_child_without_a_current_generation_is_refused() -> None:
    """The factory must not be invoked once the generation is gone.

    The message comes from ``assert_bound_loop``, which checks ``_current is
    None`` first; ``spawn_child``'s own "requires a current generation" guard
    is consequently unreachable and is left untested rather than contrived.
    """
    supervisor = _supervisor()
    supervisor.mark_closed(1)
    spawned: list[str] = []

    def _factory():  # noqa: ANN202
        spawned.append("factory")  # pragma: no cover - must never be reached
        raise AssertionError("child must not start")

    with pytest.raises(RuntimeError, match="Client not initialized"):
        await supervisor.spawn_child("label", _factory)

    assert spawned == []


# ---------------------------------------------------------------------------
# Drain hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_drain_hooks_without_any_registered_is_a_no_op() -> None:
    supervisor = _supervisor()

    await supervisor.run_drain_hooks()


@pytest.mark.asyncio
async def test_every_drain_hook_runs_even_when_one_fails(caplog) -> None:
    """A feature's close hook must not be able to skip another feature's."""
    supervisor = _supervisor()
    ran: list[str] = []

    async def _ok() -> None:
        ran.append("ok")

    async def _boom() -> None:
        ran.append("boom")
        raise RuntimeError("hook failed")

    supervisor.register_drain_hook("boom", _boom)
    supervisor.register_drain_hook("ok", _ok)

    with caplog.at_level("WARNING"):
        await supervisor.run_drain_hooks()

    assert sorted(ran) == ["boom", "ok"]
    assert "hook failed" in caplog.text


@pytest.mark.asyncio
async def test_registering_a_hook_under_an_existing_name_replaces_it() -> None:
    supervisor = _supervisor()
    ran: list[str] = []

    async def _first() -> None:  # pragma: no cover - replaced before running
        ran.append("first")

    async def _second() -> None:
        ran.append("second")

    supervisor.register_drain_hook("only", _first)
    supervisor.register_drain_hook("only", _second)

    await supervisor.run_drain_hooks()

    assert ran == ["second"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit()], ids=["keyboard-interrupt", "system-exit"]
)
async def test_an_interpreter_exit_from_a_drain_hook_takes_precedence(
    error: BaseException,
) -> None:
    """It is re-raised after the sweep rather than logged like an ordinary failure."""
    supervisor = _supervisor()
    ran: list[str] = []

    async def _exiting() -> None:
        raise error

    async def _ordinary() -> None:
        ran.append("ordinary")
        raise RuntimeError("logged, not raised")

    supervisor.register_drain_hook("exiting", _exiting)
    supervisor.register_drain_hook("ordinary", _ordinary)

    with pytest.raises(type(error)):
        await supervisor.run_drain_hooks()

    assert ran == ["ordinary"], "the sweep still completes"
