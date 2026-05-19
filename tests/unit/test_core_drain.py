"""Unit tests for :class:`notebooklm._core_drain.TransportDrainTracker`.

Covers the drain helper in isolation — the ``Session`` facade contract
(``drain``, ``_begin_transport_post``, ``_begin_transport_task``,
``_finish_transport_post``, ``_current_operation_depth``,
``_get_drain_condition``) is exercised end-to-end in
``tests/unit/test_observability.py`` and the close-cancellation suite at
``tests/unit/concurrency/test_close_cancellation_leak.py``. This file
pins the helper-class invariants the facade depends on:

* ``__init__`` is event-loop-agnostic (no ``asyncio.*`` primitives).
* ``get_drain_condition`` lazily constructs the ``asyncio.Condition``
  on first call from inside a running loop.
* ``begin_transport_post`` / ``finish_transport_post`` correctly track
  nested per-task depth (a child begin from inside an admitted operation
  is itself admitted post-drain, but a fresh top-level begin is
  rejected).
* ``drain`` blocks until ``_in_flight_posts`` reaches zero, raises
  ``TimeoutError`` on expiry, and leaves the tracker in draining mode
  after timeout (so a missed deadline doesn't accidentally admit new
  work).
* ``current_operation_depth(None)`` returns zero (the documented
  "outside any task" branch).
* The ``Session.__new__(Session)`` regression path: the drain
  property setters on a ``__new__``-built core (no ``__init__`` ran)
  must succeed because the setters call
  ``_ensure_observability_state`` before writethrough.
"""

from __future__ import annotations

import asyncio

import pytest

from notebooklm._core_drain import TransportDrainTracker, _TransportOperationToken
from notebooklm._session import Session

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_init_uses_no_asyncio_primitives() -> None:
    """``TransportDrainTracker`` must be constructible outside a running event loop.

    Regression guard: ``Session`` is routinely instantiated synchronously
    (e.g. ``NotebookLMClient(auth)`` before ``asyncio.run``). If
    ``TransportDrainTracker.__init__`` ever introduces an
    ``asyncio.Lock``/``Event``/``Condition``, the synchronous construction
    path will break on Python versions where those primitives require a
    running loop.
    """
    tracker = TransportDrainTracker()

    assert tracker._in_flight_posts == 0
    assert tracker._draining is False
    assert tracker._drain_condition is None
    # ``WeakKeyDictionary`` is plain Python; no loop required.
    assert len(tracker._operation_depths) == 0


@pytest.mark.asyncio
async def test_get_drain_condition_lazy_allocates() -> None:
    """First call inside a loop allocates; subsequent calls return same instance."""
    tracker = TransportDrainTracker()
    assert tracker._drain_condition is None

    condition_first = tracker.get_drain_condition()
    assert isinstance(condition_first, asyncio.Condition)

    condition_second = tracker.get_drain_condition()
    assert condition_second is condition_first


# ---------------------------------------------------------------------------
# Depth bookkeeping
# ---------------------------------------------------------------------------


def test_current_operation_depth_returns_zero_for_none_task() -> None:
    """The "outside any task" branch must not raise."""
    tracker = TransportDrainTracker()
    assert tracker.current_operation_depth(None) == 0


@pytest.mark.asyncio
async def test_current_operation_depth_reports_nested_begins() -> None:
    """Two consecutive begins from the same task must produce depth = 2."""
    tracker = TransportDrainTracker()
    task = asyncio.current_task()
    assert task is not None
    assert tracker.current_operation_depth(task) == 0

    token_outer = await tracker.begin_transport_post("outer")
    assert tracker.current_operation_depth(task) == 1

    token_inner = await tracker.begin_transport_post("inner")
    assert tracker.current_operation_depth(task) == 2

    await tracker.finish_transport_post(token_inner)
    assert tracker.current_operation_depth(task) == 1

    await tracker.finish_transport_post(token_outer)
    assert tracker.current_operation_depth(task) == 0


@pytest.mark.asyncio
async def test_nested_begin_finish_returns_counter_to_zero() -> None:
    """Symmetric begin/finish pairs must leave both counter and depth at zero."""
    tracker = TransportDrainTracker()
    outer = await tracker.begin_transport_post("outer")
    inner = await tracker.begin_transport_post("inner")
    assert tracker._in_flight_posts == 2

    await tracker.finish_transport_post(inner)
    assert tracker._in_flight_posts == 1
    await tracker.finish_transport_post(outer)
    assert tracker._in_flight_posts == 0
    # Operation_depths entry for the current task should be cleaned up.
    task = asyncio.current_task()
    assert task is not None
    assert task not in tracker._operation_depths


