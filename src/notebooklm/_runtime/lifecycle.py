"""Root-owned, phased lifecycle for client transport resources.

``ClientLifecycle`` arbitrates public open, drain, and close waves. Concrete
resource ownership lives in lifecycle-shaped transport participants; admission
and generation bookkeeping remains behind the narrow ``LifecycleSupervisor``
protocol implemented by B0a's ``CallSupervisor``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import httpx

from .._loop_affinity import assert_bound_loop as _assert_bound_loop
from .._web.transport.kernel import Kernel
from .._web.transport.lifecycle import (
    CookieRotator,
    CookieSaver,
    WebTransportLifecycle,
    _default_cookie_rotator,
)
from ..auth import AuthTokens
from .config import CORE_LOGGER_NAME

if TYPE_CHECKING:
    from .._chat import ChatAPI
    from .._client_composed import ClientComposed
    from .._transport_drain import TransportDrainTracker
    from .._web.sources.upload import SourceUploadPipeline
    from .._web.transport.auth import AuthRefreshCoordinator
    from .._web.transport.cookie_persistence import CookiePersistence
    from .._web.transport.reqid_counter import ReqidCounter
    from ..types import ConnectionLimits
    from .call_supervisor import CallSupervisor

logger = logging.getLogger(CORE_LOGGER_NAME)


@runtime_checkable
class TransportLifecycle(Protocol):
    """A concrete resource owner participating in root lifecycle phases."""

    name: str

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None: ...

    async def prepare_close(self) -> None: ...

    async def close_resources(self) -> None: ...


@runtime_checkable
class LoopParticipant(Protocol):
    """Owner of lazy loop state rebuilt by an open generation."""

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None: ...

    def reset_after_open(self) -> None: ...


class LifecycleSupervisor(LoopParticipant, Protocol):
    """Narrow B0a/B0b seam; lifecycle never reads supervisor internals."""

    def prepare_generation(self, epoch: int) -> None: ...

    def start_accepting(self, epoch: int) -> None: ...

    async def stop_accepting(self, epoch: int) -> None: ...

    async def wait_for_idle(self, epoch: int, timeout: float | None) -> None: ...

    async def begin_closing(self, epoch: int) -> None: ...

    def mark_closed(self, epoch: int) -> None: ...

    async def run_drain_hooks(self) -> None: ...


class _ResourceState(Enum):
    CLOSED = auto()
    OPENING = auto()
    OPEN = auto()
    CLOSING = auto()


class _OpenOutcome(Enum):
    OPENED = auto()
    ABORTED_BY_OWNER = auto()
    FAILED = auto()


@dataclass(frozen=True)
class _OpenResult:
    outcome: _OpenOutcome
    error: BaseException | None = None


@dataclass(frozen=True)
class _OpenWave:
    loop: asyncio.AbstractEventLoop
    owner: asyncio.Task[Any] | None
    epoch: int
    prepare_task: asyncio.Task[None]
    result: asyncio.Future[_OpenResult]


@dataclass
class _CloseWave:
    loop: asyncio.AbstractEventLoop
    epoch: int
    drain: bool
    drain_timeout: float | None
    abort_graceful: asyncio.Event
    task: asyncio.Task[None] | None = None


class _CompatSupervisor:
    """Temporary adapter for testing B0b before the parallel B0a merge.

    Integration replaces this private class with ``CallSupervisor``. It must
    not grow beyond the protocol above; generation isolation belongs to B0a.
    """

    def __init__(self, drain: TransportDrainTracker) -> None:
        self._drain = drain
        self._epoch: int | None = None

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._drain.set_bound_loop(loop)

    def reset_after_open(self) -> None:
        self._drain.reset_after_open()

    def prepare_generation(self, epoch: int) -> None:
        self._epoch = epoch

    def start_accepting(self, epoch: int) -> None:
        self._require_epoch(epoch)

    async def stop_accepting(self, epoch: int) -> None:
        self._require_epoch(epoch)
        condition = self._drain.get_drain_condition()
        async with condition:
            # Compatibility bridge only; B0a owns the real admission state.
            self._drain._draining = True

    async def wait_for_idle(self, epoch: int, timeout: float | None) -> None:
        self._require_epoch(epoch)
        await self._drain.drain(timeout=timeout)

    async def begin_closing(self, epoch: int) -> None:
        await self.stop_accepting(epoch)

    def mark_closed(self, epoch: int) -> None:
        self._require_epoch(epoch)
        self._epoch = None

    async def run_drain_hooks(self) -> None:
        await self._drain.run_drain_hooks()

    def _require_epoch(self, epoch: int) -> None:
        if self._epoch != epoch:
            raise RuntimeError(
                f"Lifecycle admission generation {epoch} is retired; active={self._epoch!r}."
            )


class ClientLifecycle:
    """Sole owner of resource state and transactional lifecycle waves."""

    def __init__(
        self,
        *,
        timeout: float,
        connect_timeout: float,
        limits: ConnectionLimits,
        keepalive_interval: float | None,
        keepalive_storage_path: Path | None,
        auth: AuthTokens | None = None,
        cookie_persistence_path: Path | None = None,
        kernel: Kernel | None = None,
        cookie_saver: CookieSaver | None = None,
        cookie_rotator: CookieRotator | None = None,
        supervisor: LifecycleSupervisor | None = None,
        transports: Sequence[TransportLifecycle] = (),
        loop_participants: Sequence[LoopParticipant] = (),
    ) -> None:
        self._kernel = kernel if kernel is not None else Kernel()
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._limits = limits
        self._keepalive_interval = keepalive_interval
        self._keepalive_storage_path = keepalive_storage_path
        self._cookie_persistence_path = cookie_persistence_path
        self._auth = auth
        self._cookie_saver: CookieSaver | None = cookie_saver
        self._cookie_rotator = cookie_rotator or _default_cookie_rotator
        self._supervisor = supervisor
        self._transports = tuple(transports)
        self._loop_participants = (
            (supervisor, *loop_participants) if supervisor is not None else tuple(loop_participants)
        )
        # A supplied supervisor means the composition root provided an
        # already-frozen assembly. The legacy production path assembles once
        # from its explicit open collaborators until B0a is merged.
        self._assembly_identity: tuple[int, ...] | None = () if supervisor is not None else None
        self._state = _ResourceState.CLOSED
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._epoch = 0
        self._state_lock = threading.Lock()
        self._open_wave: _OpenWave | None = None
        self._close_wave: _CloseWave | None = None
        self._retained_tasks: set[asyncio.Task[Any]] = set()
        self._web_transport: WebTransportLifecycle | None = None

    @property
    def _http_client(self) -> httpx.AsyncClient | None:
        return self._kernel.http_client

    @property
    def _keepalive_task(self) -> asyncio.Task[None] | None:
        return None if self._web_transport is None else self._web_transport._keepalive_task

    def is_open(self) -> bool:
        """Resource ownership signal; manual drain does not change it."""
        with self._state_lock:
            return self._state in {_ResourceState.OPEN, _ResourceState.CLOSING}

    def get_bound_loop(self) -> asyncio.AbstractEventLoop | None:
        return self._bound_loop

    @property
    def current_epoch(self) -> int | None:
        with self._state_lock:
            if self._state in {_ResourceState.OPEN, _ResourceState.CLOSING}:
                return self._epoch
            return None

    def assert_bound_loop(self) -> None:
        _assert_bound_loop(self._bound_loop)

    def get_http_client(self, *, expected_epoch: int | None = None) -> httpx.AsyncClient:
        return self._kernel.get_http_client(expected_epoch=expected_epoch)

    async def save_cookies(
        self,
        cookie_persistence: CookiePersistence,
        jar: httpx.Cookies,
        path: Path | None = None,
        *,
        expected_epoch: int | None = None,
    ) -> None:
        if expected_epoch is not None:
            self._kernel.assert_epoch(expected_epoch)
        effective_path = path if path is not None else self._cookie_persistence_path
        if self._cookie_saver is None:
            logger.debug(
                "Cookie persistence route: type=canonical_store status=dispatch path=%s",
                effective_path,
            )
            await cookie_persistence._save_canonical(
                jar, effective_path, to_thread=asyncio.to_thread
            )
        else:
            logger.debug(
                "Cookie persistence route: type=explicit_v0_callback status=dispatch path=%s",
                effective_path,
            )
            await cookie_persistence._save_v0_callback(
                jar,
                effective_path,
                save_cookies_to_storage=self._cookie_saver,
                to_thread=asyncio.to_thread,
            )
        if expected_epoch is not None:
            self._kernel.assert_epoch(expected_epoch)
        if self._auth is not None:
            self._auth.cookie_snapshot = cookie_persistence.loaded_cookie_snapshot

    def _assemble_once(
        self,
        *,
        auth: AuthTokens,
        drain_tracker: TransportDrainTracker,
        auth_coord: AuthRefreshCoordinator,
        reqid: ReqidCounter,
        cookie_persistence: CookiePersistence,
        composed: ClientComposed,
        uploader: SourceUploadPipeline,
        chat: ChatAPI,
    ) -> None:
        identity = tuple(
            map(
                id,
                (
                    auth,
                    drain_tracker,
                    auth_coord,
                    reqid,
                    cookie_persistence,
                    composed,
                    uploader,
                    chat,
                ),
            )
        )
        if self._assembly_identity is not None:
            if identity != self._assembly_identity:
                raise RuntimeError("Client lifecycle collaborators changed after assembly.")
            return
        supervisor = self._supervisor or _CompatSupervisor(drain_tracker)
        web = WebTransportLifecycle(
            auth=auth,
            auth_coord=auth_coord,
            cookie_persistence=cookie_persistence,
            kernel=self._kernel,
            timeout=self._timeout,
            connect_timeout=self._connect_timeout,
            limits=self._limits,
            keepalive_interval=self._keepalive_interval,
            keepalive_storage_path=self._keepalive_storage_path,
            cookie_persistence_path=self._cookie_persistence_path,
            save_cookies=self.save_cookies,
            cookie_rotator=self._cookie_rotator,
        )
        uploader_transports: tuple[TransportLifecycle, ...] = ()
        uploader_participants: tuple[LoopParticipant, ...] = ()
        if isinstance(uploader, TransportLifecycle):
            uploader_transports = (cast(TransportLifecycle, uploader),)
        else:
            # Temporary pre-B0a/B0b compatibility: the current uploader owns
            # loop-bound semaphores but has not yet gained transport phases.
            uploader_participants = (cast(LoopParticipant, uploader),)
        self._supervisor = supervisor
        self._web_transport = web
        self._transports = (web, *self._transports, *uploader_transports)
        # ClientComposed remains explicit until B0a moves its semaphore.
        self._loop_participants = (
            supervisor,
            reqid,
            auth_coord,
            composed,
            *uploader_participants,
            chat,
            *self._loop_participants,
        )
        self._assembly_identity = identity

    async def open(
        self,
        *,
        auth: AuthTokens | None = None,
        drain_tracker: TransportDrainTracker | None = None,
        auth_coord: AuthRefreshCoordinator | None = None,
        reqid: ReqidCounter | None = None,
        cookie_persistence: CookiePersistence | None = None,
        composed: ClientComposed | None = None,
        uploader: SourceUploadPipeline | None = None,
        chat: ChatAPI | None = None,
    ) -> None:
        """Open all transports transactionally and coalesce concurrent callers."""
        if self._assembly_identity is None:
            if any(
                value is None
                for value in (
                    auth,
                    drain_tracker,
                    auth_coord,
                    reqid,
                    cookie_persistence,
                    composed,
                    uploader,
                    chat,
                )
            ):
                raise RuntimeError("Client lifecycle requires a complete transport assembly.")
            self._assemble_once(
                auth=cast(AuthTokens, auth),
                drain_tracker=cast("TransportDrainTracker", drain_tracker),
                auth_coord=cast("AuthRefreshCoordinator", auth_coord),
                reqid=cast("ReqidCounter", reqid),
                cookie_persistence=cast("CookiePersistence", cookie_persistence),
                composed=cast("ClientComposed", composed),
                uploader=cast("SourceUploadPipeline", uploader),
                chat=cast("ChatAPI", chat),
            )
        loop = asyncio.get_running_loop()
        while True:
            owner = False
            with self._state_lock:
                if self._state is _ResourceState.OPEN:
                    self._assert_loop(loop)
                    return
                if self._state is _ResourceState.CLOSING:
                    self._assert_loop(loop)
                    raise RuntimeError(
                        "NotebookLMClient is closing; wait for close() before open()."
                    )
                if self._state is _ResourceState.OPENING:
                    wave = cast(_OpenWave, self._open_wave)
                    self._assert_wave_loop(loop, wave.loop, "open")
                else:
                    self._epoch += 1
                    epoch = self._epoch
                    result: asyncio.Future[_OpenResult] = loop.create_future()
                    prepare = asyncio.create_task(self._prepare_open(loop, epoch))
                    wave = _OpenWave(loop, asyncio.current_task(), epoch, prepare, result)
                    self._open_wave = wave
                    self._state = _ResourceState.OPENING
                    owner = True
            if not owner:
                outcome = await asyncio.shield(wave.result)
                if outcome.outcome is _OpenOutcome.OPENED:
                    return
                if outcome.outcome is _OpenOutcome.ABORTED_BY_OWNER:
                    continue
                assert outcome.error is not None
                raise outcome.error
            await self._run_open_owner(wave)
            return

    async def _prepare_open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        self._bound_loop = loop
        for participant in self._loop_participants:
            participant.set_bound_loop(loop)
            participant.reset_after_open()
        supervisor = self._require_supervisor()
        supervisor.prepare_generation(epoch)
        for transport in self._transports:
            await transport.open(loop, epoch)

    async def _run_open_owner(self, wave: _OpenWave) -> None:
        try:
            await wave.prepare_task
        except asyncio.CancelledError as cancelled:
            if not wave.prepare_task.done():
                wave.prepare_task.cancel()
            cleanup = self._retain_task(
                asyncio.create_task(self._rollback_open(wave, _OpenOutcome.ABORTED_BY_OWNER))
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
            raise cancelled
        except BaseException as exc:
            cleanup = self._retain_task(
                asyncio.create_task(self._rollback_open(wave, _OpenOutcome.FAILED, exc))
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
            raise
        with self._state_lock:
            self._require_supervisor().start_accepting(wave.epoch)
            self._state = _ResourceState.OPEN
            self._open_wave = None
            if not wave.result.done():
                wave.result.set_result(_OpenResult(_OpenOutcome.OPENED))

    async def _rollback_open(
        self,
        wave: _OpenWave,
        outcome: _OpenOutcome,
        error: BaseException | None = None,
    ) -> None:
        await asyncio.gather(wave.prepare_task, return_exceptions=True)
        prepare_results = await asyncio.gather(
            *(transport.prepare_close() for transport in self._transports),
            return_exceptions=True,
        )
        close_results = await asyncio.gather(
            *(transport.close_resources() for transport in self._transports),
            return_exceptions=True,
        )
        rollback_results = (*prepare_results, *close_results)
        rollback_transports = (*self._transports, *self._transports)
        for transport, result in zip(rollback_transports, rollback_results, strict=True):
            if isinstance(result, BaseException) and not isinstance(
                result, (KeyboardInterrupt, SystemExit)
            ):
                logger.warning(
                    "Ignoring %s rollback failure to preserve open outcome: %s",
                    transport.name,
                    result,
                )
        mark_error: BaseException | None = None
        try:
            self._require_supervisor().mark_closed(wave.epoch)
        except BaseException as exc:
            mark_error = exc
            if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
                logger.warning("Ignoring admission rollback failure: %s", exc)
        process_exit = next(
            (
                result
                for result in (*rollback_results, mark_error)
                if isinstance(result, (KeyboardInterrupt, SystemExit))
            ),
            None,
        )
        final_outcome = _OpenOutcome.FAILED if process_exit is not None else outcome
        final_error = process_exit if process_exit is not None else error
        with self._state_lock:
            self._state = _ResourceState.CLOSED
            self._open_wave = None
            if not wave.result.done():
                wave.result.set_result(_OpenResult(final_outcome, final_error))
        if process_exit is not None:
            raise process_exit

    async def drain(self, timeout: float | None = None) -> None:
        """Stop top-level admission while leaving resources open."""
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {timeout!r}")
        loop = asyncio.get_running_loop()
        while True:
            with self._state_lock:
                state = self._state
                if state is _ResourceState.CLOSED:
                    return
                open_wave = self._open_wave
                close_wave = self._close_wave
                epoch = self._epoch
                if state is _ResourceState.OPENING:
                    assert open_wave is not None
                    self._assert_wave_loop(loop, open_wave.loop, "drain")
                elif state is _ResourceState.CLOSING:
                    assert close_wave is not None
                    self._assert_wave_loop(loop, close_wave.loop, "drain")
                else:
                    self._assert_loop(loop)
            if state is _ResourceState.OPENING:
                assert open_wave is not None
                result = await asyncio.shield(open_wave.result)
                if result.outcome is _OpenOutcome.FAILED:
                    assert result.error is not None
                    raise result.error
                continue
            if state is _ResourceState.CLOSING:
                assert close_wave is not None and close_wave.task is not None
                await asyncio.shield(close_wave.task)
                return
            supervisor = self._require_supervisor()
            await supervisor.stop_accepting(epoch)
            await supervisor.wait_for_idle(epoch, timeout)
            return

    async def close(
        self,
        *,
        drain: bool = True,
        drain_timeout: float | None = None,
        **_legacy_kwargs: Any,
    ) -> None:
        """Coalesce one phased close wave and retain it across cancellation."""
        if _legacy_kwargs:
            # Direct lifecycle callers from the pre-root API already performed
            # admission drain outside this method.
            drain = False
        if drain and drain_timeout is not None and drain_timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {drain_timeout!r}")
        with self._state_lock:
            legacy_unmanaged = (
                self._state is _ResourceState.CLOSED and self._http_client is not None
            )
        if legacy_unmanaged:
            # A few private compatibility callers install a Kernel client
            # directly, without running ``open()``. Keep that established seam
            # leak-free even when the public wrapper supplies no legacy kwargs.
            # A genuinely closed assembled client has no Kernel handle and
            # remains a no-op without re-running feature hooks.
            await self._close_legacy_unmanaged(**_legacy_kwargs)
            return
        loop = asyncio.get_running_loop()
        while True:
            with self._state_lock:
                if self._state is _ResourceState.CLOSED:
                    return
                if self._state is _ResourceState.OPENING:
                    open_wave = cast(_OpenWave, self._open_wave)
                    self._assert_wave_loop(loop, open_wave.loop, "close")
                    close_wave = None
                elif self._state is _ResourceState.CLOSING:
                    close_wave = cast(_CloseWave, self._close_wave)
                    self._assert_wave_loop(loop, close_wave.loop, "close")
                    open_wave = None
                else:
                    self._assert_loop(loop)
                    open_wave = None
                    close_wave = _CloseWave(
                        loop=loop,
                        epoch=self._epoch,
                        drain=drain,
                        drain_timeout=drain_timeout,
                        abort_graceful=asyncio.Event(),
                    )
                    self._state = _ResourceState.CLOSING
                    close_wave.task = self._retain_task(
                        asyncio.create_task(self._run_close(close_wave))
                    )
                    self._close_wave = close_wave
            if open_wave is not None:
                result = await asyncio.shield(open_wave.result)
                if result.outcome is _OpenOutcome.FAILED:
                    assert result.error is not None
                    raise result.error
                continue
            assert close_wave is not None and close_wave.task is not None
            await self._await_close_wave(close_wave)
            return

    async def _close_legacy_unmanaged(self, **collaborators: Any) -> None:
        """Tear down a pre-B0 directly-installed Kernel client."""
        auth_coord = collaborators.get("auth_coord")
        drain_tracker = collaborators.get("drain_tracker")
        cookie_persistence = collaborators.get("cookie_persistence")
        try:
            if auth_coord is not None:
                await auth_coord.cancel_inflight_refresh()
            if drain_tracker is not None:
                await drain_tracker.run_drain_hooks()
            if cookie_persistence is not None and self._http_client is not None:
                try:
                    await self.save_cookies(cookie_persistence, self._kernel.cookies)
                except Exception as exc:
                    logger.warning("Failed to sync refreshed cookies during close: %s", exc)
        finally:
            if self._http_client is not None:
                await asyncio.shield(self._kernel.aclose())

    async def _await_close_wave(self, wave: _CloseWave) -> None:
        assert wave.task is not None
        try:
            await asyncio.shield(wave.task)
        except asyncio.CancelledError as cancelled:
            wave.abort_graceful.set()
            try:
                await asyncio.shield(wave.task)
            except asyncio.CancelledError:
                pass
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                pass
            raise cancelled

    async def _run_close(self, wave: _CloseWave) -> None:
        timeout_error: TimeoutError | None = None
        supervisor = self._require_supervisor()
        if wave.drain:
            try:
                await supervisor.stop_accepting(wave.epoch)
                prephase = asyncio.create_task(self._run_graceful_prephase(wave))
                abort_wait = asyncio.create_task(wave.abort_graceful.wait())
                done, _pending = await asyncio.wait(
                    {prephase, abort_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if abort_wait in done and wave.abort_graceful.is_set() and not prephase.done():
                    prephase.cancel()
                    await asyncio.gather(prephase, return_exceptions=True)
                else:
                    abort_wait.cancel()
                    await asyncio.gather(abort_wait, return_exceptions=True)
                    await prephase
            except TimeoutError as exc:
                timeout_error = exc
            except asyncio.CancelledError:
                raise
            except BaseException:
                with self._state_lock:
                    self._state = _ResourceState.OPEN
                    self._close_wave = None
                raise
        try:
            await supervisor.begin_closing(wave.epoch)
        except BaseException:
            # Resource ownership did not begin teardown, so report it honestly
            # as still open and allow a later force-close retry. Admission is a
            # separate state machine: the supervisor retains whichever fenced
            # state it reached before failing, rather than lifecycle guessing
            # at an unsafe rollback transition.
            with self._state_lock:
                if self._close_wave is wave:
                    self._state = _ResourceState.OPEN
                    self._close_wave = None
            raise
        prepare_results = await asyncio.gather(
            *(transport.prepare_close() for transport in self._transports),
            return_exceptions=True,
        )
        hook_results = await asyncio.gather(supervisor.run_drain_hooks(), return_exceptions=True)
        close_results = await asyncio.gather(
            *(transport.close_resources() for transport in self._transports),
            return_exceptions=True,
        )
        mark_error: BaseException | None = None
        try:
            supervisor.mark_closed(wave.epoch)
        except BaseException as exc:
            mark_error = exc
        finally:
            with self._state_lock:
                self._state = _ResourceState.CLOSED
                self._close_wave = None
        ordered = [*prepare_results, *hook_results, *close_results]
        if mark_error is not None:
            ordered.append(mark_error)
        process_exit = next(
            (r for r in ordered if isinstance(r, (KeyboardInterrupt, SystemExit))), None
        )
        if process_exit is not None:
            raise process_exit
        if timeout_error is not None:
            ordinary = next((r for r in ordered if isinstance(r, Exception)), None)
            if ordinary is not None:
                raise timeout_error from ordinary
            raise timeout_error
        failure = next((r for r in ordered if isinstance(r, BaseException)), None)
        if failure is not None:
            raise failure

    async def _run_graceful_prephase(self, wave: _CloseWave) -> None:
        supervisor = self._require_supervisor()
        await supervisor.run_drain_hooks()
        await supervisor.wait_for_idle(wave.epoch, wave.drain_timeout)

    def _require_supervisor(self) -> LifecycleSupervisor:
        if self._supervisor is None:
            raise RuntimeError("Client lifecycle has not been assembled.")
        return self._supervisor

    def _assert_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._bound_loop is not None and self._bound_loop is not loop:
            _assert_bound_loop(self._bound_loop)

    @staticmethod
    def _assert_wave_loop(
        loop: asyncio.AbstractEventLoop,
        wave_loop: asyncio.AbstractEventLoop,
        action: str,
    ) -> None:
        if loop is not wave_loop:
            raise RuntimeError(
                f"Cannot {action} NotebookLMClient from a different event loop while a lifecycle "
                "wave is active. Wait on the loop that owns the client."
            )

    def _retain_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self._retained_tasks.add(task)

        def _settled(done: asyncio.Task[Any]) -> None:
            self._retained_tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                done.get_loop().call_exception_handler(
                    {
                        "message": "Process-exit signal from retained lifecycle task",
                        "exception": error,
                    }
                )

        task.add_done_callback(_settled)
        return task


__all__ = [
    "ClientLifecycle",
    "LifecycleSupervisor",
    "LoopParticipant",
    "TransportLifecycle",
]
