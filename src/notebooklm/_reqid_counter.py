"""Request-id counter helper for the NotebookLM client runtime.

Owns the monotonic ``_reqid`` value that Google's chat backend requires per
request, plus the lazily-allocated ``asyncio.Lock`` that serialises the
read-modify-write under concurrent ``ChatAPI.ask`` callers. The reqid
surface has one home (this file) instead of being woven into the runtime
composition root alongside metrics, drain, and auth state.

Design constraints (load-bearing — see ``tests/unit/test_reqid_counter.py`` and
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

from ._loop_affinity import assert_bound_loop
from ._loop_bound import LoopBoundPrimitive

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
    ``tests/unit/test_reqid_counter.py``) without a metrics sink wired up.
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
        # :class:`~notebooklm._loop_bound.LoopBoundPrimitive` base. This
        # counter only stores the binding, so it uses the default no-op
        # ``_on_loop_rebind`` (the lazy ``Lock`` is never held across
        # ``open()`` and is rebuilt implicitly per ``open()``).

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
