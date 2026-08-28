"""Request-id counter helper for the NotebookLM client runtime.

Owns the monotonic ``_reqid`` value that Google's chat backend requires per
request, plus the lazily-allocated ``asyncio.Lock`` that serialises the
read-modify-write under concurrent ``ChatAPI.ask`` callers. The reqid
surface has one home (this file) instead of being woven into the runtime
composition root alongside metrics, drain, and auth state.

Design constraints (load-bearing — see ``tests/unit/test_web/transport/reqid_counter.py`` and
``tests/unit/test_session_reqid_concurrent.py``):

* ``__init__`` MUST be event-loop-agnostic — it must NOT instantiate
  ``asyncio.Lock()`` eagerly. ``NotebookLMClient`` is routinely built outside a
  running loop (sync-mode ``NotebookLMClient(...)`` before the caller's
  ``asyncio.run``), and ``asyncio.Lock()`` binds to the running loop on
  construction in some Python versions. The lock is therefore allocated
  lazily inside :meth:`next_reqid`, which is always called from an async
  context.

* Baseline ``_value`` is ``100000`` and the default ``step`` is ``100000``.
  Both are part of the chat-API contract (the per-request ``_reqid`` URL
  parameter must be a large positive integer); do NOT change them.

* :meth:`next_reqid` rejects ``bool`` and non-positive ``step`` explicitly so
  ``next_reqid(step=True)`` cannot silently degrade to ``step=1`` (``bool``
  is a subclass of ``int`` in Python) and ``step=0`` / ``step<0`` cannot
  break the chat-side uniqueness / monotonicity guarantees.

* Optional ``on_lock_wait`` callback receives the seconds spent blocked on
  :attr:`_lock`. Decouples the counter from
  :class:`notebooklm._client_metrics.ClientMetrics` so this class is unit-
  testable in isolation; the runtime-init helper wires it up to
  ``ClientMetrics.record_lock_wait`` at construction (see
  ``_runtime.init.build_collaborators``).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from ..._loop_affinity import assert_bound_loop
from ..._loop_bound import LoopBoundPrimitive

# Baseline counter value (matches the chat-API expectation of a large positive
# integer). Module-level constant so tests / future callers can reference it
# instead of hard-coding ``100000``.
DEFAULT_BASELINE: int = 100000
# Default step applied by :meth:`ReqidCounter.next_reqid`. Matches the
# historical bump that ``ChatAPI.ask`` performed.
DEFAULT_STEP: int = 100000


def _noop_record_lock_wait(_wait_seconds: float) -> None:
    """Default ``on_lock_wait`` — does nothing.

    Used when the counter is constructed standalone (e.g. in
    ``tests/unit/test_web/transport/reqid_counter.py``) without a metrics sink wired up.
    """


class ReqidCounter(LoopBoundPrimitive):
    """Monotonic request-id counter with lazy ``asyncio.Lock`` serialisation.

    The accessor surface is ``self._reqid._value`` and
    ``self._reqid._lock``; the field names are kept stable for direct
    access from unit-test fixtures.
    """

    def __init__(
        self,
        *,
        baseline: int = DEFAULT_BASELINE,
        on_lock_wait: Callable[[float], None] | None = None,
    ) -> None:
        # Plain int; mutated only inside :meth:`next_reqid` under ``_lock`` or
        # via direct ``self._reqid._value = …`` writethrough from test fixtures
        # that want to seed the counter to a deterministic baseline.
        self._value: int = baseline
        # Lazily-created — ``asyncio.Lock()`` needs a running loop in some
        # Python versions, and this object is constructed at client
        # composition time (``NotebookLMClient.__init__``), which may run
        # outside a loop.
        self._lock: asyncio.Lock | None = None
        # No-op default keeps standalone construction (unit tests) free of a
        # client-shaped dependency. ``NotebookLMClient`` injects its own
        # metrics-aware recorder so lock-wait latency continues to be tracked
        # in the cumulative ``ClientMetricsSnapshot``.
        self._on_lock_wait: Callable[[float], None] = (
            on_lock_wait if on_lock_wait is not None else _noop_record_lock_wait
        )
        # ``_bound_loop`` (the loop-affinity guard consulted by
        # :meth:`next_reqid` before touching the lazy ``_lock``) and
        # ``set_bound_loop`` are provided by the
        # :class:`~notebooklm._loop_bound.LoopBoundPrimitive` base. The
        # counter overrides ``_on_loop_rebind`` (and exposes
        # ``reset_after_open``) to discard the lazy ``Lock`` on a loop
        # change / reopen — the lock is NOT rebuilt implicitly per
        # ``open()``; once cached it survives close→reopen and would be
        # reused on the new loop (#2106). ``_value`` always survives so
        # reqid monotonicity holds across reopen.

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Discard the cached lazy lock when the bound loop changes.

        Fires from :meth:`~notebooklm._loop_bound.LoopBoundPrimitive.set_bound_loop`
        only on a real loop change (and before ``_bound_loop`` is updated), so
        ``set_bound_loop`` is self-consistent even if called independently of
        :meth:`reset_after_open`: a stale lock bound to the old loop must
        never be reused after a rebind.

        Currently a latent (not active) hazard (#2106): the critical section
        in :meth:`next_reqid` is purely synchronous, and ``asyncio.Lock``
        binds its loop only on the *contended* acquire path, so a stale lock
        cannot actually trip the cross-loop ``RuntimeError`` today. The
        discard brings the counter in line with its clear-on-rebind siblings
        so any future ``await`` under the lock cannot activate the trap.

        ``_value`` is deliberately preserved — reqid monotonicity must
        survive close→reopen (Google's chat backend relies on strictly
        increasing ``_reqid`` values per client).
        """
        self._lock = None

    def reset_after_open(self) -> None:
        """Discard the lazy lock so a reopened client rebinds it.

        Called from :meth:`ClientLifecycle.open` (alongside the
        per-collaborator ``set_bound_loop`` propagation) so a client that was
        closed and reopened on a *different* event loop builds a fresh
        ``asyncio.Lock`` on the new loop instead of reusing the stale one
        bound to the old (now-dead) loop. On Python 3.10/3.11 a stale lock
        raises "bound to a different event loop" on the contended acquire
        path; today that path is unreachable here (no ``await`` runs under
        the lock — see :meth:`_on_loop_rebind`), so this is a consistency
        hardening, not an active-bug fix (#2106).

        Mirrors :meth:`ClientComposed.reset_after_open`. Deliberately narrow:
        dropping the reference is enough because the lock is reallocated
        lazily on the next :meth:`next_reqid` call from inside the new loop.
        ``_value`` is left untouched so reqid monotonicity survives reopen.
        """
        self._lock = None

    @property
    def value(self) -> int:
        """Snapshot of the current counter value (sync read, no lock).

        Returns a point-in-time read; the value can race with a concurrent
        :meth:`next_reqid` mutation. Callers that need an atomic post-
        increment value MUST use :meth:`next_reqid`, which serialises the
        read-modify-write under :attr:`_lock`. This accessor exists so the
        counter read path and test assertions
        (``assert core._reqid_counter == ...`` after a known sequence) stay
        lock-free.
        """
        return self._value

    def set_value(self, new_value: int) -> None:
        """Replace the counter value.

        Used by test fixtures that seed the counter to a deterministic
        baseline.
        """
        self._value = new_value

    async def next_reqid(self, step: int = DEFAULT_STEP) -> int:
        """Atomically increment the counter and return the new value.

        Args:
            step: Increment applied to the counter. Defaults to
                :data:`DEFAULT_STEP` to match the historical bump used by
                ``ChatAPI.ask``. Must be a positive ``int`` (not ``bool``);
                ``step <= 0`` would break monotonicity / uniqueness guarantees
                that Google's chat backend relies on.

        Returns:
            The post-increment counter value. Successive calls return strictly
            monotonic, distinct values even under ``asyncio.gather``.

        Raises:
            TypeError: If ``step`` is not an ``int`` (``bool`` is rejected
                even though it is a subclass of ``int``).
            ValueError: If ``step`` is not positive.
        """
        # ``bool`` is a subclass of ``int`` in Python — reject it explicitly so
        # ``next_reqid(step=True)`` doesn't silently degrade to ``step=1``.
        if not isinstance(step, int) or isinstance(step, bool):
            raise TypeError(f"step must be int, got {type(step).__name__}")
        if step <= 0:
            raise ValueError(f"step must be positive, got {step!r}")
        # Loop-affinity guard. Runs BEFORE the lazy ``Lock()`` allocation
        # so a cross-loop call (counter created under loop A, awaited from
        # loop B) raises ``RuntimeError`` at the call site instead of binding
        # the lazy lock to the wrong loop. The check is a silent no-op when
        # ``_bound_loop is None`` (standalone fixtures / unopened helpers).
        assert_bound_loop(self._bound_loop)
        # Safe: no await between check and assign, so no other coroutine can
        # race us here. Allocating ``asyncio.Lock()`` requires a running loop
        # in some Python versions; we're already awaited so we know one is
        # running.
        if self._lock is None:
            self._lock = asyncio.Lock()
        wait_start = time.perf_counter()
        await self._lock.acquire()
        # Lock release MUST happen before the ``on_lock_wait``
        # callback runs. A misbehaving callback (slow telemetry sink,
        # accidental re-entry, or one that itself awaits) must not widen
        # the critical section by holding ``_lock`` while it executes.
        # The increment + read happens under the lock; the wait-time
        # recording happens AFTER release. Tests:
        # ``tests/unit/concurrency/test_reqid_callback_outside_lock.py``.
        try:
            self._value += step
            new_value = self._value
        finally:
            self._lock.release()
        # Lock is released; safe to invoke arbitrary user-supplied
        # telemetry. Exceptions from the callback propagate to the caller
        # (the existing contract — ``ClientMetrics.record_lock_wait``
        # can't raise, but the API surface keeps this defensive).
        self._on_lock_wait(time.perf_counter() - wait_start)
        return new_value


__all__ = ["DEFAULT_BASELINE", "DEFAULT_STEP", "ReqidCounter"]
