"""Auth refresh coordinator helper for the client runtime.

Owns the auth refresh state machine and snapshot serialization that historically
lived inline on the former ``Session`` facade (now deleted):

* ``_refresh_lock`` — single-flight lock guarding refresh-task creation. Lazy
  because ``asyncio.Lock()`` needs a running loop in some Python versions and
  the coordinator can be constructed outside one.
* ``_refresh_task`` — the shared in-flight refresh task. Slot is intentionally
  preserved across waiter cancellation so siblings can still join, and is
  replaced only on the next refresh wave once the existing task hits
  ``done()`` (see :meth:`await_refresh` docstring).
* ``_refresh_callback`` — the user-supplied async callable that performs the
  actual refresh. ``None`` disables refresh-on-401.
* ``_auth_snapshot_lock`` — serializes the four-scalar reads in
  :meth:`snapshot` with the two-scalar writes in :meth:`update_auth_tokens`
  so RPC snapshots cannot observe a torn ``(csrf, session_id)`` pair while
  refresh is in flight. Intentionally distinct from ``_refresh_lock``:
  mixing them would re-introduce the reentrancy ambiguity that
  snapshot-side serialization was added to avoid.

Design constraints (load-bearing — see tests/unit/test_refresh_*.py and
tests/integration/concurrency/test_refresh_cancellation_propagation.py):

* ``__init__`` MUST be event-loop-agnostic — it stores only a plain callable
  and ``None`` placeholders. Never call ``asyncio.get_running_loop()`` or
  instantiate ``asyncio.*`` primitives at construction time.
* :meth:`await_refresh` MUST hold no lock across ``await self._refresh_callback()``.
  The refresh lock gates *task creation* only; the await on the task itself
  happens outside the lock so other waiters can join. Mixing this contract
  would silently deadlock waiters on a slow callback.
* :meth:`update_auth_tokens` writes ONLY ``auth.csrf_token`` and
  ``auth.session_id`` under the snapshot lock. It does NOT touch the
  http client. The cookie-jar sync is a separate concern handled by
  :meth:`update_auth_headers` (sync, no await — it runs the
  ``kernel.get_http_client().cookies`` read outside any auth lock).
* The ``_refresh_task`` slot is intentionally NOT cleared when a waiter is
  cancelled mid-shield — concurrency tests assert task identity across
  cancellation so siblings joined to the same single-flight refresh see the
  same completion. Per ``_core.py`` history at the relocated comment site.

Collaborator surface (no Session-shaped host):

The coordinator was historically wired through a structural
``_AuthRefreshHost`` Protocol that re-declared three private ``Session``
slots (``auth`` / ``_metrics_obj`` / ``_kernel``). That Protocol coupled
the coordinator to ``Session``'s private attribute names and forced
fake-host shims in tests. It was deleted in favor of explicit per-method
collaborators: :meth:`snapshot` and :meth:`update_auth_tokens` take an
``auth: AuthTokens`` kwarg, :meth:`update_auth_headers` takes
``auth: AuthTokens`` plus ``kernel: Kernel``, and the lock-wait metric is
recorded through ``self._metrics`` (already supplied at construction).
This keeps every dependency on the coordinator's surface concrete and
narrow — there is no Session shape to fake.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any, cast

from ._loop_affinity import assert_bound_loop
from ._request_types import AuthSnapshot
from ._runtime_config import CORE_LOGGER_NAME
from .auth import AuthTokens

if TYPE_CHECKING:
    from ._client_metrics import ClientMetrics
    from ._kernel import Kernel

# Logger name pinned via :data:`CORE_LOGGER_NAME` so log filters in
# tests — e.g. ``caplog.at_level("DEBUG", logger=CORE_LOGGER_NAME)`` —
# keep matching after the extraction.
logger = logging.getLogger(CORE_LOGGER_NAME)


class AuthRefreshCoordinator:
    """Owns refresh single-flight, snapshot serialization, and auth-header sync.

    Field names (``_refresh_lock``, ``_refresh_task``, ``_refresh_callback``,
    ``_auth_snapshot_lock``) deliberately mirror the retired legacy
    ``Session`` ivars so bridge-retirement diffs and coordinator-specific
    tests remain easy to audit.
    """

    def __init__(
        self,
        *,
        refresh_callback: Callable[[], Awaitable[AuthTokens]] | None = None,
        metrics: ClientMetrics | None = None,
    ) -> None:
        # Lazily-created — ``asyncio.Lock()`` needs a running loop in some
        # Python versions, and this object can be constructed outside one.
        self._refresh_lock: asyncio.Lock | None = None
        self._refresh_task: asyncio.Task[AuthTokens] | None = None
        self._refresh_callback: Callable[[], Awaitable[AuthTokens]] | None = refresh_callback
        # Wave 3b of session-decoupling: ``await_refresh`` records lock-wait
        # latency via this metrics dep (was previously read off the host
        # parameter — see plan Task 1.0). The same ``self._metrics`` slot is
        # now read by :meth:`snapshot` and :meth:`update_auth_tokens` too —
        # the deleted ``_AuthRefreshHost`` Protocol's ``_metrics_obj`` slot
        # is no longer threaded through method args. ``None`` is a safe
        # fallback for tests that construct the coordinator standalone
        # without a metrics collaborator; the lock-wait latency is simply not
        # recorded in that case.
        self._metrics: ClientMetrics | None = metrics
        # Distinct from ``_refresh_lock`` — see module docstring.
        self._auth_snapshot_lock: asyncio.Lock | None = None
        # P0-2: loop-affinity guard. Set by :meth:`ClientLifecycle.open`
        # so :meth:`await_refresh` can short-circuit cross-loop misuse
        # before touching the lazily-built ``_refresh_lock`` (bound to
        # the loop the lock was first acquired under). ``None`` is a
        # silent no-op for standalone fixtures.
        self._bound_loop: asyncio.AbstractEventLoop | None = None

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Capture or clear the event-loop binding for the affinity guard.

        :meth:`ClientLifecycle.open` propagates the captured loop here.
        Passing ``None`` clears the binding for the next ``open()``
        (which will rebind to a fresh loop).
        """
        self._bound_loop = loop

    @property
    def has_refresh_callback(self) -> bool:
        """``True`` iff a refresh callback was wired at construction.

        Used by :class:`notebooklm._middleware_auth_refresh.AuthRefreshMiddleware`
        to gate the refresh-and-retry branch: a client constructed without
        a ``refresh_callback`` should propagate auth errors directly,
        matching the pre-Tier-12 leaf behavior. Exposing this as a
        property avoids reaching into the private
        ``_refresh_callback`` attribute from outside the coordinator
        (PR 12.9 cleanup of the inline lambda the chain seed used pre-12.9).
        """
        return self._refresh_callback is not None

    # ------------------------------------------------------------------
    # Lazy lock accessors. Both follow the same race-free check-then-assign
    # pattern as ``_reqid_lock``: asyncio is single-threaded, so no other
    # coroutine can execute between the ``is None`` check and the
    # assignment unless we ``await`` — and we don't.
    # ------------------------------------------------------------------

    def get_refresh_lock(self) -> asyncio.Lock:
        """Return the lazily-initialised refresh lock.

        Concurrent callers resolve to the *same* instance because allocation
        is synchronous and asyncio is single-threaded; this preserves the
        single-flight refresh-task creation invariant in :meth:`await_refresh`.
        """
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    def get_auth_snapshot_lock(self) -> asyncio.Lock:
        """Return the lazily-initialised auth-snapshot lock.

        Held only across the four scalar reads in :meth:`snapshot` and the
        two scalar writes in :meth:`update_auth_tokens` — never across an
        ``await`` — so RPC throughput is not serialized to refresh latency.
        """
        if self._auth_snapshot_lock is None:
            self._auth_snapshot_lock = asyncio.Lock()
        return self._auth_snapshot_lock

    # ------------------------------------------------------------------
    # Auth snapshot + token write — the load-bearing AST-guarded pair.
    # These two methods are the canonical implementations of the
    # concurrency invariants: ``snapshot`` holds the ``_auth_snapshot_lock``
    # across four synchronous scalar reads, and ``update_auth_tokens``
    # forbids any ``await`` inside the csrf/session_id mutation try-block.
    # The AST guards in ``tests/unit/test_concurrency_refresh_race.py``
    # (``test_snapshot_acquires_auth_snapshot_lock`` and
    # ``test_update_auth_tokens_has_no_await_inside_mutation_block``)
    # inspect THIS module's source via ``inspect.getsource(...)`` + AST
    # parsing — any structural change to either method body (e.g.
    # extracting a helper, refactoring the lock dance, adding an
    # ``await`` mid-mutation) will trip those guards. ``Session._snapshot``
    # and ``Session.update_auth_tokens`` (`_session.py`) are now thin
    # delegates that forward through ``self._auth_coord``; there is no
    # longer a "second body" to keep in sync.
    # ------------------------------------------------------------------

    async def snapshot(self, *, auth: AuthTokens) -> AuthSnapshot:
        """Capture the current auth scalars as a frozen snapshot.

        Acquires :attr:`_auth_snapshot_lock` for the four scalar reads so a
        concurrent :meth:`update_auth_tokens` cannot interleave between
        ``csrf_token`` / ``session_id`` / ``authuser`` / ``account_email``.
        The critical section is purely synchronous attribute reads — no
        ``await`` — so the lock is uncontested in steady state and refresh's
        tiny write block cannot block RPC throughput.

        The whole-request atomicity for ``(csrf, sid, cookies)`` on the wire
        still depends on the no-await invariant between this method returning
        and ``client.post(...)`` inside ``_perform_authed_post`` (see the AST
        guard in ``tests/unit/test_concurrency_refresh_race.py``). The lock
        guarantees the four scalars in the snapshot are coherent with each
        other; the no-await rule keeps the cookie axis aligned with them.

        ``auth`` is passed explicitly per call (the previous
        ``_AuthRefreshHost`` Protocol re-declared the ``auth`` slot from
        ``Session``); the lock-wait metric is recorded through
        ``self._metrics`` (supplied at construction).
        """
        wait_start = time.perf_counter()
        async with self.get_auth_snapshot_lock():
            if self._metrics is not None:
                self._metrics.record_lock_wait(time.perf_counter() - wait_start)
            return AuthSnapshot(
                csrf_token=auth.csrf_token,
                session_id=auth.session_id,
                authuser=auth.authuser,
                account_email=auth.account_email,
            )

    async def update_auth_tokens(
        self,
        *,
        auth: AuthTokens,
        csrf: str,
        session_id: str,
    ) -> None:
        """Atomically update ``auth.csrf_token`` + ``auth.session_id`` only.

        Does NOT touch the http client — the cookie-jar sync is the separate
        :meth:`update_auth_headers` concern. Conflating the two would let a
        snapshot acquired between this method and the header sync observe a
        new token pair against stale cookies, which is exactly the torn-state
        scenario the snapshot lock exists to prevent.

        ``auth`` is passed explicitly (no ``_AuthRefreshHost`` shape).
        """
        lock = self.get_auth_snapshot_lock()
        wait_start = time.perf_counter()
        await lock.acquire()
        try:
            # ``record_lock_wait`` lives INSIDE the ``try`` so a metric-side
            # exception (e.g. a misconfigured spy in tests, or a runtime bug
            # in :class:`ClientMetrics`) cannot leave the snapshot lock held
            # — the ``finally`` releases unconditionally. The call is
            # synchronous so the no-await guard pinned by
            # ``test_update_auth_tokens_has_no_await_inside_mutation_block``
            # still holds.
            if self._metrics is not None:
                self._metrics.record_lock_wait(time.perf_counter() - wait_start)
            auth.csrf_token = csrf
            auth.session_id = session_id
        finally:
            lock.release()

    def update_auth_headers(self, *, auth: AuthTokens, kernel: Kernel) -> None:
        """Sync ``auth.cookie_jar`` with the live HTTP client's jar.

        Synchronous on purpose — no await — so callers can run this without
        any auth lock held. The httpx client's cookie jar is authoritative
        once the session is open; re-injecting startup cookies here would
        overwrite cookies refreshed during redirects to
        ``accounts.google.com``.

        ``auth`` and ``kernel`` are passed explicitly per call so the
        coordinator does not need a ``_AuthRefreshHost``-shaped host (the
        Protocol that re-declared Session's ``auth`` / ``_kernel`` slots was
        deleted alongside this signature change).

        Raises:
            RuntimeError: If the kernel's HTTP client is not initialised (the
                error originates from :meth:`Kernel.get_http_client`).
        """
        auth.cookie_jar = kernel.get_http_client().cookies

    # ------------------------------------------------------------------
    # Single-flight refresh task.
    # ------------------------------------------------------------------

    async def await_refresh(self) -> None:
        """Run / join the shared refresh task.

        Concurrent callers share one refresh task so a thundering herd of
        401s on the same client triggers exactly one token refresh. The lock
        protects task-creation only; the await on the task itself happens
        outside the lock so other callers can join.

        The join is wrapped in :func:`asyncio.shield` so that a caller
        cancelled while waiting — e.g. via ``asyncio.wait_for(..., timeout=...)``
        — unwinds locally without propagating the ``CancelledError`` into the
        *shared* refresh task. Without the shield, one cancelled waiter would
        cancel the underlying task, taking down every sibling joined to the
        same single-flight refresh. The slot at :attr:`_refresh_task` is left
        intact across the cancellation and is replaced only on the next
        refresh wave once the current task transitions to ``done()``.

        Wave 3b of the session-decoupling plan (Task 1.0): this method no
        longer takes a host parameter — the only host attribute it touched
        (``_metrics_obj``) is supplied via the ``metrics`` kwarg on
        :meth:`__init__`. The other coordinator methods (``snapshot``,
        ``update_auth_tokens``, ``update_auth_headers``) were migrated to
        explicit per-method collaborators in a follow-up so the
        coordinator no longer depends on any Session-shaped Protocol.
        """
        # P0-2: catch cross-loop refresh before touching ``_refresh_lock``.
        # The lock is lazily bound to the loop that first awaited
        # ``get_refresh_lock`` — a cross-loop call would hang on the
        # ``await lock.acquire()`` if we let it through.
        assert_bound_loop(self._bound_loop)
        if self._refresh_callback is None:
            raise RuntimeError(
                "AuthRefreshCoordinator.await_refresh called without a "
                "refresh_callback configured — wire one via "
                "AuthRefreshCoordinator(refresh_callback=...) (or by "
                "constructing Session with refresh_callback=...) before "
                "triggering an auth refresh."
            )

        # Lazy-init the lock on first refresh attempt. Every concurrent
        # caller resolves to the same instance because ``get_refresh_lock``
        # runs synchronously in a single-threaded asyncio loop, so the
        # single-flight task creation below is preserved.
        lock = self.get_refresh_lock()
        wait_start = time.perf_counter()
        await lock.acquire()
        try:
            # ``record_lock_wait`` lives INSIDE the ``try`` so a metric-side
            # exception (e.g. a misconfigured spy in tests, or a runtime bug
            # in :class:`ClientMetrics`) cannot leave the refresh lock held —
            # the ``finally`` releases unconditionally. Mirrors the same
            # hardening on :meth:`update_auth_tokens`; the call is synchronous
            # so no await runs between acquiring and releasing the lock.
            if self._metrics is not None:
                self._metrics.record_lock_wait(time.perf_counter() - wait_start)
            if self._refresh_task is not None and not self._refresh_task.done():
                refresh_task = self._refresh_task
                logger.debug("Joining existing refresh task")
            else:
                coro = cast(Coroutine[Any, Any, AuthTokens], self._refresh_callback())
                self._refresh_task = asyncio.create_task(coro)
                refresh_task = self._refresh_task
        finally:
            lock.release()

        await asyncio.shield(refresh_task)

    async def cancel_inflight_refresh(self) -> None:
        """Cancel any in-flight refresh task during ``ClientLifecycle.close``.

        Mirrors the legacy close block previously inlined in
        :meth:`ClientLifecycle.close` so the lifecycle never touches the
        private ``_refresh_task`` slot on this coordinator:

        - **No-op** when ``_refresh_task is None`` — a freshly-opened client
          that never triggered an auth refresh has no task to cancel.
        - **No-op** when ``_refresh_task.done()`` — a refresh wave that
          already finished must not be re-cancelled (it would be harmless
          but ``gather(return_exceptions=True)`` would still log noise).
        - **Cancel** an unfinished task and ``await`` it via
          ``asyncio.gather(..., return_exceptions=True)`` so the resulting
          :class:`asyncio.CancelledError` is absorbed and ``close()`` itself
          stays non-raising in the normal racing case.

        Slot-preservation invariant (CRITICAL — load-bearing): the
        ``self._refresh_task`` slot is INTENTIONALLY left intact after a
        cancel. Sibling waiters joined to the same single-flight refresh
        (see :meth:`await_refresh` and the ``asyncio.shield`` it wraps
        around the join) read the slot to identify the shared task; clearing
        it here would break the concurrency invariant pinned by
        ``tests/unit/test_runtime_auth.py::test_await_refresh_cancellation_preserves_task_slot``.
        The slot is replaced only on the NEXT refresh wave once the current
        task transitions to ``done()`` — never here, never in close.

        Behavior is identical to the pre-Wave-1 inlined block:

            refresh_task = host._auth_coord._refresh_task
            if refresh_task is not None and not refresh_task.done():
                refresh_task.cancel()
                await asyncio.gather(refresh_task, return_exceptions=True)

        Regression coverage:
        ``tests/unit/concurrency/test_session_close_refresh_race.py`` and
        the three focused unit tests added with this method in
        ``tests/unit/test_runtime_auth.py`` (the two companion
        ``reset_after_open`` tests for :class:`TransportDrainTracker` live
        in ``tests/unit/test_runtime_lifecycle.py``).
        """
        refresh_task = self._refresh_task
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            await asyncio.gather(refresh_task, return_exceptions=True)


__all__ = ["AuthRefreshCoordinator"]
