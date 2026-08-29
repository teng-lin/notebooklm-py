"""Protocol-neutral logical-call admission and accounting.

``CallSupervisor`` is the one owner of the policy shared by the web and
Android transports: drain admission, terminal RPC telemetry, and the
client-wide RPC semaphore.  Wire-specific retry, authentication, request
encoding, and error mapping deliberately stay outside this module.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

from .._client_metrics import ClientMetrics
from .._deadline import RuntimeDeadline
from .._logging import get_request_id
from .._loop_affinity import assert_bound_loop
from .._loop_bound import LoopBoundPrimitive
from .._transport_drain import TransportDrainTracker, _TransportOperationToken
from ..types import RpcTelemetryEvent

_T = TypeVar("_T")
# Drain-hook warnings historically came from the bookkeeping module.  Keep the
# logger stable while moving hook ownership to the supervisor.
logger = logging.getLogger("notebooklm._transport_drain")


class AdmissionState(str, Enum):
    """Admission phase for one resource generation."""

    CLOSED = "closed"
    ACCEPTING = "accepting"
    DRAINING = "draining"
    CLOSING = "closing"


@dataclass(eq=False)
class AdmissionGeneration:
    """All admission state that must not bleed across close/reopen."""

    epoch: int
    loop: asyncio.AbstractEventLoop
    drain: TransportDrainTracker
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    state: AdmissionState = AdmissionState.CLOSED
    in_flight: int = 0
    depths: weakref.WeakKeyDictionary[asyncio.Task[Any], int] = field(
        default_factory=weakref.WeakKeyDictionary
    )
    semaphore: asyncio.Semaphore | None = None


@dataclass(frozen=True)
class _AdmissionToken:
    generation: AdmissionGeneration
    task: asyncio.Task[Any] | None
    drain_token: _TransportOperationToken


@dataclass(frozen=True)
class CallLease:
    """Proof that one transport call was admitted for ``epoch``."""

    epoch: int
    deadline: RuntimeDeadline | None
    _token: _AdmissionToken = field(repr=False, compare=False)


@dataclass(frozen=True)
class OperationLease:
    """Generation-bearing proof held across a multi-call workflow."""

    epoch: int
    _token: _AdmissionToken = field(repr=False, compare=False)


@dataclass
class _SettlementState:
    """Mutable observation state shared with a settlement done callback."""

    abandoned: bool = False
    reported: bool = False


@dataclass(frozen=True)
class _SettlementResult:
    """A retained task result that never raises across its Task boundary."""

    error: BaseException | None = None


class CallSupervisor(LoopBoundPrimitive):
    """Apply ``Drain -> Metrics -> Semaphore`` around logical calls.

    Construction is event-loop agnostic.  The drain condition and RPC
    semaphore remain lazy, and ``set_bound_loop``/``reset_after_open`` are the
    only binding points used by the current root lifecycle.
    """

    def __init__(
        self,
        *,
        metrics: ClientMetrics,
        drain_tracker: TransportDrainTracker,
        max_concurrent_rpcs: int | None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if max_concurrent_rpcs is not None and max_concurrent_rpcs < 1:
            raise ValueError(f"max_concurrent_rpcs must be >= 1, got {max_concurrent_rpcs!r}")
        self._metrics = metrics
        # The injected tracker belongs exclusively to the first admission
        # generation.  Every later generation receives a fresh tracker so a
        # late epoch-N settlement keeps using epoch N's loop-local condition
        # after the supervisor has reopened on another loop.
        self._first_generation_drain: TransportDrainTracker | None = drain_tracker
        self._max_concurrent_rpcs = max_concurrent_rpcs
        self._rpc_semaphore: asyncio.Semaphore | None = None
        self._monotonic = time.perf_counter if monotonic is None else monotonic
        self._current: AdmissionGeneration | None = None
        self._retired: dict[int, AdmissionGeneration] = {}
        self._last_epoch = 0
        self._settlement_tasks: set[asyncio.Task[_SettlementResult]] = set()
        self._drain_hooks: dict[str, Callable[[], Awaitable[None]]] = {}

    @property
    def bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the loop used by polling and lifecycle-affinity checks."""
        return self._bound_loop

    def get_bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the historical loop binding through a method-shaped seam."""
        return self._bound_loop

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Bind supervisor-owned current-generation primitives to ``loop``."""
        super().set_bound_loop(loop)

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        self._rpc_semaphore = None

    def reset_after_open(self) -> None:
        """Reset loop-local lazy state without changing admission state.

        ``ClientLifecycle`` is the sole owner of generation transitions.  In
        particular, open preparation calls this method before
        :meth:`prepare_generation`, so resetting must never allocate, retire,
        or start a generation.  Retired generation records remain reachable
        until their late settlements reclaim them.
        """
        self._rpc_semaphore = None

    def assert_bound_loop(self) -> None:
        """Fail on an unavailable generation before checking loop affinity."""
        if self._current is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        assert_bound_loop(self._bound_loop)

    def is_closing(self) -> bool:
        """Return whether the current generation is in forced teardown.

        Feature-owned drain hooks use this narrow query to distinguish the
        graceful ``DRAINING`` prephase, where admitted child work must settle
        naturally, from ``CLOSING``, where registered work must be cancelled
        and gathered before transport resources are released.
        """
        self.assert_bound_loop()
        generation = self._current
        assert generation is not None
        return generation.state is AdmissionState.CLOSING

    def record_started(self, method: str | None) -> None:
        """Validate and count one logical RPC start.

        The check is synchronous and immediately precedes the executor's pure
        request preparation, so a drain transition cannot interleave between
        validation and the later scope entry on the same event loop.  This
        preserves the historical pre-open failure (including its zero metrics)
        without touching Kernel resources before admission.

        ``None`` remains a true no-op for non-RPC calls.
        """
        if method is None:
            return

        generation = self._current
        if generation is None or generation.state is AdmissionState.CLOSED:
            raise RuntimeError("Client not initialized. Use 'async with' context.")

        # Once a resource generation exists, retain the Phase A logical-call
        # placement: ``started`` is recorded before Drain can reject a top-level
        # call in DRAINING/CLOSING.  Nested same-generation work is admitted by
        # ``call_scope``; every other state decision remains there.
        self._metrics.increment(rpc_calls_started=1)

    def prepare_generation(self, epoch: int) -> None:
        """Allocate a closed admission record for ``epoch``.

        This method is synchronous so the root lifecycle can include it in its
        checkpoint-free open preparation/commit sequence.  No resource is
        admitted until :meth:`start_accepting` runs.
        """
        assert_bound_loop(self._bound_loop)
        if self._bound_loop is None:
            loop = asyncio.get_running_loop()
            self.set_bound_loop(loop)
        else:
            loop = self._bound_loop
        if self._current is not None:
            raise RuntimeError(
                f"cannot prepare admission generation {epoch}; generation "
                f"{self._current.epoch} is still current"
            )
        if epoch <= self._last_epoch or epoch in self._retired:
            raise RuntimeError(f"admission generation {epoch} is not newer than prior epochs")
        drain = self._first_generation_drain
        if drain is None:
            drain = TransportDrainTracker()
        else:
            self._first_generation_drain = None
        drain.set_bound_loop(loop)
        drain.reset_after_open()
        generation = AdmissionGeneration(epoch=epoch, loop=loop, drain=drain)
        self._current = generation
        self._last_epoch = epoch
        self._rpc_semaphore = None

    def start_accepting(self, epoch: int) -> None:
        """Commit a prepared generation to top-level admission."""
        generation = self._require_current(epoch)
        self._assert_generation_loop(generation)
        if generation.state is not AdmissionState.CLOSED:
            raise RuntimeError(
                f"admission generation {epoch} cannot start from {generation.state.value}"
            )
        generation.state = AdmissionState.ACCEPTING

    async def stop_accepting(self, epoch: int) -> None:
        """Atomically fence top-level admission for graceful drain."""
        generation = self._require_current(epoch)
        self._assert_generation_loop(generation)
        async with generation.condition:
            if generation.state is AdmissionState.ACCEPTING:
                generation.state = AdmissionState.DRAINING
            elif generation.state is not AdmissionState.DRAINING:
                raise RuntimeError(
                    f"admission generation {epoch} cannot drain from {generation.state.value}"
                )

    async def wait_for_idle(self, epoch: int, timeout: float | None) -> None:
        """Wait until every token belonging to ``epoch`` has settled."""
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {timeout!r}")
        generation = self._find_generation(epoch)
        self._assert_generation_loop(generation)
        async with generation.condition:
            if generation.in_flight == 0:
                return
            try:
                await asyncio.wait_for(
                    generation.condition.wait_for(lambda: generation.in_flight == 0),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                # Python 3.10's asyncio timeout is not yet an alias of the
                # built-in type used by lifecycle timeout policy.
                raise TimeoutError from None

    async def begin_closing(self, epoch: int) -> None:
        """Fence all new scopes and children before resource teardown."""
        generation = self._require_current(epoch)
        self._assert_generation_loop(generation)
        async with generation.condition:
            if generation.state in {AdmissionState.ACCEPTING, AdmissionState.DRAINING}:
                generation.state = AdmissionState.CLOSING
            elif generation.state is not AdmissionState.CLOSING:
                raise RuntimeError(
                    f"admission generation {epoch} cannot close from {generation.state.value}"
                )

    def mark_closed(self, epoch: int) -> None:
        """Retire ``epoch`` without mutating a future generation's counters."""
        generation = self._require_current(epoch)
        generation.state = AdmissionState.CLOSED
        self._current = None
        self._rpc_semaphore = None
        if generation.in_flight:
            self._retired[epoch] = generation

    def _require_current(self, epoch: int) -> AdmissionGeneration:
        generation = self._current
        if generation is None or generation.epoch != epoch:
            raise RuntimeError(f"admission generation {epoch} is not current")
        return generation

    def _find_generation(self, epoch: int) -> AdmissionGeneration:
        if self._current is not None and self._current.epoch == epoch:
            return self._current
        generation = self._retired.get(epoch)
        if generation is None:
            raise RuntimeError(f"admission generation {epoch} is unknown")
        return generation

    def _assert_generation_loop(self, generation: AdmissionGeneration) -> None:
        assert_bound_loop(generation.loop)

    def _task_depth(
        self,
        task: asyncio.Task[Any] | None,
        epoch: int,
    ) -> int:
        if task is None:
            return 0
        try:
            generation = self._find_generation(epoch)
        except RuntimeError:
            return 0
        return generation.depths.get(task, 0)

    def _has_other_generation_depth(
        self,
        task: asyncio.Task[Any] | None,
        epoch: int,
    ) -> bool:
        if task is None:
            return False
        generations = list(self._retired.values())
        if self._current is not None:
            generations.append(self._current)
        return any(
            generation.epoch != epoch and generation.depths.get(task, 0) > 0
            for generation in generations
        )

    async def _admit(
        self,
        label: str,
        *,
        expected_epoch: int | None = None,
    ) -> _AdmissionToken:
        generation = self._current
        if generation is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        self.assert_bound_loop()
        task = asyncio.current_task()
        self._assert_generation_loop(generation)
        if expected_epoch is not None and generation.epoch != expected_epoch:
            raise RuntimeError(
                "NotebookLMClient operation belongs to a retired resource generation "
                f"({label}; expected={expected_epoch}, active={generation.epoch})."
            )
        async with generation.condition:
            depth = generation.depths.get(task, 0) if task is not None else 0
            if self._has_other_generation_depth(task, generation.epoch):
                raise RuntimeError(
                    "NotebookLMClient operation belongs to a retired resource generation "
                    f"({label})."
                )
            if generation.state is AdmissionState.ACCEPTING or (
                generation.state is AdmissionState.DRAINING and depth > 0
            ):
                pass
            else:
                raise RuntimeError(
                    "NotebookLMClient is not accepting new operations "
                    f"({label}; state={generation.state.value})."
                )
            generation.in_flight += 1
            if task is not None:
                generation.depths[task] = depth + 1
        try:
            drain_token = await generation.drain.begin_transport_post(label)
        except BaseException as exc:
            settlement, state = self._publish_partial_settlement(
                generation=generation,
                task=task,
            )
            try:
                await self._await_settlement(
                    settlement,
                    state,
                    cancellation_already_active=isinstance(exc, asyncio.CancelledError),
                )
            except BaseException:
                # The admission failure/cancellation owns precedence.  A
                # re-cancel may detach this waiter, but the strongly retained
                # settlement still retires the generation token.
                pass
            raise
        return _AdmissionToken(
            generation=generation,
            task=task,
            drain_token=drain_token,
        )

    async def _finish_generation_token(
        self,
        generation: AdmissionGeneration,
        task: asyncio.Task[Any] | None,
    ) -> None:
        self._assert_generation_loop(generation)
        async with generation.condition:
            if task is not None:
                depth = generation.depths.get(task, 0)
                if depth <= 1:
                    generation.depths.pop(task, None)
                else:
                    generation.depths[task] = depth - 1
            generation.in_flight -= 1
            if generation.in_flight < 0:
                raise RuntimeError(f"admission generation {generation.epoch} settled below zero")
            if generation.in_flight == 0:
                generation.condition.notify_all()
                if generation.state is AdmissionState.CLOSED:
                    self._retired.pop(generation.epoch, None)

    async def _acquire_rpc_slot(
        self,
        generation: AdmissionGeneration,
        deadline: RuntimeDeadline | None,
    ) -> asyncio.Semaphore | None:
        semaphore = generation.semaphore
        limit = self._max_concurrent_rpcs
        if semaphore is None and limit is None:
            return None
        if semaphore is None:
            assert limit is not None
            semaphore = asyncio.Semaphore(limit)
            generation.semaphore = semaphore
            if self._current is generation:
                self._rpc_semaphore = semaphore
        if deadline is None:
            await semaphore.acquire()
        else:
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=deadline.remaining())
            except asyncio.TimeoutError:
                # Keep queue-deadline behavior interpreter-independent; on
                # Python 3.10 asyncio.TimeoutError is a distinct class.
                raise TimeoutError from None
        return semaphore

    def _retain_settlement(
        self,
        settle: Coroutine[Any, Any, None],
        *,
        epoch: int,
    ) -> tuple[asyncio.Task[_SettlementResult], _SettlementState]:
        """Publish one strongly retained, process-exit-safe settlement awaitable."""

        async def _capture() -> _SettlementResult:
            try:
                await settle
            except BaseException as exc:
                return _SettlementResult(exc)
            return _SettlementResult()

        state = _SettlementState()
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            _capture(),
            name=f"notebooklm-settle-{epoch}",
        )
        self._settlement_tasks.add(task)

        def _done(done: asyncio.Task[_SettlementResult]) -> None:
            self._settlement_tasks.discard(done)
            self._report_abandoned_settlement(done, state)

        task.add_done_callback(_done)
        return task, state

    @staticmethod
    def _report_abandoned_settlement(
        task: asyncio.Task[_SettlementResult],
        state: _SettlementState,
    ) -> None:
        """Forward an unobservable settlement failure exactly once."""
        if not state.abandoned or state.reported or not task.done() or task.cancelled():
            return
        error = task.result().error
        if error is None:
            return
        state.reported = True
        task.get_loop().call_exception_handler(
            {
                "message": "NotebookLM admission settlement failed after caller unwind",
                "exception": error,
                "task": task,
            }
        )

    def _abandon_settlement(
        self,
        task: asyncio.Task[_SettlementResult],
        state: _SettlementState,
    ) -> None:
        """Detach one waiter without losing an already-landed failure."""
        state.abandoned = True
        self._report_abandoned_settlement(task, state)

    def _publish_partial_settlement(
        self,
        *,
        generation: AdmissionGeneration,
        task: asyncio.Task[Any] | None,
        child: asyncio.Task[Any] | None = None,
    ) -> tuple[asyncio.Task[_SettlementResult], _SettlementState]:
        """Settle a generation reservation that has no legacy drain token."""

        async def _settle() -> None:
            try:
                if child is not None:
                    await asyncio.gather(child, return_exceptions=True)
            finally:
                await self._finish_generation_token(generation, task)

        return self._retain_settlement(_settle(), epoch=generation.epoch)

    def _publish_settlement(
        self,
        *,
        token: _AdmissionToken,
        queue_wait: float | None,
    ) -> tuple[asyncio.Task[_SettlementResult], _SettlementState]:
        async def _settle() -> None:
            first_error: BaseException | None = None
            try:
                await token.generation.drain.finish_transport_post(token.drain_token)
            except BaseException as exc:
                first_error = exc
            try:
                await self._finish_generation_token(token.generation, token.task)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            if queue_wait is not None:
                try:
                    self._metrics.record_rpc_queue_wait(queue_wait)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error

        return self._retain_settlement(_settle(), epoch=token.generation.epoch)

    async def _await_settlement(
        self,
        task: asyncio.Task[_SettlementResult],
        state: _SettlementState,
        *,
        cancellation_already_active: bool,
    ) -> None:
        first_cancel: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(task)
                if first_cancel is not None:
                    raise first_cancel
                if result.error is not None:
                    raise result.error
                return
            except asyncio.CancelledError as exc:
                if cancellation_already_active:
                    self._abandon_settlement(task, state)
                    raise
                if first_cancel is not None:
                    self._abandon_settlement(task, state)
                    raise first_cancel from None
                first_cancel = exc
                # A first cancellation cannot cancel the shielded settlement.
                # Keep waiting; only an explicit re-cancel lets this caller
                # unwind before the retained task lands.
                continue

    async def _complete_call(
        self,
        *,
        token: _AdmissionToken,
        method: str | None,
        started_at: float,
        queue_wait: float,
        semaphore: asyncio.Semaphore | None,
        outcome: BaseException | None,
    ) -> None:
        # Callback-visible order is load-bearing: release -> event -> drain
        # finish -> queue recorder.
        if semaphore is not None:
            semaphore.release()

        terminal_error: BaseException | None = None
        elapsed = self._monotonic() - started_at
        try:
            if method is not None and outcome is None:
                self._metrics.increment(
                    rpc_calls_succeeded=1,
                    rpc_latency_seconds_total=elapsed,
                )
                await self._metrics.emit_rpc_event(
                    RpcTelemetryEvent(
                        method=method,
                        status="success",
                        elapsed_seconds=elapsed,
                        request_id=get_request_id(),
                    )
                )
            elif method is not None and isinstance(outcome, Exception):
                self._metrics.increment(
                    rpc_calls_failed=1,
                    rpc_latency_seconds_total=elapsed,
                )
                await self._metrics.emit_rpc_event(
                    RpcTelemetryEvent(
                        method=method,
                        status="error",
                        elapsed_seconds=elapsed,
                        request_id=get_request_id(),
                        error_type=type(outcome).__qualname__,
                    )
                )
        except BaseException as exc:
            terminal_error = exc

        settlement, state = self._publish_settlement(
            token=token,
            queue_wait=queue_wait,
        )
        settlement_error: BaseException | None = None
        try:
            await self._await_settlement(
                settlement,
                state,
                cancellation_already_active=isinstance(outcome, asyncio.CancelledError)
                or isinstance(terminal_error, asyncio.CancelledError),
            )
        except BaseException as exc:
            settlement_error = exc

        # The transport/body signal always wins over recorder failures.
        if outcome is not None:
            return
        if terminal_error is not None:
            raise terminal_error
        if settlement_error is not None:
            raise settlement_error

    @asynccontextmanager
    async def call_scope(
        self,
        label: str,
        method: str | None,
        deadline: RuntimeDeadline | None,
        *,
        expected_epoch: int | None = None,
    ) -> AsyncIterator[CallLease]:
        """Admit one logical call and apply metrics/semaphore policy."""
        token = await self._admit(label, expected_epoch=expected_epoch)
        started_at = self._monotonic()
        queue_started_at = started_at
        semaphore: asyncio.Semaphore | None = None
        try:
            semaphore = await self._acquire_rpc_slot(token.generation, deadline)
        except BaseException as exc:
            await self._complete_call(
                token=token,
                method=method,
                started_at=started_at,
                queue_wait=self._monotonic() - queue_started_at,
                semaphore=None,
                outcome=exc,
            )
            raise

        queue_wait = self._monotonic() - queue_started_at
        lease = CallLease(epoch=token.generation.epoch, deadline=deadline, _token=token)
        try:
            yield lease
        except BaseException as exc:
            await self._complete_call(
                token=token,
                method=method,
                started_at=started_at,
                queue_wait=queue_wait,
                semaphore=semaphore,
                outcome=exc,
            )
            raise
        else:
            await self._complete_call(
                token=token,
                method=method,
                started_at=started_at,
                queue_wait=queue_wait,
                semaphore=semaphore,
                outcome=None,
            )

    async def run(
        self,
        label: str,
        method: str | None,
        deadline: RuntimeDeadline | None,
        invoke: Callable[[CallLease], Awaitable[_T]],
        *,
        expected_epoch: int | None = None,
    ) -> _T:
        """Run a lazily-created unary invocation inside :meth:`call_scope`."""
        async with self.call_scope(
            label,
            method,
            deadline,
            expected_epoch=expected_epoch,
        ) as lease:
            return await invoke(lease)

    @asynccontextmanager
    async def operation_scope(
        self,
        label: str,
        *,
        expected_epoch: int | None = None,
    ) -> AsyncIterator[OperationLease]:
        """Hold admission across a complete multi-call workflow."""
        token = await self._admit(label, expected_epoch=expected_epoch)
        lease = OperationLease(epoch=token.generation.epoch, _token=token)
        try:
            yield lease
        except BaseException as exc:
            settlement, state = self._publish_settlement(
                token=token,
                queue_wait=None,
            )
            try:
                await self._await_settlement(
                    settlement,
                    state,
                    cancellation_already_active=isinstance(exc, asyncio.CancelledError),
                )
            except BaseException:
                pass
            raise
        else:
            settlement, state = self._publish_settlement(
                token=token,
                queue_wait=None,
            )
            await self._await_settlement(
                settlement,
                state,
                cancellation_already_active=False,
            )

    async def spawn_child(
        self,
        label: str,
        factory: Callable[[], Awaitable[_T]],
    ) -> asyncio.Task[_T]:
        """Atomically reserve and spawn same-generation internal work.

        The wrapper's private gate is deliberately incomplete during task
        construction.  Even under an eager task factory, ``factory`` cannot be
        invoked until the parent token and child depth are associated while
        the drain condition remains held.
        """
        self.assert_bound_loop()
        parent = asyncio.current_task()
        generation = self._current
        if generation is None:
            raise RuntimeError(
                f"NotebookLMClient child work requires a current generation ({label})."
            )
        self._assert_generation_loop(generation)
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        token: _AdmissionToken | None = None
        wrapper_started = False

        async def _wrapper() -> _T:
            nonlocal wrapper_started
            body_error: BaseException | None = None
            try:
                await gate
                assert token is not None
                wrapper_started = True
                if not started.done():
                    started.set_result(None)
                return await factory()
            except BaseException as exc:
                body_error = exc
                raise
            finally:
                if token is not None:
                    settlement, state = self._publish_settlement(
                        token=token,
                        queue_wait=None,
                    )
                    try:
                        await self._await_settlement(
                            settlement,
                            state,
                            cancellation_already_active=isinstance(
                                body_error, asyncio.CancelledError
                            ),
                        )
                    except BaseException:
                        if body_error is None:
                            raise

        async with generation.condition:
            parent_depth = generation.depths.get(parent, 0) if parent is not None else 0
            if parent_depth == 0:
                raise RuntimeError(
                    "NotebookLMClient child work requires a same-generation parent token "
                    f"({label})."
                )
            if generation.state not in {
                AdmissionState.ACCEPTING,
                AdmissionState.DRAINING,
            }:
                raise RuntimeError(
                    "NotebookLMClient is not accepting child work "
                    f"({label}; state={generation.state.value})."
                )
            task = asyncio.create_task(_wrapper(), name=label)
            generation.depths[task] = generation.depths.get(task, 0) + 1
            generation.in_flight += 1
        try:
            drain_token = await generation.drain.begin_transport_task(task, label)
        except BaseException as exc:
            gate.cancel()
            task.cancel()
            settlement, state = self._publish_partial_settlement(
                generation=generation,
                task=task,
                child=task,
            )
            try:
                await self._await_settlement(
                    settlement,
                    state,
                    cancellation_already_active=isinstance(exc, asyncio.CancelledError),
                )
            except BaseException:
                pass
            raise
        token = _AdmissionToken(
            generation=generation,
            task=task,
            drain_token=drain_token,
        )
        # No coroutine factory can run before both generation and legacy-drain
        # tokens have been associated with the gated wrapper.
        if not gate.done():
            gate.set_result(None)
        try:
            # Do not publish the Task until the wrapper has crossed its start
            # gate.  A caller can therefore never cancel the returned Task in
            # the pre-first-step window where a coroutine ``finally`` cannot
            # run and admission tokens would otherwise leak.
            await asyncio.shield(started)
        except asyncio.CancelledError as cancelled:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if not wrapper_started:
                assert token is not None
                settlement, state = self._publish_settlement(
                    token=token,
                    queue_wait=None,
                )
                try:
                    await self._await_settlement(
                        settlement,
                        state,
                        cancellation_already_active=True,
                    )
                except BaseException:
                    pass
            raise cancelled
        return task

    def register_drain_hook(self, name: str, hook: Callable[[], Awaitable[None]]) -> None:
        """Register or replace a feature-owned close-time hook."""
        self._drain_hooks[name] = hook

    async def run_drain_hooks(self) -> None:
        """Run feature hooks concurrently and preserve process-exit precedence."""
        named_hooks = list(self._drain_hooks.items())
        if not named_hooks:
            return

        async def _run_hook(hook: Callable[[], Awaitable[None]]) -> BaseException | None:
            try:
                await hook()
            except BaseException as exc:
                return exc
            return None

        results = await asyncio.gather(*(_run_hook(hook) for _name, hook in named_hooks))
        process_exit: KeyboardInterrupt | SystemExit | None = None
        for (name, _hook), result in zip(named_hooks, results, strict=True):
            if isinstance(result, (KeyboardInterrupt, SystemExit)):
                if process_exit is None:
                    process_exit = result
            elif isinstance(result, BaseException):
                logger.warning(
                    "Drain hook %r raised during close: %s", name, result, exc_info=result
                )
        if process_exit is not None:
            raise process_exit


__all__ = ["CallLease", "CallSupervisor", "OperationLease"]