# ---------------------------------------------------------------------------
# Drain semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_returns_immediately_when_nothing_in_flight() -> None:
    """No in-flight ops → ``drain`` completes synchronously and sets the flag."""
    tracker = TransportDrainTracker()
    await tracker.drain(timeout=1.0)
    assert tracker._draining is True
    assert tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_drain_blocks_until_in_flight_drops_to_zero() -> None:
    """``drain`` must wait for the last admitted operation to finish."""
    tracker = TransportDrainTracker()
    started = asyncio.Event()
    release = asyncio.Event()

    async def in_flight() -> None:
        token = await tracker.begin_transport_post("worker")
        started.set()
        try:
            await release.wait()
        finally:
            await tracker.finish_transport_post(token)

    worker = asyncio.create_task(in_flight())
    await started.wait()

    drain_task = asyncio.create_task(tracker.drain(timeout=2.0))
    # Yield once so drain has a chance to acquire the condition and park.
    await asyncio.sleep(0)
    assert not drain_task.done()
    assert tracker._draining is True

    # Releasing the worker drops in_flight to zero; drain must wake up.
    release.set()
    await drain_task
    await worker
    assert tracker._in_flight_posts == 0


@pytest.mark.asyncio
async def test_drain_rejects_new_top_level_work() -> None:
    """Once draining, fresh begins from a task with depth 0 must raise."""
    tracker = TransportDrainTracker()
    await tracker.drain(timeout=0.5)
    assert tracker._draining is True

    with pytest.raises(RuntimeError, match="draining"):
        await tracker.begin_transport_post("new-after-drain")


@pytest.mark.asyncio
async def test_drain_allows_nested_begin_from_admitted_task() -> None:
    """A nested begin from a task with depth > 0 must succeed even mid-drain.

    This is the contract that lets an in-flight source upload finish its
    sub-RPCs after ``drain()`` starts; otherwise close would deadlock.
    """
    tracker = TransportDrainTracker()
    outer = await tracker.begin_transport_post("outer")
    try:
        drain_task = asyncio.create_task(tracker.drain(timeout=1.0))
        await asyncio.sleep(0)
        assert not drain_task.done()

        # Nested begin must NOT raise because current task's depth is 1.
        nested = await tracker.begin_transport_post("nested")
        await tracker.finish_transport_post(nested)
    finally:
        await tracker.finish_transport_post(outer)

    await drain_task


@pytest.mark.asyncio
async def test_drain_rejects_child_task_spawned_from_admitted_operation() -> None:
    """Spawning a brand-new task from inside an admitted op must still be
    rejected once draining — the gate keys on the *spawner*'s depth, and a
    freshly-created task has its own depth of 0 from ``asyncio.current_task()``
    inside its body.

    Mirrors
    ``tests/unit/test_observability.py::test_drain_rejects_child_task_spawned_from_accepted_operation``.
    """
    tracker = TransportDrainTracker()
    outer = await tracker.begin_transport_post("outer")
    try:
        drain_task = asyncio.create_task(tracker.drain(timeout=1.0))
        await asyncio.sleep(0)

        async def child_work() -> None:
            child_token = await tracker.begin_transport_post("child")
            await tracker.finish_transport_post(child_token)

        with pytest.raises(RuntimeError, match="draining"):
            await asyncio.create_task(child_work())
    finally:
        await tracker.finish_transport_post(outer)

    await drain_task


@pytest.mark.asyncio
async def test_drain_timeout_raises_and_keeps_draining_flag() -> None:
    """Drain timeout must raise ``TimeoutError`` and leave the flag set.

    Shutdown callers (``Session.close``) must not accidentally admit new
    work after a missed deadline. The contract is "drain mode is sticky";
    pin it.
    """
    tracker = TransportDrainTracker()
    token = await tracker.begin_transport_post("hangs-forever")
    # Do NOT finish — drain must time out.
    try:
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await tracker.drain(timeout=0.05)
    finally:
        # Clean up so the event loop teardown doesn't see a leaked counter.
        await tracker.finish_transport_post(token)

    assert tracker._draining is True


@pytest.mark.asyncio
async def test_drain_rejects_negative_timeout() -> None:
    """A negative timeout is invalid input — surface it loudly."""
    tracker = TransportDrainTracker()
    with pytest.raises(ValueError, match="timeout must be >= 0 or None"):
        await tracker.drain(timeout=-1.0)


