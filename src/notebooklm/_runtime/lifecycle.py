"""Root-owned, protocol-neutral client lifecycle orchestration."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from functools import partial
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

from .._loop_affinity import assert_bound_loop as _assert_bound_loop
from .config import CORE_LOGGER_NAME

logger = logging.getLogger(CORE_LOGGER_NAME)

_T = TypeVar("_T")


@runtime_checkable
class TransportLifecycle(Protocol):
    """A concrete resource owner participating in root lifecycle phases."""

    name: str

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None: ...

    async def prepare_close(self) -> None: ...

    async def close_resources(self) -> None: ...


@runtime_checkable
class LoopParticipant(Protocol):
    """Owner of lazy loop state rebuilt by an open generation."""

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None: ...

    def reset_after_open(self) -> None: ...


class LifecycleSupervisor(LoopParticipant, Protocol):
    """Narrow admission seam consumed by the root lifecycle."""

    def prepare_generation(self, epoch: int) -> None: ...

    def start_accepting(self, epoch: int) -> None: ...

    async def stop_accepting(self, epoch: int) -> None: ...

    async def wait_for_idle(self, epoch: int, timeout: float | None) -> None: ...

    async def begin_closing(self, epoch: int) -> None: ...

    def mark_closed(self, epoch: int) -> None: ...

    async def run_drain_hooks(self) -> None: ...


class _ResourceState(Enum):
    CLOSED = auto()
    OPENING = auto()
    OPEN = auto()
    CLOSING = auto()


class _OpenOutcome(Enum):
    OPENED = auto()
    ABORTED_BY_OWNER = auto()
    FAILED = auto()


@dataclass(frozen=True)
class _Captured:
    """Internal task result that never raises across an asyncio Task boundary."""

    error: BaseException | None = None


async def _capture(awaitable: Awaitable[_T]) -> _Captured:
    """Capture even process-exit signals so sibling cleanup can finish first."""
    try:
        await awaitable
    except BaseException as exc:
        return _Captured(exc)
    return _Captured()


async def _capture_after_gate(
    gate: asyncio.Future[None],
    factory: Callable[[], Awaitable[_T]],
) -> _Captured:
    """Keep eager tasks inert until their creator releases its state lock."""
    try:
        await gate
    except BaseException as exc:
        return _Captured(exc)
    return await _capture(factory())


@dataclass(frozen=True)
class _OpenResult:
    outcome: _OpenOutcome
    error: BaseException | None = None


@dataclass(frozen=True)
class _OpenWave:
    loop: asyncio.AbstractEventLoop
    owner: asyncio.Task[Any] | None
    epoch: int
    prepare_task: asyncio.Task[_Captured]
    result: asyncio.Future[_OpenResult]


@dataclass
class _CloseWave:
    loop: asyncio.AbstractEventLoop
    epoch: int
    drain: bool
    drain_timeout: float | None
    abort_graceful: asyncio.Event
    task: asyncio.Task[_Captured] | None = None


@dataclass
class _RetainedWaitState:
    """Track whether a retained task's process-exit result was observed."""

    waiters: int = 0
    settled: bool = False
    detached: bool = False
    observed: bool = False


