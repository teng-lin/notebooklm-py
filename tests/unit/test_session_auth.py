"""Unit tests for :mod:`notebooklm._session_auth`.

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
* ``update_auth_tokens`` writes ONLY ``host.auth.csrf_token`` and
  ``host.auth.session_id`` (does NOT touch the http client);
* ``update_auth_headers`` syncs ``host.auth.cookie_jar`` from
  ``host.get_http_client().cookies`` (the SEPARATE cookie-jar sync surface);
* ``await_refresh`` cancellation propagation — a cancelled waiter unwinds
  locally without killing the shared refresh task, and the task slot is
  preserved across cancellation.

Tests are intentionally helper-shaped (instantiate ``AuthRefreshCoordinator``
directly with a Protocol-conformant stub host) so they cover the coordinator
without taking on a ``Session`` dependency.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import httpx
import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._session_auth import AuthRefreshCoordinator
from notebooklm.auth import AuthTokens

# Tight enough to fail fast if a regression hangs the suite, generous enough
# not to flake on a slow CI runner. Mirrors ``test_refresh_state_machine.py``.
EVENT_TIMEOUT_S = 5.0


class _StubHost:
    """Minimal :class:`_AuthRefreshHost`-conformant host for unit tests.

    Mirrors the live ``Session`` shape:
    * ``auth`` is a real :class:`AuthTokens` — :meth:`update_auth_tokens`
      writes ``csrf_token`` / ``session_id`` directly on it.
    * ``_metrics_obj`` is a real :class:`ClientMetrics` — the coordinator's
      :meth:`record_lock_wait` calls land on it.
    * :meth:`get_http_client` returns an ``httpx.AsyncClient`` whose cookie
      jar :meth:`update_auth_headers` reads. The client's lifecycle is
      owned by the test fixture (`http_host`), not by the stub.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self.auth = AuthTokens(
            csrf_token="CSRF_OLD",
            session_id="SID_OLD",
            cookies={"SID": "old_cookie"},
        )
        self._metrics_obj = ClientMetrics(on_rpc_event=None)
        self.http_client = http_client

    def get_http_client(self) -> httpx.AsyncClient:
        assert self.http_client is not None, "Test forgot to wire an http client."
        return self.http_client


@pytest.fixture
def stub_host() -> _StubHost:
    """A coordinator host with no http client wired."""
    return _StubHost()


@pytest.fixture
async def http_host() -> Any:
    """A coordinator host with a real ``httpx.AsyncClient`` wired."""
    async with httpx.AsyncClient() as client:
        # Pre-populate a cookie so ``update_auth_headers`` has something to
        # observe propagating from the live jar to ``auth.cookie_jar``.
        client.cookies.set("SID", "live_jar_cookie")
        host = _StubHost(http_client=client)
        yield host


# ---------------------------------------------------------------------------
# Lazy lock allocation
# ---------------------------------------------------------------------------


def test_locks_unallocated_at_construction() -> None:
    """Both locks are ``None`` at construction.

    Lazy allocation is load-bearing: ``asyncio.Lock()`` binds to the running
    loop in some Python versions, and a ``Session`` (which constructs a
    coordinator) is routinely instantiated outside a running loop.
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
    module docstring for ``_session_auth.py``.
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
    http_host: _StubHost,
) -> None:
    """``update_auth_tokens`` mutates ONLY ``auth.csrf_token`` + ``auth.session_id``.

    Cookies and the http client's jar must stay untouched — the cookie-jar
    sync is the separate :meth:`update_auth_headers` concern. This pin
    prevents a "helpful" maintainer from conflating the two and reopening
    the torn-state window the snapshot lock exists to close.
    """
    coord = AuthRefreshCoordinator()
    pre_client_cookies = dict(http_host.get_http_client().cookies)
    pre_auth_cookies = dict(http_host.auth.cookies)

    await coord.update_auth_tokens(http_host, csrf="CSRF_NEW", session_id="SID_NEW")

    assert http_host.auth.csrf_token == "CSRF_NEW"
    assert http_host.auth.session_id == "SID_NEW"
    # http_client untouched
    assert dict(http_host.get_http_client().cookies) == pre_client_cookies
    # auth.cookies untouched (cookie sync is update_auth_headers's job)
    assert dict(http_host.auth.cookies) == pre_auth_cookies


@pytest.mark.asyncio
async def test_update_auth_tokens_holds_snapshot_lock_on_entry(
    stub_host: _StubHost,
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

    write_task = asyncio.create_task(coord.update_auth_tokens(stub_host, csrf="X", session_id="Y"))
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

    assert stub_host.auth.csrf_token == "X"
    assert stub_host.auth.session_id == "Y"


# ---------------------------------------------------------------------------
# update_auth_headers — syncs auth.cookie_jar from get_http_client().cookies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_auth_headers_syncs_cookie_jar_from_get_http_client(
    http_host: _StubHost,
) -> None:
    """``update_auth_headers`` copies ``get_http_client().cookies`` onto auth.

    Pins:
    * the read is via the ``get_http_client()`` METHOD (not a ``_http_client``
      attribute), matching :class:`_AuthRefreshHost` and ``_auth/session.py``;
    * the destination is ``host.auth.cookie_jar`` (the cookie jar reference,
      not a dict copy).
    """
    coord = AuthRefreshCoordinator()
    # Sanity: pre-call, auth.cookie_jar is whatever AuthTokens initialised.
    live_jar = http_host.get_http_client().cookies

    coord.update_auth_headers(http_host)

    # The auth.cookie_jar attribute is now identically the live jar.
    assert http_host.auth.cookie_jar is live_jar


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
async def test_await_refresh_is_single_flight(stub_host: _StubHost) -> None:
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
async def test_await_refresh_creates_new_task_after_first_done(
    stub_host: _StubHost,
) -> None:
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
async def test_await_refresh_cancellation_preserves_task_slot(
    stub_host: _StubHost,
) -> None:
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
