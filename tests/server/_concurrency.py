"""Synchronization helpers for REST concurrency tests."""

import asyncio


class ActiveHold:
    """Track concurrent fake-handler entries and hold them until released."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self._lock = asyncio.Lock()

    async def enter(self) -> None:
        async with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.entered.set()

    async def leave(self) -> None:
        async with self._lock:
            self.active -= 1