class ClientLifecycle:
    """Sole owner of resource state and transactional lifecycle waves.

    The root is deliberately neutral: concrete transports own their resources,
    and the fully assembled supervisor/transport/participant tuples are frozen
    at construction time.
    """

    def __init__(
        self,
        *,
        supervisor: LifecycleSupervisor,
        transports: Sequence[TransportLifecycle],
        loop_participants: Sequence[LoopParticipant],
    ) -> None:
        self._supervisor = supervisor
        self._transports = tuple(transports)
        self._loop_participants = tuple(loop_participants)
        self._state = _ResourceState.CLOSED
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._epoch = 0
        self._state_lock = threading.Lock()
        self._open_wave: _OpenWave | None = None
        self._close_wave: _CloseWave | None = None
        self._retained_tasks: set[asyncio.Task[_Captured]] = set()
        self._retained_waits: dict[asyncio.Task[_Captured], _RetainedWaitState] = {}

    def is_open(self) -> bool:
        """Return whether this root still owns an open resource generation."""
        with self._state_lock:
            return self._state in {_ResourceState.OPEN, _ResourceState.CLOSING}

    def get_bound_loop(self) -> asyncio.AbstractEventLoop | None:
        return self._bound_loop

    def assert_bound_loop(self) -> None:
        _assert_bound_loop(self._bound_loop)

    async def open(self) -> None:
        """Open all transports transactionally and coalesce concurrent callers."""
        loop = asyncio.get_running_loop()
        while True:
            owner = False
            start_gate: asyncio.Future[None] | None = None
            with self._state_lock:
                if self._state is _ResourceState.OPEN:
                    self._assert_loop(loop)
                    return
                if self._state is _ResourceState.CLOSING:
                    self._assert_loop(loop)
                    raise RuntimeError(
                        "NotebookLMClient is closing; wait for close() before open()."
                    )
                if self._state is _ResourceState.OPENING:
                    wave = cast(_OpenWave, self._open_wave)
                    self._assert_wave_loop(loop, wave.loop, "open")
                else:
                    self._epoch += 1
                    epoch = self._epoch
                    result: asyncio.Future[_OpenResult] = loop.create_future()
                    start_gate = loop.create_future()
                    prepare = asyncio.create_task(
                        _capture_after_gate(
                            start_gate,
                            partial(self._prepare_open, loop, epoch),
                        )
                    )
                    wave = _OpenWave(loop, asyncio.current_task(), epoch, prepare, result)
                    self._open_wave = wave
                    self._state = _ResourceState.OPENING
                    owner = True
            if start_gate is not None and not start_gate.done():
                start_gate.set_result(None)
            if not owner:
                outcome = await asyncio.shield(wave.result)
                if outcome.outcome is _OpenOutcome.OPENED:
                    return
                if outcome.outcome is _OpenOutcome.ABORTED_BY_OWNER:
                    continue
                assert outcome.error is not None
                raise outcome.error
            await self._run_open_owner(wave)
            return

    async def _prepare_open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        self._bound_loop = loop
        for participant in self._loop_participants:
            participant.set_bound_loop(loop)
            participant.reset_after_open()
        self._supervisor.prepare_generation(epoch)
        for transport in self._transports:
            await transport.open(loop, epoch)

    async def _run_open_owner(self, wave: _OpenWave) -> None:
        try:
            prepared = await asyncio.shield(wave.prepare_task)
        except asyncio.CancelledError as cancelled:
            if not wave.prepare_task.done():
                wave.prepare_task.cancel()
            cleanup = self._retain_task(
                asyncio.create_task(
                    _capture(self._rollback_open(wave, _OpenOutcome.ABORTED_BY_OWNER))
                )
            )
            self._begin_retained_wait(cleanup)
            try:
                cleanup_result = await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                self._end_retained_wait(cleanup, detached=True)
                raise cancelled from None
            self._end_retained_wait(cleanup, observed=True)
            if isinstance(cleanup_result.error, (KeyboardInterrupt, SystemExit)):
                raise cleanup_result.error from cancelled
            raise cancelled
        if prepared.error is not None:
            await self._fail_open(wave, prepared.error)
            raise prepared.error
        try:
            with self._state_lock:
                self._supervisor.start_accepting(wave.epoch)
                self._state = _ResourceState.OPEN
                self._open_wave = None
                if not wave.result.done():
                    wave.result.set_result(_OpenResult(_OpenOutcome.OPENED))
        except BaseException as exc:
            await self._fail_open(wave, exc)
            raise

    async def _fail_open(self, wave: _OpenWave, error: BaseException) -> None:
        cleanup = self._retain_task(
            asyncio.create_task(_capture(self._rollback_open(wave, _OpenOutcome.FAILED, error)))
        )
        self._begin_retained_wait(cleanup)
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                cleanup_result = await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                if cancelled is not None:
                    self._end_retained_wait(cleanup, detached=True)
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise error from None
                    raise cancelled from None
                cancelled = exc
        self._end_retained_wait(cleanup, observed=True)
        # A process-exit signal that failed the open was observed before any
        # later caller cancellation or rollback outcome.  Preserve that
        # precedence explicitly: the rollback task normally republishes the
        # same signal, but a commit-time signal is not part of
        # ``wave.prepare_task`` and therefore cannot be rediscovered there.
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise error
        if cleanup_result.error is not None:
            raise cleanup_result.error
        if cancelled is not None:
            raise cancelled

    def _observe_close_race(
        self,
        loop: asyncio.AbstractEventLoop,
        epoch: int,
    ) -> tuple[_CloseWave | None, bool]:
        """Observe whether ``epoch`` was claimed or retired by ``close()``.

        A public ``drain()`` cannot hold ``_state_lock`` across admission
        awaits.  Consequently, a close wave may claim ``CLOSING`` after drain
        snapshots ``OPEN`` but before either supervisor transition lands.  The
        returned boolean distinguishes a fully retired/replaced epoch from an
        epoch that remains independently drainable.
        """
        with self._state_lock:
            if self._epoch != epoch or self._state is _ResourceState.CLOSED:
                return None, True
            if self._state is _ResourceState.CLOSING:
                wave = cast(_CloseWave, self._close_wave)
                self._assert_wave_loop(loop, wave.loop, "drain")
                return wave, False
            return None, False

    async def _join_close_race(self, loop: asyncio.AbstractEventLoop, epoch: int) -> bool:
        """Join a close that raced drain, or acknowledge a retired epoch."""
        wave, retired = self._observe_close_race(loop, epoch)
        if wave is not None:
            await self._await_close_wave(wave)
            return True
        return retired

    async def _run_transport_phase(self, method: str) -> list[BaseException | None]:
        tasks = [
            asyncio.create_task(_capture(getattr(transport, method)()))
            for transport in self._transports
        ]
        if not tasks:
            return []
        captured = await asyncio.gather(*tasks)
        return [result.error for result in captured]

    async def _rollback_open(
        self,
        wave: _OpenWave,
        outcome: _OpenOutcome,
        error: BaseException | None = None,
    ) -> None:
        prepare_result = await asyncio.shield(wave.prepare_task)
        prepare_results = await self._run_transport_phase("prepare_close")
        close_results = await self._run_transport_phase("close_resources")
        rollback_results = (*prepare_results, *close_results)
        rollback_transports = (*self._transports, *self._transports)
        for transport, result in zip(rollback_transports, rollback_results, strict=True):
            if isinstance(result, BaseException) and not isinstance(
                result, (KeyboardInterrupt, SystemExit)
            ):
                logger.warning(
                    "Ignoring %s rollback failure to preserve open outcome: %s",
                    transport.name,
                    result,
                )
        mark_error: BaseException | None = None
        try:
            self._supervisor.mark_closed(wave.epoch)
        except BaseException as exc:
            mark_error = exc
            if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
                logger.warning("Ignoring admission rollback failure: %s", exc)
        process_exit = next(
            (
                result
                for result in (prepare_result.error, *rollback_results, mark_error)
                if isinstance(result, (KeyboardInterrupt, SystemExit))
            ),
            None,
        )
        final_outcome = _OpenOutcome.FAILED if process_exit is not None else outcome
        final_error = process_exit if process_exit is not None else error
        with self._state_lock:
            self._state = _ResourceState.CLOSED
            self._open_wave = None
            if not wave.result.done():
                wave.result.set_result(_OpenResult(final_outcome, final_error))
        if process_exit is not None:
            raise process_exit

    async def drain(self, timeout: float | None = None) -> None:
        """Stop top-level admission while leaving resources open."""
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {timeout!r}")
        loop = asyncio.get_running_loop()
        while True:
            with self._state_lock:
                state = self._state
                if state is _ResourceState.CLOSED:
                    return
                open_wave = self._open_wave
                close_wave = self._close_wave
                epoch = self._epoch
                if state is _ResourceState.OPENING:
                    assert open_wave is not None
                    self._assert_wave_loop(loop, open_wave.loop, "drain")
                elif state is _ResourceState.CLOSING:
                    assert close_wave is not None
                    self._assert_wave_loop(loop, close_wave.loop, "drain")
                else:
                    self._assert_loop(loop)
            if state is _ResourceState.OPENING:
                assert open_wave is not None
                result = await asyncio.shield(open_wave.result)
                if result.outcome is _OpenOutcome.FAILED:
                    assert result.error is not None
                    raise result.error
                continue
            if state is _ResourceState.CLOSING:
                assert close_wave is not None
                await self._await_close_wave(close_wave)
                return
            try:
                await self._supervisor.stop_accepting(epoch)
            except RuntimeError:
                # ``close()`` may have claimed and even retired this epoch
                # while the admission transition was awaiting its condition.
                # Suppress only that lifecycle race; unrelated supervisor
                # failures still propagate.
                if await self._join_close_race(loop, epoch):
                    return
                raise
            if await self._join_close_race(loop, epoch):
                return
            try:
                await self._supervisor.wait_for_idle(epoch, timeout)
            except RuntimeError:
                if await self._join_close_race(loop, epoch):
                    return
                raise
            # A forced close can retire an in-flight generation while this
            # waiter is parked.  Match the existing CLOSING-observer contract
            # by joining that wave before returning.
            if await self._join_close_race(loop, epoch):
                return
            return

    async def close(
        self,
        *,
        drain: bool = True,
        drain_timeout: float | None = None,
    ) -> None:
        """Coalesce one phased close wave and retain it across cancellation."""
        if drain and drain_timeout is not None and drain_timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {drain_timeout!r}")
        loop = asyncio.get_running_loop()
        while True:
            start_gate: asyncio.Future[None] | None = None
            with self._state_lock:
                if self._state is _ResourceState.CLOSED:
                    return
                if self._state is _ResourceState.OPENING:
                    open_wave = cast(_OpenWave, self._open_wave)
                    self._assert_wave_loop(loop, open_wave.loop, "close")
                    close_wave = None
                elif self._state is _ResourceState.CLOSING:
                    close_wave = cast(_CloseWave, self._close_wave)
                    self._assert_wave_loop(loop, close_wave.loop, "close")
                    open_wave = None
                else:
                    self._assert_loop(loop)
                    open_wave = None
                    close_wave = _CloseWave(
                        loop=loop,
                        epoch=self._epoch,
                        drain=drain,
                        drain_timeout=drain_timeout,
                        abort_graceful=asyncio.Event(),
                    )
                    self._state = _ResourceState.CLOSING
                    start_gate = loop.create_future()
                    close_wave.task = self._retain_task(
                        asyncio.create_task(
                            _capture_after_gate(
                                start_gate,
                                partial(self._run_close, close_wave),
                            )
                        )
                    )
                    self._close_wave = close_wave
            if start_gate is not None and not start_gate.done():
                start_gate.set_result(None)
            if open_wave is not None:
                result = await asyncio.shield(open_wave.result)
                if result.outcome is _OpenOutcome.FAILED:
                    assert result.error is not None
                    raise result.error
                continue
            assert close_wave is not None
            await self._await_close_wave(close_wave)
            return

    async def _await_close_wave(self, wave: _CloseWave) -> None:
        assert wave.task is not None
        self._begin_retained_wait(wave.task)
        try:
            result = await asyncio.shield(wave.task)
        except asyncio.CancelledError as cancelled:
            wave.abort_graceful.set()
            try:
                result = await asyncio.shield(wave.task)
            except asyncio.CancelledError:
                self._end_retained_wait(wave.task, detached=True)
                raise cancelled from None
            self._end_retained_wait(wave.task, observed=True)
            if isinstance(result.error, (KeyboardInterrupt, SystemExit)):
                raise result.error from cancelled
            raise cancelled
        self._end_retained_wait(wave.task, observed=True)
        if result.error is not None:
            raise result.error

    async def _run_close(self, wave: _CloseWave) -> None:
        timeout_error: TimeoutError | None = None
        prephase_results: list[BaseException] = []
        if wave.drain:
            prephase = asyncio.create_task(
                _capture(self._run_graceful_prephase(wave, prephase_results))
            )
            abort_wait = asyncio.create_task(_capture(wave.abort_graceful.wait()))
            _done, _pending = await asyncio.wait(
                {prephase, abort_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            aborted = wave.abort_graceful.is_set()
            if aborted:
                if not prephase.done():
                    prephase.cancel()
            else:
                abort_wait.cancel()
            prephase_result, _abort_result = await asyncio.gather(prephase, abort_wait)
            if not aborted and prephase_result.error is not None:
                prephase_results.append(prephase_result.error)
            if aborted:
                prephase_results[:] = [
                    result
                    for result in prephase_results
                    if isinstance(result, (KeyboardInterrupt, SystemExit))
                ]
            prephase_process_exit = next(
                (
                    result
                    for result in prephase_results
                    if isinstance(result, (KeyboardInterrupt, SystemExit))
                ),
                None,
            )
            timeout_error = next(
                (result for result in prephase_results if isinstance(result, TimeoutError)),
                None,
            )
            prephase_failure = next(
                (
                    result
                    for result in prephase_results
                    if not isinstance(result, (KeyboardInterrupt, SystemExit, TimeoutError))
                ),
                None,
            )
            if prephase_process_exit is None and prephase_failure is not None:
                with self._state_lock:
                    if self._close_wave is wave:
                        self._state = _ResourceState.OPEN
                        self._close_wave = None
                raise prephase_failure
        closing_result = await _capture(self._supervisor.begin_closing(wave.epoch))
        existing_process_exit = next(
            (
                result
                for result in prephase_results
                if isinstance(result, (KeyboardInterrupt, SystemExit))
            ),
            None,
        )
        if (
            closing_result.error is not None
            and not isinstance(closing_result.error, (KeyboardInterrupt, SystemExit))
            and existing_process_exit is None
        ):
            with self._state_lock:
                if self._close_wave is wave:
                    self._state = _ResourceState.OPEN
                    self._close_wave = None
            raise closing_result.error
        prepare_results = await self._run_transport_phase("prepare_close")
        hook_result = await _capture(self._supervisor.run_drain_hooks())
        close_results = await self._run_transport_phase("close_resources")
        mark_error: BaseException | None = None
        try:
            self._supervisor.mark_closed(wave.epoch)
        except BaseException as exc:
            mark_error = exc
        finally:
            with self._state_lock:
                self._state = _ResourceState.CLOSED
                self._close_wave = None
        teardown_ordered = [
            closing_result.error,
            *prepare_results,
            hook_result.error,
            *close_results,
            mark_error,
        ]
        ordered = [*prephase_results, *teardown_ordered]
        process_exit = next(
            (result for result in ordered if isinstance(result, (KeyboardInterrupt, SystemExit))),
            None,
        )
        if process_exit is not None:
            raise process_exit
        if timeout_error is not None:
            ordinary = next(
                (result for result in teardown_ordered if isinstance(result, Exception)), None
            )
            if ordinary is not None:
                raise timeout_error from ordinary
            raise timeout_error
        failure = next((result for result in ordered if isinstance(result, BaseException)), None)
        if failure is not None:
            raise failure

    async def _run_graceful_prephase(
        self,
        wave: _CloseWave,
        results: list[BaseException],
    ) -> None:
        phases: tuple[Callable[[], Awaitable[None]], ...] = (
            partial(self._supervisor.stop_accepting, wave.epoch),
            self._supervisor.run_drain_hooks,
            partial(self._supervisor.wait_for_idle, wave.epoch, wave.drain_timeout),
        )
        for phase in phases:
            result = await _capture(phase())
            if result.error is None:
                continue
            results.append(result.error)
            if not isinstance(result.error, (KeyboardInterrupt, SystemExit)):
                return

    def _assert_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._bound_loop is not None and self._bound_loop is not loop:
            _assert_bound_loop(self._bound_loop)

    @staticmethod
    def _assert_wave_loop(
        loop: asyncio.AbstractEventLoop,
        wave_loop: asyncio.AbstractEventLoop,
        action: str,
    ) -> None:
        if loop is not wave_loop:
            raise RuntimeError(
                f"Cannot {action} NotebookLMClient from a different event loop while a lifecycle "
                "wave is active. Wait on the loop that owns the client."
            )

    def _retain_task(self, task: asyncio.Task[_Captured]) -> asyncio.Task[_Captured]:
        self._retained_tasks.add(task)
        state = _RetainedWaitState()
        self._retained_waits[task] = state

        def _settled(done: asyncio.Task[_Captured]) -> None:
            self._retained_tasks.discard(done)
            state.settled = True
            self._finish_retained_wait(done, state)

        task.add_done_callback(_settled)
        return task

    def _begin_retained_wait(self, task: asyncio.Task[_Captured]) -> None:
        self._retained_waits[task].waiters += 1

    def _end_retained_wait(
        self,
        task: asyncio.Task[_Captured],
        *,
        detached: bool = False,
        observed: bool = False,
    ) -> None:
        state = self._retained_waits.get(task)
        if state is None:
            return
        state.waiters -= 1
        state.detached = state.detached or detached
        state.observed = state.observed or observed
        self._finish_retained_wait(task, state)

    def _finish_retained_wait(
        self,
        task: asyncio.Task[_Captured],
        state: _RetainedWaitState,
    ) -> None:
        if not state.settled or state.waiters:
            return
        if state.detached and not state.observed and not task.cancelled():
            process_exit = task.result().error
            if isinstance(process_exit, (KeyboardInterrupt, SystemExit)):
                task.get_loop().call_exception_handler(
                    {
                        "message": "Process-exit signal from detached lifecycle task",
                        "exception": process_exit,
                    }
                )
        self._retained_waits.pop(task, None)


__all__ = [
    "ClientLifecycle",
    "LifecycleSupervisor",
    "LoopParticipant",
    "TransportLifecycle",
]
