"""Transport drain bookkeeping helper for the NotebookLM client runtime.

Owns the in-flight transport-operation counters, the lazy
``asyncio.Condition`` that ``drain()`` parks on, the per-``asyncio.Task``
operation-depth map, and the ``_draining`` flag. The drain surface has
one home (this file) instead of being woven into the runtime composition root
alongside metrics, reqid, and auth state.

Design constraints (load-bearing — see
``tests/unit/concurrency/test_close_cancellation_leak.py``,
``tests/unit/test_session_close.py``, and ``tests/unit/test_observability.py``):

* ``__init__`` MUST be event-loop-agnostic. ``NotebookLMClient`` is routinely
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

Field names (``_in_flight_posts``, ``_draining``, ``_drain_condition``,
``_operation_depths``) are kept stable for grep-discoverability across the
test suite; the drain bookkeeping needs each of them.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from ._loop_affinity import assert_bound_loop
from ._loop_bound import LoopBoundPrimitive

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TransportOperationToken:
    """Token for one accepted transport operation on a specific asyncio task.

    Returned from :meth:`TransportDrainTracker.begin_transport_post` /
    :meth:`TransportDrainTracker.begin_transport_task` and consumed by
    :meth:`TransportDrainTracker.finish_transport_post`. The ``task`` field
    is the ``asyncio.Task`` whose operation depth was bumped on admission
    (or ``None`` for the unusual case of a begin issued outside any task).

    Frozen so token equality is by value.
    """

    task: asyncio.Task[Any] | None


class TransportDrainTracker(LoopBoundPrimitive):
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
        # ``_bound_loop`` (the loop-affinity guard consulted by
        # :meth:`drain` / :meth:`begin_transport_post` before touching the
        # lazy ``_drain_condition``) is provided by the
        # :class:`~notebooklm._loop_bound.LoopBoundPrimitive` base, which also
        # owns ``set_bound_loop``. This tracker only stores the binding, so it
        # uses the default no-op ``_on_loop_rebind``.
        # ADR-0014 Rule 1: close-time drain hooks are owned here, not on
        # the client. Insertion order is preserved (Python 3.7+ dict
        # invariant) and :meth:`run_drain_hooks` fires them in that order
        # under ``ClientLifecycle.close``.
        self._drain_hooks: dict[str, Callable[[], Awaitable[None]]] = {}

    def reset_after_open(self) -> None:
        """Clear the drain flag so a reopened client admits new transport work.

        Called from :meth:`ClientLifecycle.open` (immediately after the
        per-collaborator ``set_bound_loop`` propagation and before the
        ``Kernel.open`` await) so a previously-drained-then-reopened client
        admits new top-level operations again. Encapsulates the legacy
        direct write ``host._drain_tracker._draining = False`` so the
        lifecycle never touches the private ``_draining`` field on this
        collaborator.

        Deliberately narrow: this resets ONLY the ``_draining`` flag. The
        ``_in_flight_posts`` counter, ``_operation_depths`` map, and lazily
        bound ``_drain_condition`` are left untouched — clearing those would
        break the load-bearing in-flight bookkeeping invariants asserted by
        ``tests/unit/test_observability.py::test_drain_allows_nested_work_inside_accepted_operation``
        and ``tests/unit/concurrency/test_close_cancellation_leak.py``.
        Field-level locking is intentionally not used here: the legacy
        direct write was an unlocked assignment, asyncio is single-threaded,
        and the assignment is atomic in CPython — adding a condition acquire
        would only serialise a no-op against in-flight transport begins.
        """
        self._draining = False

    def get_drain_condition(self) -> asyncio.Condition:
        """Return the per-instance drain ``asyncio.Condition``, creating it lazily.

        Lazy construction is required because ``asyncio.Condition()`` binds
        to the running event loop in some Python versions, and ``NotebookLMClient``
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

        Catch cross-loop admission *before* touching the lazy
        ``_drain_condition``. The condition is loop-bound on first
        ``get_drain_condition`` — a cross-loop call would either silently
        bind it to the wrong loop or hang on ``async with condition``
        against a primitive belonging to the originally-bound loop.
        Mirrors the existing guard in :meth:`drain` so both admission
        and shutdown paths surface the same diagnostic.
        """
        assert_bound_loop(self._bound_loop)
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

    @asynccontextmanager
    async def operation_scope(self, label: str) -> AsyncIterator[None]:
        """Drain-tracked operation scope for feature-owned work (ADR-0014 Rule 1).

        Wraps :meth:`begin_transport_post` / :meth:`finish_transport_post`
        so feature code can write ``async with tracker.operation_scope("upload"):``
        without managing the token by hand. Satisfies the
        ``_artifact.polling.OperationScopeProvider`` Protocol directly
        (inlined into that module in issue #1327).
        """
        token = await self.begin_transport_post(label)
        try:
            yield None
        finally:
            await self.finish_transport_post(token)

    def register_drain_hook(self, name: str, hook: Callable[[], Awaitable[None]]) -> None:
        """Register or replace a feature-owned close-time drain hook.

        Per ADR-0014 Rule 1, this tracker owns the drain-hook storage so
        ``DrainHookRegistration`` is satisfied directly.
        ``ClientLifecycle.close`` fires registered hooks via
        :meth:`run_drain_hooks`.
        """
        self._drain_hooks[name] = hook

    async def run_drain_hooks(self) -> None:
        """Fire every registered drain hook in registration order.

        Called from two sites during a graceful ``close(drain=True)``: first
        from ``NotebookLMClient.close`` *before* the drain wait (so a poll
        counted in ``operation_scope`` is cancelled-and-settled rather than
        blocking ``drain()`` — issue #1161), then again from
        ``ClientLifecycle.close`` after the auth-refresh task has been
        cancelled and before the HTTP client is shut down. Hooks must
        therefore tolerate being re-run; the production poll hook
        (``artifacts.polls``) is a no-op on the second run because
        already-settled poll tasks are filtered out of
        ``PollRegistry.active_tasks``. The ``drain=False`` and lifecycle-only
        paths invoke it just once.

        Exceptions in individual hooks are caught and logged via
        ``logger.warning`` (with the registration ``name`` so operators can
        identify the misbehaving feature), then suppressed so a single
        misbehaving hook cannot block the shutdown path. The hooks fire
        concurrently via ``asyncio.gather(..., return_exceptions=True)`` —
        the per-hook log happens after the gather completes.
        """
        named_hooks = list(self._drain_hooks.items())
        if not named_hooks:
            return
        results = await asyncio.gather(
            *(hook() for _name, hook in named_hooks),
            return_exceptions=True,
        )
        for (name, _hook), result in zip(named_hooks, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Drain hook %r raised during close: %s", name, result, exc_info=result
                )

    async def drain(self, timeout: float | None = None) -> None:
        """Stop accepting new top-level work and wait for in-flight ops to finish.

        If ``timeout`` expires, ``TimeoutError`` is raised and the
        tracker remains in draining mode so shutdown callers do not
        accidentally admit new work after a missed deadline.
        """
        # Catch cross-loop drain before touching ``_drain_condition``.
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
