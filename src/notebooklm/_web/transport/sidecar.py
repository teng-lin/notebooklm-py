"""Lazy Web compatibility transport for deprecated Android ``rpc_call``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import NoReturn

from .init import WebRuntime

_NOT_OPEN = "Client not initialized. Use 'async with' context."


class LazyWebSidecar:
    """A pre-registered, inert lifecycle proxy that materialises Web once.

    Android clients register this object in the root lifecycle's frozen
    transport and participant tuples.  Merely constructing or opening the
    client therefore allocates no Web collaborator.  The compatibility bundle
    is built only for the deprecated root ``rpc_call`` method.
    """

    name = "deprecated-web-sidecar"

    def __init__(self, build: Callable[[], WebRuntime]) -> None:
        self._build = build
        self._runtime: WebRuntime | None = None
        self._candidate_retirement: asyncio.Task[BaseException | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_epoch: int | None = None
        self._prepared_epoch: int | None = None
        self._lock: asyncio.Lock | None = None

    @property
    def runtime(self) -> WebRuntime | None:
        """Return the already-materialised bundle without creating it."""

        return self._runtime

    @property
    def is_materialized(self) -> bool:
        """Whether the deprecated Web bundle has been constructed."""

        return self._runtime is not None

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remain inert during the root participant-binding phase."""

        del loop

    def reset_after_open(self) -> None:
        """Remain inert until the transport ``open`` phase records the epoch."""

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        """Record a generation, reopening a previously materialised bundle."""

        if self._loop is not loop or self._lock is None:
            self._lock = asyncio.Lock()
        lock = self._lock
        async with lock:
            await self._join_candidate_retirement()
            self._loop = loop
            self._active_epoch = epoch
            self._prepared_epoch = None
            runtime = self._runtime
            if runtime is not None:
                self._bind_runtime(runtime, loop)
                await self._open_runtime(runtime, loop, epoch)

    async def materialize(self, expected_epoch: int) -> WebRuntime:
        """Build and open the Web bundle once inside an admitted operation."""

        loop = asyncio.get_running_loop()
        lock = self._lock
        if (
            lock is None
            or self._loop is not loop
            or self._active_epoch != expected_epoch
            or self._prepared_epoch == expected_epoch
        ):
            raise RuntimeError(_NOT_OPEN)

        async with lock:
            if (
                self._active_epoch != expected_epoch
                or self._prepared_epoch == expected_epoch
                or self._loop is not loop
            ):
                raise RuntimeError(_NOT_OPEN)
            await self._join_candidate_retirement()
            runtime = self._runtime
            if runtime is not None:
                return runtime

            candidate = self._build()
            self._bind_runtime(candidate, loop)
            try:
                await self._open_runtime(candidate, loop, expected_epoch)
            except BaseException as error:
                await self._retire_failed_candidate(candidate, error)

            # ``prepare_close`` shares this lock, so this branch is defensive
            # against a future lifecycle implementation that can retire an
            # epoch without first joining the proxy's phase.
            if self._active_epoch != expected_epoch or self._prepared_epoch == expected_epoch:
                await self._retire_failed_candidate(candidate, RuntimeError(_NOT_OPEN))
            self._runtime = candidate
            return candidate

    async def prepare_close(self) -> None:
        """Fence publication and prepare a materialised bundle under one lock."""

        lock = self._lock
        if lock is None:
            self._active_epoch = None
            return
        async with lock:
            epoch = self._active_epoch
            self._prepared_epoch = epoch
            self._active_epoch = None
            await self._join_candidate_retirement()
            runtime = self._runtime
            if runtime is not None:
                await self._run_phase(runtime, "prepare_close")

    async def close_resources(self) -> None:
        """Close a materialised bundle while retaining it for a later reopen."""

        lock = self._lock
        if lock is None:
            self._active_epoch = None
            return
        async with lock:
            self._active_epoch = None
            await self._join_candidate_retirement()
            runtime = self._runtime
            if runtime is not None:
                await self._run_phase(runtime, "close_resources")

    @staticmethod
    def _bind_runtime(runtime: WebRuntime, loop: asyncio.AbstractEventLoop) -> None:
        for participant in (runtime.reqid, runtime.auth_coord):
            participant.set_bound_loop(loop)
            participant.reset_after_open()

    @staticmethod
    async def _open_runtime(
        runtime: WebRuntime,
        loop: asyncio.AbstractEventLoop,
        epoch: int,
    ) -> None:
        await runtime.web_transport.open(loop, epoch)
        await runtime.source_uploader.open(loop, epoch)

    @classmethod
    async def _retire_candidate(cls, runtime: WebRuntime) -> None:
        try:
            await cls._run_phase(runtime, "prepare_close")
        finally:
            await cls._run_phase(runtime, "close_resources")

    async def _retire_failed_candidate(
        self,
        runtime: WebRuntime,
        failure: BaseException,
    ) -> NoReturn:
        """Retire an unpublished candidate without letting cancellation orphan it."""

        if self._candidate_retirement is not None:  # pragma: no cover - lock invariant
            raise AssertionError("a Web sidecar candidate retirement is already active")
        task = asyncio.create_task(self._capture_candidate_retirement(runtime))
        self._candidate_retirement = task
        cancellation = failure if isinstance(failure, asyncio.CancelledError) else None
        while True:
            try:
                cleanup_error = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                # The cancellation that interrupted candidate open is the first
                # one. A subsequent cancellation may detach its caller, while
                # the strongly retained task remains joinable by root close.
                if cancellation is not None:
                    if isinstance(failure, (KeyboardInterrupt, SystemExit)):
                        raise failure from None
                    raise cancellation from None
                cancellation = error

        if self._candidate_retirement is task:
            self._candidate_retirement = None
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
            raise cleanup_error from failure
        if cancellation is not None:
            raise cancellation from None
        if cleanup_error is not None:
            raise failure from cleanup_error
        raise failure

    async def _join_candidate_retirement(self) -> None:
        """Join cleanup detached by re-cancellation before another lifecycle step."""

        task = self._candidate_retirement
        if task is None:
            return
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                cleanup_error = await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                if cancellation is not None:
                    raise cancellation from None
                cancellation = error

        if self._candidate_retirement is task:
            self._candidate_retirement = None
        if isinstance(cleanup_error, (KeyboardInterrupt, SystemExit)):
            raise cleanup_error from cancellation
        if cancellation is not None:
            raise cancellation from None
        if cleanup_error is not None:
            raise cleanup_error

    @classmethod
    async def _capture_candidate_retirement(cls, runtime: WebRuntime) -> BaseException | None:
        """Return cleanup failures so a detached task never warns unobserved."""

        try:
            await cls._retire_candidate(runtime)
        except BaseException as error:
            return error
        return None

    @staticmethod
    async def _run_phase(runtime: WebRuntime, method: str) -> None:
        calls = (
            getattr(runtime.web_transport, method)(),
            getattr(runtime.source_uploader, method)(),
        )
        results = await asyncio.gather(*calls, return_exceptions=True)
        process_exit = next(
            (result for result in results if isinstance(result, (KeyboardInterrupt, SystemExit))),
            None,
        )
        if process_exit is not None:
            raise process_exit
        failure = next((result for result in results if isinstance(result, BaseException)), None)
        if failure is not None:
            raise failure


__all__ = ["LazyWebSidecar"]
