"""Own one profile's client and isolate unfinished cancellation cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from ..client import NotebookLMClient


class ProfileClientOwner:
    """Keep cleanup owned without making healthy profiles wait for it.

    An expired attempt cannot publish a client or overlap its replacement.
    Shutdown drains the owner task, including cleanup of an expired attempt.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task: asyncio.Task[None] | None = None
        self._release = asyncio.Event()
        self._published = False
        self._closed = False

    def _assert_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Profile client must be used on its owning event loop")

    async def open(
        self,
        factory: Callable[[], AbstractAsyncContextManager[NotebookLMClient]],
        timeout: float,
    ) -> NotebookLMClient:
        self._assert_loop()
        if self._closed:
            raise RuntimeError("Profile owner is closed")
        if self._task is not None and not self._task.done():
            raise RuntimeError("Previous profile cleanup is still pending")
        ready: asyncio.Future[NotebookLMClient] = asyncio.get_running_loop().create_future()
        release = self._release = asyncio.Event()
        self._published = False

        async def own_client() -> None:
            async with factory() as client:
                if ready.cancelled():
                    return
                ready.set_result(client)
                await release.wait()

        task = self._task = asyncio.create_task(own_client())
        # Retrieve failures even when the request already returned a timeout.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            waiters: set[asyncio.Future[NotebookLMClient] | asyncio.Task[None]] = {ready, task}
            done, _ = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if self._closed:
                raise RuntimeError("Profile owner is closed")
            if ready in done:
                self._published = True
                return ready.result()
            if task in done:
                await task  # Propagate the construction error to the loader.
                raise RuntimeError("Profile construction ended without a client")
            raise asyncio.TimeoutError("Profile startup timed out")
        except BaseException:
            ready.cancel()
            release.set()
            task.cancel()
            # Cleanup continues in its owner task. New attempts are refused
            # until it settles; no late result can enter the profile registry.
            raise

    async def close(self) -> None:
        self._assert_loop()
        self._closed = True
        self._release.set()
        if self._task is not None:
            results = await asyncio.gather(self._task, return_exceptions=True)
            result = next(iter(results))
            if self._published and isinstance(result, BaseException):
                raise result
