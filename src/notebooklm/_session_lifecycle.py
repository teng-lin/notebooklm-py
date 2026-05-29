"""HTTP-client lifecycle helper for the client-owned runtime.

Owns the open/close ordering that historically lived inline on the deleted
session facade while delegating the raw HTTP transport to
:class:`notebooklm._kernel.Kernel`:

* ``_http_client`` — compatibility property backed by the concrete Kernel's
  live ``httpx.AsyncClient`` (or ``None`` when closed).
* ``_bound_loop`` — the event loop ``open()`` ran on; the cross-loop affinity
  guard in the transport path compares against this captured reference.
* ``_keepalive_task`` — the optional background task that pokes
  ``accounts.google.com/RotateCookies`` while the client is open.
* ``_keepalive_interval`` / ``_keepalive_storage_path`` — keepalive
  configuration; the interval is clamped against ``keepalive_min_interval``
  via :func:`notebooklm._session_helpers._resolve_keepalive_interval`.
* ``_timeout`` / ``_connect_timeout`` / ``_limits`` — HTTP timeouts and
  connection-pool tuning consumed in :meth:`open`.

Design constraints (load-bearing — see ``tests/unit/test_client_keepalive.py``,
``tests/unit/test_session_close.py``, ``tests/unit/test_vcr_config.py``, and
``tests/unit/test_auth_cookie_save_race.py``):

* ``__init__`` MUST be event-loop-agnostic. ``NotebookLMClient`` is routinely
  constructed outside a running loop (sync-mode ``NotebookLMClient(auth)``
  before ``asyncio.run``), so this helper may not call
  ``asyncio.get_running_loop()`` or instantiate any ``asyncio.*`` primitive
  at construction time. The keepalive task is spawned inside :meth:`open`,
  which runs from a coroutine.

* :meth:`open` is idempotent — calling it twice with a live ``_http_client``
  is a no-op, preserving the legacy client-open contract.

* :meth:`close` cancellation ordering: stop keepalive → run registered drain
  hooks → save cookies → shielded Kernel ``aclose()``. Reversing any of these
  reintroduces the leak modes ``test_session_close.py`` pins down. The shielded
  ``aclose()`` is critical: without it, a ``CancelledError`` arriving
  mid-close leaks the underlying httpx transport.

* :meth:`open` no longer wraps the inner transport for synthetic-error
  injection — Tier-12 PR 12.6 lifted that path into the chain
  (:class:`notebooklm._middleware_error_injection.ErrorInjectionMiddleware`,
  wired by client internals composition). When
  ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set, the chain middleware
  short-circuits before the chain leaf reaches httpx, so the httpx-layer
  transport stays a real, unwrapped transport at all times.

* :meth:`save_cookies` forwards the lifecycle's ``_cookie_saver`` wrapper
  (``_default_cookie_saver`` by default) to ``CookiePersistence._save``;
  the wrapper late-binds ``save_cookies_to_storage`` from
  ``notebooklm._auth.storage`` at call time so a ``monkeypatch.setattr``
  on the canonical seam keeps affecting the live save path.

* ``_bound_loop`` is bound exactly once per :meth:`open` call; :meth:`close`
  does NOT unbind so an accidental cross-loop call after close still raises
  actionably rather than silently re-binding on the next ``open``. (See
  ``tests/integration/concurrency/test_cross_loop_affinity.py``.)

Field names (``_http_client``, ``_bound_loop``, ``_keepalive_task``,
``_keepalive_interval``, ``_keepalive_storage_path``, ``_timeout``,
``_connect_timeout``, ``_limits``) historically mirrored the legacy
legacy session ivars when the deleted session facade still held ``@property``
bridges that forwarded to them. Those bridges were retired in the
session-shrink arc
(see ``tests/_lint/test_no_session_compat_bridges.py`` and the
"closed for the property-shim debt" note in ``docs/architecture.md``);
the names are kept verbatim now for grep discoverability across the test
suite — callers reach the storage through the client-owned lifecycle
collaborator. ``_http_client`` is a thin accessor returning the live
``httpx.AsyncClient`` from the concrete Kernel.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ._kernel import Kernel
from ._session_config import CORE_LOGGER_NAME
from .auth import AuthTokens

if TYPE_CHECKING:
    from ._client_composed import ClientComposed
    from ._cookie_persistence import CookiePersistence
    from ._reqid_counter import ReqidCounter
    from ._session_auth import AuthRefreshCoordinator
    from ._source_upload import SourceUploadPipeline
    from ._transport_drain import TransportDrainTracker
    from .auth import CookieSaveResult
    from .types import ConnectionLimits


# ---------------------------------------------------------------------------
# Injectable seams (Phase 2 PR 3 of `.sisyphus/plans/refactor-completion-plan.md`)
# ---------------------------------------------------------------------------
#
# These two callable seams let host integrations swap the on-disk cookie
# writer and the identity-surface poke without monkeypatching the
# canonical seams directly. The defaults preserve the late-binding
# contract: tests patch ``notebooklm._auth.storage.save_cookies_to_storage``
# or ``notebooklm._auth.keepalive._rotate_cookies`` and the wrapper body
# observes the swap because it resolves the target inside its body — see
# ``_default_cookie_saver`` / ``_default_cookie_rotator`` below.
#
# Concrete return types (not ``Callable[..., Any]``) are deliberate so mypy
# rejects an ``async def`` mistakenly passed for ``cookie_saver`` (the
# storage writer runs INSIDE ``asyncio.to_thread`` and must be sync) and a
# plain ``def`` mistakenly passed for ``cookie_rotator`` (the rotator is
# awaited from the keepalive loop and must return an ``Awaitable``).

#: Callable shape for the on-disk cookie writer. ``CookieSaveResult`` is
#: imported under ``TYPE_CHECKING``; the inner forward-string keeps the
#: alias evaluable at runtime without a circular auth import.
CookieSaver = Callable[..., "bool | CookieSaveResult"]

#: Callable shape for the keepalive-loop cookie rotator. ``Awaitable[None]``
#: pins the async-callable contract so mypy rejects sync ``def`` callables
#: at the injection point.
CookieRotator = Callable[..., Awaitable[None]]


def _default_cookie_saver(*args: Any, **kwargs: Any) -> bool | CookieSaveResult:
    """Default ``cookie_saver``: late-bind to ``_auth.storage.save_cookies_to_storage``.

    The import lives INSIDE the function body (intentionally, NOT at
    module top) so any
    ``monkeypatch.setattr("notebooklm._auth.storage.save_cookies_to_storage", …)``
    swap is observed at call time. A top-level import would capture the
    original reference at module-import time and silently ignore later
    patches. The historical ``notebooklm._core`` indirection was removed
    in v0.5.0 when the ``_core`` compatibility shim was deleted.

    ``def`` (not ``async def``) is load-bearing: this wrapper is invoked
    INSIDE ``asyncio.to_thread(_save)`` in
    :meth:`CookiePersistence._save`. ``save_cookies_to_storage`` itself is
    a sync writer at ``_auth/storage.py:303``. Making this wrapper ``async``
    would surface as a ``TypeError`` at runtime when ``to_thread`` tries
    to call the coroutine in a worker thread.
    """
    from ._auth.storage import save_cookies_to_storage

    return save_cookies_to_storage(*args, **kwargs)


async def _default_cookie_rotator(*args: Any, **kwargs: Any) -> None:
    """Default ``cookie_rotator``: late-bind to ``_auth.keepalive._rotate_cookies``.

    The import lives INSIDE the function body so any
    ``monkeypatch.setattr("notebooklm._auth.keepalive._rotate_cookies", …)``
    swap is observed at call time. The historical ``notebooklm._core``
    indirection was removed in v0.5.0 when the ``_core`` compatibility
    shim was deleted.

    ``async def`` (not ``def``) is load-bearing: ``_rotate_cookies`` at
    ``_auth/keepalive.py:298`` is async and must be awaited.
    """
    from ._auth.keepalive import _rotate_cookies

    await _rotate_cookies(*args, **kwargs)


# Logger name pinned via :data:`CORE_LOGGER_NAME` so log filters in
# tests — e.g. ``caplog.at_level("DEBUG", logger=CORE_LOGGER_NAME)`` —
# keep matching after the extraction.
logger = logging.getLogger(CORE_LOGGER_NAME)


class ClientLifecycle:
    """Owns HTTP-client open/close, keepalive, cookie persistence on close.

    Field names mirror the legacy lifecycle ivars for grep discoverability
    across the test suite. The ``@property`` bridges that historically
    delegated with ``return self._lifecycle._<attr>`` were retired in the
    session-shrink arc; callers now reach these fields directly via
    the client-owned lifecycle collaborator.

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
        cookie_saver: CookieSaver | None = None,
        cookie_rotator: CookieRotator | None = None,
    ) -> None:
        self._kernel = kernel if kernel is not None else Kernel()
        self._timeout: float = timeout
        self._connect_timeout: float = connect_timeout
        # ``ConnectionLimits`` is constructed by the caller, which applies the
        # ``None -> ConnectionLimits()`` default before passing here. Keeping
        # the default-resolution out of this helper avoids a types.py import
        # cycle.
        self._limits: ConnectionLimits = limits
        # Pre-clamped by :func:`notebooklm._session_helpers._resolve_keepalive_interval`
        # at the client composition boundary so the floor-vs-user-value
        # branching stays in one place — the seam helper.
        self._keepalive_interval: float | None = keepalive_interval
        self._keepalive_storage_path: Path | None = keepalive_storage_path
        # The live HTTP client is owned by ``self._kernel``. The
        # ``_http_client`` property below preserves the historical lifecycle
        # attribute for tests and private callers that probe it directly.
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        # Injectable seams (Phase 2 PR 3). ``None`` resolves to the module-
        # level late-binding default — the default wraps the canonical
        # ``_auth.storage`` / ``_auth.keepalive`` lookup inside its body.
        # Custom callables skip the late-bind hop entirely and run directly
        # (host integrations that want to bypass the monkeypatch surface).
        # ``or`` (not ``if x is not None else``) is fine here: ``None`` is
        # the only documented sentinel and any other callable is truthy.
        self._cookie_saver: CookieSaver = cookie_saver or _default_cookie_saver
        self._cookie_rotator: CookieRotator = cookie_rotator or _default_cookie_rotator

    @property
    def _http_client(self) -> httpx.AsyncClient | None:
        # Read-only forwarder over the concrete kernel's live client. The
        # corresponding setter was retired alongside ``Kernel.http_client``'s
        # setter: production never mutated this attribute (open() builds the
        # client through the kernel's injected ``async_client_factory``;
        # close() nulls it via :meth:`Kernel.aclose`). Tests that need to
        # install a stand-in client should use the constructor-time
        # ``async_client_factory`` injection on the test client shell
        # (preferred) or the ``install_http_client_for_test`` helper in
        # ``tests/_fixtures/kernel_test_helpers.py``.
        return self._kernel.http_client

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

    def assert_bound_loop(self) -> None:
        """Satisfies the ``LoopGuard`` capability Protocol (ADR-014 Rule 1).

        Delegates to the free function in :mod:`notebooklm._loop_affinity`
        with this lifecycle's captured loop. Feature APIs that depend on
        ``LoopGuard`` take :class:`ClientLifecycle` directly.
        """
        from ._loop_affinity import assert_bound_loop as _assert

        _assert(self._bound_loop)

    def get_http_client(self) -> httpx.AsyncClient:
        """Return the live HTTP client via the concrete Kernel."""
        return self._kernel.get_http_client()

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    async def open(
        self,
        *,
        auth: AuthTokens,
        drain_tracker: TransportDrainTracker,
        auth_coord: AuthRefreshCoordinator,
        reqid: ReqidCounter,
        cookie_persistence: CookiePersistence,
        composed: ClientComposed,
        uploader: SourceUploadPipeline,
    ) -> None:
        """Open the HTTP client connection.

        Idempotent: if ``_http_client`` is already non-``None`` this is a
        no-op. Captures the running event loop in ``_bound_loop`` so the
        cross-loop affinity guard in the transport path fails fast if the
        same client is later driven from a different loop.
        Re-opening on a different loop (after a prior :meth:`close`)
        intentionally replaces the binding — ``open()`` is the only binding
        moment.

        Synthetic-error injection moved from this layer to the chain in
        Tier-12 PR 12.6 — see
        :class:`notebooklm._middleware_error_injection.ErrorInjectionMiddleware`
        for the new substitution point. The httpx transport built here is
        always a real, unwrapped transport.

        Wave 2 of plan ``host-protocol-removal`` narrowed this signature
        from the legacy ``host`` Protocol to explicit
        keyword-only collaborators so the lifecycle never reaches into
        ``host.<X>`` attributes; the caller
        (:meth:`notebooklm.client.NotebookLMClient.__aenter__`) passes its
        owned collaborators through.
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
        # loop through ``ClientLifecycle.get_bound_loop()`` so no further
        # propagation is needed there.
        drain_tracker.set_bound_loop(self._bound_loop)
        reqid.set_bound_loop(self._bound_loop)
        auth_coord.set_bound_loop(self._bound_loop)
        # The RPC concurrency semaphore is the fourth loop-bound primitive
        # propagated here (issue #1169): it was previously the only loop-bound
        # primitive without an affinity guard or a close→reopen reset, so
        # reopening on a different loop could reuse a stale
        # ``asyncio.Semaphore`` and break on Python 3.10/3.11. Propagating the
        # captured loop lets ``ClientComposed.get_rpc_semaphore`` short-circuit
        # cross-loop misuse with the shared diagnostic.
        composed.set_bound_loop(self._bound_loop)
        # The Sources upload semaphore is the second lazily-built loop-bound
        # ``asyncio.Semaphore`` with the same close→reopen hazard as the RPC
        # semaphore above (the bug #1196 fixed for RPC): a client closed on
        # loop A and reopened on loop B would otherwise reuse a semaphore
        # bound to the now-dead loop A. Propagating the captured loop lets the
        # uploader discard the stale semaphore on a loop change.
        uploader.set_bound_loop(self._bound_loop)
        # Reset the drain flag so a previously-drained-then-reopened client
        # admits new transport work again. Wave 1 of plan
        # ``host-protocol-removal`` encapsulated the legacy direct write
        # ``host._drain_tracker._draining = False`` behind a method on the
        # tracker so the lifecycle never reaches into private collaborator
        # fields; the method is intentionally narrow (clears ``_draining``
        # only, leaves in-flight counters intact — see its docstring).
        drain_tracker.reset_after_open()
        # Discard the lazy RPC semaphore so a client reopened on a different
        # loop rebuilds it on the new loop instead of reusing the stale one
        # bound to the prior (now-dead) loop (issue #1169). Narrow by design —
        # the semaphore is reconstructed lazily on the next ``get_rpc_semaphore``
        # call from inside the new loop; ``max_concurrent_rpcs`` is untouched.
        composed.reset_after_open()
        # Same close→reopen reset for the Sources upload semaphore so a
        # reopened client rebuilds it on the new loop instead of reusing the
        # stale one bound to the prior (now-dead) loop. Narrow by design — the
        # semaphore is reconstructed lazily on the next ``get_upload_semaphore``
        # call from inside the new loop; ``max_concurrent_uploads`` is untouched.
        uploader.reset_after_open()

        # Delegate HTTP-client construction and open-time cookie baseline
        # capture to the concrete transport kernel. The lifecycle still owns
        # loop binding and open/close ordering.
        await self._kernel.open(
            auth=auth,
            timeout=self._timeout,
            connect_timeout=self._connect_timeout,
            limits=self._limits,
            capture_cookie_snapshot=cookie_persistence.capture_open_snapshot,
        )

        # Spawn the keepalive task once the client is ready.
        if self._keepalive_interval is not None:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(
                    cookie_persistence=cookie_persistence,
                    interval=self._keepalive_interval,
                )
            )

    async def save_cookies(
        self,
        cookie_persistence: CookiePersistence,
        jar: httpx.Cookies,
        path: Path | None = None,
    ) -> None:
        """Persist a cookie jar through the shared cookie-persistence collaborator.

        Single chokepoint used by :meth:`close`, :meth:`_keepalive_loop`, and
        ``NotebookLMClient.refresh_auth``. The storage writer is delegated
        to ``self._cookie_saver`` (Phase 2 PR 3 injectable seam). The
        default :func:`_default_cookie_saver` wrapper performs a late-bound
        ``from ._auth.storage import save_cookies_to_storage`` lookup inside
        its body so a ``monkeypatch.setattr`` on the canonical seam keeps
        affecting the live save path through the wrapper. Custom callables
        bypass the late-bind hop entirely.

        Wave 2 of plan ``host-protocol-removal`` narrowed the first
        positional argument from the legacy ``host`` Protocol to the
        :class:`CookiePersistence` collaborator directly. Callers
        (lifecycle ``close`` / keepalive loop, :func:`refresh_auth_session`)
        pass the collaborator they already hold rather than a broad host
        wrapper.
        """
        await cookie_persistence.save(
            jar,
            path,
            save_cookies_to_storage=self._cookie_saver,
            to_thread=asyncio.to_thread,
        )

    async def close(
        self,
        *,
        auth_coord: AuthRefreshCoordinator,
        drain_tracker: TransportDrainTracker,
        cookie_persistence: CookiePersistence,
    ) -> None:
        """Close the HTTP client connection.

        Cancellation safety: the entire close sequence is wrapped in
        ``try/finally`` and the final ``aclose()`` is wrapped in
        :func:`asyncio.shield` — without the shield, a ``CancelledError``
        arriving during keepalive teardown or the cookie save would skip
        ``aclose()`` and leak the underlying httpx transport.
        :meth:`Kernel.aclose` clears the live HTTP client in its own
        ``finally`` so the instance is consistently marked closed even if
        shielded teardown raises.

        Drain hooks: feature-owned close hooks are awaited before the HTTP
        client is torn down. Without this, a feature task waking mid-aclose
        could issue a request against an already-closed transport and surface
        as a confusing httpx error. The drain uses ``return_exceptions=True``
        so a single misbehaving hook can't block the rest of the close
        sequence.

        Stage B1 PR 2 of the post-refactoring plan removed the
        close-time ``host._rpc_executor = None`` step. The composition
        root (:func:`notebooklm._session_init.compose_client_internals`)
        binds the executor exactly once via
        :meth:`notebooklm._client_composed.ClientComposed.bind_executor`,
        and the binding is preserved across ``close()`` → ``open()``
        cycles. The executor's
        underlying transport collaborator (:class:`Kernel`) rebuilds
        its ``httpx.AsyncClient`` on each :meth:`open`, so the executor
        continues to operate against the fresh transport state without
        a fresh executor instance.

        Wave 2 of plan ``host-protocol-removal`` narrowed this signature
        from the legacy ``host`` Protocol to explicit
        keyword-only collaborators; the caller
        (:meth:`notebooklm.client.NotebookLMClient.close`) passes its owned
        collaborators through.
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
            # warning. Wave 1 of plan ``host-protocol-removal`` encapsulated
            # the cancel+gather block behind a method on the coordinator so
            # the lifecycle never reaches into the private ``_refresh_task``
            # slot; the method preserves both ``is None`` and ``done()``
            # short-circuits (true no-op outside the racing case) AND the
            # critical slot-preservation invariant (the ``_refresh_task``
            # slot is NOT cleared on cancel — sibling waiters joined to the
            # same single-flight refresh still observe the shared task).
            # See :meth:`AuthRefreshCoordinator.cancel_inflight_refresh`.
            await auth_coord.cancel_inflight_refresh()

            await drain_tracker.run_drain_hooks()

            if self._http_client:
                try:
                    # Single source of truth for the on-close save: takes the
                    # in-process lock, snapshots, off-loads. Serializes
                    # naturally with any keepalive save still finishing in a
                    # worker thread — close() owns the freshest jar and must
                    # win, not the older snapshot.
                    await self.save_cookies(cookie_persistence, self._kernel.cookies)
                except Exception as e:
                    logger.warning("Failed to sync refreshed cookies during close: %s", e)
        finally:
            if self._http_client:
                # Shield: cancellation arriving mid-aclose must not leak
                # the transport. The shielded aclose runs to completion;
                # ``self._http_client = None`` then makes ``is_open``
                # return False correctly. Stage B1 PR 2 dropped the
                # ``host._rpc_executor = None`` step that previously
                # lived here — the executor is composition-root-bound
                # and persists across close() → open() cycles.
                await asyncio.shield(self._kernel.aclose())

    # ------------------------------------------------------------------
    # Keepalive
    # ------------------------------------------------------------------

    async def _keepalive_loop(
        self,
        *,
        cookie_persistence: CookiePersistence,
        interval: float,
    ) -> None:
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

        Wave 2 of plan ``host-protocol-removal`` narrowed this signature
        from the legacy ``host`` Protocol to the
        :class:`CookiePersistence` collaborator (used for the per-iteration
        cookie save). :meth:`open` spawns the task with the same
        ``cookie_persistence`` it received, so the loop saves through the
        same collaborator the open path captured.
        """
        logger.debug("Keepalive task started (interval=%.1fs)", interval)
        # Rotation is delegated to ``self._cookie_rotator`` (Phase 2 PR 3
        # injectable seam). The default :func:`_default_cookie_rotator`
        # wrapper performs a late-bound ``from ._auth.keepalive import
        # _rotate_cookies`` lookup inside its body so a
        # ``monkeypatch.setattr`` on the canonical seam keeps affecting
        # the live keepalive loop. Custom callables bypass the late-bind
        # hop entirely.

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
                    await self._cookie_rotator(client, self._keepalive_storage_path)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - opportunistic best-effort
                    logger.debug("Keepalive poke failed (non-fatal): %s", exc)
                    continue

                if self._keepalive_storage_path is None:
                    continue

                try:
                    # save_cookies handles snapshot + lock + off-load.
                    await self.save_cookies(cookie_persistence, client.cookies)
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


__all__ = [
    "ClientLifecycle",
    "CookieRotator",
    "CookieSaver",
    "_default_cookie_rotator",
    "_default_cookie_saver",
]
