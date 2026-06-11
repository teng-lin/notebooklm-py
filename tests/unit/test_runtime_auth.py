"""Unit tests for :mod:`notebooklm._runtime.auth`.

Covers the load-bearing behaviors of :class:`AuthRefreshCoordinator` directly,
in addition to the existing ``Session``-shaped tests in
``test_refresh_state_machine.py`` / ``test_refresh_lock_lazy_init.py`` /
``test_concurrency_refresh_race.py`` which exercise the same helper through
the compat facade.

Specifically pinned here:

* single-flight refresh — concurrent ``await_refresh`` callers share one
  in-flight refresh task;
* lazy lock allocation — ``_refresh_lock`` and ``_auth_snapshot_lock`` are
  ``None`` at construction and materialize on first use;
* ``update_auth_tokens`` writes ONLY ``auth.csrf_token`` and
  ``auth.session_id`` (does NOT touch the http client);
* ``update_auth_headers`` syncs ``auth.cookie_jar`` from
  ``kernel.get_http_client().cookies`` (the SEPARATE cookie-jar sync
  surface; Wave 11b of session-decoupling routes the live HTTP client
  through the :class:`Kernel` collaborator rather than a
  ``Session.get_http_client`` forward);
* ``await_refresh`` cancellation propagation — a cancelled waiter unwinds
  locally without killing the shared refresh task, and the task slot is
  preserved across cancellation.

The coordinator no longer accepts a Session-shaped ``_AuthRefreshHost``
host — :meth:`snapshot` and :meth:`update_auth_tokens` take an explicit
``auth: AuthTokens`` kwarg, :meth:`update_auth_headers` takes ``auth`` +
``kernel: Kernel`` kwargs, and lock-wait latency is recorded through the
coordinator's own ``self._metrics`` (supplied at construction). The
tests below pass each collaborator explicitly; there is no host shape
to fake.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from typing import cast

import httpx
import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._kernel import Kernel
from notebooklm._runtime.auth import AuthRefreshCoordinator
from notebooklm.auth import AuthTokens

# Tight enough to fail fast if a regression hangs the suite, generous enough
# not to flake on a slow CI runner. Mirrors ``test_refresh_state_machine.py``.
EVENT_TIMEOUT_S = 5.0


class _KernelStub:
    """Minimal kernel-shaped stub exposing only :meth:`get_http_client`.

    The coordinator's :meth:`update_auth_headers` reads
    ``kernel.get_http_client().cookies`` and nothing else; an
    ``httpx.AsyncClient``-backed shim satisfies that surface without
    pulling in the full :class:`Kernel`.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self.http_client = http_client

    def get_http_client(self) -> httpx.AsyncClient:
        assert self.http_client is not None, "Test forgot to wire an http client."
        return self.http_client


def _fresh_auth() -> AuthTokens:
    return AuthTokens(
        csrf_token="CSRF_OLD",
        session_id="SID_OLD",
        cookies={"SID": "old_cookie"},
    )


@pytest.fixture
def auth() -> AuthTokens:
    """A fresh :class:`AuthTokens` per test (the coordinator mutates it)."""
    return _fresh_auth()


@pytest.fixture
async def auth_with_kernel() -> AsyncIterator[tuple[AuthTokens, _KernelStub]]:
    """``(auth, kernel)`` with a real ``httpx.AsyncClient`` wired."""
    async with httpx.AsyncClient() as client:
        # Pre-populate a cookie so ``update_auth_headers`` has something to
        # observe propagating from the live jar to ``auth.cookie_jar``.
        client.cookies.set("SID", "live_jar_cookie")
        yield _fresh_auth(), _KernelStub(http_client=client)


# ---------------------------------------------------------------------------
# Lazy lock allocation
# ---------------------------------------------------------------------------


def test_locks_unallocated_at_construction() -> None:
    """Both locks are ``None`` at construction.

    Lazy allocation is load-bearing: ``asyncio.Lock()`` binds to the running
    loop in some Python versions, and ``NotebookLMClient`` routinely constructs
    the coordinator outside a running loop.
    """
    coord = AuthRefreshCoordinator()
    assert coord._refresh_lock is None
    assert coord._auth_snapshot_lock is None
    assert coord._refresh_task is None
    assert coord._refresh_callback is None


