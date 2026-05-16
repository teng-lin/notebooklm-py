"""Private capability adapters for feature APIs."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import httpx

from ._core_polling import PollRegistry
from .auth import authuser_query, format_authuser_value


class PollRegistryProvider(Protocol):
    """Provider for the shared artifact polling registry."""

    @property
    def poll_registry(self) -> PollRegistry:
        """Return the existing per-core poll registry."""
        ...


class AuthRouteProvider(Protocol):
    """Provider for NotebookLM selected-account routing values."""

    @property
    def authuser(self) -> int:
        """Return the integer Google authuser index."""
        ...

    @property
    def account_email(self) -> str | None:
        """Return the stable selected-account email, when available."""
        ...

    def authuser_query(self) -> str:
        """Return the URL query value for NotebookLM auth routing."""
        ...

    def authuser_header(self) -> str:
        """Return the ``x-goog-authuser`` header value."""
        ...


class CookieJarProvider(Protocol):
    """Provider for the live HTTP client's cookie jar."""

    def live_cookies(self) -> httpx.Cookies:
        """Return the live HTTP-client cookies."""
        ...


class TransportOperationProvider(Protocol):
    """Provider for shared transport operation bookkeeping."""

    async def begin_transport_post(self, log_label: str) -> object: ...
    async def begin_transport_task(
        self,
        task: asyncio.Task[Any],
        log_label: str,
    ) -> object: ...
    async def finish_transport_post(self, token: object) -> None: ...


class UploadConcurrencyProvider(Protocol):
    """Provider for shared source-upload concurrency and queue metrics."""

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        """Return the existing per-core upload semaphore."""
        ...

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        """Record how long an upload waited for the semaphore."""
        ...


class ClientCoreCapabilities(
    PollRegistryProvider,
    AuthRouteProvider,
    CookieJarProvider,
    TransportOperationProvider,
    UploadConcurrencyProvider,
):
    """Narrow capability adapter around a ``ClientCore``-shaped object.

    Construction is intentionally lazy: only store the core. Individual
    capability properties and methods read the underlying core when called.
    """

    def __init__(self, core: Any) -> None:
        self._core = core

    @property
    def poll_registry(self) -> PollRegistry:
        return self._core.poll_registry

    @property
    def authuser(self) -> int:
        return self._core.auth.authuser

    @property
    def account_email(self) -> str | None:
        return self._core.auth.account_email

    def authuser_query(self) -> str:
        return authuser_query(self.authuser, self.account_email)

    def authuser_header(self) -> str:
        return format_authuser_value(self.authuser, self.account_email)

    def live_cookies(self) -> httpx.Cookies:
        return self._core.get_http_client().cookies

    async def begin_transport_post(self, log_label: str) -> object:
        return await self._core._begin_transport_post(log_label)

    async def begin_transport_task(
        self,
        task: asyncio.Task[Any],
        log_label: str,
    ) -> object:
        return await self._core._begin_transport_task(task, log_label)

    async def finish_transport_post(self, token: object) -> None:
        await self._core._finish_transport_post(token)

    def get_upload_semaphore(self) -> asyncio.Semaphore:
        return self._core.get_upload_semaphore()

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        self._core.record_upload_queue_wait(wait_seconds)
