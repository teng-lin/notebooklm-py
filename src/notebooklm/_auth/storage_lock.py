"""Dependency-bottom process and OS lock mechanics for auth storage."""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import random
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger("notebooklm.auth")

_LOCK_CONTENTION_ERRNOS = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}
_LOCK_ACQUIRE_DEADLINE_SECONDS = 90.0
_LOCK_ACQUIRE_INITIAL_DELAY_SECONDS = 0.01
_LOCK_ACQUIRE_MAX_DELAY_SECONDS = 0.5


class LockState(Enum):
    """Result of an exclusive storage-sentinel lock attempt."""

    HELD = "held"
    CONTENDED = "contended"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LockRequest:
    """One storage-sentinel acquisition request."""

    path: Path
    blocking: bool
    operation: str
    deadline_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.blocking and self.deadline_seconds is not None:
            raise ValueError("blocking lock requests cannot set a deadline")


class _PlatformLockGateway:
    """Concrete gateway for the platform's byte/file locking primitive."""

    @property
    def blocking_uses_retry(self) -> bool:
        return sys.platform == "win32"

    def acquire(self, fd: int, *, blocking: bool, operation: str) -> LockState:
        if sys.platform != "win32":
            import fcntl

            op = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(fd, op)
                return LockState.HELD
            except OSError as exc:
                if not blocking and exc.errno in _LOCK_CONTENTION_ERRNOS:
                    logger.debug("%s: lock contended (%s)", operation, type(exc).__name__)
                    return LockState.CONTENDED
                logger.debug("%s: lock op unavailable (%s)", operation, type(exc).__name__)
                return LockState.UNAVAILABLE

        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return LockState.HELD
        except OSError as exc:
            if exc.errno in _LOCK_CONTENTION_ERRNOS:
                if not blocking:
                    logger.debug("%s: lock contended (%s)", operation, type(exc).__name__)
                return LockState.CONTENDED
            logger.debug("%s: lock op unavailable (%s)", operation, type(exc).__name__)
            return LockState.UNAVAILABLE

    def release(self, fd: int) -> None:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class StorageLockManager:
    """Own process-wide thread-lock identity and platform lock mechanics."""

    _default: ClassVar[StorageLockManager | None] = None
    _default_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        *,
        _gateway: _PlatformLockGateway | None = None,
        _monotonic: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], None] = time.sleep,
        _jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._registry_guard = threading.Lock()
        self._gateway = _gateway if _gateway is not None else _PlatformLockGateway()
        self._monotonic = _monotonic
        self._sleep = _sleep
        self._jitter = _jitter
        self._cookie_warning_claimed = False
        self._warning_guard = threading.Lock()

    @classmethod
    def process_default(cls) -> StorageLockManager:
        """Return the identity-stable manager shared by production modules."""
        with cls._default_guard:
            if cls._default is None:
                cls._default = cls()
            return cls._default

    def _inprocess_lock_for(self, path: Path) -> threading.Lock:
        key = os.fspath(path)
        with self._registry_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _claim_cookie_warning(self) -> bool:
        """Atomically claim the cookie fail-open warning for this lifecycle."""
        with self._warning_guard:
            if self._cookie_warning_claimed:
                return False
            self._cookie_warning_claimed = True
            return True

    def _sleep_backoff(self, delay: float, deadline: float) -> float | None:
        now = self._monotonic()
        if now >= deadline:
            return None
        sleep_for = min(delay + self._jitter(0.0, delay), max(0.0, deadline - now))
        self._sleep(sleep_for)
        return min(delay * 2, _LOCK_ACQUIRE_MAX_DELAY_SECONDS)

    def _acquire_os_lock(self, fd: int, request: LockRequest) -> LockState:
        if not (request.blocking and self._gateway.blocking_uses_retry):
            return self._gateway.acquire(
                fd,
                blocking=request.blocking,
                operation=request.operation,
            )

        deadline = self._monotonic() + _LOCK_ACQUIRE_DEADLINE_SECONDS
        delay = _LOCK_ACQUIRE_INITIAL_DELAY_SECONDS
        while True:
            state = self._gateway.acquire(fd, blocking=True, operation=request.operation)
            if state is not LockState.CONTENDED:
                return state
            next_delay = self._sleep_backoff(delay, deadline)
            if next_delay is None:
                logger.debug(
                    "%s: bounded msvcrt lock acquire exceeded %.0fs deadline; giving up",
                    request.operation,
                    _LOCK_ACQUIRE_DEADLINE_SECONDS,
                )
                return LockState.UNAVAILABLE
            delay = next_delay

    @contextlib.contextmanager
    def _acquire_once(self, request: LockRequest) -> Iterator[LockState]:
        inprocess_lock = self._inprocess_lock_for(request.path)
        if not inprocess_lock.acquire(blocking=request.blocking):
            logger.debug("%s: in-process lock contended", request.operation)
            yield LockState.CONTENDED
            return
        try:
            try:
                request.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(request.path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as exc:
                logger.debug(
                    "%s: lock file unavailable %s (%s)",
                    request.operation,
                    request.path,
                    type(exc).__name__,
                )
                yield LockState.UNAVAILABLE
                return
            locked = False
            try:
                state = self._acquire_os_lock(fd, request)
                locked = state is LockState.HELD
                yield state
            finally:
                if locked:
                    try:
                        self._gateway.release(fd)
                    except OSError as exc:
                        logger.debug(
                            "%s: failed to release file lock (%s)",
                            request.operation,
                            type(exc).__name__,
                        )
                os.close(fd)
        finally:
            inprocess_lock.release()

    @contextlib.contextmanager
    def acquire(self, request: LockRequest) -> Iterator[LockState]:
        """Acquire one sentinel and yield a typed lock state."""
        if request.blocking or request.deadline_seconds is None:
            with self._acquire_once(request) as state:
                yield state
            return

        deadline = self._monotonic() + request.deadline_seconds
        delay = _LOCK_ACQUIRE_INITIAL_DELAY_SECONDS
        while True:
            with self._acquire_once(request) as state:
                if state is LockState.HELD:
                    yield state
                    return
                if state is LockState.UNAVAILABLE:
                    yield state
                    return
            next_delay = self._sleep_backoff(delay, deadline)
            if next_delay is None:
                logger.debug(
                    "%s: bounded storage-lock acquire exceeded %.0fs deadline; giving up",
                    request.operation,
                    request.deadline_seconds,
                )
                yield LockState.UNAVAILABLE
                return
            delay = next_delay