@pytest.mark.asyncio
async def test_get_refresh_lock_is_idempotent() -> None:
    """Repeated calls resolve to the SAME lock instance.

    Single-flight refresh depends on every waiter acquiring the same lock;
    a re-creating lazy-init would silently break dedupe.
    """
    coord = AuthRefreshCoordinator()
    first = coord.get_refresh_lock()
    second = coord.get_refresh_lock()
    assert first is second
    assert isinstance(first, asyncio.Lock)


@pytest.mark.asyncio
async def test_get_auth_snapshot_lock_is_idempotent() -> None:
    """Same idempotency contract for the snapshot lock."""
    coord = AuthRefreshCoordinator()
    first = coord.get_auth_snapshot_lock()
    second = coord.get_auth_snapshot_lock()
    assert first is second
    assert isinstance(first, asyncio.Lock)


@pytest.mark.asyncio
async def test_snapshot_and_refresh_locks_are_distinct() -> None:
    """The two locks must not share an instance.

    Mixing them would re-introduce the reentrancy ambiguity that the
    separate snapshot-side serialization was added to avoid — see the
    module docstring for ``_runtime/auth.py``.
    """
    coord = AuthRefreshCoordinator()
    refresh_lock = coord.get_refresh_lock()
    snapshot_lock = coord.get_auth_snapshot_lock()
    assert refresh_lock is not snapshot_lock


# ---------------------------------------------------------------------------
# update_auth_tokens — writes csrf_token + session_id ONLY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_auth_tokens_writes_csrf_and_session_id_only(
    auth_with_kernel: tuple[AuthTokens, _KernelStub],
) -> None:
    """``update_auth_tokens`` mutates ONLY ``auth.csrf_token`` + ``auth.session_id``.

    Cookies and the http client's jar must stay untouched — the cookie-jar
    sync is the separate :meth:`update_auth_headers` concern. This pin
    prevents a "helpful" maintainer from conflating the two and reopening
    the torn-state window the snapshot lock exists to close.
    """
    auth, kernel = auth_with_kernel
    coord = AuthRefreshCoordinator()
    pre_client_cookies = dict(kernel.get_http_client().cookies)
    pre_auth_cookies = dict(auth.cookies)

    await coord.update_auth_tokens(auth=auth, csrf="CSRF_NEW", session_id="SID_NEW")

    assert auth.csrf_token == "CSRF_NEW"
    assert auth.session_id == "SID_NEW"
    # http_client untouched
    assert dict(kernel.get_http_client().cookies) == pre_client_cookies
    # auth.cookies untouched (cookie sync is update_auth_headers's job)
    assert dict(auth.cookies) == pre_auth_cookies


@pytest.mark.asyncio
async def test_update_auth_tokens_holds_snapshot_lock_on_entry(
    auth: AuthTokens,
) -> None:
    """The write happens under the snapshot lock — proved by contention.

    Start the coordinator's write while a concurrent task is holding the
    snapshot lock; the write must block until the lock is released. This
    pins that the lock is acquired BEFORE the mutation block (the
    snapshot-lock serialization that makes ``_snapshot`` reads atomic with
    ``update_auth_tokens`` writes).
    """
    coord = AuthRefreshCoordinator()
    lock = coord.get_auth_snapshot_lock()

    enter_held = asyncio.Event()
    release_held = asyncio.Event()

    async def hold_lock() -> None:
        async with lock:
            enter_held.set()
            await release_held.wait()

    holder = asyncio.create_task(hold_lock())
    await asyncio.wait_for(enter_held.wait(), EVENT_TIMEOUT_S)

    write_task = asyncio.create_task(coord.update_auth_tokens(auth=auth, csrf="X", session_id="Y"))
    # Yield a few times so the writer reaches lock.acquire() and blocks.
    for _ in range(5):
        await asyncio.sleep(0)
    assert not write_task.done(), (
        "update_auth_tokens did not block on the snapshot lock — "
        "the mutation block is no longer guarded."
    )

    # Releasing the holder lets the writer through.
    release_held.set()
    await asyncio.wait_for(holder, EVENT_TIMEOUT_S)
    await asyncio.wait_for(write_task, EVENT_TIMEOUT_S)

    assert auth.csrf_token == "X"
    assert auth.session_id == "Y"


