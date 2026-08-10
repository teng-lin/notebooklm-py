"""Cross-loop single-flight coalescing + a per-path success epoch.

This module FORMALIZES the cross-loop coalescing pattern that used to live,
hand-rolled and duplicated, in :mod:`notebooklm._auth.refresh` (per-loop
future map + ``_REFRESH_GENERATIONS`` counter) and
:mod:`notebooklm._auth.recovery` (per-loop in-flight task maps + settle loop).
It is **not** an extraction from ``AuthRefreshCoordinator``: that coordinator
is loop-bound by ADR-0004 contract, hands followers a shared ``asyncio.Task``,
and has its own pinned task-identity/slot invariants — it is deliberately left
untouched (plan §c.7).

Two facilities, both process-global:

* **Flight registry** — one in-flight operation shared per ``flight_key``. A
  *leader* drives the work as an ``asyncio.Task`` on its own loop (held in a
  strong-ref set so the asyncio GC cannot collect it) and mirrors completion
  into a :class:`concurrent.futures.Future`. *Followers* on ANY event loop
  bridge to that future via ``asyncio.shield(asyncio.wrap_future(f))`` with a
  settle-before-propagate loop, so a single follower's cancellation cannot
  detonate its siblings and a cross-loop caller never has to run the leader's
  coroutine itself (``run_coroutine_threadsafe`` is deliberately ruled out).

* **Success epoch** — one process-global counter per canonical storage PATH.
  It relocates the ``_REFRESH_GENERATIONS`` semantics from ``refresh.py``: a
  caller captures the epoch *before* it waits, and after its coalesced work
  either succeeds (the worker bumps the epoch) or fails (no bump, so waiters
  retry). A late waiter whose captured epoch is already stale skips its own
  subprocess and just reloads the freshly-written storage — and via
  :func:`claim_if_epoch_current` that epoch compare and the flight claim happen
  under a SINGLE lock hold (compare-under-exclusion), so a sibling that bumps
  and prompt-pops its flight in between can never trick the waiter into spawning
  a redundant second subprocess.

Threading discipline: each owner has one :class:`threading.Lock` (``_lock``)
that guards ONLY the brief synchronous claim/registry/epoch mutations. It is
NEVER held across an ``await`` or a subprocess.
``concurrent.futures.Future`` completion is set from the leader's loop (its
task done-callback) and is safe to observe from any other loop through
``asyncio.wrap_future``.

Value-free retention: the registry stores only flight bookkeeping and integer
epochs. A flight's *result* (e.g. cold recovery's jar-bearing
``ColdRecoveryResult``) rides the per-flight future and is dropped from the
registry the moment the flight settles (prompt-pop); it is never copied into a
long-lived registry structure, so no new credential-lifetime surface appears.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import threading
from collections.abc import Callable, Coroutine, Hashable
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

logger = logging.getLogger("notebooklm.auth")

T = TypeVar("T")

# A flight is keyed per (canonical storage path, rung policy) — matching the
# shape of the registries this core replaces (recovery keyed on
# ``(path, allow_headless)``; refresh keyed on the path plus its single
# refresh-cmd policy). The policy component is any hashable discriminator.
FlightKey = tuple[str, Hashable]


@dataclass
class Flight(Generic[T]):
    """One in-flight operation shared across event loops.

    ``bridge`` is a :class:`concurrent.futures.Future` mirroring the leader
    task's terminal state; followers on any loop await it via
    ``asyncio.wrap_future``. ``task`` is the leader's ``asyncio.Task`` (bound to
    the leader's loop) and is populated immediately after construction, under
    the registry lock, before any follower can observe the flight.
    """

    bridge: concurrent.futures.Future[T] = field(default_factory=concurrent.futures.Future)
    task: asyncio.Task[T] | None = None


class SingleFlight:
    """Own one cross-loop flight registry and its path success epochs."""

    _process_default_owner: ClassVar[SingleFlight]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[FlightKey, Flight[Any]] = {}
        self._leader_tasks: set[asyncio.Task[Any]] = set()
        self._success_epochs: dict[str, int] = {}

    @classmethod
    def process_default(cls) -> SingleFlight:
        """Return the identity-stable process owner used by production."""
        return cls._process_default_owner

    def read_success_epoch(self, path_key: str) -> int:
        with self._lock:
            return self._success_epochs.get(path_key, 0)

    def note_success(self, path_key: str) -> None:
        with self._lock:
            self._success_epochs[path_key] = self._success_epochs.get(path_key, 0) + 1

    def claim(
        self,
        flight_key: FlightKey,
        factory: Callable[[], Coroutine[Any, Any, T]],
    ) -> tuple[bool, Flight[T]]:
        is_leader, flight = self._claim(
            flight_key,
            factory,
            path_key=None,
            epoch_before=None,
        )
        assert flight is not None
        return is_leader, flight

    def claim_if_epoch_current(
        self,
        flight_key: FlightKey,
        factory: Callable[[], Coroutine[Any, Any, T]],
        *,
        path_key: str,
        epoch_before: int,
    ) -> tuple[bool, Flight[T]] | None:
        is_leader, flight = self._claim(
            flight_key,
            factory,
            path_key=path_key,
            epoch_before=epoch_before,
        )
        if flight is None:
            return None
        return is_leader, flight

    def _claim(
        self,
        flight_key: FlightKey,
        factory: Callable[[], Coroutine[Any, Any, T]],
        *,
        path_key: str | None,
        epoch_before: int | None,
    ) -> tuple[bool, Flight[T] | None]:
        loop = asyncio.get_running_loop()
        with self._lock:
            if (
                path_key is not None
                and epoch_before is not None
                and self._success_epochs.get(path_key, 0) > epoch_before
            ):
                return False, None
            existing = self._flights.get(flight_key)
            if existing is not None and not existing.bridge.done():
                return False, existing
            flight = Flight[T]()
            task = loop.create_task(factory())
            flight.task = task
            self._flights[flight_key] = flight
            self._leader_tasks.add(task)

        def _on_done(settled: asyncio.Task[T]) -> None:
            with self._lock:
                self._leader_tasks.discard(settled)
            self._mirror(settled, flight)
            self._pop(flight_key, flight)
            with contextlib.suppress(BaseException):
                settled.exception()

        task.add_done_callback(_on_done)
        return True, flight

    @staticmethod
    def _mirror(task: asyncio.Task[Any], flight: Flight[Any]) -> None:
        bridge = flight.bridge
        if bridge.done():
            return
        if task.cancelled():
            bridge.set_exception(asyncio.CancelledError())
            return
        exc = task.exception()
        if exc is not None:
            bridge.set_exception(exc)
        else:
            bridge.set_result(task.result())

    def _pop(self, flight_key: FlightKey, flight: Flight[Any]) -> None:
        with self._lock:
            if self._flights.get(flight_key) is flight:
                del self._flights[flight_key]

    async def await_flight(self, flight: Flight[T]) -> T:
        wrapped = asyncio.wrap_future(flight.bridge)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            while not flight.bridge.done():
                try:
                    await asyncio.shield(wrapped)
                except asyncio.CancelledError:
                    if flight.bridge.done():
                        break
                    continue
                except BaseException:  # noqa: BLE001 - settle before propagating
                    break

            def _drain(fut: asyncio.Future[Any]) -> None:
                if not fut.cancelled():
                    with contextlib.suppress(BaseException):
                        fut.result()

            if wrapped.done():
                _drain(wrapped)
            else:
                wrapped.add_done_callback(_drain)
            raise

    def _reset_for_tests(self) -> None:
        with self._lock:
            if self._flights or self._leader_tasks:
                raise RuntimeError("cannot reset SingleFlight with live work")
            self._flights.clear()
            self._leader_tasks.clear()
            self._success_epochs.clear()


SingleFlight._process_default_owner = SingleFlight()


def read_success_epoch(path_key: str) -> int:
    return SingleFlight.process_default().read_success_epoch(path_key)


def note_success(path_key: str) -> None:
    return SingleFlight.process_default().note_success(path_key)


def claim(
    flight_key: FlightKey, factory: Callable[[], Coroutine[Any, Any, T]]
) -> tuple[bool, Flight[T]]:
    return SingleFlight.process_default().claim(flight_key, factory)


def claim_if_epoch_current(
    flight_key: FlightKey,
    factory: Callable[[], Coroutine[Any, Any, T]],
    *,
    path_key: str,
    epoch_before: int,
) -> tuple[bool, Flight[T]] | None:
    return SingleFlight.process_default().claim_if_epoch_current(
        flight_key,
        factory,
        path_key=path_key,
        epoch_before=epoch_before,
    )


async def await_flight(flight: Flight[T]) -> T:
    return await SingleFlight.process_default().await_flight(flight)


def _reset_for_tests() -> None:
    return SingleFlight.process_default()._reset_for_tests()
