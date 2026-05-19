"""HTTP-client lifecycle helper for :class:`ClientCore`.

Owns the session-side open/close ordering that historically lived inline on
``ClientCore`` while delegating the raw HTTP transport to
:class:`notebooklm._kernel.Kernel`:

* ``_http_client`` — compatibility property backed by the concrete Kernel's
  live ``httpx.AsyncClient`` (or ``None`` when closed).
* ``_bound_loop`` — the event loop ``open()`` ran on; the cross-loop affinity
  guard in :meth:`ClientCore._perform_authed_post` (via
  :class:`AuthedTransport`) compares against this captured reference.
* ``_keepalive_task`` — the optional background task that pokes
  ``accounts.google.com/RotateCookies`` while the client is open.
* ``_keepalive_interval`` / ``_keepalive_storage_path`` — keepalive
  configuration; the interval is clamped against ``keepalive_min_interval``
  via :func:`notebooklm._core_helpers._resolve_keepalive_interval` (re-exported
  from :mod:`notebooklm._core` so the legacy import path keeps resolving).
* ``_timeout`` / ``_connect_timeout`` / ``_limits`` — HTTP timeouts and
  connection-pool tuning consumed in :meth:`open`.

Design constraints (load-bearing — see ``tests/unit/test_client_keepalive.py``,
``tests/unit/test_core_close.py``, ``tests/unit/test_vcr_config.py``, and
``tests/unit/test_auth_cookie_save_race.py``):

* ``__init__`` MUST be event-loop-agnostic. ``ClientCore`` is routinely
  constructed outside a running loop (sync-mode ``NotebookLMClient(auth)``
  before ``asyncio.run``), so this helper may not call
  ``asyncio.get_running_loop()`` or instantiate any ``asyncio.*`` primitive
  at construction time. The keepalive task is spawned inside :meth:`open`,
  which runs from a coroutine.

* :meth:`open` is idempotent — calling it twice with a live ``_http_client``
  is a no-op, preserving the legacy ``ClientCore.open()`` contract.

* :meth:`close` cancellation ordering: stop keepalive → drain poll tasks →
  save cookies → shielded Kernel ``aclose()``. Reversing any of these
  reintroduces the leak modes ``test_core_close.py`` pins down. The shielded
  ``aclose()`` is critical: without it, a ``CancelledError`` arriving
  mid-close leaks the underlying httpx transport.

* :meth:`open` no longer wraps the inner transport for synthetic-error
  injection — Tier-12 PR 12.6 lifted that path into the chain
  (:class:`notebooklm._middleware_error_injection.ErrorInjectionMiddleware`,
  wired by :class:`ClientCore.__init__`). When
  ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set, the chain middleware
  short-circuits before the chain leaf reaches httpx, so the httpx-layer
  transport stays a real, unwrapped transport at all times.

* :meth:`save_cookies` resolves ``save_cookies_to_storage`` from
  ``notebooklm._core`` at call time so the
  ``monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", …)``
  surface used by 8+ test files keeps affecting the live save path.

* ``_bound_loop`` is bound exactly once per :meth:`open` call; :meth:`close`
  does NOT unbind so an accidental cross-loop call after close still raises
  actionably rather than silently re-binding on the next ``open``. (See
  ``tests/integration/concurrency/test_cross_loop_affinity.py``.)

Field names (``_http_client``, ``_bound_loop``, ``_keepalive_task``,
``_keepalive_interval``, ``_keepalive_storage_path``, ``_timeout``,
``_connect_timeout``, ``_limits``) deliberately mirror the legacy
``ClientCore`` ivars so the compat ``@property`` bridges on ``ClientCore``
can stay readable for reviewers grepping the codebase. ``_http_client`` is now
a property bridge to the Kernel rather than lifecycle-owned storage.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

from ._core_constants import CORE_LOGGER_NAME
from ._kernel import Kernel
from .auth import AuthTokens

if TYPE_CHECKING:
    from ._core_auth import AuthRefreshCoordinator
    from ._core_cookie_persistence import CookiePersistence
    from ._core_drain import TransportDrainTracker
    from ._core_metrics import ClientMetrics
    from ._core_polling import PollRegistry
    from ._core_reqid import ReqidCounter
    from ._core_rpc import RpcExecutor
    from ._core_transport import AuthedTransport
    from .types import ConnectionLimits

# Logger name pinned via :data:`CORE_LOGGER_NAME` so log filters in
# tests — e.g. ``caplog.at_level("DEBUG", logger=CORE_LOGGER_NAME)`` —
# keep matching after the extraction.
logger = logging.getLogger(CORE_LOGGER_NAME)


class _LifecycleHost(Protocol):
    """Structural host boundary required by :class:`ClientLifecycle`.

    The Protocol pins exactly which collaborators the lifecycle reaches into
    on the host, so future refactors that move state around ``ClientCore``
    surface as Protocol violations rather than silent ``AttributeError``s
    at close-time. ``cookie_persistence`` and ``poll_registry`` mirror
    today's public attribute names on ``ClientCore``; ``_metrics_obj``,
    ``_drain_tracker``, and ``_auth_coord`` are the post-A1/A2/B1 helper
    handles. ``_authed_transport`` and ``_rpc_executor`` are nulled out by
    :meth:`ClientLifecycle.close` so a follow-up ``open()`` rebuilds them
    against the new ``httpx.AsyncClient`` (avoids stale closures over the
    previous client).
    """

    auth: AuthTokens
    _metrics_obj: ClientMetrics
    _drain_tracker: TransportDrainTracker
    _auth_coord: AuthRefreshCoordinator
    _reqid: ReqidCounter
    cookie_persistence: CookiePersistence
    poll_registry: PollRegistry
    _authed_transport: AuthedTransport | None
    _rpc_executor: RpcExecutor | None


class ClientLifecycle:
    """Owns HTTP-client open/close, keepalive, cookie persistence on close.

    Field names mirror the legacy ``ClientCore`` ivars so the compat
    ``@property`` bridges on ``ClientCore`` can delegate with
    ``return self._lifecycle._<attr>`` and stay readable.

    Construction is event-loop-agnostic — only plain values and ``None``
    placeholders are stored. The ``httpx.AsyncClient`` and the keepalive
    ``asyncio.Task`` are created inside :meth:`open` from a running loop.
    """

    def __init__(
        self,
        *,
        timeout: float,
        connect_timeout: float,
        limits: ConnectionLimits,
        keepalive_interval: float | None,
        keepalive_storage_path: Path | None,
        kernel: Kernel | None = None,
    ) -> None:
        self._kernel = kernel if kernel is not None else Kernel()
        self._timeout: float = timeout
        self._connect_timeout: float = connect_timeout
        # ``ConnectionLimits`` is constructed by the caller (``ClientCore``
        # applies the ``None → ConnectionLimits()`` default before passing
        # here). Keeping the default-resolution out of this helper avoids a
        # types.py import cycle.
        self._limits: ConnectionLimits = limits
        # Pre-clamped by :func:`notebooklm._core_helpers._resolve_keepalive_interval`
        # (re-exported as ``notebooklm._core._resolve_keepalive_interval``) at
        # the ``ClientCore`` boundary so the floor-vs-user-value branching
        # stays in one place — the seam helper.
        self._keepalive_interval: float | None = keepalive_interval
        self._keepalive_storage_path: Path | None = keepalive_storage_path
        # The live HTTP client is owned by ``self._kernel``. The
        # ``_http_client`` property below preserves the historical lifecycle
        # attribute for tests and private callers that probe it directly.
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._keepalive_task: asyncio.Task[None] | None = None

    @property
    def _http_client(self) -> httpx.AsyncClient | None:
        return self._kernel.http_client

    @_http_client.setter
    def _http_client(self, value: httpx.AsyncClient | None) -> None:
        self._kernel.http_client = value

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    def is_open(self) -> bool:
        """Return whether :meth:`open` has run without a subsequent close."""
        return self._http_client is not None

    def get_bound_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the event loop :meth:`open` captured, or ``None`` if never opened.

        Phase C1's RPC-dispatch facade uses this accessor (instead of reaching
        for ``self._lifecycle._bound_loop`` directly) so the two-underscore
        attribute stays an implementation detail of this helper.
        """
        return self._bound_loop

    def get_http_client(self) -> httpx.AsyncClient:
        """Return the live HTTP client via the concrete Kernel."""
        return self._kernel.get_http_client()

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    async def open(self, host: _LifecycleHost) -> None:
        """Open the HTTP client connection.

        Idempotent: if ``_http_client`` is already non-``None`` this is a
        no-op. Captures the running event loop in ``_bound_loop`` so the
        cross-loop affinity guard in :meth:`ClientCore._perform_authed_post`
        fails fast if the same client is later driven from a different loop.
        Re-opening on a different loop (after a prior :meth:`close`)
        intentionally replaces the binding — ``open()`` is the only binding
        moment.

        Synthetic-error injection moved from this layer to the chain in
        Tier-12 PR 12.6 — see
        :class:`notebooklm._middleware_error_injection.ErrorInjectionMiddleware`
        for the new substitution point. The httpx transport built here is
        always a real, unwrapped transport.
        """
        if self._http_client is not None:
            return

        # Capture event-loop affinity before any awaitable resource is built
        # so the binding is consistent with the loop that owns every primitive
        # constructed below.
        self._bound_loop = asyncio.get_running_loop()
        # P0-2: propagate the captured loop into every helper that owns a
        # loop-bound primitive (lock / condition / task slot). Each helper
        # consults its own ``_bound_loop`` at the top of its async entry
        # points (``drain``, ``next_reqid``, ``await_refresh``) so a
        # cross-loop call surfaces an actionable ``RuntimeError`` at the
        # call site rather than hanging on a primitive bound to a dead
        # loop. ``ChatAPI`` / ``ArtifactPollingService`` reach the bound
        # loop through ``ClientCore.bound_loop`` (which reads
        # ``ClientLifecycle.get_bound_loop()``) so no further propagation
        # is needed there.
        host._drain_tracker.set_bound_loop(self._bound_loop)
        host._reqid.set_bound_loop(self._bound_loop)
        host._auth_coord.set_bound_loop(self._bound_loop)
        # Reset the drain flag so a previously-drained-then-reopened client
        # admits new transport work again. Direct attribute write mirrors the
        # legacy ``self._draining = False`` line.
        host._drain_tracker._draining = False

        # Delegate HTTP-client construction and open-time cookie baseline
        # capture to the concrete transport kernel. The lifecycle still owns
        # loop binding and open/close ordering.
        await self._kernel.open(
            auth=host.auth,
            timeout=self._timeout,
            connect_timeout=self._connect_timeout,
            limits=self._limits,
            capture_cookie_snapshot=host.cookie_persistence.capture_open_snapshot,
        )

        # Spawn the keepalive task once the client is ready.
        if self._keepalive_interval is not None:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(host, self._keepalive_interval)
            )

    async def save_cookies(
        self,
        host: _LifecycleHost,
        jar: httpx.Cookies,
        path: Path | None = None,
    ) -> None:
        """Persist a cookie jar through the shared cookie-persistence collaborator.

        Single chokepoint used by :meth:`close`, :meth:`_keepalive_loop`, and
        ``NotebookLMClient.refresh_auth``. The storage writer is resolved
        from ``notebooklm._core`` at call time so the
        ``monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", …)``
        surface used by 8+ test files keeps affecting the live save path.
        """
        from . import _core as _core_module

        await host.cookie_persistence.save(
            jar,
            path,
            save_cookies_to_storage=_core_module.save_cookies_to_storage,
            to_thread=asyncio.to_thread,
        )

    async def close(self, host: _LifecycleHost) -> None:
        """Close the HTTP client connection.

        Cancellation safety: the entire close sequence is wrapped in
        ``try/finally`` and the final ``aclose()`` is wrapped in
        :func:`asyncio.shield` — without the shield, a ``CancelledError``
        arriving during keepalive teardown or the cookie save would skip
        ``aclose()`` and leak the underlying httpx transport.
        :meth:`Kernel.aclose` clears the live HTTP client in its own
        ``finally`` so the instance is consistently marked closed even if
        shielded teardown raises.

        Poll-task drain: in-flight artifact poll tasks held by
        ``host.poll_registry`` are cancelled and awaited before the HTTP
        client is torn down. Without this, a leader poll waking mid-aclose
        would issue a request against an already-closed transport and
        surface as a confusing httpx error. The drain uses
        ``return_exceptions=True`` so a single misbehaving task can't block
        the rest of the close sequence.

        Nulls out ``host._authed_transport`` and ``host._rpc_executor`` so a
        follow-up :meth:`open` rebuilds the transport collaborators against
        the new ``httpx.AsyncClient`` (the old ones close over the previous
        client and would issue requests against the torn-down pool).
        """
        try:
            # Stop the keepalive task before tearing down the HTTP client so
            # the loop can't issue a poke against an already-closed transport.
            if self._keepalive_task is not None:
                self._keepalive_task.cancel()
                await asyncio.gather(self._keepalive_task, return_exceptions=True)
                self._keepalive_task = None

            # P0-1: cancel any in-flight auth refresh task BEFORE the cookie
            # save or shielded ``aclose()``. Without this, a slow refresh
            # racing against close would survive the close path and continue
            # holding the now-torn-down ``httpx.AsyncClient``, surfacing as a
            # confusing httpx error or a "coroutine was never awaited" GC
            # warning. ``gather(..., return_exceptions=True)`` absorbs the
            # ``CancelledError`` so close itself stays non-raising. We check
            # both ``is None`` (no refresh has ever fired) and ``done()`` (a
            # successful refresh wave already finished) so the cancel is a
            # true no-op outside the racing case.
            refresh_task = host._auth_coord._refresh_task
            if refresh_task is not None and not refresh_task.done():
                refresh_task.cancel()
                await asyncio.gather(refresh_task, return_exceptions=True)

            # Drain in-flight artifact poll tasks. Snapshot first so concurrent
            # registry mutations (a finishing leader removing its entry) don't
            # race with the cancel/gather pair.
            poll_tasks = host.poll_registry.active_tasks()
            if poll_tasks:
                for task in poll_tasks:
                    task.cancel()
                await asyncio.gather(*poll_tasks, return_exceptions=True)

            if self._http_client:
                try:
                    # Single source of truth for the on-close save: takes the
                    # in-process lock, snapshots, off-loads. Serializes
                    # naturally with any keepalive save still finishing in a
                    # worker thread — close() owns the freshest jar and must
                    # win, not the older snapshot.
                    await self.save_cookies(host, self._kernel.cookies)
                except Exception as e:
                    logger.warning("Failed to sync refreshed cookies during close: %s", e)
        finally:
            if self._http_client:
                try:
                    # Shield: cancellation arriving mid-aclose must not leak
                    # the transport. The shielded aclose runs to completion;
                    # ``self._http_client = None`` then makes ``is_open``
                    # return False correctly.
                    await asyncio.shield(self._kernel.aclose())
                finally:
                    # Null out the transport collaborators so a follow-up
                    # ``open()`` rebuilds them against the new
                    # ``httpx.AsyncClient`` (the old ones close over the
                    # previous client).
                    host._authed_transport = None
                    host._rpc_executor = None

    # ------------------------------------------------------------------
    # Keepalive
    # ------------------------------------------------------------------

    async def _keepalive_loop(self, host: _LifecycleHost, interval: float) -> None:
        """Background loop that periodically pokes the identity surface.

        Sleeps ``interval`` seconds between iterations, then calls
        :func:`notebooklm.auth._rotate_cookies` to elicit ``__Secure-1PSIDTS``
        rotation. Any rotated cookies are persisted to ``storage_state.json``
        immediately (off-loop, via :func:`asyncio.to_thread`) so a long-lived
        client's freshness survives a crash.

        Error handling is split by failure mode:

        - Poke failures (network blips, ``accounts.google.com`` downtime) are
          opportunistic and logged at DEBUG. The next iteration retries.
        - Persistence failures hide the most important class of bug — a
          rotated cookie that exists in memory but not on disk — so they are
          logged at WARNING with the storage path.

        Both classes never propagate; the loop only exits via
        :class:`asyncio.CancelledError` from :meth:`close`.
        """
        logger.debug("Keepalive task started (interval=%.1fs)", interval)
        # Resolved from ``notebooklm._core`` once, before the loop, so the
        # existing ``monkeypatch.setattr("notebooklm._core._rotate_cookies",
        # …)`` surface in ``test_close_cancellation_leak.py`` keeps affecting
        # the live keepalive loop after the extraction. The attribute lookup
        # on ``_core_module._rotate_cookies`` still happens at call time, so
        # late monkeypatches remain effective without re-importing every tick.
        from . import _core as _core_module

        try:
            while True:
                await asyncio.sleep(interval)
                client = self._http_client
                if client is None:
                    # Client closed concurrently; exit gracefully.
                    return

                try:
                    # Bypass the layer-1 dedup guards: this loop is self-paced
                    # by ``keepalive_min_interval`` and never runs concurrently
                    # with itself. Pass the storage path so the bare call
                    # bumps the *per-profile* in-process timestamp, letting
                    # concurrent layer-1 callers (e.g. spawned ``fetch_tokens``
                    # tasks on the same profile) and other keepalive loops on
                    # the same profile see the fresh rotation and skip.
                    await _core_module._rotate_cookies(client, self._keepalive_storage_path)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - opportunistic best-effort
                    logger.debug("Keepalive poke failed (non-fatal): %s", exc)
                    continue

                if self._keepalive_storage_path is None:
                    continue

                try:
                    # save_cookies handles snapshot + lock + off-load.
                    await self.save_cookies(host, client.cookies)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Keepalive cookie persistence to %s failed: %s",
                        self._keepalive_storage_path,
                        exc,
                    )
        except asyncio.CancelledError:
            logger.debug("Keepalive task cancelled")
            raise


__all__ = ["ClientLifecycle", "_LifecycleHost"]
