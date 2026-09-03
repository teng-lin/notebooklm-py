"""Lazy Web compatibility transport for deprecated Android ``rpc_call``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

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
            runtime = self._runtime
            if runtime is not None:
                return runtime

            candidate = self._build()
            self._bind_runtime(candidate, loop)
            try:
                await self._open_runtime(candidate, loop, expected_epoch)
            except BaseException:
                await self._retire_candidate(candidate)
                raise

            # ``prepare_close`` shares this lock, so this branch is defensive
            # against a future lifecycle implementation that can retire an
            # epoch without first joining the proxy's phase.
            if self._active_epoch != expected_epoch or self._prepared_epoch == expected_epoch:
                await self._retire_candidate(candidate)
                raise RuntimeError(_NOT_OPEN)
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
