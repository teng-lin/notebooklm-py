"""Client-neutral authentication recovery adapters."""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .extraction import _LoginRedirectError
    from .storage import CookieSnapshot

logger = logging.getLogger("notebooklm.auth")


@dataclass(frozen=True)
class ColdRecoveryResult:
    """Final shared jar and the baseline preceding validation mutations."""

    cookie_jar: httpx.Cookies
    snapshot: CookieSnapshot


_COLD_LOCKS_BY_LOOP: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[Path, asyncio.Lock]
] = weakref.WeakKeyDictionary()
_COLD_INFLIGHT_BY_LOOP: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[tuple[Path, bool], asyncio.Task[ColdRecoveryResult]],
] = weakref.WeakKeyDictionary()
_COLD_SUCCESS_GENERATIONS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Path, int]] = (
    weakref.WeakKeyDictionary()
)


def _cold_path_lock(path: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    per_loop = _COLD_LOCKS_BY_LOOP.setdefault(loop, {})
    return per_loop.setdefault(path, asyncio.Lock())


async def _run_cold_recovery(
    *,
    storage_path: Path,
    allow_headless: bool,
    validate: Callable[[httpx.Cookies], Awaitable[None]],
    initial_error: _LoginRedirectError,
) -> ColdRecoveryResult:
    from .cookies import build_httpx_cookies_from_storage
    from .extraction import _LoginRedirectError
    from .storage import snapshot_cookie_jar

    async with _cold_path_lock(storage_path):
        working_jar = await asyncio.to_thread(build_httpx_cookies_from_storage, storage_path)
        snapshot = snapshot_cookie_jar(working_jar)
        generations = _COLD_SUCCESS_GENERATIONS.setdefault(asyncio.get_running_loop(), {})
        last_redirect = initial_error
        if generations.get(storage_path, 0) > 0:
            try:
                await validate(working_jar)
            except _LoginRedirectError as redirect_error:
                last_redirect = redirect_error
            else:
                return ColdRecoveryResult(working_jar, snapshot)

        attempts = (
            lambda: try_headless_reauth(
                storage_path=storage_path,
                cookie_jar=working_jar,
                allow_headless=allow_headless,
            ),
            lambda: try_master_token_reauth(
                storage_path=storage_path,
                cookie_jar=working_jar,
            ),
        )
        for attempt in attempts:
            if not await attempt():
                continue
            snapshot = snapshot_cookie_jar(working_jar)
            try:
                await validate(working_jar)
            except _LoginRedirectError as redirect_error:
                last_redirect = redirect_error
                continue
            generations[storage_path] = generations.get(storage_path, 0) + 1
            return ColdRecoveryResult(working_jar, snapshot)
        raise last_redirect


async def coalesced_cold_recovery(
    *,
    storage_path: Path,
    allow_headless: bool,
    validate: Callable[[httpx.Cookies], Awaitable[None]],
    initial_error: _LoginRedirectError,
) -> ColdRecoveryResult:
    """Share one complete same-loop cold ladder for equivalent callers."""
    canonical_path = storage_path.expanduser().resolve()
    key = (canonical_path, allow_headless)
    loop = asyncio.get_running_loop()
    registry = _COLD_INFLIGHT_BY_LOOP.setdefault(loop, {})
    task = registry.get(key)
    if task is None or task.done():
        task = asyncio.create_task(
            _run_cold_recovery(
                storage_path=canonical_path,
                allow_headless=allow_headless,
                validate=validate,
                initial_error=initial_error,
            )
        )
        registry[key] = task

        def settle(done: asyncio.Task[ColdRecoveryResult]) -> None:
            if registry.get(key) is done:
                registry.pop(key, None)

        task.add_done_callback(settle)

    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:  # noqa: BLE001 - settle before propagating cancellation
                break
        if task.done() and not task.cancelled():
            task.exception()
        raise


async def try_headless_reauth(
    *,
    storage_path: Path | None,
    cookie_jar: httpx.Cookies,
    allow_headless: bool,
) -> bool:
    """Drive opt-in browser recovery and reload the persisted cookie jar."""
    if storage_path is None:
        logger.debug("Headless re-auth skipped: auth has no writable storage path.")
        return False

    from ..paths import get_browser_profile_dir
    from .cookies import _replace_cookie_jar, build_httpx_cookies_from_storage
    from .headless_reauth import HeadlessReauthStatus, attempt_headless_reauth

    result = await asyncio.to_thread(
        attempt_headless_reauth,
        storage_path=storage_path,
        allow_headless=allow_headless,
        browser_profile=get_browser_profile_dir(storage_path=storage_path),
    )
    if result.status is not HeadlessReauthStatus.SUCCESS:
        logger.debug(
            "Headless re-auth did not succeed (%s): %s",
            result.status.value,
            result.reason,
        )
        return False
    try:
        fresh_jar = await asyncio.to_thread(build_httpx_cookies_from_storage, storage_path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Headless re-auth wrote storage but its cookies failed to load (%s).",
            type(exc).__name__,
        )
        return False
    _replace_cookie_jar(cookie_jar, fresh_jar)
    logger.info("Headless re-auth succeeded; reloaded re-minted cookies for retry.")
    return True


async def try_master_token_reauth(*, storage_path: Path | None, cookie_jar: httpx.Cookies) -> bool:
    """Re-mint stored cookies from a sibling durable master token."""
    if storage_path is None:
        return False
    master_token_path = storage_path.parent / "master_token.json"
    if not master_token_path.exists():
        return False

    from .cookies import _replace_cookie_jar, build_httpx_cookies_from_storage
    from .master_token import MasterTokenError, mint_cookies, persist_minted_jar, read_master_token

    try:
        record = await asyncio.to_thread(read_master_token, master_token_path)
        if record is None:
            return False
        jar = await mint_cookies(record["email"], record["master_token"], record["android_id"])
    except MasterTokenError as exc:
        logger.warning("Master-token re-mint failed (%s); authentication error stands.", exc)
        return False

    try:
        await asyncio.to_thread(persist_minted_jar, storage_path, jar, email=record.get("email"))
        fresh_jar = await asyncio.to_thread(build_httpx_cookies_from_storage, storage_path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Master-token re-mint could not persist/reload cookies (%s); "
            "authentication error stands.",
            type(exc).__name__,
        )
        return False
    _replace_cookie_jar(cookie_jar, fresh_jar)
    logger.info("Master-token re-mint succeeded; reloaded fresh cookies for retry.")
    return True
