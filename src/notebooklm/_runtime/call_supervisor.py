"""Protocol-neutral logical-call admission and accounting.

``CallSupervisor`` is the one owner of the policy shared by the web and
Android transports: drain admission, terminal RPC telemetry, and the
client-wide RPC semaphore.  Wire-specific retry, authentication, request
encoding, and error mapping deliberately stay outside this module.
"""

from __future__ import annotations

import asyncio
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
        self._drain = drain_tracker
        self._max_concurrent_rpcs = max_concurrent_rpcs
        self._rpc_semaphore: asyncio.Semaphore | None = None
        self._monotonic = time.perf_counter if monotonic is None else monotonic
        self._current: AdmissionGeneration | None = None
        self._retired: dict[int, AdmissionGeneration] = {}
        self._last_epoch = 0
        self._settlement_tasks: set[asyncio.Task[None]] = set()

    @property
    def drain_tracker(self) -> TransportDrainTracker:
        """Return the owned bookkeeping implementation for lifecycle migration."""
        return self._drain

    @property
    def bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the loop used by polling and lifecycle-affinity checks."""
        return self._bound_loop

    def get_bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the historical loop binding through a method-shaped seam."""
        return self._bound_loop

    @property
    def max_concurrent_rpcs(self) -> int | None:
        """Return the configured client-wide transport-call cap."""
        return self._max_concurrent_rpcs

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Bind both supervisor-owned primitives to ``loop``."""
        super().set_bound_loop(loop)
        self._drain.set_bound_loop(loop)

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        self._rpc_semaphore = None

    def reset_after_open(self) -> None:
        """Transitional adapter for the pre-B0b web lifecycle.

        B0b calls ``prepare_generation`` and ``start_accepting`` explicitly.
        The current web lifecycle has no epoch allocator yet, so this adapter
        retires its previous generation and creates the next integer epoch.
        """
        if self._current is not None:
            self.mark_closed(self._current.epoch)
        epoch = self._last_epoch + 1
        self.prepare_generation(epoch)
        self._drain.reset_after_open()
        self.start_accepting(epoch)

    def assert_bound_loop(self) -> None:
        """Fail before touching a primitive owned by a different event loop."""
        assert_bound_loop(self._bound_loop)

    def record_started(self, method: str | None) -> None:
        """Count one logical RPC start; non-RPC calls remain uncounted."""
        if method is not None:
            self._metrics.increment(rpc_calls_started=1)

    def prepare_generation(self, epoch: int) -> None:
        """Allocate a closed admission record for ``epoch``.

        This method is synchronous so the root lifecycle can include it in its
        checkpoint-free open preparation/commit sequence.  No resource is
        admitted until :meth:`start_accepting` runs.
        """
        self.assert_bound_loop()
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
        generation = AdmissionGeneration(epoch=epoch, loop=loop)
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
            await asyncio.wait_for(
                generation.condition.wait_for(lambda: generation.in_flight == 0),
                timeout=timeout,
            )

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

    async def _admit(self, label: str) -> _AdmissionToken:
        self.assert_bound_loop()
        task = asyncio.current_task()
        generation = self._current
        if generation is None:
            raise RuntimeError(
                f"Client not initialized or NotebookLMClient is not accepting operations ({label})."
            )
        self._assert_generation_loop(generation)
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
            drain_token = await self._drain.begin_transport_post(label)
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
            await asyncio.wait_for(semaphore.acquire(), timeout=deadline.remaining())
        return semaphore

    def _retain_settlement(
        self,
        settle: Coroutine[Any, Any, None],
        *,
        epoch: int,
    ) -> tuple[asyncio.Task[None], _SettlementState]:
        """Publish one strongly retained settlement awaitable."""
        state = _SettlementState()
        loop = asyncio.get_running_loop()
        task: asyncio.Task[None] = loop.create_task(
            settle,
            name=f"notebooklm-settle-{epoch}",
        )
        self._settlement_tasks.add(task)

        def _done(done: asyncio.Task[None]) -> None:
            self._settlement_tasks.discard(done)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is not None and state.abandoned:
                loop.call_exception_handler(
                    {
                        "message": "NotebookLM admission settlement failed after caller unwind",
                        "exception": exc,
                        "task": done,
                    }
                )

        task.add_done_callback(_done)
        return task, state

    def _publish_partial_settlement(
        self,
        *,
        generation: AdmissionGeneration,
        task: asyncio.Task[Any] | None,
        child: asyncio.Task[Any] | None = None,
    ) -> tuple[asyncio.Task[None], _SettlementState]:
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
    ) -> tuple[asyncio.Task[None], _SettlementState]:
        async def _settle() -> None:
            try:
                await self._drain.finish_transport_post(token.drain_token)
            finally:
                try:
                    await self._finish_generation_token(token.generation, token.task)
                finally:
                    if queue_wait is not None:
                        self._metrics.record_rpc_queue_wait(queue_wait)

        return self._retain_settlement(_settle(), epoch=token.generation.epoch)

    async def _await_settlement(
        self,
        task: asyncio.Task[None],
        state: _SettlementState,
        *,
        cancellation_already_active: bool,
    ) -> None:
        first_cancel: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(task)
                if first_cancel is not None:
                    raise first_cancel
                return
            except asyncio.CancelledError as exc:
                if cancellation_already_active:
                    state.abandoned = True
                    raise
                if first_cancel is not None:
                    state.abandoned = True
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
    ) -> AsyncIterator[CallLease]:
        """Admit one logical call and apply metrics/semaphore policy."""
        token = await self._admit(label)
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
    ) -> _T:
        """Run a lazily-created unary invocation inside :meth:`call_scope`."""
        async with self.call_scope(label, method, deadline) as lease:
            return await invoke(lease)

    @asynccontextmanager
    async def operation_scope(self, label: str) -> AsyncIterator[OperationLease]:
        """Hold admission across a complete multi-call workflow."""
        token = await self._admit(label)
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
        token: _AdmissionToken | None = None

        async def _wrapper() -> _T:
            await gate
            assert token is not None
            body_error: BaseException | None = None
            try:
                return await factory()
            except BaseException as exc:
                body_error = exc
                raise
            finally:
                settlement, state = self._publish_settlement(
                    token=token,
                    queue_wait=None,
                )
                try:
                    await self._await_settlement(
                        settlement,
                        state,
                        cancellation_already_active=isinstance(body_error, asyncio.CancelledError),
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
            drain_token = await self._drain.begin_transport_task(task, label)
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
        return task

    async def drain(self, timeout: float | None = None) -> None:
        """Stop accepting top-level work and wait for every admitted token."""
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {timeout!r}")
        generation = self._current
        if generation is None:
            return
        await self.stop_accepting(generation.epoch)
        await asyncio.gather(
            self.wait_for_idle(generation.epoch, timeout),
            self._drain.drain(timeout=timeout),
        )

    def register_drain_hook(self, name: str, hook: Callable[[], Awaitable[None]]) -> None:
        """Register a feature-owned close-time hook on the owned tracker."""
        self._drain.register_drain_hook(name, hook)

    async def run_drain_hooks(self) -> None:
        """Run feature hooks in registration order."""
        await self._drain.run_drain_hooks()


__all__ = ["CallLease", "CallSupervisor", "OperationLease"]