# ---------------------------------------------------------------------------
# Lock-wait metrics — record_lock_wait routes through self._metrics
# ---------------------------------------------------------------------------


class _RecordingMetrics:
    """Captures every :meth:`record_lock_wait` call (test seam only).

    Production code uses :class:`notebooklm._client_metrics.ClientMetrics`;
    this spy mirrors only the one method ``AuthRefreshCoordinator`` calls
    so the test asserts the metric path independent of the broader
    ``ClientMetrics`` API surface.
    """

    def __init__(self) -> None:
        self.lock_waits: list[float] = []

    def record_lock_wait(self, duration: float) -> None:
        self.lock_waits.append(duration)


@pytest.mark.asyncio
async def test_snapshot_records_lock_wait_through_constructor_metrics(
    auth: AuthTokens,
) -> None:
    """``snapshot`` routes ``record_lock_wait`` through the coordinator's
    own ``self._metrics`` (supplied at construction), NOT through a
    host-shaped collaborator.

    Pin matters because the explicit-collaborator migration removed the
    ``host._metrics_obj`` route; without this assertion a future revert
    that forgets to call ``self._metrics.record_lock_wait`` would still
    pass the existing behavior tests (which check only auth scalars).
    """
    metrics = _RecordingMetrics()
    coord = AuthRefreshCoordinator(metrics=cast(ClientMetrics, metrics))

    snapshot = await coord.snapshot(auth=auth)

    assert snapshot.csrf_token == auth.csrf_token
    assert snapshot.session_id == auth.session_id
    assert len(metrics.lock_waits) == 1
    assert metrics.lock_waits[0] >= 0.0


@pytest.mark.asyncio
async def test_update_auth_tokens_records_lock_wait_through_constructor_metrics(
    auth: AuthTokens,
) -> None:
    """Companion pin for :meth:`update_auth_tokens` — same routing."""
    metrics = _RecordingMetrics()
    coord = AuthRefreshCoordinator(metrics=cast(ClientMetrics, metrics))

    await coord.update_auth_tokens(auth=auth, csrf="C", session_id="S")

    assert auth.csrf_token == "C"
    assert auth.session_id == "S"
    assert len(metrics.lock_waits) == 1
    assert metrics.lock_waits[0] >= 0.0


class _ExplodingMetrics:
    """Lock-wait recorder that raises on every call — simulates a bug or
    misconfigured test spy inside the metrics path.
    """

    def record_lock_wait(self, duration: float) -> None:
        raise RuntimeError("metrics blew up")


@pytest.mark.asyncio
async def test_update_auth_tokens_releases_lock_when_metric_raises(
    auth: AuthTokens,
) -> None:
    """A metric-side exception must NOT leave the snapshot lock held.

    Pins the deadlock-safety property that the metric write lives inside
    the ``try`` block guarded by the ``finally: lock.release()``. Without
    this guard, a buggy metrics implementation (or a test spy that
    raises) would silently hang every subsequent ``snapshot`` /
    ``update_auth_tokens`` caller on the leaked lock.
    """
    metrics = _ExplodingMetrics()
    coord = AuthRefreshCoordinator(metrics=cast(ClientMetrics, metrics))

    with pytest.raises(RuntimeError, match="metrics blew up"):
        await coord.update_auth_tokens(auth=auth, csrf="X", session_id="Y")

    # The lock must be released even though the metric write raised.
    # A second call must acquire the lock without blocking. Wrap in
    # ``wait_for`` so a leaked lock surfaces as a fast failure rather
    # than hanging the suite.
    metrics2 = _RecordingMetrics()
    coord._metrics = cast(ClientMetrics, metrics2)
    await asyncio.wait_for(
        coord.update_auth_tokens(auth=auth, csrf="Z", session_id="W"),
        timeout=EVENT_TIMEOUT_S,
    )
    assert auth.csrf_token == "Z"
    assert auth.session_id == "W"


