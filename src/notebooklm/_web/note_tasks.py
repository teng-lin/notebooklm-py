"""Drain-settled registry for Web note finalize and cleanup tasks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .._runtime.call_supervisor import CallSupervisor

_T = TypeVar("_T")


class NoteTaskRegistry:
    """Own same-generation note children until completion or forced teardown.

    ``CallSupervisor.spawn_child`` closes the eager-task admission race, while
    this registry closes the smaller publication race between the awaited
    spawn and attaching the returned task. During that window the spawning
    parent is kept as a reservation, so a drain-free close can cancel and
    settle it instead of letting an unpublished child reach a closed Web
    transport.
    """

    def __init__(self, supervisor: CallSupervisor) -> None:
        self._supervisor = supervisor
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reservations: set[asyncio.Task[Any]] = set()

    async def spawn(
        self,
        label: str,
        factory: Callable[[], Awaitable[_T]],
    ) -> asyncio.Task[_T]:
        """Spawn one admitted child and publish it without an await gap."""
        parent = asyncio.current_task()
        if parent is None:  # pragma: no cover - asyncio always supplies one here
            raise RuntimeError(f"note child work requires a current task ({label})")
        self._reservations.add(parent)
        try:
            task = await self._supervisor.spawn_child(label, factory)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return task
        finally:
            self._reservations.discard(parent)

    def active_tasks(self) -> list[asyncio.Task[Any]]:
        """Return unpublished spawners and published children still running."""
        current = asyncio.current_task()
        active = [task for task in self._tasks if task is not current and not task.done()]
        active.extend(
            task for task in self._reservations if task is not current and not task.done()
        )
        return list(dict.fromkeys(active))

    async def drain(self) -> None:
        """Settle registered work when the root has entered forced teardown.

        The root also runs hooks during the graceful ``DRAINING`` prephase.
        Admitted note children already hold supervisor tokens, so that pass
        must leave them running and let ``wait_for_idle`` observe their natural
        completion. The later ``CLOSING`` pass (or the only pass for
        ``drain=False``) cancels and gathers both published children and
        reservation parents, closing the spawn-publication race before Web
        transport resources are released.
        """
        if not self._supervisor.is_closing():
            return
        while tasks := self.active_tasks():
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["NoteTaskRegistry"]
