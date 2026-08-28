"""Phased lifecycle for web-only transport resources."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..._runtime.config import CORE_LOGGER_NAME
from ...auth import AuthTokens
from .cookie_persistence import SaveCookiesToStorage
from .kernel import Kernel

if TYPE_CHECKING:
    from ...types import ConnectionLimits
    from .auth import AuthRefreshCoordinator
    from .cookie_persistence import CookiePersistence

CookieSaver = SaveCookiesToStorage
CookieRotator = Callable[..., Awaitable[None]]

logger = logging.getLogger(CORE_LOGGER_NAME)


async def _default_cookie_rotator(*args: Any, **kwargs: Any) -> None:
    """Late-bind the canonical cookie rotator for the established test seam."""
    from ..._auth.keepalive import _rotate_cookies

    await _rotate_cookies(*args, **kwargs)


class WebTransportLifecycle:
    """Own the web Kernel, keepalive, auth task, and cookie persistence."""

    name = "web"

    def __init__(
        self,
        *,
        auth: AuthTokens,
        auth_coord: AuthRefreshCoordinator,
        cookie_persistence: CookiePersistence,
        kernel: Kernel,
        timeout: float,
        connect_timeout: float,
        limits: ConnectionLimits,
        keepalive_interval: float | None,
        keepalive_storage_path: Path | None,
        cookie_persistence_path: Path | None,
        save_cookies: Callable[[CookiePersistence, httpx.Cookies, Path | None], Awaitable[None]],
        cookie_rotator: CookieRotator,
    ) -> None:
        self._auth = auth
        self._auth_coord = auth_coord
        self._cookie_persistence = cookie_persistence
        self._kernel = kernel
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._limits = limits
        self._keepalive_interval = keepalive_interval
        self._keepalive_storage_path = keepalive_storage_path
        self._cookie_persistence_path = cookie_persistence_path
        self._save_cookies = save_cookies
        self._cookie_rotator = cookie_rotator
        self._keepalive_task: asyncio.Task[None] | None = None
        self._active_epoch: int | None = None

    async def open(self, loop: asyncio.AbstractEventLoop, epoch: int) -> None:
        """Activate Kernel/Auth together before publishing a live HTTP handle."""
        del loop
        if self._active_epoch == epoch and self._kernel.http_client is not None:
            return
        self._active_epoch = epoch
        self._kernel.activate_epoch(epoch)
        self._auth_coord.activate_epoch(epoch)
        await self._cookie_persistence._prepare_open_baseline(
            self._cookie_persistence_path,
            to_thread=asyncio.to_thread,
        )
        await self._kernel.open(
            auth=self._auth,
            timeout=self._timeout,
            connect_timeout=self._connect_timeout,
            limits=self._limits,
            capture_cookie_snapshot=self._cookie_persistence.capture_open_snapshot,
            expected_epoch=epoch,
        )
        self._auth.cookie_snapshot = self._cookie_persistence.loaded_cookie_snapshot
        if self._keepalive_interval is not None:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop(epoch))

    async def prepare_close(self) -> None:
        """Fence Kernel/Auth synchronously, then settle web background work."""
        epoch = self._active_epoch
        self._active_epoch = None
        self._kernel.fence_epoch(epoch)
        self._auth_coord.fence_epoch(epoch)
        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._auth_coord.cancel_inflight_refresh()

    async def close_resources(self) -> None:
        """Persist cookies best-effort and clear the Kernel handle in all cases."""
        try:
            if self._kernel.http_client is not None:
                try:
                    await self._save_cookies(self._cookie_persistence, self._kernel.cookies, None)
                except Exception as exc:  # noqa: BLE001 - persistence is best effort
                    logger.warning("Failed to sync refreshed cookies during close: %s", exc)
        finally:
            await self._kernel.aclose()
            self._active_epoch = None

    async def _keepalive_loop(self, epoch: int) -> None:
        assert self._keepalive_interval is not None
        logger.debug("Keepalive task started (interval=%.1fs)", self._keepalive_interval)
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval)
                client = self._kernel.get_http_client(expected_epoch=epoch)
                try:
                    await self._cookie_rotator(client, self._keepalive_storage_path)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - opportunistic poke
                    logger.debug("Keepalive poke failed (non-fatal): %s", exc)
                    continue
                if self._keepalive_storage_path is None:
                    continue
                try:
                    await self._save_cookies(self._cookie_persistence, client.cookies, None)
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
    "CookieRotator",
    "CookieSaver",
    "WebTransportLifecycle",
    "_default_cookie_rotator",
]
