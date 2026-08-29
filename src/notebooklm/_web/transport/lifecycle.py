"""Phased lifecycle for web-only transport resources."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class _BackgroundResult:
    """A keepalive result that never raises across its Task boundary."""

    error: BaseException | None = None


async def _capture_background(factory: Callable[[], Awaitable[None]]) -> _BackgroundResult:
    try:
        await factory()
    except BaseException as exc:
        return _BackgroundResult(exc)
    return _BackgroundResult()


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
        cookie_saver: CookieSaver | None,
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
        self._cookie_saver = cookie_saver
        self._cookie_rotator = cookie_rotator
        self._keepalive_task: asyncio.Task[_BackgroundResult] | None = None
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
            self._keepalive_task = asyncio.create_task(
                _capture_background(lambda: self._keepalive_loop(epoch))
            )

    async def prepare_close(self) -> None:
        """Fence Kernel/Auth synchronously, then settle web background work."""
        epoch = self._active_epoch
        self._active_epoch = None
        self._kernel.fence_epoch(epoch)
        self._auth_coord.fence_epoch(epoch)
        task = self._keepalive_task
        self._keepalive_task = None
        keepalive_error: BaseException | None = None
        if task is not None:
            if not task.done():
                task.cancel()
            settled = (await asyncio.gather(task, return_exceptions=True))[0]
            if isinstance(settled, _BackgroundResult):
                if not isinstance(settled.error, asyncio.CancelledError):
                    keepalive_error = settled.error
            elif not isinstance(settled, asyncio.CancelledError):
                keepalive_error = settled
        auth_error: BaseException | None = None
        try:
            await self._auth_coord.cancel_inflight_refresh()
        except BaseException as exc:
            auth_error = exc
        process_exit = next(
            (
                error
                for error in (keepalive_error, auth_error)
                if isinstance(error, (KeyboardInterrupt, SystemExit))
            ),
            None,
        )
        if process_exit is not None:
            raise process_exit
        if keepalive_error is not None:
            raise keepalive_error
        if auth_error is not None:
            raise auth_error

    async def close_resources(self) -> None:
        """Persist cookies best-effort and clear the Kernel handle in all cases."""
        save_process_exit: KeyboardInterrupt | SystemExit | None = None
        try:
            if self._kernel.http_client is not None:
                try:
                    await self.save_cookies(self._kernel.cookies)
                except (KeyboardInterrupt, SystemExit) as exc:
                    save_process_exit = exc
                except Exception as exc:  # noqa: BLE001 - persistence is best effort
                    logger.warning("Failed to sync refreshed cookies during close: %s", exc)
        finally:
            close_error: BaseException | None = None
            try:
                await self._kernel.aclose()
            except BaseException as exc:
                close_error = exc
            finally:
                self._active_epoch = None

            # Cookie persistence runs before Kernel teardown, so an observed
            # process-exit signal from that phase beats a later ordinary close
            # failure.  Kernel process exits still propagate when no earlier
            # signal exists.
            if save_process_exit is not None:
                if close_error is not None:
                    raise save_process_exit from close_error
                raise save_process_exit
            if close_error is not None:
                raise close_error

    async def save_cookies(
        self,
        jar: httpx.Cookies,
        path: Path | None = None,
        *,
        expected_epoch: int | None = None,
    ) -> None:
        """Persist the live web jar through the one web-owned save boundary."""
        if expected_epoch is not None:
            self._kernel.assert_epoch(expected_epoch)
        effective_path = path if path is not None else self._cookie_persistence_path
        if self._cookie_saver is None:
            logger.debug(
                "Cookie persistence route: type=canonical_store status=dispatch path=%s",
                effective_path,
            )
            await self._cookie_persistence._save_canonical(
                jar,
                effective_path,
                to_thread=asyncio.to_thread,
            )
        else:
            logger.debug(
                "Cookie persistence route: type=explicit_v0_callback status=dispatch path=%s",
                effective_path,
            )
            await self._cookie_persistence._save_v0_callback(
                jar,
                effective_path,
                save_cookies_to_storage=self._cookie_saver,
                to_thread=asyncio.to_thread,
            )
        if expected_epoch is not None:
            self._kernel.assert_epoch(expected_epoch)
        self._auth.cookie_snapshot = self._cookie_persistence.loaded_cookie_snapshot

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
                    await self.save_cookies(client.cookies, expected_epoch=epoch)
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
