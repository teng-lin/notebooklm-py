"""Cookie persistence collaborator for :mod:`notebooklm._core`."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import httpx

from .auth import (
    AuthTokens,
    CookieSaveResult,
    CookieSnapshot,
    advance_cookie_snapshot_after_save,
    snapshot_cookie_jar,
)


class SaveCookiesToStorage(Protocol):
    """Callable shape for the storage writer resolved by ``ClientCore``."""

    def __call__(
        self,
        cookie_jar: httpx.Cookies,
        path: Path | None = None,
        *,
        original_snapshot: CookieSnapshot | None = None,
        return_result: bool = False,
    ) -> bool | CookieSaveResult: ...


ToThread = Callable[[Callable[[], None]], Awaitable[None]]


class CookiePersistence:
    """Owns cookie save snapshots, in-process serialization, and baseline state."""

    def __init__(
        self,
        auth: AuthTokens,
        default_path: Path | None,
        *,
        save_lock: threading.Lock | None = None,
    ) -> None:
        self.auth = auth
        self.default_path = default_path
        self.save_lock = save_lock if save_lock is not None else threading.Lock()
        self.loaded_cookie_snapshot: CookieSnapshot | None = None

    def capture_open_snapshot(self, jar: httpx.Cookies) -> CookieSnapshot:
        """Capture and publish the baseline used for later delta saves."""
        self.loaded_cookie_snapshot = (
            dict(self.auth.cookie_snapshot)
            if self.auth.cookie_snapshot is not None
            else snapshot_cookie_jar(jar)
        )
        self.auth.cookie_snapshot = self.loaded_cookie_snapshot
        return self.loaded_cookie_snapshot

    async def save(
        self,
        jar: httpx.Cookies,
        path: Path | None = None,
        *,
        save_cookies_to_storage: SaveCookiesToStorage,
        to_thread: ToThread,
    ) -> None:
        """Persist ``jar`` through the shared in-process save lock.

        The jar copy and post-save snapshot are taken before dispatching the
        worker so the background thread never iterates a live
        ``AsyncClient.cookies`` object. The blocking lock is acquired only
        inside the worker closure passed to ``to_thread``.
        """
        effective_path = path if path is not None else self.default_path
        if effective_path is None:
            return
        save_path: Path = effective_path

        jar_copy = httpx.Cookies(jar)
        post_save_snapshot = snapshot_cookie_jar(jar_copy)

        def _save(
            s: httpx.Cookies = jar_copy,
            p: Path = save_path,
            lock: threading.Lock = self.save_lock,
            post: CookieSnapshot = post_save_snapshot,
            persistence: CookiePersistence = self,
        ) -> None:
            """Worker-thread save: hold the in-process lock around the disk write."""
            with lock:
                snap = persistence.loaded_cookie_snapshot
                result = save_cookies_to_storage(
                    s,
                    p,
                    original_snapshot=snap,
                    return_result=True,
                )
                persistence._advance_baseline_after_save(snap, post, result)

        await to_thread(_save)

    def _advance_baseline_after_save(
        self,
        original_snapshot: CookieSnapshot | None,
        post_save_snapshot: CookieSnapshot,
        result: bool | CookieSaveResult,
    ) -> None:
        if isinstance(result, CookieSaveResult):
            if result.ok:
                self.loaded_cookie_snapshot = post_save_snapshot
            elif result.cas_rejected_keys:
                self.loaded_cookie_snapshot = advance_cookie_snapshot_after_save(
                    original_snapshot,
                    post_save_snapshot,
                    result.cas_rejected_keys,
                )
            if self.loaded_cookie_snapshot is not None:
                self.auth.cookie_snapshot = self.loaded_cookie_snapshot
        elif result:
            self.loaded_cookie_snapshot = post_save_snapshot
            self.auth.cookie_snapshot = post_save_snapshot
