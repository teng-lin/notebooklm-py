"""Web-owned asset lifetime and live session credentials."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .._artifact._download_client import _is_trusted_download_host
from .._artifact._guarded_transfer import _await_advisory_cleanup
from .._artifact.downloads import AssetDownloadService, DownloadResult
from .._hop_credentials import CredentialPolicy, HopCredentials
from .._http_client_factory import HttpClientFactories
from .._loop_affinity import assert_bound_loop
from .._loop_bound import EpochFenced
from .._request_policy import RequestPolicyOwner, request_scoped
from .._runtime.call_supervisor import CallSupervisor
from ..exceptions import AuthError


class WebAssetDownloadService(RequestPolicyOwner, EpochFenced, AssetDownloadService):
    """Own admitted transfers, HTTP resources, and publication through settlement."""

    name = "web-assets"

    def __init__(
        self,
        *,
        supervisor: CallSupervisor,
        cookies: Callable[[int], httpx.Cookies],
        http_client_factories: HttpClientFactories | None = None,
    ) -> None:
        AssetDownloadService.__init__(
            self,
            credential_policy_factory=self._live_policy,
            http_client_factories=http_client_factories,
        )
        EpochFenced.__init__(
            self, "Web asset transfer belongs to a retired resource generation", assert_loop=True
        )
        self._supervisor = supervisor
        self._cookies = cookies
        self._tasks: dict[asyncio.Task[Any], int] = {}
        self._clients: set[Any] = set()

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        assert_bound_loop(loop)
        if self._tasks or self._clients:
            raise RuntimeError("Web asset resources have not settled")
        self.set_bound_loop(loop)
        self.activate(epoch)

    async def prepare_close(self) -> None:
        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        self.fence()
        for task in tuple(self._tasks):
            task.cancel()

    async def close_resources(self) -> None:
        """Interrupt transports and observe tasks, including their writer cleanup."""
        if self._bound_loop is not None:
            assert_bound_loop(self._bound_loop)
        tasks = tuple(self._tasks)
        clients = tuple(self._clients)

        async def settle() -> BaseException | None:
            for task in tasks:
                task.cancel()
            errors = await asyncio.gather(
                *(client.aclose() for client in clients), return_exceptions=True
            )
            await asyncio.gather(*tasks, return_exceptions=True)
            for error in errors:
                if isinstance(error, BaseException):
                    return error
            return None

        # Root close already retains its task; this also makes direct teardown
        # safe if its caller is cancelled repeatedly during thread settlement.
        settlement = asyncio.create_task(settle())
        cancellation: asyncio.CancelledError | None = None
        while not settlement.done():
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError as error:
                cancellation = error
        failure = settlement.result()
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        if cancellation is not None:
            raise cancellation
        if failure is not None:
            raise RuntimeError("Web asset transport close failed") from failure

    def _expected_epoch(self) -> int:
        task = asyncio.current_task()
        if task is None or task not in self._tasks:
            raise RuntimeError("Web asset transfer has no admitted owner")
        epoch = self._tasks[task]
        self.assert_epoch(epoch)
        return epoch

    def _assert_active(self) -> None:
        self._expected_epoch()

    async def _load_cookies(self) -> httpx.Cookies:
        return self._cookies(self._expected_epoch())

    def _live_policy(self, _cookies: Any) -> CredentialPolicy:
        async def credential_for(url: str) -> HopCredentials | None:
            parsed = urlparse(url)
            if parsed.scheme == "https" and _is_trusted_download_host(parsed.hostname):
                return HopCredentials(cookies=await self._load_cookies())
            return None

        return credential_for

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        epoch = self._active_epoch
        if epoch is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        async with self._supervisor.operation_scope("web artifact transfer", expected_epoch=epoch):
            self.assert_epoch(epoch)
            task = asyncio.current_task()
            assert task is not None
            nested = task in self._tasks
            self._tasks[task] = epoch
            try:
                yield
            finally:
                if not nested:
                    self._tasks.pop(task, None)

    @asynccontextmanager
    async def _client_scope(self, client: Any) -> AsyncIterator[Any]:
        self._assert_active()
        self._clients.add(client)
        try:
            async with client:
                self._assert_active()
                yield client
        finally:
            try:
                await _await_advisory_cleanup(client.aclose(), pending_error=None)
            finally:
                self._clients.discard(client)

    @request_scoped
    async def download_url(self, url: str, output_path: str) -> str:
        async with self._operation():
            return await super().download_url(url, output_path)

    @request_scoped
    async def download_urls_batch(
        self,
        urls_and_paths: list[tuple[str, str]],
        *,
        credential_policy_factory: Callable[[Any], CredentialPolicy] | None = None,
        on_auth_error: Callable[[str, AuthError], Awaitable[None]] | None = None,
    ) -> DownloadResult:
        if credential_policy_factory is not None:
            raise TypeError("Web asset downloads own their live credential policy")
        async with self._operation():
            return await super().download_urls_batch(urls_and_paths, on_auth_error=on_auth_error)

    @request_scoped
    async def write_file(self, output_path: str, writer: Callable[[Path], object]) -> str:
        async with self._operation():
            return await super().write_file(output_path, writer)