# ---------------------------------------------------------------------------
# begin_transport_task — child-task admission path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_begin_transport_task_bumps_depth_on_target_task() -> None:
    """``begin_transport_task`` keys depth on the *spawned* task, not the spawner."""
    tracker = TransportDrainTracker()

    async def child_body() -> tuple[_TransportOperationToken, int]:
        spawned = asyncio.current_task()
        assert spawned is not None
        token = await tracker.begin_transport_task(spawned, "child")
        # Depth for the spawned task should be 1.
        depth = tracker.current_operation_depth(spawned)
        await tracker.finish_transport_post(token)
        return token, depth

    child = asyncio.create_task(child_body())
    token, depth = await child
    assert depth == 1
    assert token.task is not None


# ---------------------------------------------------------------------------
# Token equality
# ---------------------------------------------------------------------------


def test_token_is_frozen_dataclass() -> None:
    """``_TransportOperationToken`` is frozen — value equality, not identity.

    Locked down so a follow-up refactor doesn't accidentally turn this into
    a mutable dataclass, which would break dict-keying and reduce safety
    around begin/finish pairing.
    """
    from dataclasses import FrozenInstanceError

    token = _TransportOperationToken(task=None)
    with pytest.raises(FrozenInstanceError):
        token.task = None  # type: ignore[misc]


def test_token_reexported_from_core_module() -> None:
    """``from notebooklm._session import _TransportOperationToken`` must still work.

    Master plan mandate: the legacy import path stays available after the
    dataclass moves into ``_core_drain``.
    """
    from notebooklm._session import _TransportOperationToken as Aliased

    assert Aliased is _TransportOperationToken


# ---------------------------------------------------------------------------
# ``Session.__new__`` backfill regression
# ---------------------------------------------------------------------------


def test_new_backfill_drain_tracker_constructed_on_first_access() -> None:
    """A ``__new__``-built core must lazy-construct the tracker on first probe.

    Regression guard: ``Session.__new__(Session)`` skips ``__init__``,
    so neither ``_metrics_obj`` nor ``_drain_tracker`` is set. The first
    facade-method call must run ``_ensure_observability_state`` and
    backfill both.
    """
    core = Session.__new__(Session)
    assert not hasattr(core, "_drain_tracker")

    core._ensure_observability_state()
    assert isinstance(core._drain_tracker, TransportDrainTracker)
    # The compat properties must read through cleanly.
    assert core._in_flight_posts == 0
    assert core._draining is False
    assert core._drain_condition is None


def test_new_backfill_drain_setter_writethrough_succeeds() -> None:
    """Setting ``core._draining = True`` on a ``__new__``-built core works.

    The property setter must call ``_ensure_observability_state`` BEFORE
    the writethrough; otherwise the writethrough hits an ``AttributeError``
    because ``_drain_tracker`` doesn't exist yet.
    """
    core = Session.__new__(Session)
    core._draining = True
    assert core._draining is True
    assert core._drain_tracker._draining is True


def test_new_backfill_drain_condition_setter_writethrough_succeeds() -> None:
    """Tests can inject ``core._drain_condition = asyncio.Condition()`` directly.

    Some legacy fixtures pre-allocate the condition; the setter must
    accept that even on a ``__new__``-built core.
    """

    async def _scope() -> None:
        core = Session.__new__(Session)
        injected = asyncio.Condition()
        core._drain_condition = injected
        assert core._drain_condition is injected
        assert core._drain_tracker._drain_condition is injected

    asyncio.run(_scope())


def test_new_backfill_in_flight_setter_writethrough_succeeds() -> None:
    """``core._in_flight_posts = 5`` on a ``__new__``-built core writes through."""
    core = Session.__new__(Session)
    core._in_flight_posts = 5
    assert core._in_flight_posts == 5
    assert core._drain_tracker._in_flight_posts == 5


# ---------------------------------------------------------------------------
# Facade integration — exercise Session delegation through the tracker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_drain_facade_delegates_to_tracker() -> None:
    """A real ``Session`` instance must route ``drain`` through the tracker.

    Construction via ``__new__`` keeps the test off the full ``__init__``
    surface (no auth, no httpx). The facade methods still rely on
    ``_ensure_observability_state`` to backfill, which is exactly the path
    we want to exercise here.
    """
    core = Session.__new__(Session)
    # Begin/finish through the facade should bump and clear in-flight.
    token = await core._begin_transport_post("facade-test")
    assert core._in_flight_posts == 1
    await core._finish_transport_post(token)
    assert core._in_flight_posts == 0
    # Drain with nothing in flight should return immediately and set flag.
    await core.drain(timeout=0.5)
    assert core._draining is True
