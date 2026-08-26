"""Runtime adapter implementing the web cookie-provider port.

The provider owns a dedicated acquisition session. It publishes immutable
generations by value; the backend session never aliases this mutable kernel.
Existing storage, refresh, recovery, and account-resolution transactions stay
behind their established collaborators.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import httpx

from .._auth.account import _probe_authuser
from .._auth.account_email import AccountEmailCacheKey, resolve_account_email
from .._auth.cookie_types import CookieJar
from .._auth.cookies import _replace_cookie_jar
from .._auth.profile_store import ProfileStore
from .._cookie_persistence import CookiePersistence
from .._kernel import Kernel
from .._loop_affinity import assert_bound_loop
from .._loop_bound import LoopBoundPrimitive
from .._reqid_counter import ReqidCounter
from .._rpc_semaphore import RpcSemaphore
from .._source_upload_port import UploadLifecycleHooks
from .._transport_drain import TransportDrainTracker
from .._web_cookie_provider import (
    WebCookieGeneration,
    WebCookieSession,
    WebCookieSessionState,
)
from ..auth import AuthTokens
from .auth import AuthRefreshCoordinator
from .lifecycle import ClientLifecycle

if TYPE_CHECKING:
    from .contracts import ChatLifecycleHooks

RefreshSession = Callable[..., Awaitable[AuthTokens]]
T = TypeVar("T")


class RuntimeWebCookieProvider(LoopBoundPrimitive):
    """Adapt the mutable auth graph to one atomic generation authority."""

    def __init__(
        self,
        *,
        auth: AuthTokens,
        kernel: Kernel,
        backend_session: WebCookieSession,
        coordinator: AuthRefreshCoordinator,
        lifecycle: ClientLifecycle,
        persistence: CookiePersistence,
        drain_tracker: TransportDrainTracker,
        reqid: ReqidCounter,
        rpc_semaphore: RpcSemaphore,
        refresh_session: RefreshSession,
    ) -> None:
        self._auth = auth
        self._kernel = kernel
        self._backend_session = backend_session
        self._coordinator = coordinator
        self._lifecycle = lifecycle
        self._persistence = persistence
        self._drain_tracker = drain_tracker
        self._reqid = reqid
        self._rpc_semaphore = rpc_semaphore
        self._refresh_session = refresh_session
        self._base_refresh_lock: asyncio.Lock | None = None
        self._base_refresh_task: asyncio.Future[AuthTokens] | None = None
        self._wider_refresh_task: asyncio.Future[AuthTokens] | None = None
        self._joined_refresh_lock: asyncio.Lock | None = None
        self._joined_refresh_task: asyncio.Task[None] | None = None
        self._identity_lock: asyncio.Lock | None = None
        self._identity_tasks: dict[bool, asyncio.Task[str | None]] = {}
        self._identity_closing = False
        self._closing = False
        self._refresh_transaction_lock: asyncio.Lock | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._current_generation = self._capture_generation(0)
        self._account_email_cache: str | None = None
        self._account_email_cache_route: AccountEmailCacheKey | None = None
        self._lifecycle.configure_cookie_rotation_runner(
            self.run_cookie_rotation,
        )

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Discard every provider-owned lock when its loop binding changes."""
        self.reset_after_open()

    def reset_after_open(self) -> None:
        """Discard the four lazy locks so reopen rebuilds them on its loop."""
        self._base_refresh_lock = None
        self._joined_refresh_lock = None
        self._identity_lock = None
        self._refresh_transaction_lock = None

    @property
    def auth(self) -> AuthTokens:
        """Preserve ADR-0016's one-object ``AuthTokens`` identity."""
        return self._auth

    @property
    def is_open(self) -> bool:
        return self._lifecycle.is_open()

    def _get_refresh_transaction_lock(self) -> asyncio.Lock:
        assert_bound_loop(self._bound_loop)
        lock = self._refresh_transaction_lock
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_transaction_lock = lock
        return lock

    def _get_base_refresh_lock(self) -> asyncio.Lock:
        assert_bound_loop(self._bound_loop)
        lock = self._base_refresh_lock
        if lock is None:
            lock = asyncio.Lock()
            self._base_refresh_lock = lock
        return lock

    def _get_joined_refresh_lock(self) -> asyncio.Lock:
        assert_bound_loop(self._bound_loop)
        lock = self._joined_refresh_lock
        if lock is None:
            lock = asyncio.Lock()
            self._joined_refresh_lock = lock
        return lock

    def _get_identity_lock(self) -> asyncio.Lock:
        assert_bound_loop(self._bound_loop)
        lock = self._identity_lock
        if lock is None:
            lock = asyncio.Lock()
            self._identity_lock = lock
        return lock

    def _capture_generation(self, generation: int) -> WebCookieGeneration:
        """Synchronously copy every provider-visible credential axis."""
        return WebCookieGeneration(
            csrf_token=self._auth.csrf_token,
            session_id=self._auth.session_id,
            authuser=self._auth.authuser,
            account_email=self._auth.account_email,
            cookies=CookieJar.from_httpx(self._kernel.get_cookies()),
            generation=generation,
        )

    def _publish_next_generation(self) -> WebCookieGeneration:
        published = self._capture_generation(self._current_generation.generation + 1)
        self._current_generation = published
        return published

    def _provider_state_changed(self) -> bool:
        current = self._current_generation
        return (
            self._auth.csrf_token != current.csrf_token
            or self._auth.session_id != current.session_id
            or self._auth.authuser != current.authuser
            or self._auth.account_email != current.account_email
            or CookieJar.from_httpx(self._kernel.get_cookies()) != current.cookies
        )

    async def generation(self) -> WebCookieGeneration:
        """Return the cached immutable commit, never a live-jar sample."""
        return self._current_generation

    async def reconciled_generation(self) -> WebCookieGeneration:
        """Adopt a matching backend response before a direct HTTP leg.

        Ordinary RPC materialization deliberately uses the lock-free
        :meth:`generation` read.  Upload and Drive cross an additional async
        boundary after their registration RPC, so they use this whole
        transaction to carry a matching backend ``Set-Cookie`` into the one
        immutable cookie/token/account-route generation they clone.
        """
        assert_bound_loop(self._bound_loop)
        if self._closing:
            raise RuntimeError("web cookie provider is closing")
        async with self._get_refresh_transaction_lock():
            if self._closing:
                raise RuntimeError("web cookie provider is closing")
            await self._reconcile_locked()
            if self._closing:
                raise RuntimeError("web cookie provider is closing")
            return self._current_generation

    async def open(self, *, uploader: UploadLifecycleHooks, chat: ChatLifecycleHooks) -> None:
        """Open the provider-owned acquisition lifecycle."""
        if self.is_open:
            return
        self.set_bound_loop(asyncio.get_running_loop())
        self.reset_after_open()
        self._base_refresh_task = None
        self._wider_refresh_task = None
        self._joined_refresh_task = None
        self._identity_tasks = {}
        self._identity_closing = False
        self._closing = False
        self._close_task = None
        async with self._get_refresh_transaction_lock():
            await self._lifecycle.open(
                auth=self._auth,
                drain_tracker=self._drain_tracker,
                auth_coord=self._coordinator,
                reqid=self._reqid,
                cookie_persistence=self._persistence,
                rpc_semaphore=self._rpc_semaphore,
                uploader=uploader,
                chat=chat,
            )
            if self._provider_state_changed():
                self._publish_next_generation()

    async def run_refresh_transaction(
        self,
        work: Callable[[], Awaitable[T]],
    ) -> T:
        """Serialize one direct refresh and publish one success epoch.

        Wider-policy join logic deliberately stays outside this lock. Only a
        direct whole transaction enters here, so the coordinator callback can
        re-enter the base policy without deadlocking.
        """
        assert_bound_loop(self._bound_loop)
        async with self._get_refresh_transaction_lock():
            # Adopt response cookies already visible before acquisition starts,
            # but do not wait for in-flight backend attempts here: refresh I/O
            # remains concurrent with same-generation RPCs. Publication itself
            # need not wait for a late old response: before any newer backend
            # attempt installs the successful refresh generation, the backend
            # barrier drains the old attempt and then overwrites its jar. The
            # same barrier prevents reconciliation from labeling that response
            # as the newer epoch.
            self._adopt_detached(self._backend_session.detach(), publish=False)
            result = await work()
            self._publish_next_generation()
            return result

    async def refresh(self, *, allow_headless: bool = False) -> AuthTokens:
        """Run or join one policy flight; preserve wider join-then-rerun."""
        assert_bound_loop(self._bound_loop)
        async with self._get_base_refresh_lock():
            task = self._wider_refresh_task if allow_headless else self._base_refresh_task
            if task is None or task.done():
                task = asyncio.ensure_future(self._refresh_session(allow_headless=allow_headless))
                if allow_headless:
                    self._wider_refresh_task = task
                else:
                    self._base_refresh_task = task
        return await asyncio.shield(task)

    @property
    def has_refresh_callback(self) -> bool:
        """Return whether middleware may initiate an automatic refresh."""
        return self._coordinator.has_refresh_callback

    async def _join_refresh_and_publish(self) -> None:
        """Run the shared refresh finalizer independently of its waiters."""
        await self._coordinator.await_refresh()
        async with self._get_refresh_transaction_lock():
            if self._provider_state_changed():
                self._publish_next_generation()
            else:
                await self._reconcile_locked()

    async def await_refresh(self) -> None:
        """Join one shielded provider flight and atomically publish its outcome.

        The standard callback delegates back through :meth:`refresh`, whose
        whole transaction already publishes.  User-supplied callbacks may
        instead mutate the preserved ``AuthTokens`` object directly; the
        changed-state check publishes that successful outcome exactly once.
        Publication runs inside a provider-owned shared task so cancellation
        of its sole waiter cannot strand a successful coordinator leader.
        """
        assert_bound_loop(self._bound_loop)
        async with self._get_joined_refresh_lock():
            task = self._joined_refresh_task
            if task is None or task.done():
                task = asyncio.create_task(self._join_refresh_and_publish())
                self._joined_refresh_task = task
        await asyncio.shield(task)

    def get_account_authuser(self) -> int:
        return self._current_generation.authuser

    async def _resolve_account_email(self, *, live_fallback: bool) -> str | None:
        async with self._get_refresh_transaction_lock():
            await self._reconcile_locked()
            email, cached_email, cached_key = await resolve_account_email(
                auth=self._auth,
                cached_email=self._account_email_cache,
                cached_key=self._account_email_cache_route,
                live_fallback=live_fallback,
                get_cookies=self._kernel.get_cookies,
                get_http_client=self._kernel.get_http_client,
                probe=_probe_authuser,
                to_thread=asyncio.to_thread,
            )
            self._account_email_cache = cached_email
            self._account_email_cache_route = cached_key
            return email

    @staticmethod
    def _observe_identity_task(task: asyncio.Task[str | None]) -> None:
        """Retrieve detached-waiter failures without changing await semantics."""
        if not task.cancelled():
            task.exception()

    async def get_account_email(self, *, live_fallback: bool = True) -> str | None:
        """Resolve identity in a provider-owned task that teardown can cancel.

        Calls with the same fallback policy share a task. Ordinary waiter
        cancellation cannot tear down another caller's probe, while provider
        close can cancel every outstanding identity operation before waiting
        for the credential transaction lock.
        """
        assert_bound_loop(self._bound_loop)
        async with self._get_identity_lock():
            if self._identity_closing and live_fallback:
                raise RuntimeError("web cookie provider is closing")
            task = self._identity_tasks.get(live_fallback)
            if task is None or task.done():
                task = asyncio.create_task(self._resolve_account_email(live_fallback=live_fallback))
                task.add_done_callback(self._observe_identity_task)
                self._identity_tasks[live_fallback] = task
        return await asyncio.shield(task)

    def register_open_baseline(self, store: ProfileStore, baseline: CookieJar) -> None:
        self._persistence.register_open_baseline(store, baseline)

    async def _reconcile_locked(self, *, allow_closing: bool = False) -> bool:
        """Adopt matching response mutations from the detached backend jar."""
        async with self._backend_session.generation_transition() as transition:
            if self._closing and not allow_closing:
                return False
            changed = self._adopt_detached(transition.state, publish=True)

            # Keep admission closed until the private backend has the exact
            # generation just published (or retained). An older response can
            # no longer land after this installation and masquerade as it.
            transition.install(self._current_generation)
            return changed

    def _adopt_detached(
        self,
        detached: WebCookieSessionState | None,
        *,
        publish: bool,
    ) -> bool:
        """Copy one matching detached jar, optionally publishing its epoch."""
        if (
            detached is None
            or detached.generation != self._current_generation.generation
            or detached.cookies == self._current_generation.cookies
        ):
            return False
        replacement = detached.cookies.to_httpx()
        _replace_cookie_jar(self._kernel.get_cookies(), replacement)
        self._coordinator.update_auth_headers(auth=self._auth, kernel=self._kernel)
        if publish:
            self._publish_next_generation()
        return True

    async def reconcile(self) -> None:
        """Adopt a matching backend session without exposing its mutable jar."""
        assert_bound_loop(self._bound_loop)
        if self._closing:
            raise RuntimeError("web cookie provider is closing")
        async with self._get_refresh_transaction_lock():
            if self._closing:
                raise RuntimeError("web cookie provider is closing")
            await self._reconcile_locked()
            if self._closing:
                raise RuntimeError("web cookie provider is closing")

    async def publish_cookie_mutation(self) -> None:
        """Publish one successful keepalive mutation when its value changed."""
        assert_bound_loop(self._bound_loop)
        async with self._get_refresh_transaction_lock():
            live = CookieJar.from_httpx(self._kernel.get_cookies())
            if live != self._current_generation.cookies:
                self._coordinator.update_auth_headers(auth=self._auth, kernel=self._kernel)
                self._publish_next_generation()

    async def run_cookie_rotation(
        self,
        client: httpx.AsyncClient,
        path: Path | None,
    ) -> None:
        """Reconcile response cookies, then rotate and publish atomically.

        ``ClientLifecycle`` persists the provider jar immediately after this
        runner returns.  Reconciliation must therefore precede rotation so a
        generation-matching backend ``Set-Cookie`` reaches that periodic save
        instead of waiting until refresh or close.
        """
        assert_bound_loop(self._bound_loop)
        async with self._get_refresh_transaction_lock():
            await self._reconcile_locked()
            before = CookieJar.from_httpx(self._kernel.get_cookies())
            await self._lifecycle.rotate_cookies(client, path)
            after = CookieJar.from_httpx(self._kernel.get_cookies())
            if after != before:
                self._coordinator.update_auth_headers(auth=self._auth, kernel=self._kernel)
                self._publish_next_generation()

    async def _cancel_refresh_leaders(self) -> None:
        """Stop provider/coordinator leaders before waiting on their transaction lock."""
        await self._coordinator.cancel_inflight_refresh()
        task = self._base_refresh_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        wider = self._wider_refresh_task
        if wider is not None and not wider.done():
            wider.cancel()
            await asyncio.gather(wider, return_exceptions=True)
        joined = self._joined_refresh_task
        if joined is not None and not joined.done():
            joined.cancel()
            await asyncio.gather(joined, return_exceptions=True)

    async def _cancel_identity_tasks(self) -> None:
        """Cancel provider-owned identity I/O before lock-bound teardown."""
        async with self._get_identity_lock():
            self._identity_closing = True
            pending = [task for task in self._identity_tasks.values() if not task.done()]
            for task in pending:
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _close_once(self, *, reconcile_backend: bool) -> None:
        self._closing = True
        await self._cancel_refresh_leaders()
        await self._lifecycle.cancel_keepalive()
        await self._cancel_identity_tasks()
        if not reconcile_backend:
            # ``drain=False`` promises immediate teardown. Snapshot any response
            # cookies already visible, but do not wait behind either the provider
            # transaction lock or the backend generation barrier.
            self._adopt_detached(self._backend_session.detach(), publish=True)
            await self._lifecycle.close(
                auth_coord=self._coordinator,
                drain_tracker=self._drain_tracker,
                cookie_persistence=self._persistence,
            )
            return

        async with self._get_refresh_transaction_lock():
            await self._reconcile_locked(allow_closing=True)
            await self._lifecycle.close(
                auth_coord=self._coordinator,
                drain_tracker=self._drain_tracker,
                cookie_persistence=self._persistence,
            )

    async def close(self, *, reconcile_backend: bool = True) -> None:
        """Close provider resources once without waiter cancellation tearing down."""
        assert_bound_loop(self._bound_loop)
        task = self._close_task
        if task is None or (task.done() and not task.cancelled() and task.exception() is not None):
            task = asyncio.create_task(self._close_once(reconcile_backend=reconcile_backend))
            self._close_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except BaseException:
            if task.done():
                self._close_task = None
            raise


def _assert_runtime_provider(provider: RuntimeWebCookieProvider) -> None:
    """Static structural check kept out of runtime execution."""
    from .._web_cookie_provider import WebCookieProvider

    _: WebCookieProvider = provider


__all__ = ["RuntimeWebCookieProvider"]
