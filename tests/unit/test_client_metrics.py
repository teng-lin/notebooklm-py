"""Unit tests for :class:`notebooklm._client_metrics.ClientMetrics`.

Covers the metrics helper in isolation — the ``Session`` facade contract
(``metrics_snapshot``, ``_increment_metrics``, ``_record_rpc_queue_wait``,
``_record_lock_wait``, ``_emit_rpc_event``) is exercised end-to-end in
``tests/unit/test_observability.py``. This file pins the helper-class
invariants the facade depends on:

* ``__init__`` is event-loop-agnostic (no ``asyncio.*`` primitives).
* ``snapshot()`` returns a defensive copy, not a live reference.
* ``increment()`` and ``record_*`` mutations are visible in subsequent
  snapshots and accumulate correctly.
* ``emit_rpc_event`` dispatches sync, async, and exception-raising callbacks
  via ``maybe_await_callback`` (back-pressure semantics — never
  fire-and-forget).
* The ``Session.__new__(Session)`` regression path: setting
  ``_on_rpc_event`` on a ``__new__``-built core (no ``__init__`` ran) must
  succeed because the property setter calls ``_ensure_observability_state``
  before writethrough.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._session import Session
from notebooklm.types import ClientMetricsSnapshot, RpcTelemetryEvent

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_init_uses_no_asyncio_primitives() -> None:
    """``ClientMetrics`` must be constructible outside a running event loop.

    Regression guard: ``Session`` is routinely instantiated synchronously
    (e.g. ``NotebookLMClient(auth)`` before ``asyncio.run``). If
    ``ClientMetrics.__init__`` ever introduces an ``asyncio.Lock``/``Event``
    /``Condition``, that synchronous construction path will break on Python
    versions where those primitives require a running loop.
    """
    # Construct outside any loop — must not raise.
    metrics = ClientMetrics()
    assert isinstance(metrics._metrics_lock, type(threading.Lock()))
    assert isinstance(metrics._metrics, ClientMetricsSnapshot)
    assert metrics._on_rpc_event is None


def test_init_accepts_callback() -> None:
    def cb(_event: RpcTelemetryEvent) -> None:
        pass

    metrics = ClientMetrics(on_rpc_event=cb)
    assert metrics._on_rpc_event is cb


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


def test_snapshot_returns_defensive_copy() -> None:
    """Snapshot must not hand out a reference to the live in-place dataclass.

    Counters are frozen today, but external code shouldn't rely on that to
    avoid mutating the live state.
    """
    metrics = ClientMetrics()
    first = metrics.snapshot()
    metrics.increment(rpc_calls_started=1)
    second = metrics.snapshot()

    # First snapshot reflects pre-increment state — proves we didn't hand
    # out a live reference that then "saw" the increment.
    assert first.rpc_calls_started == 0
    assert second.rpc_calls_started == 1
    assert first is not second
    assert first is not metrics._metrics


# ---------------------------------------------------------------------------
# increment()
# ---------------------------------------------------------------------------


def test_increment_accumulates_across_calls() -> None:
    metrics = ClientMetrics()
    metrics.increment(rpc_calls_started=1)
    metrics.increment(rpc_calls_started=2, rpc_calls_succeeded=3)
    snapshot = metrics.snapshot()
    assert snapshot.rpc_calls_started == 3
    assert snapshot.rpc_calls_succeeded == 3
    assert snapshot.rpc_calls_failed == 0


def test_increment_holds_metrics_lock_during_update() -> None:
    """``increment()`` itself must run under ``_metrics_lock``.

    Spawn a worker that calls ``metrics.increment(...)`` while the main
    thread already holds ``_metrics_lock``. The worker must block inside
    ``increment`` until the main thread releases — verified by the
    ``timeout=0.05`` ``Event.wait()`` returning False. Once the main thread
    drops the lock, the worker completes and the increment is visible in
    the next snapshot.
    """
    metrics = ClientMetrics()
    increment_finished = threading.Event()

    def run_increment() -> None:
        metrics.increment(rpc_calls_started=1)
        increment_finished.set()

    # Hold the same lock that ``increment()`` needs; the worker must block
    # inside its ``with self._metrics_lock`` until we release here.
    with metrics._metrics_lock:
        worker = threading.Thread(target=run_increment)
        worker.start()
        # Worker is blocked inside increment(); the post-increment event
        # must not have fired yet.
        assert not increment_finished.wait(timeout=0.05)
    worker.join(timeout=1.0)
    assert worker.is_alive() is False
    assert increment_finished.is_set()
    # And the increment actually landed — the lock release didn't drop it.
    assert metrics.snapshot().rpc_calls_started == 1


# ---------------------------------------------------------------------------
# record_rpc_queue_wait / record_upload_queue_wait / record_lock_wait
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method_name", "total_field", "max_field"),
    [
        ("record_rpc_queue_wait", "rpc_queue_wait_seconds_total", "rpc_queue_wait_seconds_max"),
        (
            "record_upload_queue_wait",
            "upload_queue_wait_seconds_total",
            "upload_queue_wait_seconds_max",
        ),
        ("record_lock_wait", "lock_wait_seconds_total", "lock_wait_seconds_max"),
    ],
)
def test_record_wait_updates_total_and_max(
    method_name: str, total_field: str, max_field: str
) -> None:
    """Each ``record_*`` family accumulates total seconds and tracks max."""
    metrics = ClientMetrics()
    record = getattr(metrics, method_name)

    record(0.10)
    record(0.25)  # new max
    record(0.05)  # smaller — total grows, max stays

    snapshot = metrics.snapshot()
    assert getattr(snapshot, total_field) == pytest.approx(0.40)
    assert getattr(snapshot, max_field) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# emit_rpc_event — sync callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_rpc_event_invokes_sync_callback() -> None:
    events: list[RpcTelemetryEvent] = []
    metrics = ClientMetrics(on_rpc_event=events.append)
    event = RpcTelemetryEvent(method="GET_NOTEBOOK", status="success", elapsed_seconds=0.1)
    await metrics.emit_rpc_event(event)
    assert events == [event]


@pytest.mark.asyncio
async def test_emit_rpc_event_noop_when_callback_none() -> None:
    """No callback configured ⇒ silent no-op (and no AttributeError)."""
    metrics = ClientMetrics(on_rpc_event=None)
    event = RpcTelemetryEvent(method="GET_NOTEBOOK", status="success", elapsed_seconds=0.0)
    # Must not raise.
    await metrics.emit_rpc_event(event)


# ---------------------------------------------------------------------------
# emit_rpc_event — async callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_rpc_event_awaits_async_callback() -> None:
    """``emit_rpc_event`` must ``await`` an async callback before returning.

    This is the back-pressure contract: a slow async callback intentionally
    throttles the RPC path. If we ever switched to ``asyncio.create_task``
    (fire-and-forget), this test would fail because ``events`` would still be
    empty at the assertion point.
    """
    events: list[RpcTelemetryEvent] = []
    started_event = asyncio.Event()
    finish_event = asyncio.Event()

    async def slow_callback(event: RpcTelemetryEvent) -> None:
        started_event.set()
        await finish_event.wait()
        events.append(event)

    metrics = ClientMetrics(on_rpc_event=slow_callback)
    event = RpcTelemetryEvent(method="ASK", status="success", elapsed_seconds=0.0)

    emit_task = asyncio.create_task(metrics.emit_rpc_event(event))
    await started_event.wait()
    # If emit_rpc_event used create_task, it would already be done — but we
    # required back-pressure, so the await is still pending.
    assert not emit_task.done()
    assert events == []

    finish_event.set()
    await emit_task
    assert events == [event]


@pytest.mark.asyncio
async def test_emit_rpc_event_uses_maybe_await_callback_dispatch() -> None:
    """``emit_rpc_event`` dispatches via ``maybe_await_callback``.

    Regression guard: a previous refactor candidate replaced the helper with
    a custom ``asyncio.iscoroutine(result)`` branch. ``inspect.isawaitable``
    (used by ``maybe_await_callback``) accepts a broader set of awaitables —
    plain coroutines, generator-based coroutines, and custom ``__await__``
    objects. Switching to ``iscoroutine`` would silently drop those.
    """

    class CustomAwaitable:
        """Synchronous-looking callable that returns an arbitrary awaitable."""

        def __init__(self) -> None:
            self.called = False

        def __call__(self, _event: RpcTelemetryEvent) -> object:
            return self._coro()

        async def _coro(self) -> None:
            self.called = True

    callback = CustomAwaitable()
    metrics = ClientMetrics(on_rpc_event=callback)
    await metrics.emit_rpc_event(
        RpcTelemetryEvent(method="ASK", status="success", elapsed_seconds=0.0)
    )
    assert callback.called is True


# ---------------------------------------------------------------------------
# emit_rpc_event — exception-swallowing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_rpc_event_swallows_callback_exception(caplog) -> None:
    """A misbehaving user callback must not surface as an RPC failure.

    Logged at WARNING under the ``notebooklm._core`` logger so existing
    user-facing log filters on that namespace keep catching the diagnostic.
    """

    def boom(_event: RpcTelemetryEvent) -> None:
        raise ValueError("test-only callback failure")

    metrics = ClientMetrics(on_rpc_event=boom)
    with caplog.at_level(logging.WARNING, logger="notebooklm._core"):
        # Must not raise.
        await metrics.emit_rpc_event(
            RpcTelemetryEvent(method="ASK", status="error", elapsed_seconds=0.0)
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING for the failing callback"
    assert any("test-only callback failure" in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_emit_rpc_event_swallows_async_callback_exception(caplog) -> None:
    """Same swallow contract for async callbacks that raise."""

    async def async_boom(_event: RpcTelemetryEvent) -> None:
        raise RuntimeError("async test-only callback failure")

    metrics = ClientMetrics(on_rpc_event=async_boom)
    with caplog.at_level(logging.WARNING, logger="notebooklm._core"):
        await metrics.emit_rpc_event(
            RpcTelemetryEvent(method="ASK", status="error", elapsed_seconds=0.0)
        )

    assert any(
        "async test-only callback failure" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


# ---------------------------------------------------------------------------
# __new__-backfill regression — Session side
#
# These tests verify the property-bridge contract on ``Session``: a
# ``__new__``-built fixture must be able to (a) set ``_on_rpc_event`` directly
# and (b) await ``_emit_rpc_event`` without a prior ``__init__``. Both hinge
# on ``_ensure_observability_state`` lazy-constructing ``_metrics_obj`` on
# the first access path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_backfill_emit_event_works_without_init() -> None:
    """``Session.__new__(Session); await core._emit_rpc_event(...)`` must work.

    Regression guard for the property-descriptor gate failure: after A1
    turned ``_metrics_lock`` / ``_metrics`` / ``_on_rpc_event`` into class
    descriptors, the legacy ``hasattr`` short-circuit always returned True
    and the backfill never ran. The gate now hinges on the real
    ``_metrics_obj`` instance attribute.
    """
    core = Session.__new__(Session)
    # No __init__ has run; no callback configured.
    event = RpcTelemetryEvent(method="ASK", status="success", elapsed_seconds=0.0)
    await core._emit_rpc_event(event)
    # No callback ⇒ silent no-op. Just assert no AttributeError surfaced.


@pytest.mark.asyncio
async def test_new_backfill_emits_rpc_event_after_explicit_backfill() -> None:
    """Behaviour test: after ``_ensure_observability_state()`` primes the
    collaborator, writing ``_on_rpc_event`` directly and then calling
    ``_emit_rpc_event`` delivers the event.

    Phase 4: the ``Session._on_rpc_event`` setter was removed; tests that
    bypassed ``__init__`` via ``Session.__new__`` must now call the
    backfill helper explicitly before writing on the collaborator.
    """
    events: list[RpcTelemetryEvent] = []
    core = Session.__new__(Session)
    core._ensure_observability_state()  # explicit backfill (was implicit via setter)
    core._metrics_obj._on_rpc_event = events.append

    event = RpcTelemetryEvent(method="ASK", status="success", elapsed_seconds=0.0)
    await core._emit_rpc_event(event)
    assert events == [event]


def test_new_backfill_metrics_snapshot_returns_zero_snapshot() -> None:
    """``metrics_snapshot()`` on a ``__new__``-built core returns the zeroed default.

    Useful for tests that call into ``Session`` helpers (e.g.
    ``rpc_call``-flavored mocks) without paying for a full ``__init__``.
    """
    core = Session.__new__(Session)
    snapshot = core.metrics_snapshot()
    assert isinstance(snapshot, ClientMetricsSnapshot)
    assert snapshot.rpc_calls_started == 0
    assert snapshot.rpc_calls_succeeded == 0


def test_new_backfill_increment_metrics_lazy_construct() -> None:
    """``_increment_metrics`` lazy-constructs the helper on a ``__new__`` core."""
    core = Session.__new__(Session)
    core._increment_metrics(rpc_calls_started=2, rpc_calls_failed=1)
    snapshot = core.metrics_snapshot()
    assert snapshot.rpc_calls_started == 2
    assert snapshot.rpc_calls_failed == 1


# ---------------------------------------------------------------------------
# Phase 4 deleted the ``_metrics_lock``, ``_metrics``, ``_on_rpc_event``
# setters on ``Session``. The getters stay (with ``_ensure_observability_state``
# backfill) so reading still works; writers reach into ``_metrics_obj``
# directly. The round-trip + writethrough tests that pinned the deleted setters
# were removed (they would tautologically assert that writing direct then
# reading direct returns the written value). The single setter-removed test
# above (``test_new_backfill_emits_rpc_event_after_explicit_backfill``)
# preserves the behaviour-side regression guard.
# ---------------------------------------------------------------------------


def test_metrics_getter_returns_live_snapshot() -> None:
    """``core._metrics`` getter returns the helper's current snapshot."""
    core = Session.__new__(Session)
    core._increment_metrics(rpc_calls_started=7)
    # The getter delegates to ``self._metrics_obj._metrics``, which has been
    # rebound by ``increment`` to a new dataclass via ``replace``.
    assert core._metrics.rpc_calls_started == 7