@pytest.mark.asyncio
async def test_await_refresh_releases_lock_when_metric_raises() -> None:
    """A metric-side exception must NOT leave the refresh lock held.

    Companion to ``test_update_auth_tokens_releases_lock_when_metric_raises``
    but for the single-flight refresh lock. Pins that ``record_lock_wait``
    lives inside the ``try`` block guarded by ``finally: lock.release()``;
    without that guard a buggy metrics implementation (or a test spy that
    raises) would silently hang every subsequent ``await_refresh`` caller on
    the leaked lock.
    """
    call_count = 0

    async def cb() -> AuthTokens:
        nonlocal call_count
        call_count += 1
        return AuthTokens(
            csrf_token=f"R{call_count}",
            session_id="S",
            cookies={"SID": f"sid{call_count}"},
        )

    metrics = _ExplodingMetrics()
    coord = AuthRefreshCoordinator(
        refresh_callback=cb,
        metrics=cast(ClientMetrics, metrics),
    )

    with pytest.raises(RuntimeError, match="metrics blew up"):
        await coord.await_refresh()

    # The refresh task is never created when the metric raises before
    # task-creation runs, so a leaked lock would not be masked by a joined
    # task — the second ``await_refresh`` must acquire the lock itself.
    assert coord._refresh_task is None

    # The lock must be released even though the metric write raised. Wrap in
    # ``wait_for`` so a leaked lock surfaces as a fast failure rather than
    # hanging the suite.
    metrics2 = _RecordingMetrics()
    coord._metrics = cast(ClientMetrics, metrics2)
    await asyncio.wait_for(coord.await_refresh(), timeout=EVENT_TIMEOUT_S)
    assert call_count == 1
    assert len(metrics2.lock_waits) == 1


# ---------------------------------------------------------------------------
# update_auth_headers — syncs auth.cookie_jar from get_http_client().cookies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_auth_headers_syncs_cookie_jar_from_get_http_client(
    auth_with_kernel: tuple[AuthTokens, _KernelStub],
) -> None:
    """``update_auth_headers`` copies ``kernel.get_http_client().cookies`` onto auth.

    Pins:
    * the read is via the ``kernel.get_http_client()`` METHOD on the
      explicit ``kernel`` collaborator (not a host-shaped attribute);
    * the destination is ``auth.cookie_jar`` (the cookie jar reference,
      not a dict copy).
    """
    auth, kernel = auth_with_kernel
    coord = AuthRefreshCoordinator()
    # Sanity: pre-call, auth.cookie_jar is whatever AuthTokens initialised.
    live_jar = kernel.get_http_client().cookies

    # _KernelStub structurally satisfies the surface that
    # ``update_auth_headers`` actually reads (``get_http_client()``) but is
    # not the nominal :class:`Kernel`; ``cast`` is cheaper than introducing
    # a Protocol just for one test seam.
    coord.update_auth_headers(auth=auth, kernel=cast(Kernel, kernel))

    # The auth.cookie_jar attribute is now identically the live jar.
    assert auth.cookie_jar is live_jar


def test_update_auth_headers_is_synchronous() -> None:
    """The method is plain ``def`` (no await).

    Async-vs-sync is a contract: callers must be able to invoke
    :meth:`update_auth_headers` outside any auth lock without paying for an
    event-loop hop. A switch to ``async def`` would silently break the
    ``_auth/session.py`` call shape (which invokes it sync).
    """
    assert not inspect.iscoroutinefunction(AuthRefreshCoordinator.update_auth_headers)


# ---------------------------------------------------------------------------
# Single-flight refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_refresh_is_single_flight() -> None:
    """Concurrent ``await_refresh`` callers share one in-flight refresh task.

    Mirrors ``test_refresh_state_machine.py::test_concurrent_callers_share_single_refresh``
    but exercises the coordinator directly (no ``Session`` facade in the
    middle). The lock protects task creation; the await on the task happens
    outside the lock so siblings can join.
    """
    callback_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    call_count = 0

    async def cb() -> AuthTokens:
        nonlocal call_count
        call_count += 1
        callback_entered.set()
        await release_refresh.wait()
        return AuthTokens(
            csrf_token="CSRF_REFRESHED",
            session_id="SID_REFRESHED",
            cookies={"SID": "post_refresh"},
        )

    coord = AuthRefreshCoordinator(refresh_callback=cb)

    tasks = [asyncio.create_task(coord.await_refresh()) for _ in range(3)]
    await asyncio.wait_for(callback_entered.wait(), EVENT_TIMEOUT_S)

    # Yield enough times for waiters 2/3 to reach ``await shield(task)``.
    for _ in range(20):
        if coord._refresh_task is not None and not coord._refresh_task.done():
            break
        await asyncio.sleep(0)
    assert coord._refresh_task is not None
    assert not coord._refresh_task.done()
    assert call_count == 1, f"Multiple refreshes fired before release: {call_count}"

    release_refresh.set()
    await asyncio.gather(*tasks)
    assert call_count == 1, f"Post-release call_count drifted to {call_count}"


