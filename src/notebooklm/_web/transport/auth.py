"""Auth refresh coordinator helper for the client runtime.

Owns the auth refresh state machine and snapshot serialization:

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
  ``kernel.cookies`` read outside any auth lock). That sync is public
  compatibility-only; first-party decisions already read the kernel jar.
* The ``_refresh_task`` slot is intentionally NOT cleared when a waiter is
  cancelled mid-shield — concurrency tests assert task identity across
  cancellation so siblings joined to the same single-flight refresh see the
  same completion.

Collaborator surface:

The coordinator depends on explicit per-method collaborators:
:meth:`snapshot` and :meth:`update_auth_tokens` take an
``auth: AuthTokens`` kwarg, :meth:`update_auth_headers` takes
``auth: AuthTokens`` plus ``kernel: Kernel``, and the lock-wait metric is
recorded through ``self._metrics`` (already supplied at construction).
This keeps every dependency on the coordinator's surface concrete and
narrow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from ..._loop_affinity import assert_bound_loop
from ..._loop_bound import LoopBoundPrimitive
from ..._runtime.config import CORE_LOGGER_NAME
from ...auth import AuthTokens
from .request_types import AuthSnapshot

if TYPE_CHECKING:
    from ..._auth.cookie_types import CookieJar
    from ..._client_metrics import ClientMetrics
    from .kernel import Kernel

# Logger name pinned via :data:`CORE_LOGGER_NAME` so log filters in
# tests — e.g. ``caplog.at_level("DEBUG", logger=CORE_LOGGER_NAME)`` —
# keep matching after the extraction.
logger = logging.getLogger(CORE_LOGGER_NAME)


@dataclass(frozen=True)
class _RefreshResult:
    """A refresh result that never raises across its shared Task boundary."""

    value: AuthTokens | None = None
    error: BaseException | None = None


async def _run_refresh_after_gate(
    gate: asyncio.Future[None],
    callback: Callable[[int], Awaitable[AuthTokens]],
    expected_epoch: int,
) -> _RefreshResult:
    """Defer callback invocation until task creation releases the refresh lock."""
    try:
        await gate
        return _RefreshResult(value=await callback(expected_epoch))
    except BaseException as exc:
        return _RefreshResult(error=exc)


class AuthRefreshCoordinator(LoopBoundPrimitive):
    """Owns refresh single-flight, snapshot serialization, and auth-header sync.

    Field names (``_refresh_lock``, ``_refresh_task``, ``_refresh_callback``,
    ``_auth_snapshot_lock``) are kept stable so coordinator-specific
    tests remain easy to audit.
    """

    def __init__(
        self,
        *,
        refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None = None,
        metrics: ClientMetrics | None = None,
    ) -> None:
        # Lazily-created — ``asyncio.Lock()`` needs a running loop in some
        # Python versions, and this object can be constructed outside one.
        self._refresh_lock: asyncio.Lock | None = None
        self._refresh_task: asyncio.Task[_RefreshResult] | None = None
        self._refresh_task_epoch: int | None = None
        self._refresh_callback: Callable[[int], Awaitable[AuthTokens]] | None = refresh_callback
        # ``await_refresh`` records lock-wait latency via this metrics dep.
        # The same ``self._metrics`` slot is read by :meth:`snapshot` and
        # :meth:`update_auth_tokens` too. ``None`` is a safe
        # fallback for tests that construct the coordinator standalone
        # without a metrics collaborator; the lock-wait latency is simply not
        # recorded in that case.
        self._metrics: ClientMetrics | None = metrics
        # Distinct from ``_refresh_lock`` — see module docstring.
        self._auth_snapshot_lock: asyncio.Lock | None = None
        # Activated/fenced in lockstep with Kernel by WebTransportLifecycle.
        # This scalar is intentionally synchronous so prepare_close can fence
        # refresh publication before its first await.
        self._active_epoch: int | None = None
        # ``_bound_loop`` (the loop-affinity guard consulted by
        # :meth:`await_refresh` before touching the lazy ``_refresh_lock``)
        # and ``set_bound_loop`` are provided by the
        # :class:`~notebooklm._loop_bound.LoopBoundPrimitive` base. The
        # coordinator overrides ``_on_loop_rebind`` (and exposes
        # ``reset_after_open``) to discard both lazy locks on a loop change /
        # reopen — a lazily-allocated lock is NOT rebuilt implicitly per
        # ``open()``; once cached it survives close→reopen and would be
        # reused on the new loop (#2106).

    @property
    def has_refresh_callback(self) -> bool:
        """``True`` iff a refresh callback was wired at construction.

        Used by :class:`notebooklm._web.transport.middleware.auth_refresh.AuthRefreshMiddleware`
        to gate the refresh-and-retry branch: a client constructed without
        a ``refresh_callback`` should propagate auth errors directly.
        Exposing this as a property avoids reaching into the private
        ``_refresh_callback`` attribute from outside the coordinator.
        """
        return self._refresh_callback is not None

    def activate_epoch(self, epoch: int) -> None:
        """Activate the web resource generation for snapshots/refreshes."""
        self._active_epoch = epoch

    def fence_epoch(self, epoch: int | None) -> None:
        """Retire ``epoch`` synchronously before refresh-task settlement."""
        if epoch is None or self._active_epoch == epoch:
            self._active_epoch = None

    def assert_epoch(self, expected_epoch: int) -> None:
        if self._active_epoch != expected_epoch:
            raise RuntimeError(
                "NotebookLMClient auth generation is retired "
                f"(expected={expected_epoch}, active={self._active_epoch!r})."
            )

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Discard both cached lazy locks when the bound loop changes.

        Fires from :meth:`~notebooklm._loop_bound.LoopBoundPrimitive.set_bound_loop`
        only on a real loop change (and before ``_bound_loop`` is updated), so
        ``set_bound_loop`` is self-consistent even if called independently of
        :meth:`reset_after_open`: a stale lock bound to the old loop must never
        be reused after a rebind. The production ``open()`` path also calls
        :meth:`reset_after_open` immediately after, so the discard is
        idempotent there.

        Currently a latent (not active) hazard (#2106): every critical section
        under these locks is purely synchronous, and ``asyncio.Lock`` binds its
        loop only on the *contended* acquire path, so a stale lock cannot
        actually trip the cross-loop ``RuntimeError`` today. The discard brings
        the coordinator in line with its clear-on-rebind siblings
        (:class:`~notebooklm._client_composed.ClientComposed`,
        ``SourceUploadPipeline``, ``ChatAPI``) so any future ``await`` under
        one of these locks cannot activate the trap.

        Deliberately does NOT touch ``_refresh_task``: the slot-preservation
        invariant in :meth:`cancel_inflight_refresh` keeps it intact so
        sibling waiters can identify the shared single-flight task; after
        ``close()`` the task is always ``done()`` (``cancel_inflight_refresh``
        gathers it) and :meth:`await_refresh` replaces done tasks on the next
        refresh wave.
        """
        self._refresh_lock = None
        self._auth_snapshot_lock = None

    def reset_after_open(self) -> None:
        """Discard both lazy locks so a reopened client rebinds them.

        Called from :meth:`ClientLifecycle.open` (alongside the
        per-collaborator ``set_bound_loop`` propagation) so a client that was
        closed and reopened on a *different* event loop builds fresh
        ``asyncio.Lock`` instances on the new loop instead of reusing stale
        ones bound to the old (now-dead) loop. On Python 3.10/3.11 a stale
        lock raises "bound to a different event loop" on the contended
        acquire path; today that path is unreachable here (no ``await`` runs
        under either lock — see :meth:`_on_loop_rebind`), so this is a
        consistency hardening, not an active-bug fix (#2106).

        Mirrors :meth:`ClientComposed.reset_after_open`. Deliberately narrow:
        dropping the references is enough because both locks are reallocated
        lazily by :meth:`get_refresh_lock` / :meth:`get_auth_snapshot_lock`
        from inside the new loop. ``_refresh_task`` and ``_refresh_callback``
        are left untouched (see :meth:`_on_loop_rebind` for the task-slot
        invariant).
        """
        self._refresh_lock = None
        self._auth_snapshot_lock = None

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
    # ``await`` mid-mutation) will trip those guards. Previously a facade
    # method mirrored each body; now ``snapshot`` and ``update_auth_tokens``
    # here are the only implementations, so there is no second body to keep
    # in sync.
    # ------------------------------------------------------------------

    async def snapshot(
        self,
        *,
        auth: AuthTokens,
        expected_epoch: int | None = None,
    ) -> AuthSnapshot:
        """Capture the current auth scalars as a frozen snapshot.

        Acquires :attr:`_auth_snapshot_lock` for the four scalar reads so a
        concurrent :meth:`update_auth_tokens` cannot interleave between
        ``csrf_token`` / ``session_id`` / ``authuser`` / ``account_email``.
        The critical section is purely synchronous attribute reads — no
        ``await`` — so the lock is uncontested in steady state and refresh's
        tiny write block cannot block RPC throughput.

        The whole-attempt atomicity for ``(csrf, sid, cookies)`` on the wire
        is completed at the transport terminal:
        :meth:`RuntimeTransport.refresh_request_for_current_auth` captures a
        fresh snapshot, rebuilds the envelope, and
        :meth:`RuntimeTransport.terminal` calls ``Kernel.post`` with no await
        between materialization and the POST (see the AST guards in
        ``tests/unit/test_concurrency_refresh_race.py``). This lock guarantees
        the four scalars in the returned snapshot are coherent with each other;
        the terminal no-await rule keeps the cookie axis aligned with the
        materialized envelope.

        ``auth`` is passed explicitly per call; the lock-wait metric is
        recorded through ``self._metrics`` (supplied at construction).
        """
        if expected_epoch is not None:
            self.assert_epoch(expected_epoch)
        wait_start = time.perf_counter()
        async with self.get_auth_snapshot_lock():
            if expected_epoch is not None:
                self.assert_epoch(expected_epoch)
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
        expected_epoch: int | None = None,
    ) -> None:
        """Atomically update ``auth.csrf_token`` + ``auth.session_id`` only.

        Does NOT touch the http client — the cookie-jar sync is the separate
        :meth:`update_auth_headers` concern. Conflating the two would let a
        snapshot acquired between this method and the header sync observe a
        new token pair against stale cookies, which is exactly the torn-state
        scenario the snapshot lock exists to prevent.

        ``auth`` is passed explicitly (no ``_AuthRefreshHost`` shape).
        """
        if expected_epoch is not None:
            self.assert_epoch(expected_epoch)
        lock = self.get_auth_snapshot_lock()
        wait_start = time.perf_counter()
        await lock.acquire()
        try:
            if expected_epoch is not None:
                self.assert_epoch(expected_epoch)
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

    async def install_profile_session(
        self,
        *,
        auth: AuthTokens,
        target_cookie_jar: httpx.Cookies,
        source_cookie_jar: httpx.Cookies,
        expected_cookie_jar: CookieJar,
        expected_authuser: int,
        expected_account_email: str | None,
        expected_generation: int,
        authuser: int,
        account_email: str | None,
        expected_epoch: int | None = None,
    ) -> bool | None:
        """Atomically install one stored cookie and account-route generation.

        ``None`` means the authoritative live jar changed while the disk sample
        was being selected, so the caller must preserve and retry that newer
        live state. Once the lock is acquired, the check and all cookie/route
        writes are synchronous: cancellation cannot leave a mixed generation,
        and :meth:`snapshot` cannot observe a torn routing pair.
        """
        if expected_epoch is not None:
            self.assert_epoch(expected_epoch)
        lock = self.get_auth_snapshot_lock()
        wait_start = time.perf_counter()
        await lock.acquire()
        try:
            if expected_epoch is not None:
                self.assert_epoch(expected_epoch)
            if self._metrics is not None:
                self._metrics.record_lock_wait(time.perf_counter() - wait_start)
            return auth._replace_profile_session(
                target_cookie_jar=target_cookie_jar,
                source_cookie_jar=source_cookie_jar,
                expected_cookie_jar=expected_cookie_jar,
                expected_authuser=expected_authuser,
                expected_account_email=expected_account_email,
                expected_generation=expected_generation,
                authuser=authuser,
                account_email=account_email,
            )
        finally:
            lock.release()

    def update_auth_headers(
        self,
        *,
        auth: AuthTokens,
        kernel: Kernel,
        expected_epoch: int | None = None,
    ) -> None:
        """Update public AuthTokens cookie shadows from the kernel-owned jar.

        Compat-only (ADR-0032 Phase A): the kernel's jar is already the sole
        internal live-cookie authority — persistence reads ``kernel.cookies``
        and no internal code reads ``auth.cookie_jar`` after open. This
        write-back exists solely so a PUBLIC holder of ``auth`` that reads
        ``auth.cookies`` / ``auth.cookie_jar`` sees fresh values. When those
        fields are removed (ADR-0032 Phase B, next major), this method and the
        write-back go with them.

        Synchronous on purpose — no await — so callers can run this without
        any auth lock held. The httpx client's cookie jar is authoritative
        once the session is open; re-injecting startup cookies here would
        overwrite cookies refreshed during redirects to
        ``accounts.google.com``.

        ``auth`` and ``kernel`` are passed explicitly per call so the
        coordinator does not need an owner-shaped host.

        Raises:
            RuntimeError: If a directly constructed kernel has neither an auth
                bootstrap seed nor an initialized HTTP client.
        """
        # Compatibility-only assignment (ADR-0032 Phase A). Rebind the derived
        # public map alongside the public jar; no first-party request, routing,
        # recovery, or persistence decision reads either shadow after the
        # bootstrap hand-off to Kernel.
        if expected_epoch is not None:
            self.assert_epoch(expected_epoch)
        auth.replace_cookie_jar(kernel.cookies)

    # ------------------------------------------------------------------
    # Single-flight refresh task.
    # ------------------------------------------------------------------

    async def await_refresh(self, expected_epoch: int) -> None:
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

        This method takes no host parameter — the metrics dependency it needs
        is supplied via the ``metrics`` kwarg on :meth:`__init__`. The other
        coordinator methods (``snapshot``, ``update_auth_tokens``,
        ``update_auth_headers``) likewise take explicit per-method
        collaborators rather than an owner facade.
        """
        # Catch cross-loop refresh before touching ``_refresh_lock``.
        # The lock is lazily bound to the loop that first awaited
        # ``get_refresh_lock`` — a cross-loop call would hang on the
        # ``await lock.acquire()`` if we let it through.
        assert_bound_loop(self._bound_loop)
        self.assert_epoch(expected_epoch)
        if self._refresh_callback is None:
            raise RuntimeError(
                "AuthRefreshCoordinator.await_refresh called without a "
                "refresh_callback configured — wire one via "
                "AuthRefreshCoordinator(refresh_callback=...) (or by "
                "constructing NotebookLMClient with refresh_callback=...) before "
                "triggering an auth refresh."
            )

        # Lazy-init the lock on first refresh attempt. Every concurrent
        # caller resolves to the same instance because ``get_refresh_lock``
        # runs synchronously in a single-threaded asyncio loop, so the
        # single-flight task creation below is preserved.
        lock = self.get_refresh_lock()
        wait_start = time.perf_counter()
        start_gate: asyncio.Future[None] | None = None
        await lock.acquire()
        try:
            self.assert_epoch(expected_epoch)
            # ``record_lock_wait`` lives INSIDE the ``try`` so a metric-side
            # exception (e.g. a misconfigured spy in tests, or a runtime bug
            # in :class:`ClientMetrics`) cannot leave the refresh lock held —
            # the ``finally`` releases unconditionally. Mirrors the same
            # hardening on :meth:`update_auth_tokens`; the call is synchronous
            # so no await runs between acquiring and releasing the lock.
            if self._metrics is not None:
                self._metrics.record_lock_wait(time.perf_counter() - wait_start)
            if self._refresh_task is not None and not self._refresh_task.done():
                if self._refresh_task_epoch != expected_epoch:
                    raise RuntimeError(
                        "Auth refresh belongs to a different resource generation "
                        f"(expected={expected_epoch}, active={self._refresh_task_epoch})."
                    )
                refresh_task = self._refresh_task
                logger.debug("Joining existing refresh task")
            else:
                start_gate = asyncio.get_running_loop().create_future()
                refresh_task = asyncio.create_task(
                    _run_refresh_after_gate(
                        start_gate,
                        self._refresh_callback,
                        expected_epoch,
                    )
                )
                self._refresh_task = refresh_task
                self._refresh_task_epoch = expected_epoch
        finally:
            lock.release()
        if start_gate is not None and not start_gate.done():
            start_gate.set_result(None)

        result = await asyncio.shield(refresh_task)
        if result.error is not None:
            raise result.error
        self.assert_epoch(expected_epoch)

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

        Behavior is equivalent to:

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
        if refresh_task is not None:
            settled = (await asyncio.gather(refresh_task, return_exceptions=True))[0]
            if isinstance(settled, _RefreshResult) and isinstance(
                settled.error, (KeyboardInterrupt, SystemExit)
            ):
                raise settled.error


__all__ = ["AuthRefreshCoordinator"]
