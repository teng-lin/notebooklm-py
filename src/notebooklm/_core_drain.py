"""Transport drain bookkeeping helper for :class:`Session`.

Owns the in-flight transport-operation counters, the lazy
``asyncio.Condition`` that ``drain()`` parks on, the per-``asyncio.Task``
operation-depth map, and the ``_draining`` flag. Lifted out of ``_core.py``
so the drain surface has one home (this file) instead of being woven into
``Session.__init__`` alongside metrics, reqid, and auth state.

Design constraints (load-bearing — see
``tests/unit/concurrency/test_close_cancellation_leak.py``,
``tests/unit/test_core_close.py``, and ``tests/unit/test_observability.py``):

* ``__init__`` MUST be event-loop-agnostic. ``Session`` is routinely
  constructed outside a running loop (sync-mode
  ``NotebookLMClient(auth)`` before ``asyncio.run``), so this helper may
  not call ``asyncio.get_running_loop()`` or instantiate any ``asyncio.*``
  primitive at construction time. The ``asyncio.Condition`` is created
  lazily on first :meth:`get_drain_condition` call from inside a running
  loop.

* :meth:`drain` blocks on ``self._drain_condition`` while
  ``_in_flight_posts > 0``. The ``Condition.wait_for`` pattern is the
  whole point of the helper — never replace it with a poll loop.

* :meth:`begin_transport_post` rejects new top-level work once
  ``_draining`` is set, but allows nested begins from a task whose
  outer operation was admitted *before* the drain started (depth > 0).
  This is what ``test_drain_allows_nested_work_inside_accepted_operation``
  pins down; the depth bookkeeping under the condition lock is exactly
  what stops the close path from deadlocking.

* Exceptions during :meth:`finish_transport_post` would orphan a
  counter and stall ``drain`` forever — keep the body trivial and
  fully inside the ``async with condition`` block.

Field names are deliberately the same as the legacy ``Session`` ivars
(``_in_flight_posts``, ``_draining``, ``_drain_condition``) so the
surviving compat ``@property`` bridges on ``Session`` can delegate via
``return self._drain_tracker._<attr>`` and stay readable. The
``_operation_depths`` compat bridge on ``Session`` was dropped in
D1-audit-full once its callers migrated; the field itself remains on
``TransportDrainTracker`` because the drain bookkeeping needs it.
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from typing import Any

from ._loop_affinity import assert_bound_loop


@dataclass(frozen=True)
class _TransportOperationToken:
    """Token for one accepted transport operation on a specific asyncio task.

    Returned from :meth:`TransportDrainTracker.begin_transport_post` /
    :meth:`TransportDrainTracker.begin_transport_task` and consumed by
    :meth:`TransportDrainTracker.finish_transport_post`. The ``task`` field
    is the ``asyncio.Task`` whose operation depth was bumped on admission
    (or ``None`` for the unusual case of a begin issued outside any task).

    Re-exported from ``notebooklm._core`` for backwards compatibility —
    keep this dataclass frozen so token equality is by value.
    """

    task: asyncio.Task[Any] | None


class TransportDrainTracker:
    """Track in-flight transport operations and gate graceful shutdown.

    Owns four pieces of state:

    * ``_in_flight_posts`` — count of currently-running transport
      operations across all tasks. Mutated only inside the
      ``async with condition`` block.
    * ``_draining`` — set ``True`` by :meth:`drain`; new top-level
      begins raise ``RuntimeError`` once this is set.
    * ``_drain_condition`` — lazily-created ``asyncio.Condition``
      that ``drain`` parks on; ``finish_transport_post`` notifies
      it when ``_in_flight_posts`` drops to zero. ``None`` until the
      first :meth:`get_drain_condition` call from inside a loop.
    * ``_operation_depths`` —
      ``weakref.WeakKeyDictionary[asyncio.Task, int]`` tracking per-task
      operation depth so nested begins (e.g. an RPC issued from inside
      a source-upload operation) don't get rejected after ``drain``
      starts.
    """

    def __init__(self) -> None:
        self._in_flight_posts: int = 0
        self._draining: bool = False
        # Lazily-created from inside a running loop — see module docstring.
        self._drain_condition: asyncio.Condition | None = None
        # Weak references so a finished task doesn't keep its depth entry
        # alive forever; the entry is also explicitly popped in
        # ``finish_transport_post`` once depth returns to zero.
        self._operation_depths: weakref.WeakKeyDictionary[asyncio.Task[Any], int] = (
            weakref.WeakKeyDictionary()
        )
        # P0-2: loop-affinity guard. Set by :meth:`ClientLifecycle.open`
        # so :meth:`drain` can short-circuit cross-loop misuse before
        # touching the lazily-built ``_drain_condition`` (which is bound
        # to the loop that constructed it). ``None`` is a silent no-op
        # for standalone fixtures.
        self._bound_loop: asyncio.AbstractEventLoop | None = None

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Capture or clear the event-loop binding for the affinity guard.

        :meth:`ClientLifecycle.open` propagates the captured loop here so
        :meth:`drain` can short-circuit cross-loop misuse. Passing ``None``
        clears the binding for the next ``open()`` (which will rebind).
        """
        self._bound_loop = loop

    def get_drain_condition(self) -> asyncio.Condition:
        """Return the per-instance drain ``asyncio.Condition``, creating it lazily.

        Lazy construction is required because ``asyncio.Condition()`` binds
        to the running event loop in some Python versions, and ``Session``
        is routinely instantiated outside one. The check-then-assign is
        race-free without an outer lock because asyncio is single-threaded:
        no other coroutine can execute between the ``is None`` check and
        the assignment unless we ``await`` (and we don't).
        """
        if self._drain_condition is None:
            self._drain_condition = asyncio.Condition()
        return self._drain_condition

    def current_operation_depth(self, task: asyncio.Task[Any] | None) -> int:
        """Return how many transport operations ``task`` currently holds."""
        if task is None:
            return 0
        return self._operation_depths.get(task, 0)

    async def begin_transport_post(self, log_label: str) -> _TransportOperationToken:
        """Reject new top-level transport work once graceful drain has started.

        Nested begins from a task with depth > 0 are still accepted — this
        is what lets an in-flight source upload finish its sub-RPCs after
        ``drain()`` starts. See
        ``tests/unit/test_observability.py::test_drain_allows_nested_work_inside_accepted_operation``.
        """
        condition = self.get_drain_condition()
        task = asyncio.current_task()
        depth = self.current_operation_depth(task)
        async with condition:
            if self._draining and depth == 0:
                raise RuntimeError(
                    "NotebookLMClient is draining; new client operations are not accepted "
                    f"({log_label})."
                )
            if task is not None:
                self._operation_depths[task] = depth + 1
            self._in_flight_posts += 1
        return _TransportOperationToken(task=task)

    async def begin_transport_task(
        self,
        task: asyncio.Task[Any],
        log_label: str,
    ) -> _TransportOperationToken:
        """Admit an internally-spawned task as part of the current operation.

        Unlike :meth:`begin_transport_post`, the admission gate keys on
        ``asyncio.current_task()`` (the *spawning* task's depth) rather
        than ``task`` (the spawned task). That way a child task spawned
        from inside an admitted operation inherits its parent's
        "admitted" status, but a child task spawned from outside any
        operation (depth 0 on the spawner) is rejected once ``_draining``.
        """
        condition = self.get_drain_condition()
        current_depth = self.current_operation_depth(asyncio.current_task())
        async with condition:
            if self._draining and current_depth == 0:
                raise RuntimeError(
                    "NotebookLMClient is draining; new client operations are not accepted "
                    f"({log_label})."
                )
            self._operation_depths[task] = self._operation_depths.get(task, 0) + 1
            self._in_flight_posts += 1
        return _TransportOperationToken(task=task)

    async def finish_transport_post(self, token: _TransportOperationToken) -> None:
        """Decrement the in-flight counter and notify waiters at zero.

        The notify wakes ``drain()`` once the last admitted operation
        finishes. If this method raised, ``_in_flight_posts`` would
        stay above zero and ``drain`` would block forever — keep the
        body trivial.
        """
        condition = self.get_drain_condition()
        async with condition:
            if token.task is not None:
                depth = self._operation_depths.get(token.task, 0)
                if depth <= 1:
                    self._operation_depths.pop(token.task, None)
                else:
                    self._operation_depths[token.task] = depth - 1
            self._in_flight_posts -= 1
            if self._in_flight_posts == 0:
                condition.notify_all()

    async def drain(self, timeout: float | None = None) -> None:
        """Stop accepting new top-level work and wait for in-flight ops to finish.

        If ``timeout`` expires, ``TimeoutError`` is raised and the
        tracker remains in draining mode so shutdown callers do not
        accidentally admit new work after a missed deadline.
        """
        # P0-2: catch cross-loop drain before touching ``_drain_condition``.
        # The condition is lazily bound to the loop that first awaited
        # ``get_drain_condition`` — a cross-loop call would hang on
        # ``async with condition`` if we let it through.
        assert_bound_loop(self._bound_loop)
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {timeout!r}")
        condition = self.get_drain_condition()
        async with condition:
            self._draining = True
            if self._in_flight_posts == 0:
                return
            await asyncio.wait_for(
                condition.wait_for(lambda: self._in_flight_posts == 0),
                timeout=timeout,
            )


__all__ = ["TransportDrainTracker", "_TransportOperationToken"]