@pytest.mark.asyncio
async def test_await_refresh_creates_new_task_after_first_done() -> None:
    """A second refresh wave creates a *new* task once the first is done."""
    call_count = 0

    async def cb() -> AuthTokens:
        nonlocal call_count
        call_count += 1
        return AuthTokens(
            csrf_token=f"R{call_count}",
            session_id="S",
            cookies={"SID": f"sid{call_count}"},
        )

    coord = AuthRefreshCoordinator(refresh_callback=cb)

    await coord.await_refresh()
    first_task = coord._refresh_task
    assert first_task is not None and first_task.done()

    await coord.await_refresh()
    second_task = coord._refresh_task
    assert second_task is not None and second_task.done()

    assert first_task is not second_task, "Second wave reused completed task"
    assert call_count == 2


@pytest.mark.asyncio
async def test_await_refresh_cancellation_preserves_task_slot() -> None:
    """A cancelled waiter does not kill the shared task; slot is preserved.

    Mirrors
    ``tests/integration/concurrency/test_refresh_cancellation_propagation.py``
    but exercises the coordinator directly. The
    ``asyncio.shield`` wrap is what stops one cancelled waiter from cancelling
    the underlying refresh task; the slot at ``_refresh_task`` is intentionally
    KEPT INTACT and is replaced only on the next refresh wave once the existing
    task hits ``done()``.
    """
    enter = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def cb() -> AuthTokens:
        nonlocal call_count
        call_count += 1
        enter.set()
        await release.wait()
        return AuthTokens(
            csrf_token="CSRF_REFRESHED",
            session_id="SID_REFRESHED",
            cookies={"SID": "post_refresh"},
        )

    coord = AuthRefreshCoordinator(refresh_callback=cb)

    waiter_a = asyncio.create_task(coord.await_refresh())
    waiter_b = asyncio.create_task(coord.await_refresh())
    await asyncio.wait_for(enter.wait(), EVENT_TIMEOUT_S)

    # Yield so both waiters reach ``await shield(task)``.
    for _ in range(20):
        if coord._refresh_task is not None and not coord._refresh_task.done():
            break
        await asyncio.sleep(0)
    shared_task = coord._refresh_task
    assert shared_task is not None and not shared_task.done()

    # Cancel waiter A. The shielded task underneath must NOT be cancelled.
    waiter_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_a

    # Waiter A unwound locally; the shared refresh task is untouched.
    assert coord._refresh_task is shared_task, (
        "Cancellation cleared the _refresh_task slot — siblings can no "
        "longer join the in-flight refresh."
    )
    assert not shared_task.done()
    assert call_count == 1

    # Release the refresh. Waiter B should resolve cleanly.
    release.set()
    await asyncio.wait_for(waiter_b, EVENT_TIMEOUT_S)
    assert shared_task.done()
    assert call_count == 1


# ---------------------------------------------------------------------------
# AuthRefreshCoordinator.cancel_inflight_refresh — Wave 1 of
# host-protocol-removal encapsulated the legacy close-time block
# (previously read/cancel/gather of ``host._auth_coord._refresh_task``
# inlined inside ``ClientLifecycle.close``) behind a method on the
# coordinator. The three tests below pin the three behavioral branches
# (no task, done task, in-flight task) AND the critical slot-preservation
# invariant (the cancel path MUST NOT clear ``self._refresh_task``).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_coord_cancel_inflight_refresh_noops_without_task() -> None:
    """``cancel_inflight_refresh`` is a true no-op when ``_refresh_task is None``.

    A freshly-constructed coordinator (or an open client that never
    triggered an auth refresh) has ``_refresh_task is None``. Close must
    invoke ``cancel_inflight_refresh`` unconditionally, so the method has
    to be safe against the ``None`` slot — calling ``.cancel()`` on ``None``
    would crash the close path.
    """
    coord = AuthRefreshCoordinator()
    assert coord._refresh_task is None

    # Must not raise.
    await coord.cancel_inflight_refresh()

    # Slot stays None — the method had nothing to cancel.
    assert coord._refresh_task is None


@pytest.mark.asyncio
async def test_auth_coord_cancel_inflight_refresh_noops_for_done_task() -> None:
    """A refresh task that already finished must not be re-cancelled.

    The ``done()`` short-circuit matters because the legacy block guarded
    both ``is None`` and ``done()`` — a successful refresh wave that ran
    to completion before ``close()`` arrives stashes the resolved task in
    the slot. Re-cancelling it would be technically harmless (cancelling
    a done task is a no-op) but the redundant ``gather(return_exceptions=True)``
    would still cycle the event loop and potentially log noise on a
    successful task that was about to be GC'd. The pin also guarantees
    the slot-preservation contract: the done task stays in the slot.
    """
    coord = AuthRefreshCoordinator()

    async def _quick_refresh() -> AuthTokens:
        return _fresh_auth()

    done_task = asyncio.create_task(_quick_refresh())
    # Let it complete.
    await done_task
    assert done_task.done() and not done_task.cancelled()
    # Snapshot the result so we can prove the task object was not touched
    # by ``cancel_inflight_refresh``.
    pre_result = done_task.result()

    coord._refresh_task = done_task

    await coord.cancel_inflight_refresh()

    assert done_task.done()
    assert not done_task.cancelled(), (
        "cancel_inflight_refresh must not call .cancel() on an already-done "
        "task — the done() short-circuit is load-bearing."
    )
    assert done_task.result() is pre_result, "done task's result was disturbed"
    assert coord._refresh_task is done_task, (
        "Slot-preservation invariant: cancel_inflight_refresh must not "
        "clear the _refresh_task slot even on the no-op path."
    )


@pytest.mark.asyncio
async def test_auth_coord_cancel_inflight_refresh_cancels_and_joins_pending_task() -> None:
    """An in-flight refresh task gets cancelled, joined, and CancelledError absorbed.

    This is the racing-close scenario the method was extracted for: a
    refresh wave parked on Google's identity surface when ``close()``
    arrives. The cancel cleans up the runaway task; the
    ``gather(..., return_exceptions=True)`` absorbs the resulting
    ``CancelledError`` so the close path itself stays non-raising.

    Slot-preservation invariant (CRITICAL): even after cancelling the
    in-flight task, ``self._refresh_task`` MUST still reference the same
    cancelled task object. The next refresh wave is responsible for
    replacing the slot once the existing task transitions to ``done()``
    — never this method, never close. This is the same contract pinned by
    ``test_await_refresh_cancellation_preserves_task_slot`` above, but for
    the close-driven cancel path rather than waiter-driven cancel.
    """
    coord = AuthRefreshCoordinator()

    async def _slow_refresh() -> AuthTokens:
        await asyncio.sleep(60.0)
        return _fresh_auth()  # unreachable in this test — cancel fires first.

    slow_task: asyncio.Task[AuthTokens] = asyncio.create_task(_slow_refresh())
    coord._refresh_task = slow_task

    # Yield so the task actually parks on its sleep.
    await asyncio.sleep(0)
    assert not slow_task.done(), "test setup: refresh task should be in-flight"

    # Drive cancel. Must NOT raise — CancelledError is absorbed by
    # ``gather(return_exceptions=True)``.
    await coord.cancel_inflight_refresh()

    assert slow_task.done()
    assert slow_task.cancelled(), (
        "cancel_inflight_refresh must cancel the in-flight task — without "
        "the cancel, a slow refresh would survive close() and continue "
        "holding the now-torn-down http client."
    )
    assert coord._refresh_task is slow_task, (
        "Slot-preservation invariant: cancel_inflight_refresh must NOT "
        "clear the _refresh_task slot after cancelling the task. Sibling "
        "waiters joined to the same single-flight refresh read this slot "
        "to identify the shared task; clearing it here would break the "
        "concurrency invariant pinned by "
        "test_await_refresh_cancellation_preserves_task_slot."
    )
